"""The building blocks of `FakeGraph`: clock, tokens, errors, and the plug-in API.

Nothing here starts a server; `_helpers.fakegraph.server` does. A *plug-in* is a `Plugin` object
that holds routes, rules and an optional setup function; the built-in ones are `identity` (Entra
token endpoints), `files`, `mail`, `teams`, `todo`, `directory` and `people`. A new service gets
its own module with the same API and is passed to `FakeGraph(plugins=[...])`; nobody edits a
shared route table.

    plugin = Plugin("planner")
    PLANNER_WRITE = Perm(app=("Tasks.ReadWrite.All",), user=("Tasks.ReadWrite",))

    RULE = plugin.rule(
        "planner.patch_needs_if_match",
        "PATCH /planner/tasks/{id} without If-Match is refused with 412.",
        source="learn.microsoft.com/graph/api/plannertask-update",
        source_date="2025-07-23",
    )

    @plugin.route("PATCH", "/planner/tasks/{task-id}", perm=PLANNER_WRITE)
    def update_task(ctx: Ctx) -> Reply: ...
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

if TYPE_CHECKING:
    from _helpers.fakegraph.server import FakeGraph

GRAPH_AUDIENCE = "https://graph.microsoft.com"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
START = datetime(2026, 10, 5, 9, 0, 0, tzinfo=UTC)


class FakeClock:
    """A controllable clock: token lifetimes, upload sessions and created times follow it.

    Args:
        start: The first instant (default 2026-10-05 09:00 UTC).
    """

    def __init__(self, start: datetime = START) -> None:
        self._now = start
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def time(self) -> float:
        """Seconds since the Unix epoch."""

        return self.now().timestamp()

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""

        with self._lock:
            self._now += timedelta(seconds=seconds)

    def iso(self, moment: datetime | None = None) -> str:
        """`2026-10-05T09:00:00Z` (the instant given, or now)."""

        return (moment or self.now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_guid(seed: str | None = None) -> str:
    """A GUID: random, or stable for `seed`."""

    return str(uuid.uuid4() if seed is None else uuid.uuid5(uuid.NAMESPACE_URL, seed))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --------------------------------------------------------------------------- JWT


def _part(data: Mapping[str, Any]) -> str:
    return b64url(json.dumps(data, separators=(",", ":")).encode())


def _signature(signing: str, key: bytes) -> str:
    # 256 bytes, like an RSA-2048 signature: a client cannot tell it from a real one.
    digest = b"".join(
        hmac.new(key, signing.encode() + bytes([i]), hashlib.sha256).digest() for i in range(8)
    )
    return b64url(digest)


def make_jwt(claims: Mapping[str, Any], key: bytes) -> str:
    """A three-part, RS256-looking token whose signature only this server can check."""

    header = {"typ": "JWT", "alg": "RS256", "x5t": "fake-key-1", "kid": "fake-key-1"}
    signing = f"{_part(header)}.{_part(claims)}"
    return f"{signing}.{_signature(signing, key)}"


def decode_jwt(token: str) -> dict[str, Any]:
    """The claims of a JWT, without verifying anything (what a client may do offline).

    Raises:
        ValueError: If `token` is not three base64url parts holding a JSON object.
    """

    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("a JWT has three parts")
    try:
        claims = json.loads(unb64url(parts[1]))
    except ValueError:
        raise ValueError("the JWT payload is not JSON") from None
    if not isinstance(claims, dict):
        raise ValueError("the JWT payload is not an object")
    return claims


def verify_signature(token: str, key: bytes) -> bool:
    """Whether `token` was issued by the server holding `key`."""

    parts = token.split(".")
    return len(parts) == 3 and hmac.compare_digest(
        _signature(f"{parts[0]}.{parts[1]}", key), parts[2]
    )


# --------------------------------------------------------------------------- permissions


@dataclass(frozen=True)
class Perm:
    """What a route needs: any one of the application roles (app token) or delegated scopes
    (user token). An empty tuple means that kind of token can never use the route."""

    app: tuple[str, ...] = ()
    user: tuple[str, ...] = ()


@dataclass(frozen=True)
class Principal:
    """Who a bearer token belongs to.

    Attributes:
        kind: `app` (client credentials, `roles` claim) or `user` (delegated, `scp` claim).
        client_id: The app registration (`appid`).
        tenant_id: The tenant (`tid`).
        grants: Application roles or delegated scopes of the token.
        user_id: The signed-in user's object id (delegated only).
        token: The raw token.
    """

    kind: str
    client_id: str
    tenant_id: str
    grants: frozenset[str]
    user_id: str | None = None
    token: str = field(default="", repr=False)

    def allows(self, perm: Perm) -> bool:
        needed = perm.app if self.kind == "app" else perm.user
        return bool(self.grants & set(needed))


# --------------------------------------------------------------------------- request and reply


@dataclass
class Request:
    """One incoming request (decoded path, query as lists, header names lower-case)."""

    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes
    target: str = ""

    def q(self, name: str, default: str | None = None) -> str | None:
        """The first value of query parameter `name` (exact name, e.g. `$top`)."""

        values = self.query.get(name)
        return values[0] if values else default

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())

    def json(self) -> Any:
        """The body parsed as JSON.

        Raises:
            GraphError: 400 `BadRequest` if it is not JSON.
        """

        try:
            return json.loads(self.body.decode("utf-8")) if self.body else None
        except ValueError:
            raise GraphError(400, "BadRequest", "Invalid JSON in the request body.") from None


@dataclass
class Reply:
    """An outgoing response: `body` is JSON-encoded unless it is `bytes`, text or `None`."""

    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str | None = None

    def encode(self) -> tuple[bytes, str]:
        if self.body is None:
            return b"", self.content_type or "application/json"
        if isinstance(self.body, bytes):
            return self.body, self.content_type or "application/octet-stream"
        if isinstance(self.body, str):
            return self.body.encode("utf-8"), self.content_type or "text/plain; charset=utf-8"
        text = json.dumps(self.body, separators=(",", ":"))
        return text.encode("utf-8"), self.content_type or "application/json;odata.metadata=minimal"


class GraphError(Exception):
    """An error answer in the Graph error format `{"error": {"code", "message", "innerError"}}`."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        headers: Mapping[str, str] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.headers = dict(headers or {})
        self.extra = dict(extra or {})

    def to_reply(self, clock: FakeClock, request_id: str, client_request_id: str) -> Reply:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "innerError": {
                "date": clock.iso(),
                "request-id": request_id,
                "client-request-id": client_request_id,
            },
        }
        error.update(self.extra)
        return Reply(self.status, {"error": error}, dict(self.headers))


class TokenError(Exception):
    """An error answer of the Entra token endpoint (`error`, `error_description`, `error_codes`)."""

    def __init__(self, status: int, error: str, aadsts: int, text: str) -> None:
        super().__init__(f"AADSTS{aadsts}: {text}")
        self.status = status
        self.error = error
        self.aadsts = aadsts
        self.text = text

    def to_reply(self, clock: FakeClock) -> Reply:
        trace, correlation = new_guid(), new_guid()
        stamp = clock.now().strftime("%Y-%m-%d %H:%M:%SZ")
        return Reply(
            self.status,
            {
                "error": self.error,
                "error_description": (
                    f"AADSTS{self.aadsts}: {self.text}\r\nTrace ID: {trace}\r\n"
                    f"Correlation ID: {correlation}\r\nTimestamp: {stamp}"
                ),
                "error_codes": [self.aadsts],
                "timestamp": stamp,
                "trace_id": trace,
                "correlation_id": correlation,
                "error_uri": f"https://login.microsoftonline.com/error?code={self.aadsts}",
            },
            content_type="application/json; charset=utf-8",
        )


def denied(family: str, perm: Perm, principal: Principal) -> GraphError:
    """The 403 Graph gives when a token lacks the permission, in the style of the service."""

    needed = perm.app if principal.kind == "app" else perm.user
    if family == "outlook":
        return GraphError(
            403, "ErrorAccessDenied", "Access is denied. Check credentials and try again."
        )
    if family == "sharepoint":
        return GraphError(403, "accessDenied", "Access denied")
    if family == "directory":
        return GraphError(
            403, "Authorization_RequestDenied", "Insufficient privileges to complete the operation."
        )
    if not needed:
        kind = "application" if principal.kind == "app" else "delegated"
        return GraphError(403, "Forbidden", f"This API is not supported for {kind} permissions.")
    word, listed = ("role", "Roles") if principal.kind == "app" else ("scope", "Scopes")
    return GraphError(
        403,
        "Forbidden",
        f"Missing {word} permissions on the request. API requires one of '{', '.join(needed)}'. "
        f"{listed} on the request '{' '.join(sorted(principal.grants))}'.",
    )


# --------------------------------------------------------------------------- rules and plug-ins


@dataclass(frozen=True)
class Rule:
    """One behavior of Microsoft the fake reproduces, with where we learned it.

    Attributes:
        id: Stable id (`teams.app_only_post_forbidden`); every rule has a self-test naming it.
        text: The rule in one sentence.
        source: The documentation page (or message) it comes from.
        source_date: The page's `updated_at` as read when the rule was written (`YYYY-MM-DD`).
        verified_live: Whether a run against a real tenant confirmed it (false until then; a
            shipped operation must not depend on an unverified rule without a documented maintainer decision).
    """

    id: str
    text: str
    source: str
    source_date: str
    verified_live: bool = False


@dataclass(frozen=True)
class Route:
    """A handler for `method template` (`template` uses Graph's names, `{user-id}`)."""

    method: str
    template: str
    handler: Callable[[Ctx], Any]
    perm: Perm | None
    family: str
    regex: re.Pattern[str]
    auth: str
    plugin: str
    also_if: Callable[[Ctx], bool] | None = None


def _regex(template: str) -> re.Pattern[str]:
    out, position = [], 0
    for match in re.finditer(r"\{([\w-]+)(\*)?\}", template):
        out.append(re.escape(template[position : match.start()]))
        name = match.group(1).replace("-", "_")
        out.append(f"(?P<{name}>.+?)" if match.group(2) else f"(?P<{name}>[^/]+)")
        position = match.end()
    out.append(re.escape(template[position:]))
    return re.compile("^" + "".join(out) + "$")


class Plugin:
    """A set of routes, rules and state that `FakeGraph` serves together.

    Args:
        name: Unique name; the plug-in's state lives in `graph.store(name)`.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.routes: list[Route] = []
        self.rules: list[Rule] = []
        self._setup: Callable[[FakeGraph], Any] | None = None

    def rule(
        self, id: str, text: str, *, source: str, source_date: str, verified_live: bool = False
    ) -> Rule:
        """Declare (and return) a rule; `FakeGraph.rules` lists every rule of every plug-in."""

        found = Rule(id, text, source, source_date, verified_live)
        self.rules.append(found)
        return found

    def setup(self, function: Callable[[FakeGraph], Any]) -> Callable[[FakeGraph], Any]:
        """Register the function that creates this plug-in's state for one server; its return
        value is `graph.store(name)`."""

        self._setup = function
        return function

    def create_store(self, graph: FakeGraph) -> Any:
        return self._setup(graph) if self._setup is not None else None

    def route(
        self,
        method: str,
        template: str,
        *,
        perm: Perm | None = None,
        family: str = "graph",
        auth: str = "bearer",
        also_if: Callable[[Ctx], bool] | None = None,
    ) -> Callable[[Callable[[Ctx], Any]], Callable[[Ctx], Any]]:
        """Register a handler.

        Args:
            method: HTTP method.
            template: Path below `/v1.0` for `bearer` routes (full path otherwise); `{name}`
                matches one segment, `{name*}` several. Dashes in names become underscores in
                `ctx.params`.
            perm: Permission needed (`bearer` routes).
            family: Style of a 403 and of unknown-user errors: `outlook`, `sharepoint`,
                `directory` or `graph`.
            auth: `bearer` (token checked, then `perm`) or `none` (token endpoint, pre-
                authenticated URLs; the handler decides).
            also_if: Called when `perm` is not met; true lets the call through anyway (Exchange
                RBAC grants that the token does not show).
        """

        def register(handler: Callable[[Ctx], Any]) -> Callable[[Ctx], Any]:
            self.routes.append(
                Route(
                    method,
                    template,
                    handler,
                    perm,
                    family,
                    _regex(template),
                    auth,
                    self.name,
                    also_if,
                )
            )
            return handler

        return register


@dataclass
class Ctx:
    """What a handler gets: the server, the request, the caller and the path variables."""

    graph: FakeGraph
    request: Request
    principal: Principal | None
    params: dict[str, str]
    route: Route
    base: str = ""

    @property
    def clock(self) -> FakeClock:
        return self.graph.clock

    @property
    def store(self) -> Any:
        """The state of the plug-in that owns this route."""

        return self.graph.store(self.route.plugin)

    def json(self) -> Any:
        return self.request.json()

    def link(self, path: str, query: Mapping[str, str] | None = None) -> str:
        """An absolute URL below `/v1.0` on this server."""

        suffix = f"?{urlencode(dict(query), safe='-_.~:/', quote_via=quote)}" if query else ""
        return f"{self.base}/v1.0{path}{suffix}"
