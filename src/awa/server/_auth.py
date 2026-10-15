"""Who is calling: `Principal`, `Authenticator` and the built-in authenticators (no web import)."""

from __future__ import annotations

import hashlib
import hmac
import inspect
from collections.abc import Awaitable, Callable, Collection, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from awa._internal.logging import get_logger
from awa.errors import AuthenticationError

if TYPE_CHECKING:
    from starlette.requests import Request

_log = get_logger("server")

_MAX_TOKEN_CHARS = 8192


@dataclass(frozen=True)
class Principal:
    """The authenticated caller of a request.

    Threads and runs belong to the principal (and tenant) that created them; rate limits count
    per principal; `ServedSystem(principal_deps=...)` turns it into run deps.

    Args:
        tenant: The caller's organization; `None` in a single-tenant deployment. Callers of
            different tenants never see each other's threads or runs.
        attributes: Further claims for your own `principal_deps`; never sent to clients.
    """

    id: str
    tenant: str | None = None
    roles: Collection[str] = field(default_factory=frozenset)
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("Principal.id must be a non-empty string")
        if self.tenant is not None and (not isinstance(self.tenant, str) or not self.tenant):
            raise ValueError("Principal.tenant must be a non-empty string or None")
        if isinstance(self.roles, str):
            raise TypeError("Principal.roles must be a collection of role names, not a string")
        object.__setattr__(self, "roles", frozenset(self.roles))
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    def has_role(self, *roles: str) -> bool:
        """Whether the principal has at least one of `roles`."""

        return any(role in self.roles for role in roles)

    def describe(self) -> dict[str, Any]:
        """The principal as JSON for audit records: id, tenant and sorted roles."""

        return {"id": self.id, "tenant": self.tenant, "roles": sorted(self.roles)}


ANONYMOUS = Principal("anonymous")
"""The principal of every request when the server has no authentication."""

API_KEY_PRINCIPAL = Principal("api-key")
"""The principal of requests authenticated with `create_app(api_key=...)`."""

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
"""Hosts only this computer reaches; serving anywhere else needs authentication or consent."""


@runtime_checkable
class Authenticator(Protocol):
    """Identifies the caller of a request.

    Implement `authenticate` for any scheme (API keys, JWT or OIDC tokens, gateway headers);
    it returns the caller's `Principal` or raises `AuthenticationError` (401).

    Example:
        ```python
        class GatewayAuthenticator:
            async def authenticate(self, request: Request) -> Principal:
                user = request.headers.get("x-authenticated-user")
                if not user:
                    raise AuthenticationError("Not authenticated by the gateway")
                return Principal(user, tenant=request.headers.get("x-tenant"))
        ```
    """

    async def authenticate(self, request: Request) -> Principal:
        """Return the caller of `request`.

        Raises:
            AuthenticationError: If the request carries no valid credentials.
        """
        ...


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def _bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > _MAX_TOKEN_CHARS:
        return None
    return token


class ApiKeyAuthenticator:
    """Authenticates requests by API key, mapping each key to its principal.

    Keys are kept only as SHA-256 digests and compared in constant time.

    Args:
        keys: API key to principal (or to a principal id, for one without tenant or roles).
        header: Header carrying the raw key instead of `Authorization: Bearer <key>`.
    """

    def __init__(self, keys: Mapping[str, Principal | str], *, header: str | None = None) -> None:
        entries: list[tuple[bytes, Principal | str]] = []
        for key, principal in keys.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("API keys must be non-empty strings")
            entries.append((_digest(key.strip()), principal))
        self._init(entries, header)

    @classmethod
    def from_digests(
        cls, digests: Mapping[str, Principal | str], *, header: str | None = None
    ) -> ApiKeyAuthenticator:
        """Build the authenticator from hex SHA-256 digests, so configuration never holds keys."""

        entries: list[tuple[bytes, Principal | str]] = []
        for digest, principal in digests.items():
            try:
                raw = bytes.fromhex(digest)
            except ValueError:
                raw = b""
            if len(raw) != hashlib.sha256().digest_size:
                raise ValueError("API key digests must be 64 hex characters (SHA-256)")
            entries.append((raw, principal))
        instance = cls.__new__(cls)
        instance._init(entries, header)
        return instance

    def _init(self, entries: list[tuple[bytes, Principal | str]], header: str | None) -> None:
        if not entries:
            raise ValueError("ApiKeyAuthenticator needs at least one key")
        if header is not None and not header.strip():
            raise ValueError("header must be a header name or None")
        self._keys = [
            (digest, principal if isinstance(principal, Principal) else Principal(principal))
            for digest, principal in entries
        ]
        self._header = header.lower() if header is not None else None

    @property
    def principals(self) -> tuple[Principal, ...]:
        """The principals the keys map to."""

        return tuple(principal for _, principal in self._keys)

    async def authenticate(self, request: Request) -> Principal:
        """Return the principal of the supplied key.

        Raises:
            AuthenticationError: If the key is missing or unknown.
        """

        if self._header is None:
            token = _bearer_token(request)
        else:
            token = request.headers.get(self._header, "").strip() or None
        if token is None or len(token) > _MAX_TOKEN_CHARS:
            raise AuthenticationError("Missing or invalid API key")
        supplied = _digest(token)
        found: Principal | None = None
        for digest, principal in self._keys:
            # No early exit: the time taken does not reveal which key matched.
            if hmac.compare_digest(supplied, digest):
                found = principal
        if found is None:
            raise AuthenticationError("Missing or invalid API key")
        return found

    def __repr__(self) -> str:
        return f"ApiKeyAuthenticator(keys={len(self._keys)})"


TokenVerifier = Callable[[str], "Principal | None | Awaitable[Principal | None]"]
"""Turns a bearer token into its `Principal` (`None` or an exception: rejected)."""


class BearerTokenAuthenticator:
    """Authenticates `Authorization: Bearer <token>` with your own verifier.

    `verify` (sync or async) validates the raw token and returns the caller's `Principal`;
    returning `None` or raising rejects the request with 401 (the exception is logged, never
    sent).

    Example:
        ```python
        import jwt  # PyJWT, your choice of library

        def verify(token: str) -> Principal:
            claims = jwt.decode(token, key=PUBLIC_KEY, algorithms=["RS256"], audience="awa")
            return Principal(claims["sub"], tenant=claims["org"], roles=claims.get("roles", []))

        app = create_app(systems, authenticator=BearerTokenAuthenticator(verify))
        ```
    """

    def __init__(self, verify: TokenVerifier) -> None:
        if not callable(verify):
            raise TypeError("verify must be callable")
        self._verify = verify

    async def authenticate(self, request: Request) -> Principal:
        """Return the principal the verifier derives from the bearer token.

        Raises:
            AuthenticationError: If the token is missing or rejected.
        """

        token = _bearer_token(request)
        if token is None:
            raise AuthenticationError("Missing bearer token")
        try:
            principal = self._verify(token)
            if inspect.isawaitable(principal):
                principal = await principal
        except AuthenticationError:
            raise
        except Exception as exc:  # noqa: BLE001 - any verifier failure is a rejection
            _log.info("Bearer token rejected: %s", type(exc).__name__)
            raise AuthenticationError("Invalid bearer token") from None
        if not isinstance(principal, Principal):
            raise AuthenticationError("Invalid bearer token")
        return principal


principal_context: ContextVar[Principal | None] = ContextVar("awa_server_principal", default=None)


def current_principal() -> Principal | None:
    """The principal of the request being served, or `None` outside a request.

    Available in tools, guardrails and deps factories of served runs.
    """

    return principal_context.get()
