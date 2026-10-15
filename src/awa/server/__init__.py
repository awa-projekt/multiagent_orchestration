"""Serving (Interfaces layer): agent systems as an HTTP API (`create_app`, `create_router`, `serve`),
as JSON jobs on any queue (`awa.server.jobs`) and as MCP tools (`awa.server.mcp`). Callers are
authenticated `Principal`s; threads and runs belong to their tenant. Needs the `server` extra.

Preview: it works and is tested, but its API may still change in a minor release.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute
from awa.persistence.threads import (
    BaseIdempotencyCache,
    BaseThreadIndex,
    IdempotencyRecord,
    ThreadOwner,
    ThreadRecord,
)
from awa.server._auth import (
    ApiKeyAuthenticator,
    Authenticator,
    BearerTokenAuthenticator,
    Principal,
    current_principal,
)
from awa.server._blueprint_app import build_blueprint_app
from awa.server._config import ServedSystem
from awa.server._limits import BaseRateLimiter, RateLimit, TokenBucketRateLimiter
from awa.server._logging import (
    JsonLogFormatter,
    RequestContextFilter,
    current_request_id,
    json_log_config,
)
from awa.server._stores import InMemoryIdempotencyCache, InMemoryThreadIndex
from awa.server.errors import (
    AuthenticationError,
    IdempotencyConflictError,
    IdempotencyMismatchError,
    InvalidRequestError,
    NoPendingInterruptError,
    PermissionDeniedError,
    RateLimitExceededError,
    RunCancelledError,
    RunNotFoundError,
    ServiceUnavailableError,
    SystemBusyError,
    SystemNotFoundError,
    ThreadNotFoundError,
)

if TYPE_CHECKING:
    from awa.persistence.threads import (
        PostgresIdempotencyCache,
        PostgresThreadIndex,
        SqliteThreadIndex,
    )
    from awa.server._app import create_app, drain, serve
    from awa.server._automation import create_automation_router
    from awa.server._data_options import DataOptions
    from awa.server._router import create_router, router_lifespan
    from awa.server._web import DEFAULT_SECURITY_HEADERS, install_error_handler

# FastAPI (and the PostgreSQL driver) load on first use, so `import awa.server` needs no extras.
_LAZY = homes(
    {
        "awa.server._app": "create_app drain serve",
        "awa.server._web": "install_error_handler DEFAULT_SECURITY_HEADERS",
        "awa.server._automation": "create_automation_router",
        "awa.server._data_options": "DataOptions",
        "awa.persistence.threads": "PostgresIdempotencyCache PostgresThreadIndex SqliteThreadIndex",
        "awa.server._router": "create_router router_lifespan",
    }
)


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _LAZY, name)


__stability__ = "preview"

__all__ = [
    "DEFAULT_SECURITY_HEADERS",
    "ApiKeyAuthenticator",
    "AuthenticationError",
    "Authenticator",
    "BaseIdempotencyCache",
    "BaseRateLimiter",
    "BaseThreadIndex",
    "BearerTokenAuthenticator",
    "DataOptions",
    "IdempotencyConflictError",
    "IdempotencyMismatchError",
    "IdempotencyRecord",
    "InMemoryIdempotencyCache",
    "InMemoryThreadIndex",
    "InvalidRequestError",
    "JsonLogFormatter",
    "NoPendingInterruptError",
    "PermissionDeniedError",
    "PostgresIdempotencyCache",
    "PostgresThreadIndex",
    "Principal",
    "RateLimit",
    "RateLimitExceededError",
    "RequestContextFilter",
    "RunCancelledError",
    "RunNotFoundError",
    "ServedSystem",
    "ServiceUnavailableError",
    "SqliteThreadIndex",
    "SystemBusyError",
    "SystemNotFoundError",
    "ThreadNotFoundError",
    "ThreadOwner",
    "ThreadRecord",
    "TokenBucketRateLimiter",
    "build_blueprint_app",
    "create_app",
    "create_automation_router",
    "create_router",
    "current_principal",
    "current_request_id",
    "drain",
    "install_error_handler",
    "json_log_config",
    "router_lifespan",
    "serve",
]
