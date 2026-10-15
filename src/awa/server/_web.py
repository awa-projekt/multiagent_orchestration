"""The web layer (the `server` extra): ASGI middleware and the JSON error handlers."""

from __future__ import annotations

import time
from collections.abc import Collection, Mapping
from typing import Any

from awa._internal.install import install_hint
from awa._internal.logging import get_logger
from awa.errors import AwaError, OptionalDependencyError
from awa.server._auth import principal_context
from awa.server._logging import (
    REQUEST_ID_HEADER,
    accept_request_id,
    current_request_id,
    request_id_context,
)
from awa.server._serialize import jsonable
from awa.server.errors import ErrorInfo, describe_error


def server_extra_message() -> str:
    """The error text for a missing `server` extra, with the install command for this install."""

    return "Serving systems over HTTP requires the optional extra: " + install_hint("server")


try:
    import anyio
    from fastapi import APIRouter, FastAPI, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, Response, StreamingResponse
    from starlette.datastructures import Headers, MutableHeaders
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.types import ASGIApp, Message, Receive, Scope, Send
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise OptionalDependencyError(server_extra_message()) from exc

_log = get_logger("server")
_access_log = get_logger("server.access")

DEFAULT_SECURITY_HEADERS: Mapping[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}
"""Headers added to every response unless it sets them itself (see `create_app(security_headers=...)`)."""

_BODY_TOO_LARGE = "The request body is too large"
TOO_LARGE = ErrorInfo(413, "PayloadTooLarge", _BODY_TOO_LARGE, code="awa.payload_too_large")
_BAD_HOST = ErrorInfo(
    421, "MisdirectedRequest", "This Host is not served here", code="awa.host_not_allowed"
)


def error_json(info: ErrorInfo, **extra: Any) -> dict[str, Any]:
    """The JSON body of an error response, with the current request id."""

    return {"error": {**info.body(current_request_id()), **extra}}


class RequestContext:
    """Outermost middleware: request id, security headers and the access log.

    Takes a well-formed client `X-Request-ID` or makes one, binds it for logs and runs and
    returns it on every response.
    """

    def __init__(
        self, app: ASGIApp, *, security_headers: Mapping[str, str], access_log: bool
    ) -> None:
        self._app = app
        self._headers = dict(security_headers)
        self._access_log = access_log

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request_id = accept_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        state = scope.setdefault("state", {})
        state["awa_request_id"] = request_id
        request_token = request_id_context.set(request_id)
        principal_token = principal_context.set(None)
        started = time.perf_counter()
        status = 500

        async def send_with_headers(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                for name, value in self._headers.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        try:
            await self._app(scope, receive, send_with_headers)
        finally:
            if self._access_log:
                principal = state.get("awa_principal")
                _access_log.info(
                    "%s %s %s",
                    scope.get("method"),
                    scope.get("path"),
                    status,
                    extra={
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        "principal": getattr(principal, "id", None),
                        "tenant": getattr(principal, "tenant", None),
                    },
                )
            principal_context.reset(principal_token)
            request_id_context.reset(request_token)


class HostGuard:
    """Answers 421 to a request whose `Host` is not one of `hosts` (any port).

    Guards a server without an API key against DNS rebinding. An IPv6 address matches with or
    without brackets (`::1`, `[::1]`).
    """

    def __init__(self, app: ASGIApp, hosts: Collection[str]) -> None:
        self._app = app
        self._hosts = frozenset(host.lower().strip("[]") for host in hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            host = Headers(scope=scope).get("host", "").lower()
            name = host[1 : host.find("]")] if host.startswith("[") else host.partition(":")[0]
            if name not in self._hosts:
                response = JSONResponse(error_json(_BAD_HOST), status_code=421)
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


class BodyLimit:
    """Refuses request bodies larger than `limit` bytes with 413.

    Runs before routing and authentication; `paths` gives other limits to exact paths.
    """

    def __init__(self, app: ASGIApp, limit: int, paths: Mapping[str, int] | None = None) -> None:
        self._app = app
        self._limit = limit
        self._paths = dict(paths or {})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        limit = self._paths.get(scope.get("path", ""), self._limit)
        for key, value in scope.get("headers", ()):
            if key.lower() == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > limit:
                    response = JSONResponse(error_json(TOO_LARGE), status_code=413)
                    await response(scope, receive, send)
                    return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise StarletteHTTPException(413, _BODY_TOO_LARGE)
            return message

        await self._app(scope, limited_receive, send)


def describe_and_log(exc: BaseException) -> ErrorInfo:
    """Describe `exc` for the client, logging it (with traceback if internal)."""

    info = describe_error(exc)
    if info.internal:
        _log.error("Request failed: %s", type(exc).__name__, exc_info=exc)
    else:
        _log.info("Request rejected: %s: %s", type(exc).__name__, exc)
    return info


def error_parts(exc: BaseException) -> tuple[int, dict[str, Any], dict[str, str]]:
    info = describe_and_log(exc)
    return info.status, error_json(info), info.headers()


def error_response(exc: BaseException) -> JSONResponse:
    """The JSON answer to `exc` (see `awa.server.errors`), logged as it deserves."""

    status, body, headers = error_parts(exc)
    return JSONResponse(body, status_code=status, headers=headers or None)


async def _on_error(request: Request, exc: Exception) -> Response:
    return error_response(exc)


_HTTP_ERRORS = {
    404: ("NotFound", "awa.not_found"),
    405: ("MethodNotAllowed", "awa.method_not_allowed"),
    413: ("PayloadTooLarge", "awa.payload_too_large"),
}


def validation_error_response(exc: RequestValidationError) -> JSONResponse:
    """The 422 answer to a request that fails validation (`awa.validation`, with details)."""

    info = ErrorInfo(422, "ValidationError", "The request is invalid", code="awa.validation")
    return JSONResponse(error_json(info, details=jsonable(exc.errors())), status_code=422)


def install_error_handler(app: FastAPI) -> None:
    """Answer library errors raised anywhere in `app` with the server's JSON error body.

    The awa routers do this for their own endpoints; call it so your endpoints that call awa
    answer the same way. Other error handlers are left alone.
    """

    app.add_exception_handler(AwaError, _on_error)


def install_app_error_handlers(app: FastAPI) -> None:
    async def on_invalid_request(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, RequestValidationError)
        return validation_error_response(exc)

    async def on_http_error(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, StarletteHTTPException)
        kind, code = _HTTP_ERRORS.get(exc.status_code, ("HTTPError", "awa.http_error"))
        info = ErrorInfo(exc.status_code, kind, str(exc.detail), code=code)
        return JSONResponse(error_json(info), status_code=exc.status_code, headers=exc.headers)

    app.add_exception_handler(AwaError, _on_error)
    app.add_exception_handler(RequestValidationError, on_invalid_request)
    app.add_exception_handler(StarletteHTTPException, on_http_error)
    app.add_exception_handler(Exception, _on_error)


__all__ = [
    "DEFAULT_SECURITY_HEADERS",
    "TOO_LARGE",
    "APIRouter",
    "BodyLimit",
    "CORSMiddleware",
    "FastAPI",
    "HostGuard",
    "JSONResponse",
    "Receive",
    "Request",
    "RequestContext",
    "RequestValidationError",
    "Response",
    "Scope",
    "Send",
    "StarletteHTTPException",
    "StreamingResponse",
    "anyio",
    "describe_and_log",
    "error_json",
    "error_parts",
    "error_response",
    "install_app_error_handlers",
    "install_error_handler",
    "server_extra_message",
    "validation_error_response",
]
