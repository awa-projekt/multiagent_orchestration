"""Access control of the local UI: host check, access token, same-origin check, headers.

The page itself holds no secret and opens for anyone who reaches the host. The script takes the
printed token from `/?token=...`, keeps it in the tab's `sessionStorage` and sends it as
`Authorization: Bearer <token>` on every other request. The token never travels in a cookie,
because a cookie for a loopback host reaches every other port of that host. State-changing
requests also need a same-origin `Origin`; other `Host` headers are refused against DNS
rebinding.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Collection, Mapping
from urllib.parse import parse_qs

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from awa.server._web import BodyLimit

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOCKED_PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<title>awa - link needed</title><body><h1>Open the link from the awa window</h1>
<p>For your security this page only opens with the link that awa printed when it started.
Go to the window where awa is running and open the link shown there, or start
<code>awa ui</code> again.</p></body></html>"""


def new_token() -> str:
    """A fresh random access token (256 bits)."""

    return secrets.token_urlsafe(32)


def content_security_policy(nonce: str) -> str:
    """The policy of every response: only this page's own nonce-tagged script and styles."""

    return (
        "default-src 'none'; "
        f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
        "img-src 'self' data:; connect-src 'self'; "
        "form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
    )


class Guard:
    """ASGI middleware enforcing the access model on every HTTP request (`hosts`: `host:port`)."""

    def __init__(self, app: ASGIApp, *, token: str, hosts: Collection[str]) -> None:
        self._app = app
        self._bearer = f"Bearer {token}".encode()
        self._token = token.encode()
        self._hosts = frozenset(host.lower() for host in hosts)
        self._origins = frozenset(f"http://{host}" for host in self._hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "").lower()
        nonce = secrets.token_urlsafe(16)
        scope.setdefault("state", {})["awa_ui_nonce"] = nonce
        response = self._refusal(scope, headers, host)
        if response is None:
            await self._app(scope, receive, _with_headers(send, nonce))
        else:
            await response(scope, receive, _with_headers(send, nonce))

    def _refusal(self, scope: Scope, headers: Headers, host: str) -> Response | None:
        if host not in self._hosts:
            return JSONResponse({"error": {"message": "Unknown host"}}, status_code=421)
        if scope.get("path") == "/" and scope.get("method", "GET") in {"GET", "HEAD"}:
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            supplied = query.get("token", [""])[0]
            if supplied and not hmac.compare_digest(supplied.encode(), self._token):
                return HTMLResponse(_LOCKED_PAGE, status_code=401)
            return None
        if not hmac.compare_digest(headers.get("authorization", "").encode(), self._bearer):
            if str(scope.get("path", "")).startswith("/api/"):
                return _error(401, "Not signed in", "Open the link shown in the awa window.")
            return HTMLResponse(_LOCKED_PAGE, status_code=401)
        if scope.get("method", "GET") in _SAFE_METHODS:
            return None
        if headers.get("origin", "").lower() not in self._origins:
            return _error(403, "Request from another site refused", "Use the awa page itself.")
        return None


class BodyLimits:
    """`BodyLimit` with bigger limits for the paths in `larger` (the file upload)."""

    def __init__(self, app: ASGIApp, *, limit: int, larger: Mapping[str, int]) -> None:
        self._small = BodyLimit(app, limit)
        self._larger = {path: BodyLimit(app, bytes_) for path, bytes_ in larger.items()}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        limiter = self._larger.get(str(scope.get("path", "")), self._small)
        await limiter(scope, receive, send)


def _error(status: int, message: str, fix: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "fix": fix}}, status_code=status)


def _with_headers(send: Send, nonce: str) -> Send:
    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = MutableHeaders(scope=message)
            headers["Content-Security-Policy"] = content_security_policy(nonce)
            headers["X-Content-Type-Options"] = "nosniff"
            headers["X-Frame-Options"] = "DENY"
            headers["Referrer-Policy"] = "no-referrer"
            headers["Cross-Origin-Opener-Policy"] = "same-origin"
            headers["Cross-Origin-Resource-Policy"] = "same-origin"
            headers.setdefault("Cache-Control", "no-store")
        await send(message)

    return wrapped
