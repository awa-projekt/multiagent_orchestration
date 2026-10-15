"""The one engine that turns a `ConnectorSpec`'s operations into tools.

Configured credential values (4+ characters, also URL-encoded) are redacted from results and errors.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast
from xml.etree import ElementTree

from pydantic import Field, create_model

from awa._internal.http import (
    HttpPolicy,
    HttpRequest,
    HttpResponse,
    Refused,
    fetch,
    normalize_hosts,
)
from awa._internal.logging import get_logger
from awa._internal.retry import backoff_delay, parse_retry_after
from awa._internal.upstream.tools import BaseTool, ToolException
from awa.connectors._outbound import cleaned_arguments, review_payload
from awa.connectors._spec import (
    ApiKeyAuth,
    Auth,
    BasicAuth,
    BearerAuth,
    ConnectorSpec,
    OAuth2ClientCredentials,
    OAuth2RefreshToken,
    Operation,
    Pagination,
    Param,
    TokenAuth,
    auth_identity,
    fill_template,
    template_variables,
)
from awa.connectors.ports import Identity, TokenProvider
from awa.core.dotenv import Environment
from awa.errors import ConfigurationError
from awa.kernel.redaction import redact_values
from awa.observability.spans import record_span
from awa.toolkits._common import DEFAULT_MAX_OUTPUT_CHARS, ToolArgs, build_tool, dig, tool_json
from awa.tools.idempotency import run_idempotency_key
from awa.tools.result_cache import IDENTITY_METADATA_KEY
from awa.tools.risk import ToolRisk

_logger = get_logger("connectors")

CONNECTOR_METADATA_KEY = "awa_connector"
"""Tool metadata key holding the connector reference (`jira.search_issues`)."""

COMMON_OPTIONS = (
    "use",
    "allowed_link_hosts",
    "base_url",
    "base_url_env",
    "env",
    "include",
    "read_only",
    "requires_approval",
    "timeout",
    "retries",
    "max_response_bytes",
    "max_output_chars",
    "allowed_hosts",
    "allow_private_networks",
    "allow_http",
    "use_env_proxies",
)
"""Configuration options every connector accepts (see the connectors guide)."""

READ_RISK = ToolRisk(untrusted_input=True, private_data=True, external_action=False)
"""Risk of a read operation: content others wrote, from an external system."""

WRITE_RISK = ToolRisk(untrusted_input=False, private_data=False, external_action=True)
"""Risk of a write or send operation."""

_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_MAX_RETRY_WAIT = 30.0
_MAX_TOOL_NAME = 64
_MIN_SECRET = 4
_LINK_NEXT = re.compile(r'<([^>]+)>\s*;[^,]*\brel="?next"?', re.IGNORECASE)
_NEXT_LINKS = (
    "@odata.nextLink|nextLink|next_link|next_url|nextUrl|next_page_url|links.next.href|links.next"
    "|_links.next.href|paging.next|meta.next|pagination.next|d.__next|next"
)
_TOKEN_KEY = secrets.token_bytes(32)  # per process: a token cannot be forged or edited by a model
_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict[str, Any],
    "array": list[Any],
}

_sleep = time.sleep


class RetryAfter(ToolException):
    """The service asked to wait longer than the engine is willing to (30 seconds per wait).

    A caller that can come back later (a trigger poll, a worker) reads `seconds` and skips this
    round instead of treating the call as failed.

    Attributes:
        seconds: How long the service asked to wait, if it said.
    """

    def __init__(self, message: str, seconds: float | None = None) -> None:
        super().__init__(message)
        self.seconds = seconds


class UncertainOutcome(ToolException):
    """A write whose answer was lost or was a server error: it may or may not have happened.

    The engine never repeats such a write on its own (a second mail, a second task); the caller
    checks the target system first.
    """


Transport = Callable[[HttpRequest], HttpResponse]
"""Sends a request; the default goes through `awa.toolkits`' hardened HTTP client."""


class _Tokens:
    """An OAuth access token, cached until shortly before it expires."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value: str | None = None
        self.expires = 0.0


@dataclass
class ConnectorContext:
    """A configured connector instance: its spec, variables, options and transport.

    `name` is the tool name prefix and the `<name>` of `connector:<name>.<operation>`; `transport`
    `None` uses the hardened HTTP client; `ungated` lists operations whose approval a blueprint
    adds instead.
    """

    spec: ConnectorSpec
    name: str
    env: Environment
    config: Mapping[str, Any] = field(default_factory=dict)
    transport: Transport | None = None
    ungated: frozenset[str] = frozenset()
    _tokens: _Tokens = field(default_factory=_Tokens, repr=False)
    _ephemeral: list[str] = field(default_factory=list, repr=False)
    _providers: dict[int, TokenProvider] = field(default_factory=dict, repr=False)

    def derive(self, spec: ConnectorSpec) -> ConnectorContext:
        """The same instance with another spec (an OpenAPI document's operations)."""

        return replace(self, spec=spec, _tokens=_Tokens(), _ephemeral=[], _providers={})

    def provider(self, auth: Auth) -> TokenProvider | None:
        """The token provider of a token-based `auth` (built once per instance), else `None`."""

        if not isinstance(auth, OAuth2ClientCredentials | OAuth2RefreshToken | TokenAuth):
            return None
        key = id(auth)
        if key not in self._providers:
            self._providers[key] = (
                auth.provider(self) if isinstance(auth, TokenAuth) else _FormGrant(auth)
            )
        return self._providers[key]

    def identity(self) -> Identity | None:
        """Whose rights the active authentication carries (`None`: unknown or not applicable)."""

        return auth_identity(self.active_auth())

    def drop_token(self) -> None:
        """Forget the cached token (the service rejected it); the next request gets a new one."""

        self._tokens.value = None
        auth = self.active_auth()
        provider = self.provider(auth) if auth is not None else None
        if provider is not None:
            provider.invalidate(self)

    def check_needs(self, operation: Operation) -> None:
        """Refuse an operation the active identity cannot serve, before anything is sent."""

        have = self.identity()
        if operation.needs == "any" or have is None or have == operation.needs:
            return
        wanted = "a signed-in person" if operation.needs == "user" else "the app's own identity"
        current = "the app's own identity" if have == "app" else "a signed-in person"
        step = self.spec.setup_steps.get(operation.needs) or (
            f"see `awa connectors show {self.spec.name}` for the setup"
        )
        raise ToolException(
            f"{self.spec.title}: {operation.name} acts as {wanted}, but this connector is set up "
            f"with {current}. Setup step: {step}"
        )

    def value(self, variable: str) -> str | None:
        """The value of `variable`, or its declared default."""

        found = self.env.get(variable)
        if found is not None:
            return found
        declared = next((v for v in self.spec.variables() if v.name == variable), None)
        return declared.default if declared is not None else None

    def require(self, variable: str) -> str:
        """The value of `variable`; an unset one is refused by name only."""

        found = self.value(variable)
        if found is None:
            raise self._unset(variable)
        return found

    def _unset(self, variable: str) -> ToolException:
        return ToolException(
            f"{self.spec.title} is not configured: set the environment variable "
            f"{self.env.name(variable)}"
        )

    def active_auth(self) -> Auth | None:
        """The authentication used: a `preferred` one whose required variables are all set,
        else the first whose required variables are all set."""

        complete = [
            auth
            for auth in self.spec.auth
            if all(self.value(v.name) is not None for v in auth.variables() if v.required)
        ]
        preferred = [a for a in complete if isinstance(a, TokenAuth) and a.preferred]
        return (preferred or complete or [None])[0]

    def missing(self) -> tuple[str, ...]:
        """Names of the required variables that are not set (after renames)."""

        skip: set[str] = set()
        needed: list[str] = []
        if self.config.get("base_url") or self.config.get("base_url_env"):
            skip.update(template_variables(self.spec.base_url or ""))
            if self.config.get("base_url_env"):
                needed.append(str(self.config["base_url_env"]))
        needed.extend(v.name for v in self.spec.env if v.required and v.name not in skip)
        explicit = self.config.get("base_url_env")
        missing = [
            name
            for name in needed
            if (self.env.get(name) if name == explicit else self.value(name)) is None
        ]
        if self.spec.auth and not self.spec.auth_optional and self.active_auth() is None:
            first = self.spec.auth[0]
            missing.extend(
                v.name for v in first.variables() if v.required and self.value(v.name) is None
            )
        return tuple(dict.fromkeys(self.env.name(name) for name in missing))

    def secrets(self) -> list[str]:
        """The credential values currently set (to remove them from any text)."""

        values = [self.env.get(v.name) for v in self.spec.variables() if v.secret]
        found = [v for v in values if v is not None]
        if self._tokens.value:
            found.append(self._tokens.value)
        return found + self._ephemeral

    def redact(self, text: str) -> str:
        """`text` with every credential value replaced by `***`."""

        return redact_values(text, self.secrets(), min_len=_MIN_SECRET)

    def option(self, name: str, default: Any = None) -> Any:
        return self.config.get(name, default)

    def operations(self) -> tuple[Operation, ...]:
        """The operations this instance offers (`include` and `read_only` applied)."""

        operations = self.spec.operations
        include = self.option("include")
        if include is not None and not self.spec.dynamic:
            names = [include] if isinstance(include, str) else list(include)
            for name in names:
                self.spec.operation(name)
            operations = tuple(op for op in operations if op.name in names)
        if self.option("read_only"):
            operations = tuple(op for op in operations if not op.write)
        return operations

    def requires_approval(self, operation: Operation) -> bool:
        """Whether the tool of `operation` waits for a human decision itself."""

        if not operation.write or {operation.name, "*"} & self.ungated:
            return False
        return self.option("requires_approval", True) is not False

    def base_url(self) -> str:
        """The API's base URL."""

        literal = self.option("base_url")
        if literal:
            url = str(literal)
        elif self.option("base_url_env"):
            variable = str(self.option("base_url_env"))
            found = self.env.get(variable)
            if found is None:
                raise self._unset(variable)
            url = found
        elif self.spec.base_url:
            url = fill_template(self.spec.base_url, self.require)
        else:
            raise ToolException(f"{self.spec.title} has no base URL; set the base_url option")
        return url.rstrip("/")

    def policy(self) -> HttpPolicy:
        """Where this instance may send requests: the base URL's host, token endpoints,
        `spec.hosts` and the `allowed_hosts` option."""

        hosts = [urllib.parse.urlsplit(self.base_url()).hostname or ""]
        for auth in self.spec.auth:
            for template in _token_urls(auth):
                with contextlib.suppress(ToolException):
                    hosts.append(
                        urllib.parse.urlsplit(fill_template(template, self.require)).hostname or ""
                    )
        hosts.extend(self.spec.hosts)
        extra = self.option("allowed_hosts") or ()
        hosts.extend([extra] if isinstance(extra, str) else extra)
        try:
            allowed = normalize_hosts([h for h in dict.fromkeys(hosts) if h])
        except ConfigurationError as exc:
            raise ToolException(str(exc)) from None
        return HttpPolicy(
            allowed_hosts=allowed,
            allow_private_networks=bool(self.option("allow_private_networks", False)),
            allow_http=bool(self.option("allow_http", False)),
            timeout=float(self.option("timeout", 30.0)),
            max_response_bytes=int(self.option("max_response_bytes", 5_000_000)),
            use_env_proxies=bool(self.option("use_env_proxies", False)),
            user_agent="awa-connectors/1",
        )

    def secret_headers(self) -> frozenset[str]:
        return frozenset(
            ["authorization", "cookie"]
            + [
                auth.header.lower()
                for auth in self.spec.auth
                if isinstance(auth, ApiKeyAuth) and auth.query is None
            ]
            + ([self.spec.csrf_header.lower()] if self.spec.csrf_header else [])
        )


def _token_urls(auth: Auth) -> tuple[str, ...]:
    """The token endpoints (`{NAME}` templates) an authentication calls."""

    if isinstance(auth, OAuth2ClientCredentials | OAuth2RefreshToken):
        return (auth.token_url,)
    if isinstance(auth, TokenAuth):
        return auth.token_urls
    return ()


def send(
    ctx: ConnectorContext,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    write: bool = False,
    authenticate: bool = True,
    follow_redirects: bool = True,
    max_bytes: int | None = None,
) -> HttpResponse:
    """Send one request with authentication and retries; error statuses are returned.

    Reads are retried after 429, 502, 503, 504 and failed connections; writes
    only after 429 (the request was not processed). A `Retry-After` is waited for
    (at least one second, at most 30); a longer one is not, the caller gets the 429.

    Args:
        write: The request cannot be repeated safely: a lost answer or a 5xx is not retried
            and a lost answer is an `UncertainOutcome`.
        authenticate: Add the connector's credentials (off for pre-authenticated URLs).
        follow_redirects: Follow redirects (secret headers are dropped across hosts); off
            returns a 3xx answer for the caller to follow without credentials.

    Raises:
        UncertainOutcome: If a write got no answer.
    """

    policy = ctx.policy()
    if max_bytes is not None:
        policy = replace(policy, max_response_bytes=max_bytes)
    # Host, scheme and user-info checks without DNS; the default transport
    # repeats them and also refuses private addresses.
    replace(policy, allow_private_networks=True).check_url(url)
    retries = max(0, int(ctx.option("retries", 2)))
    reauthenticated = False
    attempt = 0
    while True:
        final = dict(headers or {})
        target = url
        if authenticate:
            final.update(_auth_headers(ctx, method, url, final, body))
            target = _keyed(ctx, url)
        request = HttpRequest(method, target, final, body)
        try:
            response = (
                ctx.transport(request)
                if ctx.transport is not None
                else fetch(
                    policy,
                    target,
                    method=method,
                    headers=final,
                    secret_headers=ctx.secret_headers(),
                    data=body,
                    follow_redirects=follow_redirects,
                )
            )
        except Refused as exc:  # nothing was sent: no retry, no uncertainty
            raise ToolException(ctx.redact(str(exc))) from None
        except ToolException as exc:
            if write:
                raise UncertainOutcome(
                    ctx.redact(
                        f"{exc}; the request may or may not have been processed, so it was not "
                        f"repeated: check {ctx.spec.title} before trying again"
                    )
                ) from None
            if attempt >= retries:
                raise ToolException(ctx.redact(str(exc))) from None
            _sleep(_backoff(attempt))
            attempt += 1
            continue
        if response.status == 401 and authenticate and not reauthenticated and ctx._tokens.value:
            ctx.drop_token()  # an expired or revoked token: fetch a new one once
            reauthenticated = True
            continue
        retryable = response.status == 429 or (response.status in _RETRY_STATUSES and not write)
        if retryable and attempt < retries:
            delay = parse_retry_after(response.headers.get("Retry-After"))
            delay = _backoff(attempt) if delay is None else max(delay, 1.0)
            if delay <= _MAX_RETRY_WAIT:
                _sleep(delay)
                attempt += 1
                continue
        return response


def _backoff(attempt: int) -> float:
    return backoff_delay(attempt, initial=0.5, cap=8.0)


def _auth_headers(
    ctx: ConnectorContext, method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> dict[str, str]:
    auth = ctx.active_auth()
    if auth is None:
        if ctx.spec.auth and not ctx.spec.auth_optional:
            missing = ", ".join(ctx.missing())
            raise ToolException(f"{ctx.spec.title} is not configured: set {missing}")
        return {}
    if isinstance(auth, BearerAuth):
        return {"Authorization": f"Bearer {ctx.require(auth.token.name)}"}
    if isinstance(auth, ApiKeyAuth):
        return {} if auth.query else {auth.header: f"{auth.prefix}{ctx.require(auth.token.name)}"}
    if isinstance(auth, BasicAuth):
        user = auth.username_format.format(ctx.require(auth.username.name))
        pair = f"{user}:{ctx.require(auth.password.name)}".encode()
        return {"Authorization": "Basic " + base64.b64encode(pair).decode("ascii")}
    provider = ctx.provider(auth)
    assert provider is not None  # every remaining kind of auth is token-based
    token = provider.token(ctx)
    ctx._tokens.value = token  # known to `secrets()` (redaction) and to the 401 retry
    return {"Authorization": f"Bearer {token}"}


class _FormGrant:
    """The `TokenProvider` of the built-in OAuth 2.0 grants: a form POST to the token URL."""

    def __init__(self, auth: OAuth2ClientCredentials | OAuth2RefreshToken) -> None:
        self.auth = auth
        self.kind: Identity = "app" if isinstance(auth, OAuth2ClientCredentials) else "user"

    def token(self, ctx: ConnectorContext) -> str:
        return _access_token(ctx, self.auth)

    def invalidate(self, ctx: ConnectorContext) -> None:
        ctx._tokens.value = None


def _without_param(url: str, name: str) -> tuple[urllib.parse.SplitResult, str]:
    parts = urllib.parse.urlsplit(url)
    pairs = [
        p for p in parts.query.split("&") if p and urllib.parse.unquote(p.split("=")[0]) != name
    ]
    return parts, "&".join(pairs)


def _keyed(ctx: ConnectorContext, url: str) -> str:
    """`url` with the API key of a query-parameter `ApiKeyAuth` (replacing one already there)."""

    auth = ctx.active_auth()
    if not isinstance(auth, ApiKeyAuth) or auth.query is None:
        return url
    parts, query = _without_param(url, auth.query)
    key = (
        urllib.parse.quote(auth.query, safe="")
        + "="
        + urllib.parse.quote(auth.prefix + ctx.require(auth.token.name), safe="")
    )
    return urllib.parse.urlunsplit(parts._replace(query=f"{query}&{key}" if query else key))


def _unkeyed(ctx: ConnectorContext, url: str) -> str:
    """`url` without a query-parameter API key (before it is handed to the model)."""

    auth = ctx.active_auth()
    if not isinstance(auth, ApiKeyAuth) or auth.query is None:
        return url
    parts, query = _without_param(url, auth.query)
    return urllib.parse.urlunsplit(parts._replace(query=query))


def _access_token(ctx: ConnectorContext, auth: OAuth2ClientCredentials | OAuth2RefreshToken) -> str:
    tokens = ctx._tokens
    with tokens.lock:
        if tokens.value is not None and time.monotonic() < tokens.expires:
            return tokens.value
        form = {
            "client_id": ctx.require(auth.client_id.name),
            "client_secret": ctx.require(auth.client_secret.name),
        }
        if isinstance(auth, OAuth2RefreshToken):
            form |= {
                "grant_type": "refresh_token",
                "refresh_token": ctx.require(auth.refresh_token.name),
            }
        else:
            form["grant_type"] = "client_credentials"
            if auth.scope:
                form["scope"] = auth.scope
        token_url = fill_template(auth.token_url, ctx.require)
        response = send(
            ctx,
            "POST",
            token_url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            body=urllib.parse.urlencode(form).encode(),
            authenticate=False,
        )
        data = _parse(response, None)
        token = dig(data, "access_token") if response.status < 400 else None
        if not isinstance(token, str) or not token:
            detail = _detail(ctx, data)
            host = urllib.parse.urlsplit(token_url).hostname
            raise ToolException(
                ctx.redact(
                    f"{ctx.spec.title}: no access token from {host} (HTTP {response.status})"
                    f"{detail}; check " + ", ".join(ctx.env.name(v.name) for v in auth.variables())
                )
            )
        try:
            seconds = float(dig(data, "expires_in") or 3600)
        except (TypeError, ValueError):
            seconds = 3600.0
        tokens.value = token
        tokens.expires = time.monotonic() + max(seconds - 60.0, 30.0)
        return token


def xml_to_data(text: str) -> Any:
    """Parse an XML answer into nested dicts (repeated elements become lists); refuse a DOCTYPE."""

    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ToolException("the answer is XML with a document type declaration; refused")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ToolException(f"the answer is not well-formed XML: {exc}") from None
    return {_local(root.tag): _element(root)}


def pick(data: Any, path: str) -> Any:
    """`dig` with alternatives: `"value|d.results"` returns the first path that is set."""

    for option in path.split("|"):
        found = dig(data, option)
        if found is not None:
            return found
    return None


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _element(element: ElementTree.Element) -> Any:
    children = list(element)
    if not children:
        return (element.text or "").strip()
    data: dict[str, Any] = {}
    for child in children:
        key, value = _local(child.tag), _element(child)
        if key in data:
            existing = data[key]
            data[key] = [*existing, value] if isinstance(existing, list) else [existing, value]
        else:
            data[key] = value
    return data


def _kind(response: HttpResponse) -> str:
    """`"json"`, `"xml"` or `"text"`, by the answer's content type."""

    if response.content_type.endswith("json"):
        return "json"
    return "xml" if response.content_type.endswith(("/xml", "+xml")) else "text"


def _parse(response: HttpResponse, kind: str | None) -> Any:
    text = response.text()
    kind = kind or _kind(response)
    if kind == "json":
        try:
            return json.loads(text) if text.strip() else None
        except ValueError:
            return text
    if kind == "xml" and text.strip():
        try:
            return xml_to_data(text)
        except ToolException:
            return text
    return text


def _detail(ctx: ConnectorContext, data: Any) -> str:
    message: Any = None
    if isinstance(data, dict | list):
        for path in ctx.spec.errors:
            found = pick(data, path)
            if found:
                message = found
                break
    elif isinstance(data, str) and data.strip():
        message = " ".join(re.sub(r"<[^>]+>", " ", data).split())
    if not message:
        return ""
    if isinstance(message, list):
        message = "; ".join(m if isinstance(m, str) else json.dumps(m) for m in message[:5])
    elif not isinstance(message, str):
        message = json.dumps(message, ensure_ascii=False, default=str)
    text = message if len(message) <= 300 else message[:300] + "…"
    return f": {text}"


def _raise_for(
    ctx: ConnectorContext, response: HttpResponse, data: Any, *, write: bool = False
) -> None:
    status, title = response.status, ctx.spec.title
    detail = _detail(ctx, data)
    if status >= 400:
        if status == 401:
            auth = ctx.active_auth()
            names = ", ".join(ctx.env.name(v.name) for v in auth.variables()) if auth else ""
            message = f"{title} rejected the credentials (HTTP 401){detail}" + (
                f"; check {names}" if names else ""
            )
        elif status == 403:
            message = (
                f"{title} denied access (HTTP 403){detail}; the credentials lack the "
                "permission for this operation"
            )
        elif status == 404:
            message = f"{title} found nothing there (HTTP 404){detail}; check the ids and names"
        elif status == 429:
            after = parse_retry_after(response.headers.get("Retry-After"))
            wait = f"; retry after {after:.0f} seconds" if after is not None else ""
            raise RetryAfter(
                ctx.redact(f"{title} is rate limiting the requests (HTTP 429){wait}"), after
            )
        elif status >= 500 and write:
            raise UncertainOutcome(
                ctx.redact(
                    f"{title} had a server error (HTTP {status}){detail}; the change may or may "
                    f"not have been made, so it was not repeated: check {title} before trying again"
                )
            )
        elif status >= 500:
            after = parse_retry_after(response.headers.get("Retry-After"))
            message = f"{title} had a server error (HTTP {status}){detail}; try again later"
            if after is not None and after > _MAX_RETRY_WAIT:
                raise RetryAfter(ctx.redact(f"{message} (retry after {after:.0f} seconds)"), after)
        else:
            message = f"{title} rejected the request (HTTP {status}){detail}"
        raise ToolException(ctx.redact(message))
    if ctx.spec.ok is not None and isinstance(data, dict) and pick(data, ctx.spec.ok) is not True:
        raise ToolException(ctx.redact(f"{title} reported an error{detail or ': unknown'}"))


def _whole(ctx: ConnectorContext, operation: Operation, response: HttpResponse, data: Any) -> None:
    """Refuse truncated structured data, or a listing answered with text, instead of no result."""

    if response.truncated and (operation.response or _kind(response)) != "text":
        raise ToolException(
            f"{ctx.spec.title}: the answer is larger than max_response_bytes and was cut off, "
            "so it is incomplete; ask for less (limit) or raise that option"
        )
    if operation.items is not None and isinstance(data, str):
        raise ToolException(
            f"{ctx.spec.title}: the answer is not a list (a sign-in or proxy page?); nothing was read"
        )


def _query_string(query: Mapping[str, Any], explode: frozenset[str]) -> str:
    pairs: list[tuple[str, str]] = []
    for key, value in query.items():
        if value is None:
            continue
        values = value if isinstance(value, list | tuple) else [value]
        texts = [str(v).lower() if isinstance(v, bool) else str(v) for v in values]
        if key in explode:
            pairs.extend((key, text) for text in texts)
        else:
            pairs.append((key, ",".join(texts)))
    return urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)


def _with_query(url: str, query: str) -> str:
    if not query:
        return url
    return url + ("&" if urllib.parse.urlsplit(url).query else "?") + query


def _put(target: dict[str, Any], path: str, value: Any) -> None:
    if not path:
        if not isinstance(value, dict):
            raise ToolException("the request body must be a JSON object")
        target.update(value)
        return
    *parents, last = path.split(".")
    for part in parents:
        target = target.setdefault(part, {})
    target[last] = value


def _form(data: Mapping[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, Mapping):
            pairs.extend(_form(value, name))
        elif isinstance(value, list):
            pairs.extend((f"{name}[{i}]", str(v)) for i, v in enumerate(value))
        elif value is not None:
            pairs.append((name, str(value).lower() if isinstance(value, bool) else str(value)))
    return pairs


def _path_value(param: Param, value: Any) -> str:
    text = str(value)
    # `quote` keeps dots, so `..` (or `a/../b` where `/` stays) would climb out of the path.
    if any(part in (".", "..") for part in (text.split("/") if "/" in param.safe else [text])):
        raise ToolException(f"{param.name} must not contain '.' or '..' path segments")
    return urllib.parse.quote(text, safe=param.safe)


@dataclass
class _Call:
    """The parts of one operation call before sending."""

    method: str
    url: str
    query: dict[str, Any]
    headers: dict[str, str]
    body: Any
    raw: str | None
    explode: frozenset[str]


def fill_path(
    ctx: ConnectorContext, operation: Operation, template: str, args: Mapping[str, Any]
) -> str:
    """The URL of `template` (a path with `{param}` placeholders of `operation`) under the base."""

    by_name = {param.name: param for param in operation.params}

    def fill(match: re.Match[str]) -> str:
        param = by_name[match.group(1)]
        return _path_value(param, args.get(param.name))

    path = re.sub(r"\{(\w+)\}", fill, template)
    return path if path.startswith(("https://", "http://")) else ctx.base_url() + path


def _merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    """Merge `source` into `target`: nested mappings merge, anything else replaces."""

    for key, value in source.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = json.loads(json.dumps(value))


def _prepare(ctx: ConnectorContext, operation: Operation, args: Mapping[str, Any]) -> _Call:
    url = fill_path(ctx, operation, operation.path, args)
    query: dict[str, Any] = dict(operation.query)
    headers: dict[str, str] = {"Accept": "application/json", **ctx.spec.headers}
    headers.update(operation.headers)
    method = str(args.get(operation.method_param) or "") if operation.method_param else ""
    method = method.upper() or operation.method
    body: Any = None
    raw: str | None = None
    if method != "GET" and operation.body_format != "raw":
        body = {}
        for key, value in operation.body.items():
            _put(body, key, json.loads(json.dumps(value)))
    for param in operation.params:
        value = args.get(param.name)
        if (
            value is None
            or param.location in ("path", "local")
            or param.name == operation.method_param
        ):
            continue
        if param.convert is not None:
            value = param.convert(value)
        if param.location == "query":
            if param.wire == "" and isinstance(value, dict):
                query.update(value)
            else:
                query[param.wire] = value
        elif param.location == "header":
            if param.wire.lower() in ctx.secret_headers():
                raise ToolException(f"header {param.wire!r} cannot be set")
            headers[param.wire] = str(value)
        elif operation.body_format == "raw":
            raw = str(value)
        elif param.wire == "" and not isinstance(value, dict):
            body = value
        else:
            body = {} if not isinstance(body, dict) else body
            _put(body, param.wire, value)
    if operation.body_builder is not None and method != "GET":
        body = body if isinstance(body, dict) else {}
        _merge(body, operation.body_builder(args))
    if operation.idempotency_header:
        headers[operation.idempotency_header] = _idempotency_key(ctx, operation)
    explode = frozenset(p.wire for p in operation.params if p.explode)
    return _Call(method, url, query, headers, body, raw, explode)


def _idempotency_key(ctx: ConnectorContext, operation: Operation) -> str:
    """The stable key of this write in the current run and tool call; random outside a run."""

    key = run_idempotency_key(f"{ctx.name}.{operation.name}")
    if key is not None:
        return key
    _logger.info(
        "No run context for %s.%s: sending a random idempotency key (not stable across a crash)",
        ctx.name,
        operation.name,
    )
    return str(uuid.uuid4())


def _encode(operation: Operation, call: _Call) -> tuple[bytes | None, dict[str, str]]:
    headers = dict(call.headers)
    if operation.body_format == "raw":
        if call.raw is None:
            return None, headers
        headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        return call.raw.encode("utf-8"), headers
    if call.body is None or (call.method in ("GET", "DELETE") and call.body == {}):
        return None, headers
    if operation.body_format == "form" and isinstance(call.body, dict):
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        return urllib.parse.urlencode(_form(call.body)).encode(), headers
    headers["Content-Type"] = "application/json"
    return json.dumps(call.body, ensure_ascii=False).encode("utf-8"), headers


def _csrf(ctx: ConnectorContext, operation: Operation) -> dict[str, str]:
    header = ctx.spec.csrf_header
    if not operation.write or header is None or not ctx.option("csrf"):
        return {}
    response = send(ctx, "GET", ctx.base_url() + "/", headers={header: "Fetch"})
    token = response.headers.get(header)
    if response.status >= 400 or not token:
        raise ToolException(
            ctx.redact(f"{ctx.spec.title} gave no CSRF token (HTTP {response.status})")
        )
    cookies = [c.split(";", 1)[0].strip() for c in response.headers.get_all("Set-Cookie") or ()]
    ctx._ephemeral.extend([token, *cookies])
    found = {header: token}
    if cookies:
        found["Cookie"] = "; ".join(cookies)
    return found


def call_operation(ctx: ConnectorContext, operation: Operation, args: Mapping[str, Any]) -> Any:
    """Run `operation` with validated `args` and return its (shaped) result."""

    if operation.check is not None:
        operation.check(args)
    if operation.html_body is not None:
        args = _filtered(ctx, operation, args)
    try:
        return _call(ctx, operation, args)
    except ToolException as exc:
        if operation.fallback is None:
            raise
        return operation.fallback(exc)


def _filtered(
    ctx: ConnectorContext, operation: Operation, args: Mapping[str, Any]
) -> Mapping[str, Any]:
    """`args` with the HTML body cleaned by the outbound filter (see `awa.connectors._outbound`)."""

    cleaned_args, cleaned = cleaned_arguments(ctx, operation.html_body, args)
    if cleaned is not None:
        record_span(
            "awa.outbound.filter",
            {
                "awa.connector.operation": f"{ctx.name}.{operation.name}",
                "awa.outbound.hidden_count": len(cleaned.hidden),
                "awa.outbound.body_sha256": cleaned.sha256,
            },
        )
        if cleaned.hidden:
            _logger.warning(
                "Outbound filter removed %d part(s) from %s.%s",
                len(cleaned.hidden),
                ctx.name,
                operation.name,
            )
    return cleaned_args


def _call(ctx: ConnectorContext, operation: Operation, args: Mapping[str, Any]) -> Any:
    ctx.check_needs(operation)
    call = _prepare(ctx, operation, args)
    call.headers.update(_csrf(ctx, operation))
    token = args.get("page_token")
    following: str | None = None
    if operation.response == "file" or operation.body_format == "file":
        from awa.connectors._transfer import transfer

        result = transfer(ctx, operation, call, args)
    elif operation.items is None:
        url = _with_query(call.url, _query_string(call.query, call.explode))
        if token:
            kind, value, _ = _read_token(token)
            if kind != "url":
                raise ToolException("page_token is not a next_page of this tool")
            url = value
        body, headers = _encode(operation, call)
        response = send(ctx, call.method, url, headers=headers, body=body, write=operation.write)
        data = _parse(response, operation.response)
        _raise_for(ctx, response, data, write=operation.write)
        _whole(ctx, operation, response, data)
        result = pick(data, operation.result) if operation.result is not None else data
        if result in (None, "") and operation.write and response.status < 300:
            result = {"ok": True, "status": response.status}
        if operation.continuation:
            following = _next_link(ctx, data, response)
    else:
        limit = int(args.get("limit") or operation.default_limit)
        result = _collect(ctx, operation, call, limit, token)
    if operation.transform is not None:
        result = operation.transform(result, args)
    if following is not None:
        result = {"data": result, "next_page": _make_token("url", _unkeyed(ctx, following))}
    return result


def _next_link(ctx: ConnectorContext, data: Any, response: HttpResponse) -> str | None:
    """The next page's URL: a `Link: rel="next"` header or a next-page URL in the body."""

    found: Any = _link_header_next(response)
    if found is None and isinstance(data, dict):
        found = pick(data, _NEXT_LINKS)
    if not isinstance(found, str) or not found.startswith(("https://", "http://", "/", "?")):
        return None
    return urllib.parse.urljoin(response.url or ctx.base_url() + "/", found)


def _link_header_next(response: HttpResponse) -> str | None:
    """The `rel="next"` URL of the answer's `Link` header, if any."""

    match = _LINK_NEXT.search(", ".join(response.headers.get_all("Link") or ()))
    return match.group(1) if match else None


def _make_token(kind: str, value: Any, skip: int = 0) -> str:
    """An opaque continuation token: how to fetch a page and how many of its items to skip."""

    raw = json.dumps([kind, value, skip], separators=(",", ":")).encode()
    mac = hmac.new(_TOKEN_KEY, raw, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(mac + raw).decode().rstrip("=")


def _read_token(token: Any) -> tuple[str, Any, int]:
    try:
        text = str(token)
        blob = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        mac, raw = blob[:16], blob[16:]
        if not hmac.compare_digest(mac, hmac.new(_TOKEN_KEY, raw, hashlib.sha256).digest()[:16]):
            raise ValueError("signature")
        kind, value, skip = json.loads(raw)
        if kind not in ("first", "url", "param") or not isinstance(skip, int) or skip < 0:
            raise ValueError(kind)
        if kind == "url" and not (
            isinstance(value, str) and value.startswith(("https://", "http://"))
        ):
            raise ValueError(value)
    except (ValueError, TypeError):
        raise ToolException("page_token is not a next_page this tool returned") from None
    return kind, value, skip


def _collect(
    ctx: ConnectorContext, operation: Operation, call: _Call, limit: int, token: Any = None
) -> Any:
    """Items of up to `AWA_MAX_PAGES_PER_CALL` pages (default 20); `next_page` continues after."""

    pages = operation.pages
    items: list[Any] = []
    url, offset = call.url, 0
    size = min(limit, pages.size_max) if pages is not None else limit
    target = _paging_target(pages, call)
    if pages is not None and pages.size_param:
        target[pages.size_param] = size
    current: tuple[str, Any] = ("first", None)
    skip = 0
    if token:
        kind, value, skip = _read_token(token)
        current = (kind, value)
        if kind == "url":
            url, call.query = value, {}
        elif kind == "param":
            if pages is None or not pages.param:
                raise ToolException("page_token is not a next_page of this tool")
            offset = value if isinstance(value, int) else offset
            target[pages.param] = value
    following: tuple[Literal["url", "param"], Any] | None = None
    for _ in range(_max_pages()):
        body, headers = _encode(operation, call)
        response = send(
            ctx,
            call.method,
            _with_query(url, _query_string(call.query, call.explode)),
            headers=headers,
            body=body,
            write=operation.write,
        )
        data = _parse(response, operation.response)
        _raise_for(ctx, response, data)
        _whole(ctx, operation, response, data)
        found = pick(data, operation.items or "")
        page = found if isinstance(found, list) else [] if found in (None, "") else [found]
        items.extend(page[skip:])
        following = _next_page(ctx, pages, data, response, page, size, offset)
        if len(items) > limit:  # the next call starts within this page
            current_skip = len(page) - (len(items) - limit)
            return _listed(items[:limit], _make_token(*_public(ctx, current), current_skip))
        if len(items) == limit or following is None:
            break
        if following == current:  # the server names the page it just sent: no progress
            following = None
            break
        skip = 0
        current = following
        kind, value = following
        if kind == "url" or pages is None:
            url, call.query = value, {}
        else:
            offset = value if isinstance(value, int) else offset
            _paging_target(pages, call)[pages.param] = value
    return _listed(items, _make_token(*_public(ctx, following)) if following else None)


def _paging_target(pages: Pagination | None, call: _Call) -> dict[str, Any]:
    """Where the paging parameters go: the JSON body when the pagination says so, else the query."""

    if pages is not None and pages.location == "body" and call.body is not None:
        return cast(dict[str, Any], call.body)
    return call.query


def _max_pages() -> int:
    """Pages one list call reads (`AWA_MAX_PAGES_PER_CALL`, default 20)."""

    from awa.runtime.termination import ceilings

    return ceilings().max_pages_per_call


def _public(ctx: ConnectorContext, position: tuple[str, Any]) -> tuple[str, Any]:
    kind, value = position
    return (kind, _unkeyed(ctx, value)) if kind == "url" else (kind, value)


def _listed(items: list[Any], token: str | None) -> dict[str, Any]:
    listed: dict[str, Any] = {"items": items, "count": len(items), "has_more": token is not None}
    if token is not None:
        listed["next_page"] = token
    return listed


def _next_page(
    ctx: ConnectorContext,
    pages: Pagination | None,
    data: Any,
    response: HttpResponse,
    page: Sequence[Any],
    size: int,
    offset: int,
) -> tuple[Literal["url", "param"], Any] | None:
    if pages is None:
        return None
    if pages.more is not None and not pick(data, pages.more):
        return None
    # A page may be empty and still have a next link (Microsoft Graph does this for filtered
    # and searched lists); only a counting scheme (offset) has nothing to go on without items.
    if pages.kind == "cursor":
        cursor = pick(data, pages.next)
        return ("param", cursor) if cursor not in (None, "") else None
    if pages.kind == "offset":
        return ("param", offset + len(page)) if page and len(page) >= size else None
    if pages.kind == "link_header":
        link = _link_header_next(response)
        return ("url", link) if link else None
    target = pick(data, pages.next)
    if not isinstance(target, str) or not target:
        return None
    if pages.relative_to == "base" and target.startswith("/"):
        return "url", ctx.base_url() + target
    return "url", urllib.parse.urljoin(response.url or ctx.base_url() + "/", target)


def _annotation(param: Param) -> Any:
    if param.enum:
        return Literal.__getitem__(param.enum)
    if param.type == "array" and param.items is not None:
        return list.__class_getitem__(_TYPES[param.items])
    return _TYPES[param.type]


def _arguments_model(name: str, operation: Operation) -> type[ToolArgs]:
    """The pydantic argument schema of an operation's tool."""

    fields: dict[str, Any] = {}
    for param in operation.params:
        annotation = _annotation(param)
        extra: dict[str, Any] = {"description": param.description}
        if param.pattern is not None and param.type == "string" and not param.enum:
            extra["pattern"] = param.pattern
        if param.required:
            fields[param.name] = (annotation, Field(..., **extra))
        else:
            fields[param.name] = (annotation | None, Field(default=param.default, **extra))
    if operation.items is not None or operation.continuation:
        fields["page_token"] = (
            str | None,
            Field(
                default=None,
                description="The next_page of an earlier answer, to continue there (keep the "
                "other arguments).",
            ),
        )
    if operation.items is not None:
        fields["limit"] = (
            int,
            Field(
                default=min(operation.default_limit, operation.max_limit),
                ge=1,
                le=operation.max_limit,
                description="Most items returned.",
            ),
        )
    title = "".join(part.title() for part in f"{name}_{operation.name}".split("_")) + "Args"
    model: type[ToolArgs] = create_model(title, __base__=ToolArgs, **fields)
    return model


def tool_name(instance: str, operation: str) -> str:
    """The tool name of `connector:<instance>.<operation>` (at most 64 characters)."""

    name = f"{instance}_{operation}"
    if len(name) <= _MAX_TOOL_NAME:
        return name
    digest = hashlib.sha1(name.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"{name[: _MAX_TOOL_NAME - 9]}_{digest}"


def build_connector_tool(
    ctx: ConnectorContext,
    operation: Operation,
    run: Callable[[ConnectorContext, Operation, Mapping[str, Any]], Any] | None = None,
    on_error: Callable[[ToolException], Exception] | None = None,
) -> BaseTool:
    """The tool that runs `operation` of the connector instance `ctx`.

    `run` does the work (default: `call_operation`); `on_error` turns a `ToolException` into the
    error the model sees (default: its redacted message). Results and other errors are redacted.
    """

    runner = run or call_operation
    gated = ctx.requires_approval(operation)

    def call(args: ToolArgs) -> str:
        try:
            result = runner(ctx, operation, args.model_dump())
        except ToolException as exc:
            raise (on_error(exc) if on_error else ToolException(ctx.redact(str(exc)))) from None
        except Exception as exc:
            raise ToolException(ctx.redact(f"{type(exc).__name__}: {exc}")) from None
        return ctx.redact(result if isinstance(result, str) else tool_json(result))

    description = operation.description
    if operation.items is not None:
        description += (
            " Returns up to `limit` items; when more exist, pass next_page as page_token."
        )
    elif operation.continuation:
        description += (
            " When the answer has further pages it comes as {data, next_page}; pass "
            "next_page as page_token for the next one."
        )
    if gated:
        description += " Needs human approval."
    return build_tool(
        call,
        name=tool_name(ctx.name, operation.name),
        description=description,
        args_schema=_arguments_model(ctx.name, operation),
        toolkit=f"connector:{ctx.name}",
        risk=WRITE_RISK if operation.write else READ_RISK,
        frame=True,
        max_output_chars=int(ctx.option("max_output_chars", DEFAULT_MAX_OUTPUT_CHARS)),
        requires_approval=gated,
        source=f"connector:{ctx.name}",
        approval_payload=review_payload(ctx, operation.html_body),
        metadata={
            CONNECTOR_METADATA_KEY: f"{ctx.name}.{operation.name}",
            IDENTITY_METADATA_KEY: ctx.identity(),
        },
    )


def http_tools(ctx: ConnectorContext) -> list[BaseTool]:
    """The tools of every operation the instance offers."""

    return [build_connector_tool(ctx, operation) for operation in ctx.operations()]
