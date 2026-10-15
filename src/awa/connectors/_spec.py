"""The declarative description of a connector: variables, authentication, operations.

A `ConnectorSpec` is data; only non-HTTP and OpenAPI connectors supply a `factory`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from awa.connectors.ports import Identity, TokenProvider
from awa.errors import ConfigurationError

if TYPE_CHECKING:
    from awa._internal.upstream.tools import BaseTool
    from awa.connectors._engine import ConnectorContext

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
"""Methods an `Operation` can send."""
ParamType = Literal["string", "integer", "number", "boolean", "object", "array"]
"""JSON type of an operation argument."""
Location = Literal["path", "query", "body", "header", "local"]
"""Where an argument goes in the request (`local`: a workspace path that is never sent)."""
Needs = Literal["app", "user", "any"]
"""The identity an operation needs (`any`: either works)."""

CATEGORIES = (
    "generic",
    "data",
    "web",
    "communication",
    "collaboration",
    "ticketing",
    "crm",
    "code",
    "files",
    "payments",
)
"""The connector categories, in display order."""

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_TEMPLATE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")


@dataclass(frozen=True)
class EnvVar:
    """An environment variable a connector reads (its name, never its value).

    `secret` values are masked in forms and redacted from errors; `default` applies to non-secret
    settings only; `example` feeds `.env.example` and setup forms.
    """

    name: str
    description: str
    secret: bool = True
    required: bool = True
    default: str | None = None
    example: str | None = None

    def __post_init__(self) -> None:
        if self.secret and self.default is not None:
            raise ConfigurationError(
                f"{self.name}: a secret has no default; mark it secret=False or drop the default"
            )


@dataclass(frozen=True)
class BearerAuth:
    """`Authorization: Bearer <token>` from one variable.

    Attributes:
        token: The variable holding the token.
        identity: Whose rights the token carries, if that matters to the connector (a pasted
            Graph token is a signed-in person's: `user`); `None` for plain API keys.
    """

    token: EnvVar
    identity: Identity | None = None

    def variables(self) -> tuple[EnvVar, ...]:
        return (self.token,)


@dataclass(frozen=True)
class ApiKeyAuth:
    """A token in a header of its own (`PRIVATE-TOKEN: <token>`) or a query parameter (`?api_key=`).

    `prefix` is put before the token (`"Token "`); `query` names the parameter used instead of
    `header`.
    """

    token: EnvVar
    header: str = "X-API-Key"
    prefix: str = ""
    query: str | None = None

    def variables(self) -> tuple[EnvVar, ...]:
        return (self.token,)


@dataclass(frozen=True)
class BasicAuth:
    """HTTP basic authentication.

    Attributes:
        username: The variable holding the user name.
        password: The variable holding the password or API token.
        username_format: Shapes the user name sent (`"{}/token"` for services that expect it).
    """

    username: EnvVar
    password: EnvVar
    username_format: str = "{}"

    def variables(self) -> tuple[EnvVar, ...]:
        return (self.username, self.password)


@dataclass(frozen=True)
class OAuth2ClientCredentials:
    """OAuth 2.0 client credentials: a token from `token_url`, cached until it expires.

    `token_url` may reference variables as `{NAME}` (a tenant id), listed in `extra`; `client_id`
    and `client_secret` name the variables holding them.
    """

    token_url: str
    client_id: EnvVar
    client_secret: EnvVar
    scope: str | None = None
    extra: tuple[EnvVar, ...] = ()

    def variables(self) -> tuple[EnvVar, ...]:
        return (*self.extra, self.client_id, self.client_secret)


@dataclass(frozen=True)
class OAuth2RefreshToken:
    """OAuth 2.0 refresh-token grant: a long-lived refresh token buys short-lived access tokens."""

    token_url: str
    client_id: EnvVar
    client_secret: EnvVar
    refresh_token: EnvVar

    def variables(self) -> tuple[EnvVar, ...]:
        return (self.client_id, self.client_secret, self.refresh_token)


@dataclass(frozen=True)
class TokenAuth:
    """A bearer token from a `TokenProvider`: the seam for sign-in flows the engine does not know.

    Attributes:
        provider: Builds the provider of one connector instance (called once, then cached).
        env: Variables the provider needs; the auth is used when all required ones are set.
        token_urls: URLs the provider will call (`{NAME}` templates), so their hosts are allowed.
        preferred: Used before the alternatives listed earlier whenever its variables are set (a
            certificate wins over a client secret); the order of `ConnectorSpec.auth` still
            decides which variables "missing" names when nothing is set.
    """

    provider: Callable[[ConnectorContext], TokenProvider]
    kind: Identity
    env: tuple[EnvVar, ...] = ()
    token_urls: tuple[str, ...] = ()
    preferred: bool = False

    def variables(self) -> tuple[EnvVar, ...]:
        return self.env


Auth = (
    BearerAuth | ApiKeyAuth | BasicAuth | OAuth2ClientCredentials | OAuth2RefreshToken | TokenAuth
)
"""The ways a connector authenticates."""


def auth_identity(auth: Auth | None) -> Identity | None:
    """Whose rights `auth` carries: `app`, `user`, or `None` when the notion does not apply."""

    if isinstance(auth, OAuth2ClientCredentials):
        return "app"
    if isinstance(auth, OAuth2RefreshToken):
        return "user"
    if isinstance(auth, BearerAuth):
        return auth.identity
    if isinstance(auth, TokenAuth):
        return auth.kind
    return None


@dataclass(frozen=True)
class Param:
    """One argument of an operation.

    Attributes:
        location: `path` (`{name}` in the path), `query`, `body`, `header`, or `local` (a
            workspace path of a file operation; never sent).
        default: Value used when the model omits it (`None`: left out).
        key: Name on the wire, if it differs (`sysparm_query`); for `body` a dotted path into the
            JSON body (`fields.project.key`); `""` makes an `object` the whole body (or, in the
            query, its parameters).
        pattern: Regular expression a string must match (ids, table names).
        safe: Characters a `path` value keeps unencoded (`"/"` for file paths).
        explode: Send a list as repeated query keys instead of comma-separated.
        convert: Reshape the value before it is placed (addresses into recipient objects).
    """

    name: str
    description: str
    type: ParamType = "string"
    location: Location = "query"
    required: bool = False
    default: Any = None
    key: str | None = None
    enum: tuple[str, ...] = ()
    items: ParamType | None = None
    pattern: str | None = None
    safe: str = ""
    explode: bool = False
    convert: Callable[[Any], Any] | None = None

    def __post_init__(self) -> None:
        if self.location == "path" and not self.required and self.default is None:
            object.__setattr__(self, "required", True)  # a path needs a value: given or default

    @property
    def wire(self) -> str:
        """The name (or body path) used in the request."""

        return self.name if self.key is None else self.key


@dataclass(frozen=True)
class Pagination:
    """How a list operation reaches its next page.

    Attributes:
        kind: `cursor` (a response value goes into the next request), `next_url` (the response
            holds the next page's URL), `link_header` (`Link: <...>; rel="next"`) or `offset`.
        next: Response path of the cursor or next URL (`response_metadata.next_cursor`).
        param: Request parameter that receives the cursor or offset.
        location: Whether `param` and `size_param` go in the `query` or the `body`.
        more: Response path that must be true for another page (`has_more`).
        relative_to: A relative next URL is joined to the `host` or the `base` URL.
    """

    kind: Literal["cursor", "next_url", "link_header", "offset"]
    next: str = ""
    param: str = ""
    location: Literal["query", "body"] = "query"
    size_param: str | None = None
    size_max: int = 100
    more: str | None = None
    relative_to: Literal["host", "base"] = "host"


@dataclass(frozen=True)
class UploadSession:
    """Large uploads in fragments: a request opens a session, the file goes to the URL it returns.

    The fragment requests carry `Content-Range` and never an `Authorization` header (the URL
    is pre-authenticated); a failed fragment resumes from the byte the service says it expects.

    Attributes:
        path: Path of the request that opens the session (POST), with `{param}` placeholders.
        url: Response path of the upload URL.
        simple_limit: Files up to this many bytes go in one request to the operation's own
            `path` (0: always use the session).
        fragment: Bytes per fragment; a multiple of `align` (OneDrive and SharePoint: 320 KiB).
        body_builder: Builds the opening request's JSON body from the arguments, file size and
            file name; `None` sends an empty object.
    """

    path: str
    url: str = "uploadUrl"
    simple_limit: int = 0
    fragment: int = 15 * 327_680
    align: int = 327_680
    body_builder: Callable[[Mapping[str, Any], int, str], Mapping[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.align < 1 or self.fragment < self.align or self.fragment % self.align:
            raise ConfigurationError(
                f"upload fragment size {self.fragment} must be a multiple of {self.align}"
            )


@dataclass(frozen=True)
class Operation:
    """One thing a connector can do; it becomes one tool.

    Attributes:
        name: Operation name (snake_case); the tool is `<connector>_<name>` and the blueprint
            reference `connector:<connector>.<name>`.
        description: What the tool does and when to use it, for the model.
        method_param: Name of an argument that chooses the method per call (one of its `enum`
            values) instead of `method`.
        write: Changes something or sends something out: the tool waits for approval by default
            and counts as an external action.
        body: Fixed body fields (dotted paths, like `Param.key`).
        body_format: `json`, `form` (URL-encoded), `raw` (one text argument) or `file` (the
            bytes of a workspace file, named by `file_param`: an upload).
        body_builder: `arguments -> mapping` for body shapes a `Param` cannot express, merged
            over the fixed `body` and the body arguments (nested mappings merge, anything else
            replaces). Raise `ToolException` for bad arguments.
        items: Response path of the item list: the tool then takes a `limit` argument and
            follows `pages`. `""` means the response is the list; `a|b` tries `a`, then `b`
            (response paths accept this everywhere).
        result: Response path returned instead of the whole response.
        response: `json`, `xml` or `text`; `None` decides by content type; `file` saves the bytes
            to the workspace file named by `file_param` (redirects are followed without
            credentials) and returns its path, size and SHA-256.
        file_param: The `local` argument holding the workspace path to write (`response="file"`)
            or read (`body_format="file"`).
        session: Uploads above `session.simple_limit` bytes go in fragments.
        needs: The identity the operation needs (`app`, `user` or `any`); a call under another
            identity is refused before anything is sent, naming the setup step.
        transform: Reshapes the result for the model: `(result, arguments) -> result`.
        check: Validates the arguments before the request; raises `ToolException`.
        idempotency_header: Header that carries the write's idempotency key, stable for one tool
            call of one run, so a replay after a crash sends the same key.
        html_body: `(arguments) -> name of the argument holding an HTML body` (`None` for a plain
            one): that body passes the outbound HTML filter, and the approver sees its plain-text
            preview and what the filter removed.
        continuation: A single-response operation that reports a further page (a `Link:
            rel="next"` header or a next-page URL in the body) as `next_page` and takes it back
            as `page_token` (the generic REST `get`).
        fallback: Turns a failure of the call into a result, or raises it again (GraphQL
            introspection that the server has switched off).
    """

    name: str
    description: str
    method: HttpMethod = "GET"
    path: str = ""
    params: tuple[Param, ...] = ()
    write: bool = False
    method_param: str | None = None
    query: Mapping[str, str] = field(default_factory=dict)
    body: Mapping[str, Any] = field(default_factory=dict)
    body_format: Literal["json", "form", "raw", "file"] = "json"
    headers: Mapping[str, str] = field(default_factory=dict)
    items: str | None = None
    pages: Pagination | None = None
    result: str | None = None
    response: Literal["json", "xml", "text", "file"] | None = None
    transform: Callable[[Any, Mapping[str, Any]], Any] | None = None
    check: Callable[[Mapping[str, Any]], None] | None = None
    idempotency_header: str | None = None
    html_body: Callable[[Mapping[str, Any]], str | None] | None = None
    default_limit: int = 25
    max_limit: int = 200
    continuation: bool = False
    fallback: Callable[[Exception], Any] | None = None
    body_builder: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    file_param: str | None = None
    session: UploadSession | None = None
    needs: Needs = "any"

    def _check_transfer(self) -> None:
        transfers = self.response == "file" or self.body_format == "file"
        if transfers:
            local = [p for p in self.params if p.name == self.file_param and p.location == "local"]
            if not local:
                raise ConfigurationError(
                    f"operation {self.name!r}: file_param must name a parameter with "
                    "location='local' (the workspace path)"
                )
            if self.response == "file" and self.body_format == "file":
                raise ConfigurationError(f"operation {self.name!r}: download or upload, not both")
            if self.items is not None or self.continuation:
                raise ConfigurationError(f"operation {self.name!r}: a file operation has no pages")
        if self.session is not None and self.body_format != "file":
            raise ConfigurationError(f"operation {self.name!r}: session needs body_format='file'")
        if self.body_builder is not None and self.body_format not in ("json", "form"):
            raise ConfigurationError(
                f"operation {self.name!r}: body_builder needs body_format json or form"
            )

    def __post_init__(self) -> None:
        self._check_transfer()
        placeholders = set(re.findall(r"\{(\w+)\}", self.path))
        missing = placeholders - {p.name for p in self.params if p.location == "path"}
        if missing:
            raise ConfigurationError(
                f"operation {self.name!r}: the path has {', '.join(sorted(missing))} but no "
                f"Param with location='path' of that name; add one"
            )


Factory = Callable[["ConnectorContext"], "list[BaseTool]"]
"""Builds the tools of a connector that is not plain HTTP."""
HealthCheck = Callable[["ConnectorContext"], str]
"""Checks a connection that is not plain HTTP; returns a short success message."""
Configure = Callable[["ConnectorSpec", str, Mapping[str, Any]], "ConnectorSpec"]
"""Derives an instance's spec from its name and options (generic connectors)."""


@dataclass(frozen=True)
class ConnectorSpec:
    """A connector: how to reach a system, how to authenticate, and what it can do.

    Attributes:
        name: Connector name (snake_case): `connector:<name>` in blueprints.
        base_url: Base URL of the API; may reference variables as `{NAME}`
            (`"https://{SHOP_HOST}/api/v2"`).
        env: Variables besides the authentication (base URL, account, sender).
        auth: Ways to authenticate; the first whose variables are all set is used.
        health: The operation (or name of one) `check_connector` calls.
        errors: Response paths that may hold an error message, tried in order.
        ok: Response path that must be true on success (APIs that answer errors with HTTP 200).
        hosts: More hosts requests and redirects may go to (`*.sharepoint.com`).
        csrf_header: Header of a CSRF token fetched before writes, when the `csrf` option is on.
        factory: Builds the tools itself (non-HTTP or described elsewhere); `check` is its
            health check.
        dynamic: The tools are listed by the system (an OpenAPI document), not by `operations`.
        configure: Derives the spec of a configured instance from its name and options (the
            generic OpenAPI, REST, OData and GraphQL connectors).
        notes: What the connector cannot do, in plain words; `check_connector` reports them.
        setup_steps: Per identity (`app`, `user`), the setup step that provides it
            (`"run awa connect microsoft --teams"`), named by the error of an operation whose
            `needs` the active authentication does not meet.
    """

    name: str
    title: str
    category: str
    summary: str
    base_url: str | None = None
    env: tuple[EnvVar, ...] = ()
    auth: tuple[Auth, ...] = ()
    auth_optional: bool = False
    operations: tuple[Operation, ...] = ()
    health: Operation | str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    errors: tuple[str, ...] = (
        "error.message",
        "error_description",
        "message",
        "errorMessages",
        "errors",
        "error",
        "detail",
        "title",
    )
    ok: str | None = None
    hosts: tuple[str, ...] = ()
    csrf_header: str | None = None
    docs: str = ""
    setup: str = ""
    factory: Factory | None = field(default=None, repr=False)
    check: HealthCheck | None = field(default=None, repr=False)
    dynamic: bool = False
    options: tuple[str, ...] = ()
    configure: Configure | None = field(default=None, repr=False)
    notes: tuple[str, ...] = ()
    setup_steps: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ConfigurationError(f"connector name {self.name!r} is not snake_case")
        if self.category not in CATEGORIES:
            raise ConfigurationError(
                f"connector {self.name!r}: unknown category {self.category!r} "
                f"(known: {', '.join(CATEGORIES)})"
            )
        names = [operation.name for operation in self.operations]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ConfigurationError(
                f"connector {self.name!r} has duplicate operations: {', '.join(duplicates)}"
            )
        for name in names:
            if not _NAME.fullmatch(name):
                raise ConfigurationError(f"operation name {name!r} is not snake_case")

    def variables(self) -> tuple[EnvVar, ...]:
        """Every variable the connector may read, without duplicates."""

        seen: dict[str, EnvVar] = {}
        for variable in (*self.env, *(v for auth in self.auth for v in auth.variables())):
            seen.setdefault(variable.name, variable)
        return tuple(seen.values())

    def operation(self, name: str) -> Operation:
        """The operation called `name`.

        Raises:
            ConfigurationError: If there is none.
        """

        for operation in self.operations:
            if operation.name == name:
                return operation
        known = ", ".join(op.name for op in self.operations) or "none"
        raise ConfigurationError(
            f"connector {self.name!r} has no operation {name!r} (known: {known})"
        )

    def reads(self) -> tuple[Operation, ...]:
        """The operations that only read."""

        return tuple(operation for operation in self.operations if not operation.write)

    def writes(self) -> tuple[Operation, ...]:
        """The operations that change something or send something out."""

        return tuple(operation for operation in self.operations if operation.write)

    def references(self, instance: str | None = None) -> tuple[str, ...]:
        """The tool references `<name>.<operation>` (`jira.search_issues`)."""

        prefix = instance or self.name
        return tuple(f"{prefix}.{operation.name}" for operation in self.operations)

    def to_dict(self) -> dict[str, Any]:
        """A JSON-compatible description (variable names, never values)."""

        return {
            "name": self.name,
            "title": self.title,
            "category": self.category,
            "summary": self.summary,
            "variables": [
                {
                    "name": variable.name,
                    "description": variable.description,
                    "secret": variable.secret,
                    "required": variable.required,
                    "default": variable.default,
                    "example": variable.example,
                }
                for variable in self.variables()
            ],
            "auth": [
                [variable.name for variable in auth.variables() if variable.required]
                for auth in self.auth
            ],
            "operations": [
                {
                    "name": op.name,
                    "write": op.write,
                    "description": op.description,
                    **({"needs": op.needs} if op.needs != "any" else {}),
                }
                for op in self.operations
            ],
            "dynamic": self.dynamic,
            "notes": list(self.notes),
            "docs": self.docs,
            "setup": self.setup,
        }


def template_variables(template: str) -> tuple[str, ...]:
    """The `{NAME}` variables a URL template references."""

    return tuple(dict.fromkeys(_TEMPLATE.findall(template)))


def fill_template(template: str, values: Callable[[str], str]) -> str:
    """Replace every `{NAME}` of `template` with `values(NAME)`."""

    return _TEMPLATE.sub(lambda match: values(match.group(1)), template)


def path_param(name: str, description: str, *, pattern: str | None = None, **options: Any) -> Param:
    """A required path parameter."""
    return Param(name, description, location="path", required=True, pattern=pattern, **options)


def body_param(name: str, description: str, key: str | None = None, **options: Any) -> Param:
    """A body parameter, sent under `key` (default: its name)."""
    return Param(name, description, location="body", key=key, **options)


def plain_var(name: str, description: str, example: str | None = None) -> EnvVar:
    """A variable that is not a credential."""
    return EnvVar(name, description, secret=False, example=example)
