"""`FakeGraph`: a local Microsoft Graph and Entra ID look-alike for offline connector tests.

A real threaded HTTP server on 127.0.0.1 (random port). Connectors reach it through the same code
as in production: point `MS_GRAPH_URL` and `AZURE_AUTHORITY_HOST` at it and pass the explicit
connector options `allow_http` and `allow_private_networks` (`graph.env(app)` and
`graph.connector_options` give both). Nothing is patched.

What it enforces is written down as *rules* (`graph.rules`): each has an id, the sentence, the
documentation it comes from, that page's date and `verified_live` (false until a run against a real
tenant confirmed it). Every rule has a test in the repository's FakeGraph tests that proves the fake
refuses what Microsoft refuses.

The core covers: the token endpoint (`_helpers.fakegraph.identity`), bearer and JWT checks, a
permission per route (Graph-style 401/403 bodies), paging and delta (`_helpers.fakegraph.odata`),
fault injection (429 with the `Retry-After` variants, 5xx, per-key rate limits, concurrency limits,
latency), a request log and a controllable clock. Services are plug-ins (`files`, `mail`, `teams`,
`todo`; `directory` and `people` on request); see `_helpers.fakegraph.core.Plugin` for how to add
one.

Example:
    ```python
    with FakeGraph() as graph:
        app = graph.add_app("client-1", "s3cret", roles=("Mail.Read",))
        graph.add_user("ada@contoso.test", name="Ada")
        tools = connector_tools("microsoft_graph", env=graph.env(app),
                                config=graph.connector_options)
    ```
"""

from __future__ import annotations

import fnmatch
import http.client
import json
import math
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import ModuleType
from typing import Any

from _helpers.fakegraph import files as fake_graph_files
from _helpers.fakegraph import identity as fake_graph_identity
from _helpers.fakegraph import mail as fake_graph_mail
from _helpers.fakegraph import teams as fake_graph_teams
from _helpers.fakegraph import todo as fake_graph_todo
from _helpers.fakegraph.core import (
    GRAPH_APP_ID,
    GRAPH_AUDIENCE,
    Ctx,
    FakeClock,
    GraphError,
    Plugin,
    Principal,
    Reply,
    Request,
    Route,
    Rule,
    TokenError,
    decode_jwt,
    denied,
    make_jwt,
    new_guid,
    verify_signature,
)
from _helpers.fakegraph.identity import App, IdentityStore, User

BUILTIN_PLUGINS: tuple[ModuleType, ...] = (
    fake_graph_identity,
    fake_graph_files,
    fake_graph_mail,
    fake_graph_teams,
    fake_graph_todo,
)
"""The plug-in modules a `FakeGraph` serves by default (each has a module-level `plugin`)."""

TENANT_ID = "6c2e3d1a-5b7f-4c9e-8a10-2f4d6e8b0c13"
TENANT_DOMAIN = "contoso.onmicrosoft.com"
V1 = "/v1.0"
_WWW_AUTHENTICATE = (
    'Bearer realm="", authorization_uri="https://login.microsoftonline.com/common/oauth2/'
    f'authorize", client_id="{GRAPH_APP_ID}"'
)


core = Plugin("core")
"""Holds the rules of the generic part (no routes)."""

R_BEARER = core.rule(
    "graph.bearer_token_required",
    "A missing, malformed, tampered, expired, revoked or wrong-audience bearer token is 401 "
    "InvalidAuthenticationToken with a WWW-Authenticate header.",
    source="learn.microsoft.com/graph/errors",
    source_date="unverified",
)
R_PERMISSION = core.rule(
    "graph.permission_per_route",
    "Every route names the application roles or delegated scopes it accepts; a token without one "
    "gets 403 in the style of the service (Forbidden, ErrorAccessDenied, accessDenied).",
    source="learn.microsoft.com/graph/permissions-reference",
    source_date="unverified",
)
R_ERROR_SHAPE = core.rule(
    "graph.error_body_shape",
    'Errors are {"error": {"code", "message", "innerError": {"date", "request-id", '
    '"client-request-id"}}} and every answer carries request-id and client-request-id.',
    source="learn.microsoft.com/graph/errors",
    source_date="unverified",
)
R_NEXT_LINK = core.rule(
    "graph.next_link_is_absolute_and_opaque",
    "A page that is not the last has an absolute @odata.nextLink holding the original query and "
    "an opaque $skiptoken; a token that is not ours is 400.",
    source="learn.microsoft.com/graph/paging",
    source_date="unverified",
)
R_THROTTLE = core.rule(
    "graph.throttling_429_retry_after",
    "A throttled request is 429 with Retry-After in seconds (sometimes without the header, "
    "sometimes long); the server may also answer 5xx; both are scripted with fail()/throttle().",
    source="learn.microsoft.com/graph/throttling",
    source_date="2026-09-17",
)


# --------------------------------------------------------------------------- records


@dataclass(frozen=True)
class LoggedRequest:
    """One request the server received, with the status it answered."""

    seq: int
    method: str
    path: str
    query: Mapping[str, list[str]]
    headers: Mapping[str, str] = field(repr=False)
    body: bytes = field(repr=False)
    status: int
    at: float

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def json(self) -> Any:
        return json.loads(self.body) if self.body else None

    def form(self) -> dict[str, str]:
        parsed = urllib.parse.parse_qs(self.body.decode("utf-8", "replace"), keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}


@dataclass(frozen=True)
class HttpReply:
    """What `FakeGraph.call` returns."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def error(self) -> tuple[str, str]:
        """`(code, message)` of a Graph error body."""

        error = self.json()["error"]
        return str(error["code"]), str(error["message"])


@dataclass
class Fault:
    """A scripted failure: matching requests answer `status` (see `FakeGraph.fail`)."""

    method: str
    pattern: str
    status: int
    after: int = 0
    times: int | None = 1
    retry_after: str | None = None
    code: str | None = None
    message: str | None = None
    seen: int = 0
    fired: int = 0

    def hit(self, request: Request, path: str) -> bool:
        if self.method not in ("*", request.method) or not fnmatch.fnmatchcase(path, self.pattern):
            return False
        self.seen += 1
        if self.seen <= self.after or (self.times is not None and self.fired >= self.times):
            return False
        self.fired += 1
        return True


@dataclass
class RateLimit:
    """At most `per_second` accepted requests per key and second of the fake clock."""

    method: str
    pattern: str
    per_second: float
    key: str
    last: dict[str, float] = field(default_factory=dict)
    rejected: int = 0


@dataclass
class ConcurrencyLimit:
    """More than `limit` requests in flight per key answer 429.

    `hold` keeps each admitted request busy: for that many seconds, or until the event is set.
    """

    pattern: str
    limit: int
    hold: float | threading.Event
    inflight: dict[str, int] = field(default_factory=dict)
    rejected: int = 0


_FAULT_TEXT = {
    429: ("TooManyRequests", "Too many requests."),
    500: ("generalException", "An internal server error occurred."),
    502: ("badGateway", "The server received an invalid response from an upstream server."),
    503: ("serviceNotAvailable", "The service is not available. Try the request again."),
    504: ("gatewayTimeout", "The gateway timed out."),
}


# --------------------------------------------------------------------------- the server


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeGraph/1"
    graph: FakeGraph

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        parts = urllib.parse.urlsplit(self.path)
        request = Request(
            method=self.command,
            path=urllib.parse.unquote(parts.path),
            query=urllib.parse.parse_qs(parts.query, keep_blank_values=True),
            headers={name.lower(): value for name, value in self.headers.items()},
            body=body,
            target=self.path,
        )
        host = self.headers.get("Host") or self.graph.host
        reply = self.graph.handle(request, f"http://{host}")
        payload, content_type = reply.encode()
        self.send_response(reply.status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in reply.headers.items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _serve


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that gave up (a timeout test) closes the socket under the answer: not an error.
        if not isinstance(sys.exception(), ConnectionError | TimeoutError):
            super().handle_error(request, client_address)


class FakeGraph:
    """The fake service.

    Entity creators (`add_*`) return the entity; knobs (`fail`, `throttle`, `rate_limit`, ...)
    return the rule object they create, so a test can inspect `fired` (a `Fault`) or `rejected`
    (a `RateLimit` or `ConcurrencyLimit`).

    Args:
        plugins: Plug-in modules (each with a module-level `plugin`) or `Plugin` objects;
            default `BUILTIN_PLUGINS`.
        clock: The clock tokens, sessions and created times follow.
        tenant_id: The tenant's id.
        tenant_domain: The tenant's default domain (accepted in the token URL).
        token_lifetime: `expires_in` of access tokens, in seconds.
    """

    def __init__(
        self,
        *,
        plugins: Iterable[ModuleType | Plugin] | None = None,
        clock: FakeClock | None = None,
        tenant_id: str = TENANT_ID,
        tenant_domain: str = TENANT_DOMAIN,
        token_lifetime: int = 3599,
    ) -> None:
        self.clock = clock or FakeClock()
        self.tenant_id = tenant_id
        self.tenant_domain = tenant_domain
        self.token_lifetime = token_lifetime
        self.apps: dict[str, App] = {}
        self.users: dict[str, User] = {}
        self.page_key = uuid.uuid4().bytes
        self._key = uuid.uuid4().bytes
        self._plugins: list[Plugin] = []
        self._stores: dict[str, Any] = {}
        self.routes: list[Route] = []
        self._requests: list[LoggedRequest] = []
        self._lock = threading.Lock()
        self._faults: list[Fault] = []
        self._limits: list[RateLimit] = []
        self._concurrency: list[ConcurrencyLimit] = []
        self._delays: list[tuple[str, float]] = []
        self.revoked: set[str] = set()
        self.internal_errors: list[str] = []
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        # Identity comes first: other plug-ins (teams) wrap its token route.
        for item in (fake_graph_identity, *(plugins if plugins is not None else BUILTIN_PLUGINS)):
            self.use(item)
        self.use(core)

    # ----------------------------------------------------------- lifecycle

    def use(self, plugin: ModuleType | Plugin) -> FakeGraph:
        """Serve another plug-in (module with a `plugin`, or a `Plugin`); returns `self`."""

        found = plugin if isinstance(plugin, Plugin) else plugin.plugin
        if found in self._plugins:
            return self
        self._plugins.append(found)
        self._stores[found.name] = found.create_store(self)
        self.routes.extend(found.routes)
        return self

    def start(self) -> FakeGraph:
        """Start listening on a free port of 127.0.0.1."""

        if self._server is not None:
            return self
        handler = type("Handler", (_Handler,), {"graph": self})
        self._server = _Server(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02},
            name="fake-graph",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Stop the server (idempotent)."""

        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = self._thread = None

    def __enter__(self) -> FakeGraph:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def host(self) -> str:
        """`127.0.0.1:<port>`."""

        if self._server is None:
            raise RuntimeError("the server is not started")
        return f"127.0.0.1:{self._server.server_address[1]}"

    @property
    def url(self) -> str:
        """The server's root URL (`http://127.0.0.1:<port>`): also the Entra authority."""

        return f"http://{self.host}"

    @property
    def authority(self) -> str:
        """The value for `AZURE_AUTHORITY_HOST`."""

        return self.url

    @property
    def graph_url(self) -> str:
        """The value for `MS_GRAPH_URL` (the connector appends `/v1.0`)."""

        return self.url

    def token_url(self, tenant: str | None = None) -> str:
        return f"{self.url}/{tenant or self.tenant_id}/oauth2/v2.0/token"

    def store(self, name: str) -> Any:
        """The state of the plug-in called `name`."""

        return self._stores[name]

    @property
    def identity(self) -> IdentityStore:
        """The token endpoint's switches and grants."""

        store: IdentityStore = self._stores["identity"]
        return store

    @property
    def rules(self) -> list[Rule]:
        """Every rule of every plug-in served."""

        return [rule for plugin in self._plugins for rule in plugin.rules]

    # ----------------------------------------------------------- configuration

    def add_app(
        self,
        client_id: str,
        secret: str | None = None,
        *,
        roles: Iterable[str] = (),
        scopes: Iterable[str] = (),
        public_client: bool = False,
        name: str = "awa",
        secret_lifetime_days: float | None = None,
    ) -> App:
        """Register an app. `roles` are admin-consented application permissions; `scopes` the
        delegated permissions a user may consent to (device code, refresh)."""

        expires = (
            self.clock.now() + timedelta(days=secret_lifetime_days)
            if secret_lifetime_days is not None
            else None
        )
        app = App(client_id, secret, tuple(roles), tuple(scopes), public_client, name, expires)
        self.apps[client_id] = app
        return app

    def add_user(self, upn: str, *, name: str | None = None) -> User:
        """Register a user (the first one signs in on a device-code flow)."""

        user = User(upn, name or upn.split("@")[0].title())
        self.users[user.id] = user
        return user

    def find_user(self, reference: str | None) -> User | None:
        """A user by object id or user principal name (any case)."""

        if reference is None:
            return None
        lowered = reference.lower()
        return next(
            (u for u in self.users.values() if u.id.lower() == lowered or u.upn.lower() == lowered),
            None,
        )

    def user(self, user_id: str) -> User:
        return self.users[user_id]

    def env(self, app: App, **extra: str) -> dict[str, str]:
        """The environment of a connector configured for `app` and this server."""

        values = {
            "AZURE_TENANT_ID": self.tenant_id,
            "AZURE_CLIENT_ID": app.client_id,
            "AZURE_AUTHORITY_HOST": self.authority,
            "MS_GRAPH_URL": self.graph_url,
        }
        if app.secret is not None:
            values["AZURE_CLIENT_SECRET"] = app.secret
        values.update(extra)
        return values

    @property
    def connector_options(self) -> dict[str, Any]:
        """The connector options a localhost server needs (both default off in production)."""

        return {"allow_http": True, "allow_private_networks": True}

    # ----------------------------------------------------------- tokens

    def issue_token(
        self,
        app: App,
        *,
        kind: str = "app",
        audience: str = GRAPH_AUDIENCE,
        scopes: Iterable[str] = (),
        user: User | None = None,
        tenant: str | None = None,
        lifetime: float | None = None,
        roles: Iterable[str] | None = None,
    ) -> str:
        """A signed access token (what the token endpoint hands out; also for tests that skip
        the endpoint). App tokens carry `roles` (none for an RBAC-only app: the claim is then
        absent), user tokens `scp` and `upn`."""

        now = self.clock.time()
        tid = tenant or self.tenant_id
        claims: dict[str, Any] = {
            "aud": audience,
            # The issuer Graph access tokens (v1 format) carry; awa never calls the deprecated
            # v1 endpoints of login.windows.net.
            "iss": f"https://sts.windows.net/{tid}/",
            "iat": int(now),
            "nbf": int(now),
            "exp": int(now + (self.token_lifetime if lifetime is None else lifetime)),
            "appid": app.client_id,
            "app_displayname": app.name,
            "tid": tid,
            "ver": "1.0",
            "uti": new_guid(),
        }
        if kind == "app":
            granted = list(app.roles if roles is None else roles)
            claims.update(
                {"idtyp": "app", "appidacr": "1", "oid": app.object_id, "sub": app.object_id}
            )
            if granted:
                claims["roles"] = granted
        else:
            if user is None:
                raise ValueError("a user token needs a user")
            claims.update(
                {
                    "appidacr": "0",
                    "oid": user.id,
                    "sub": user.id,
                    "upn": user.upn,
                    "unique_name": user.upn,
                    "name": user.name,
                    "scp": " ".join(scopes),
                }
            )
        return make_jwt(claims, self._key)

    def revoke_access_token(self, token: str) -> None:
        """Make `token` fail with 401 even though it has not expired."""

        self.revoked.add(str(decode_jwt(token)["uti"]))

    def revoke_refresh_tokens(self) -> None:
        """Revoke every refresh token (password reset, admin "revoke sessions")."""

        fake_graph_identity.revoke_refresh_tokens(self.identity)

    # ----------------------------------------------------------- fault injection

    def fail(
        self,
        method: str = "*",
        path: str = "*",
        *,
        status: int = 503,
        after: int = 0,
        times: int | None = 1,
        retry_after: float | str | None = None,
        code: str | None = None,
        message: str | None = None,
    ) -> Fault:
        """Answer matching requests with `status` instead of the real answer.

        Args:
            method: HTTP method or `*`.
            path: `fnmatch` pattern of the path (below `/v1.0` for Graph routes, the full path
                for the rest). Faults apply after authentication.
            status: The status to answer (429, 500, 502, 503, 504, ...).
            after: Let this many matching requests through first.
            times: Fail this many matching requests (`None`: all of them).
            retry_after: The `Retry-After` header (seconds); `None` leaves it out.
            code: The error code (default by status).
            message: The error message (default by status).
        """

        fault = Fault(
            method.upper() if method != "*" else "*",
            path,
            status,
            after,
            times,
            None if retry_after is None else str(retry_after),
            code,
            message,
        )
        self._faults.append(fault)
        return fault

    def throttle(
        self,
        path: str = "*",
        *,
        retry_after: float | str | None = 2,
        method: str = "*",
        after: int = 0,
        times: int | None = 1,
    ) -> Fault:
        """`fail` with 429 and `Retry-After` (`None` leaves the header out)."""

        return self.fail(
            method,
            path,
            status=429,
            after=after,
            times=times,
            retry_after=retry_after,
            code="TooManyRequests",
        )

    def rate_limit(
        self, method: str, path: str, per_second: float, *, key: str = "path"
    ) -> RateLimit:
        """At most `per_second` accepted requests per `key` and second of the *fake* clock; the
        next answers 429 with `Retry-After`. `key`: `path` (one channel), `user`, `app`, `all`."""

        limit = RateLimit(method.upper(), path, per_second, key)
        self._limits.append(limit)
        return limit

    def limit_concurrency(
        self, path: str = "/users/*", *, limit: int = 4, hold: float | threading.Event = 0.05
    ) -> ConcurrencyLimit:
        """Outlook's four concurrent requests per mailbox: more answer 429 `ApplicationThrottled`
        (`MailboxConcurrency`). Every admitted request is held `hold` real seconds, or until the
        `hold` event is set, so tests can overlap."""

        found = ConcurrencyLimit(path, limit, hold)
        self._concurrency.append(found)
        return found

    def delay(self, path: str, seconds: float) -> None:
        """Hold matching requests for `seconds` of real time before answering."""

        self._delays.append((path, seconds))

    def clear_faults(self) -> None:
        self._faults.clear()
        self._limits.clear()
        self._concurrency.clear()
        self._delays.clear()

    # ----------------------------------------------------------- request log

    @property
    def requests(self) -> list[LoggedRequest]:
        """Every request received so far, in arrival order."""

        with self._lock:
            return list(self._requests)

    def calls(self, method: str | None = None, path: str = "*") -> list[LoggedRequest]:
        """The requests matching `method` and the `fnmatch` pattern `path` (full path)."""

        return [
            r
            for r in self.requests
            if (method is None or r.method == method.upper()) and fnmatch.fnmatchcase(r.path, path)
        ]

    def clear_requests(self) -> None:
        with self._lock:
            self._requests.clear()

    # ----------------------------------------------------------- dispatch

    def handle(self, request: Request, base: str) -> Reply:
        """Answer one request (called by the server thread)."""

        request_id = new_guid()
        client_id = request.header("client-request-id") or new_guid()
        try:
            reply = self._dispatch(request, base)
        except GraphError as error:
            reply = error.to_reply(self.clock, request_id, client_id)
        except TokenError as error:
            reply = error.to_reply(self.clock)
        except Exception:
            self.internal_errors.append(traceback.format_exc())
            reply = GraphError(
                500,
                "FakeGraphInternalError",
                "FakeGraph failed: " + self.internal_errors[-1][-400:],
            ).to_reply(self.clock, request_id, client_id)
        reply.headers.setdefault("request-id", request_id)
        reply.headers.setdefault("client-request-id", client_id)
        with self._lock:
            self._requests.append(
                LoggedRequest(
                    len(self._requests) + 1,
                    request.method,
                    request.path,
                    request.query,
                    request.headers,
                    request.body,
                    reply.status,
                    time.monotonic(),
                )
            )
        return reply

    def _match(self, request: Request) -> tuple[Route, dict[str, str], str] | None:
        other = False
        path = request.path.rstrip("/") or "/"
        for route in self.routes:
            inner = path
            if route.auth == "bearer":
                if not (path == V1 or path.startswith(V1 + "/")):
                    continue
                inner = path[len(V1) :] or "/"
            found = route.regex.match(inner)
            if found is None:
                continue
            if route.method != request.method:
                other = True
                continue
            return route, found.groupdict(), inner
        if other:
            raise GraphError(405, "MethodNotAllowed", f"{request.method} is not allowed here.")
        return None

    def _dispatch(self, request: Request, base: str) -> Reply:
        matched = self._match(request)
        if matched is None:
            raise GraphError(
                404,
                "FakeGraphNoRoute",
                f"FakeGraph implements no route for {request.method} {request.path}.",
            )
        route, params, inner = matched
        principal: Principal | None = None
        ctx = Ctx(self, request, None, params, route, base)
        if route.auth == "bearer":
            principal = self._authenticate(request)
            ctx.principal = principal
            if (
                route.perm is not None
                and not principal.allows(route.perm)
                and not (route.also_if and route.also_if(ctx))
            ):
                raise denied(route.family, route.perm, principal)
        self._inject(request, inner, principal)
        release = self._enter_concurrency(inner, request)
        try:
            for pattern, seconds in self._delays:
                if fnmatch.fnmatchcase(inner, pattern):
                    time.sleep(seconds)
            result = route.handler(ctx)
        finally:
            release()
        if isinstance(result, Reply):
            return result
        return Reply(204) if result is None else Reply(200, result)

    def _authenticate(self, request: Request) -> Principal:
        header = request.header("authorization") or ""
        headers = {"WWW-Authenticate": _WWW_AUTHENTICATE}

        def invalid(text: str) -> GraphError:
            return GraphError(401, "InvalidAuthenticationToken", text, headers=headers)

        scheme, _, token = header.partition(" ")
        token = token.strip()
        if scheme.lower() != "bearer" or not token:
            raise invalid("Access token is empty.")
        try:
            claims = decode_jwt(token)
        except ValueError:
            raise invalid("CompactToken parsing failed with error code: 80049217") from None
        if not verify_signature(token, self._key):
            raise invalid("Signature validation failed. Signature is invalid.")
        now = self.clock.time()
        if now >= float(claims.get("exp", 0)):
            raise invalid("Lifetime validation failed, the token is expired.")
        if now < float(claims.get("nbf", 0)) - 300:
            raise invalid("Lifetime validation failed, the token is not yet valid.")
        if claims.get("aud") not in (GRAPH_AUDIENCE, GRAPH_APP_ID):
            raise invalid("Access token validation failure. Invalid audience.")
        if str(claims.get("uti")) in self.revoked:
            raise invalid("Access token has been revoked.")
        if claims.get("idtyp") == "app":
            return Principal(
                "app",
                str(claims["appid"]),
                str(claims["tid"]),
                frozenset(claims.get("roles", ())),
                token=token,
            )
        return Principal(
            "user",
            str(claims["appid"]),
            str(claims["tid"]),
            frozenset(str(claims.get("scp", "")).split()),
            user_id=str(claims["oid"]),
            token=token,
        )

    def _inject(self, request: Request, path: str, principal: Principal | None) -> None:
        for fault in self._faults:
            if fault.hit(request, path):
                code, message = _FAULT_TEXT.get(fault.status, ("error", "The request failed."))
                headers = {"Retry-After": fault.retry_after} if fault.retry_after else {}
                raise GraphError(
                    fault.status, fault.code or code, fault.message or message, headers=headers
                )
        for limit in self._limits:
            if limit.method not in ("*", request.method) or not fnmatch.fnmatchcase(
                path, limit.pattern
            ):
                continue
            key = {
                "path": path,
                "user": principal.user_id if principal and principal.user_id else "-",
                "app": principal.client_id if principal else "-",
            }.get(limit.key, "all")
            now = self.clock.time()
            last = limit.last.get(key)
            if last is not None and now - last < 1.0 / limit.per_second:
                limit.rejected += 1
                wait = max(1, math.ceil(1.0 / limit.per_second - (now - last)))
                raise GraphError(
                    429, "TooManyRequests", "Too many requests.", headers={"Retry-After": str(wait)}
                )
            limit.last[key] = now

    def _enter_concurrency(self, path: str, request: Request) -> Callable[[], None]:
        entered: list[tuple[ConcurrencyLimit, str]] = []
        for limit in self._concurrency:
            if not fnmatch.fnmatchcase(path, limit.pattern):
                continue
            key = "/".join(path.split("/")[:3])
            with self._lock:
                count = limit.inflight.get(key, 0) + 1
                limit.inflight[key] = count
            entered.append((limit, key))
            if count > limit.limit:
                self._leave(entered)
                limit.rejected += 1
                raise GraphError(
                    429,
                    "ApplicationThrottled",
                    "Application is over its MailboxConcurrency limit.",
                    headers={"Retry-After": "1"},
                )
            if isinstance(limit.hold, threading.Event):
                limit.hold.wait(timeout=30)
            else:
                time.sleep(limit.hold)
        return lambda: self._leave(entered)

    def _leave(self, entered: list[tuple[ConcurrencyLimit, str]]) -> None:
        with self._lock:
            for limit, key in entered:
                limit.inflight[key] = max(0, limit.inflight.get(key, 1) - 1)
        entered.clear()

    # ----------------------------------------------------------- a raw HTTP client for tests

    def call(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_body: Any = None,
        form: Mapping[str, str] | None = None,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpReply:
        """Send one request with `http.client` (no redirects followed, nothing added but what
        is passed). `path` is a path (`/v1.0/...`) or an absolute URL of this server."""

        parts = urllib.parse.urlsplit(path)
        target = urllib.parse.quote(
            parts.path + (f"?{parts.query}" if parts.query else ""), safe="%/?&=$:@,;+'()*!~-._#"
        )
        sent = dict(headers or {})
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        payload = data
        if json_body is not None:
            payload = json.dumps(json_body).encode()
            sent.setdefault("Content-Type", "application/json")
        elif form is not None:
            payload = urllib.parse.urlencode(dict(form)).encode()
            sent.setdefault("Content-Type", "application/x-www-form-urlencoded")
        host, _, port = (parts.netloc or self.host).partition(":")
        connection = http.client.HTTPConnection(host, int(port), timeout=10)
        try:
            connection.request(method, target or "/", body=payload, headers=sent)
            response = connection.getresponse()
            return HttpReply(
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                response.read(),
            )
        finally:
            connection.close()

    def app_token(self, app: App, **kwargs: Any) -> str:
        """An app token for `app` obtained through the token endpoint."""

        reply = self.call(
            "POST",
            f"/{self.tenant_id}/oauth2/v2.0/token",
            form={
                "grant_type": "client_credentials",
                "client_id": app.client_id,
                "client_secret": app.secret or "",
                "scope": f"{GRAPH_AUDIENCE}/.default",
                **kwargs,
            },
        )
        assert reply.status == 200, reply.text
        return str(reply.json()["access_token"])


__all__ = [
    "BUILTIN_PLUGINS",
    "TENANT_DOMAIN",
    "TENANT_ID",
    "V1",
    "ConcurrencyLimit",
    "Fault",
    "FakeGraph",
    "HttpReply",
    "LoggedRequest",
    "RateLimit",
]
