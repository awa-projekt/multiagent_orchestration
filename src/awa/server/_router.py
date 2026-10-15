"""Mount agent systems into an existing FastAPI app: `create_router` and `router_lifespan`."""

# No `from __future__ import annotations`: FastAPI reads the endpoint annotations when defined.
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, ExitStack, asynccontextmanager, nullcontext
from contextvars import Token
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, Literal, TypeAlias, Unpack

from awa.errors import OptionalDependencyError
from awa.server._app import build_server, server_of
from awa.server._auth import Principal, principal_context
from awa.server._config import THREAD_ID_PATTERN, ServingKw, ServingOptions, SystemsArg
from awa.server._logging import REQUEST_ID_HEADER, accept_request_id, request_id_context
from awa.server._requests import ResumeRequest, RunRequest
from awa.server._schemas import (
    ErrorResponse,
    HealthResponse,
    InterruptList,
    ReadyResponse,
    RunAccepted,
    RunList,
    RunResponse,
    RunState,
    SystemDescription,
    SystemList,
    ThreadList,
)
from awa.server._server import Server
from awa.server._stores import IDEMPOTENCY_KEY_PATTERN
from awa.server._web import error_response, server_extra_message, validation_error_response

try:
    from fastapi import APIRouter, Depends, FastAPI, Header, Path, Query, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse, Response, StreamingResponse
    from fastapi.routing import APIRoute
    from starlette.datastructures import MutableHeaders
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from starlette.types import Message, Receive, Scope, Send
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise OptionalDependencyError(server_extra_message()) from exc

DEFAULT_ROUTER_PREFIX = "/agents"
"""Path prefix of `create_router`'s endpoints unless another is given."""

AuthDependency: TypeAlias = Callable[..., Any]
"""A FastAPI dependency returning the caller's `Principal` (sync or async; it may
declare its own dependencies, such as the host app's session or user)."""

_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorResponse, "description": description}
    for status, description in (
        (400, "The request is malformed or asks for something that is not allowed"),
        (401, "Missing or invalid credentials"),
        (403, "The caller lacks a required role"),
        (404, "Unknown system, thread, run, upload or build (or one of another tenant)"),
        (408, "The upload stalled or took too long"),
        (409, "Busy, cancelled, or not in a state that allows this"),
        (413, "The request body or a file in it is too large"),
        (415, "The content type or a file type is not accepted"),
        (422, "Invalid request, an unsafe archive, or a library error inside the run"),
        (429, "Rate, usage, quota or concurrent build limit exceeded (see Retry-After)"),
        (500, "Unexpected error (details hidden)"),
        (502, "The model provider failed"),
        (503, "Shutting down, at capacity, or the model provider is overloaded"),
    )
}

ThreadId: TypeAlias = Annotated[str, Path(pattern=THREAD_ID_PATTERN)]
RunId: TypeAlias = Annotated[str, Path(pattern=THREAD_ID_PATTERN)]
Limit: TypeAlias = Annotated[int, Query(ge=1, le=500)]
IdempotencyKey: TypeAlias = Annotated[
    str | None, Header(alias="Idempotency-Key", pattern=IDEMPOTENCY_KEY_PATTERN)
]


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """The OpenAPI entries of these error statuses."""

    return {status: _ERRORS[status] for status in statuses}


@dataclass
class _Bound:
    """The request id (and principal slot) a router route bound for one request."""

    request_id: str
    request_token: Token[str | None]
    principal_token: Token[Principal | None]

    def reset(self) -> None:
        try:
            principal_context.reset(self.principal_token)
            request_id_context.reset(self.request_token)
        except ValueError:  # pragma: no cover - reset from another context
            pass


def _bind(request: Request) -> _Bound | None:
    """Bind a request id unless `create_app`'s middleware already did."""

    state = request.scope.setdefault("state", {})
    if "awa_request_id" in state:
        return None
    request_id = accept_request_id(request.headers.get(REQUEST_ID_HEADER))
    state["awa_request_id"] = request_id
    return _Bound(request_id, request_id_context.set(request_id), principal_context.set(None))


class _BoundResponse(Response):
    """Sends `inner` with `X-Request-ID`, then releases the request's context."""

    def __init__(self, inner: Response, bound: _Bound) -> None:
        self._inner = inner
        self._bound = bound
        self.status_code = inner.status_code
        self.background = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if REQUEST_ID_HEADER not in headers:
                    headers[REQUEST_ID_HEADER] = self._bound.request_id
            await send(message)

        try:
            await self._inner(scope, receive, send_with_id)
        finally:
            self._bound.reset()


class AgentRoute(APIRoute):
    """A route answering library, request and unexpected errors in the server's JSON shape.

    The host app's own `HTTPException` goes to its handlers. Outside `create_app` the route also
    binds a request id.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            bound = _bind(request)
            try:
                response = await handler(request)
            except RequestValidationError as exc:
                response = validation_error_response(exc)
            except StarletteHTTPException:
                if bound is not None:
                    bound.reset()
                raise
            except Exception as exc:  # noqa: BLE001 - a library error, or a hidden 500
                response = error_response(exc)
            except BaseException:
                if bound is not None:
                    bound.reset()
                raise
            return response if bound is None else _BoundResponse(response, bound)

        return handle


def _runs_path(request: Request, name: str) -> str:
    """Where this router serves `GET .../runs/{run_id}`, seen from the client."""

    path = str(request.scope.get("path", ""))
    root = str(request.scope.get("root_path", ""))
    if root and not path.startswith(root):
        path = root + path
    for suffix in (f"/systems/{name}/runs", f"/systems/{name}/runs/"):
        if path.endswith(suffix):
            return path[: -len(suffix)] + "/runs"
    return path.rsplit("/systems/", 1)[0] + "/runs"  # pragma: no cover - defensive


_ADMIN_ROLE = "admin"
"""The role of a principal that may read the admin-only parts of open endpoints."""


async def _is_admin(server: Server, request: Request, auth: Any) -> bool:
    """Whether the server's own authenticator knows the caller of an open endpoint as an admin."""

    if auth is not None or server.authenticator is None:
        return False
    try:
        principal = await server.authenticator.authenticate(request)
    except Exception:  # noqa: BLE001 - no or wrong credentials: an anonymous caller
        return False
    return isinstance(principal, Principal) and principal.has_role(_ADMIN_ROLE)


def _numbered_checkpointers(checks: Mapping[str, str]) -> dict[str, str]:
    numbered: dict[str, str] = {}
    count = 0
    for label, outcome in checks.items():
        if label.startswith("checkpointer:"):
            count += 1
            label = f"checkpointer:{count}"
        numbered[label] = outcome
    return numbered


class _AgentRouter(APIRouter):
    """An `APIRouter` holding the serving state of its systems (see `create_router`)."""

    awa_server: Server


def _draining(
    servers: Sequence[Server],
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[Any]] | None = None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[Any]]:
    """A lifespan subscribing the servers' subscribers and draining their runs at shutdown."""

    @asynccontextmanager
    async def serving(app: FastAPI) -> AsyncIterator[Any]:
        async with lifespan(app) if lifespan is not None else nullcontext() as state:
            with ExitStack() as stack:
                for server in servers:
                    stack.enter_context(server.subscribed())
                try:
                    yield state
                finally:
                    await asyncio.gather(
                        *(server.drain(server.drain_timeout) for server in servers)
                    )

    return serving


def build_router(
    server: Server,
    *,
    prefix: str,
    auth: AuthDependency | None = None,
    tags: Sequence[str] = (),
    drain_on_shutdown: bool = True,
) -> APIRouter:
    """The endpoints serving `server`'s systems (`create_app` and `create_router`)."""

    caller: Callable[..., Awaitable[Principal]]
    if auth is None:

        async def authenticate(request: Request) -> Principal:
            return await server.authenticate(request)

        caller = authenticate
    else:

        async def from_dependency(
            request: Request, principal: Annotated[Any, Depends(auth)]
        ) -> Principal:
            return server.bind_principal(request, principal)

        caller = from_dependency

    route_tags: list[str | Enum] = list(tags)
    router = _AgentRouter(
        prefix=prefix,
        tags=route_tags or None,
        route_class=AgentRoute,
        lifespan=_draining([server]) if drain_on_shutdown else None,
    )
    router.awa_server = server

    @router.get("/health", summary="Liveness check", response_model=HealthResponse)
    async def health(request: Request) -> Response:
        from awa.core.config import preflight_skipped
        from awa.plugins.switches import security_off

        # Started with `--skip-preflight`: said here, so a monitor sees the checks were skipped.
        extra: dict[str, Any] = {"preflight_skipped": True} if preflight_skipped() else {}
        if (off := security_off()) and await _is_admin(server, request, auth):
            # Which security parts are off or replaced: shown to an authenticated admin only,
            # never to an anonymous caller who could aim at them.
            extra["security_off"] = off
        return JSONResponse({"status": "ok", **extra})

    @router.get(
        "/ready",
        summary="Readiness check",
        response_model=ReadyResponse,
        responses={503: {"model": ReadyResponse, "description": "Not ready"}},
    )
    async def ready(request: Request) -> Response:
        is_ready, checks = await server.readiness()
        secured = server.authenticator is not None or auth is not None
        if secured and not await _is_admin(server, request, auth):
            # The checkpointer labels name systems: an anonymous caller sees numbers instead.
            checks = _numbered_checkpointers(checks)
        return JSONResponse(
            {"status": "ready" if is_ready else "unavailable", "checks": checks},
            status_code=200 if is_ready else 503,
        )

    @router.get(
        "/systems",
        summary="List the served systems",
        response_model=SystemList,
        responses=error_responses(401),
    )
    async def list_systems(principal: Annotated[Principal, Depends(caller)]) -> Response:
        return JSONResponse(server.list_systems())

    @router.get(
        "/systems/{name}",
        summary="Describe a system",
        response_model=SystemDescription,
        responses=error_responses(401, 404),
    )
    async def describe_system(
        name: str, principal: Annotated[Principal, Depends(caller)]
    ) -> Response:
        return JSONResponse(server.describe_system(name))

    run_responses = {
        **error_responses(400, 401, 404, 409, 413, 422, 429, 500, 502, 503),
        "202": {"model": RunAccepted, "description": "Async run started (`?mode=async`)"},
        "200": {
            "description": "The result, or an event stream with `?stream=true`",
            "content": {"text/event-stream": {}},
        },
    }

    @router.post(
        "/systems/{name}/runs",
        summary="Run a system",
        response_model=RunResponse,
        responses=run_responses,
    )
    async def create_run(
        request: Request,
        name: str,
        body: RunRequest,
        principal: Annotated[Principal, Depends(caller)],
        stream: bool = False,
        mode: Literal["sync", "async"] = "sync",
        idempotency_key: IdempotencyKey = None,
    ) -> Response:
        return await server.start_run(
            name,
            body,
            principal,
            stream=stream,
            mode=mode,
            idempotency_key=idempotency_key,
            runs_path=_runs_path(request, name),
        )

    @router.post(
        "/systems/{name}/runs/stream",
        summary="Run a system, streaming events",
        response_class=StreamingResponse,
        responses=error_responses(400, 401, 404, 409, 413, 422, 429, 503),
    )
    async def stream_run(
        name: str, body: RunRequest, principal: Annotated[Principal, Depends(caller)]
    ) -> Response:
        return await server.start_run(
            name, body, principal, stream=True, mode="sync", idempotency_key=None
        )

    @router.get(
        "/systems/{name}/threads",
        summary="List the caller's threads",
        response_model=ThreadList,
        responses=error_responses(400, 401, 404),
    )
    async def list_threads(
        name: str, principal: Annotated[Principal, Depends(caller)], limit: Limit = 50
    ) -> Response:
        return JSONResponse(await server.list_threads(name, principal, limit))

    @router.get(
        "/systems/{name}/threads/{thread_id}/interrupts",
        summary="List pending approvals",
        response_model=InterruptList,
        responses=error_responses(400, 401, 404),
    )
    async def list_interrupts(
        name: str, thread_id: ThreadId, principal: Annotated[Principal, Depends(caller)]
    ) -> Response:
        return JSONResponse(await server.interrupts(name, thread_id, principal))

    @router.post(
        "/systems/{name}/threads/{thread_id}/resume",
        summary="Resume a paused run",
        response_model=RunResponse,
        responses=error_responses(400, 401, 403, 404, 409, 422, 429, 500, 502, 503),
    )
    async def resume_run(
        name: str,
        thread_id: ThreadId,
        body: ResumeRequest,
        principal: Annotated[Principal, Depends(caller)],
    ) -> Response:
        return await server.run_resume(name, thread_id, body, principal)

    @router.get(
        "/runs",
        summary="List the caller's async and streamed runs",
        response_model=RunList,
        responses=error_responses(401),
    )
    async def list_runs(
        principal: Annotated[Principal, Depends(caller)], limit: Limit = 50
    ) -> Response:
        return JSONResponse(server.list_runs(principal, limit))

    @router.get(
        "/runs/{run_id}",
        summary="Get a run's status and result",
        response_model=RunState,
        responses=error_responses(401, 404),
    )
    async def get_run(run_id: RunId, principal: Annotated[Principal, Depends(caller)]) -> Response:
        return JSONResponse(server.run_state(run_id, principal))

    @router.delete(
        "/runs/{run_id}",
        summary="Cancel a run",
        response_model=RunState,
        responses=error_responses(401, 404),
    )
    async def cancel_run(
        run_id: RunId, principal: Annotated[Principal, Depends(caller)]
    ) -> Response:
        return JSONResponse(await server.cancel_run(run_id, principal))

    return router


def create_router(
    systems: SystemsArg,
    *,
    prefix: str = DEFAULT_ROUTER_PREFIX,
    auth: AuthDependency | None = None,
    tags: Sequence[str] = ("agents",),
    **serving: Unpack[ServingKw],
) -> APIRouter:
    """Build the agent endpoints as a FastAPI `APIRouter` for an existing app.

    The router serves the endpoints of `create_app` with the same semantics under `prefix`
    instead of `/v1`; `create_app` is built on it. The host app keeps its own middleware and
    error handlers. Pass `router_lifespan(router)` as the app's `lifespan` (or call
    `drain(router)`) so runs in flight are drained on shutdown. Options not listed here are those
    of `create_app`.

    Args:
        prefix: Path prefix of the endpoints (`""` for none); starts with `/`, does not end with
            one.
        auth: A FastAPI dependency returning the caller's `Principal`; it may declare its own
            dependencies and raise the host app's `HTTPException` or an `AuthenticationError`.
        tags: OpenAPI tags of the endpoints.

    Example:
        ```python
        from fastapi import Depends, FastAPI
        from awa.server import Principal, create_router, router_lifespan

        async def agent_caller(user: User = Depends(get_current_user)) -> Principal:
            return Principal(user.id, tenant=user.org_id, roles=user.roles)

        agents = create_router({"support": support_team}, auth=agent_caller)
        app = FastAPI(lifespan=router_lifespan(agents))
        app.include_router(agents, prefix="/ai")  # POST /ai/agents/systems/support/runs
        ```
    """

    if prefix and (not prefix.startswith("/") or prefix.endswith("/")):
        raise ValueError("prefix must start with '/' and must not end with '/' (or be '')")
    if auth is not None:
        if not callable(auth):
            raise TypeError("auth must be a FastAPI dependency (a callable returning a Principal)")
        if serving.get("authenticator") is not None or serving.get("api_key") is not None:
            raise ValueError("Pass auth, authenticator or api_key, not several")
    options = ServingOptions(**serving)
    server = build_server(systems, options, auth_dependency=auth is not None)
    return build_router(server, prefix=prefix, auth=auth, tags=tags)


def router_lifespan(
    *routers: APIRouter,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[Any]] | None = None,
) -> Callable[[FastAPI], AbstractAsyncContextManager[Any]]:
    """A FastAPI `lifespan` that drains agent routers when the app shuts down.

    While the app runs, each router's `subscribers` are subscribed globally; at shutdown runs in
    flight get each router's `drain_timeout`. The app's own `lifespan` wraps the drain, so pools
    it opens stay usable until the runs have ended.

    Example:
        ```python
        @asynccontextmanager
        async def resources(app: FastAPI) -> AsyncIterator[None]:
            async with db_pool:
                yield

        app = FastAPI(lifespan=router_lifespan(agents, lifespan=resources))
        ```
    """

    return _draining([server_of(router) for router in routers], lifespan)
