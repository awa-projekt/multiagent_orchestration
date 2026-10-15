"""The FastAPI application serving agent systems (`create_app`), `serve` and `drain`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from typing import TYPE_CHECKING, Any, Unpack

from awa.errors import ConfigurationError, OptionalDependencyError
from awa.server._auth import API_KEY_PRINCIPAL, LOOPBACK_HOSTS, ApiKeyAuthenticator
from awa.server._config import (
    API_PREFIX,
    DEFAULT_MAX_BODY_BYTES,
    ServingKw,
    ServingOptions,
    SystemsArg,
    normalise_systems,
    system_checkpointer,
)
from awa.server._logging import json_log_config
from awa.server._server import Server
from awa.server._stores import thread_index_for
from awa.server._web import (
    DEFAULT_SECURITY_HEADERS,
    APIRouter,
    BodyLimit,
    CORSMiddleware,
    FastAPI,
    HostGuard,
    JSONResponse,
    Request,
    RequestContext,
    install_app_error_handlers,
    server_extra_message,
)

if TYPE_CHECKING:
    from awa.automation import Automation
    from awa.server._data_options import DataOptions


def create_app(
    systems: SystemsArg,
    *,
    cors_origins: Sequence[str] = (),
    title: str = "awa agent systems",
    docs: bool = False,
    allowed_hosts: Sequence[str] | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[Any]] | None = None,
    max_body_bytes: int | None = DEFAULT_MAX_BODY_BYTES,
    security_headers: bool | Mapping[str, str] = True,
    access_log: bool = False,
    automations: Sequence[Automation] | Mapping[str, Automation] | None = None,
    data: DataOptions | None = None,
    **serving: Unpack[ServingKw],
) -> FastAPI:
    """Build an HTTP API (FastAPI, under `/v1`) serving one or more agent systems.

    Every request is made by a `Principal`; threads and runs belong to the tenant (with
    `thread_scope="principal"`, the principal) that created them, everyone else gets 404. The
    endpoints are listed in the serving guide.

    Args:
        systems: One system, a list, or a mapping of served name to system; wrap one in
            `ServedSystem` for deps, limits and roles. May be empty when `data` is given.
        docs: Serve the OpenAPI document at `/openapi.json` (no interactive page).
        allowed_hosts: Host names the app answers (other `Host` headers get 421); set it for an
            app without authentication against DNS rebinding.
        lifespan: Async context manager around the server's lifetime; runs in flight are
            drained before it exits.
        max_body_bytes: Largest request body (413 before it is read); uploads are limited by
            `data` instead.
        security_headers: `DEFAULT_SECURITY_HEADERS` (`True`), none, or these headers.
        automations: Automations whose endpoints the app serves too, authenticated like the rest.
        data: Turns on the upload, profile and build endpoints (`DataOptions`).
        **serving: The serving options below, shared with `create_router`.

    Other Parameters:
        checkpointer (BaseCheckpointSaver | None): Where the server reads thread state for
            systems other than `Agent` and `Team`; never attached to a system, so build those
            with the same checkpointer.
        api_key (str | None): Shorthand for one bearer key (the principal `"api-key"`).
        authenticator (Authenticator | None): Identifies callers; without it (and `api_key`)
            every caller is `"anonymous"`.
        sse_ping_interval (float | None): Seconds between keep-alive comments on idle streams
            (15); `None` disables.
        thread_scope (str): `"tenant"` (default), `"principal"` (the creator, plus approvers for
            approvals) or `"shared"` (no ownership checks).
        thread_index (BaseThreadIndex | None): Where thread ownership is recorded. With an
            `authenticator` and a durable checkpointer the in-memory default is refused: pass
            `PostgresThreadIndex`, or `InMemoryThreadIndex()` explicitly.
        rate_limit (RateLimit | BaseRateLimiter | None): Runs and resumes per principal across
            all systems; 429 beyond it.
        idempotency_cache (BaseIdempotencyCache | None): Where `Idempotency-Key` answers are kept.
        idempotency_ttl (float): Seconds an `Idempotency-Key` answer is replayed (a day).
        drain_timeout (float | None): Seconds to wait at shutdown for runs in flight (30; see
            `drain`).
        run_retention (float): Seconds a finished run stays readable (an hour).
        sse_queue_size (int): Events buffered per stream (1000).
        sse_overflow (str): When a slow client's buffer is full: `"drop"` (default) discards
            text deltas and disconnects only when other events cannot be buffered,
            `"disconnect"` ends at once; either way the stream ends with an `error` event and
            the run is cancelled.
        readiness_checks (Sequence[ReadinessCheck]): Extra async checks for `GET /v1/ready`;
            `False` or an error means not ready.
        readiness_timeout (float): Seconds each readiness check may take (5).
        audit_log (AuditSubscriber | None): Receives the events of every served run with the
            caller as actor.
        subject_index (SubjectIndex | None): Links every thread to its creator, so
            `erase_subject` finds them.
        subscribers (Sequence[EventSubscriber]): Event callbacks subscribed globally while the
            app runs (every run of the process, without request identity).

    Raises:
        ConfigurationError: If an `authenticator` is combined with a durable checkpointer and no
            `thread_index`.

    Example:
        ```python
        from awa.server import ApiKeyAuthenticator, Principal, ServedSystem, create_app

        auth = ApiKeyAuthenticator({os.environ["ACME_KEY"]: Principal("acme-app", tenant="acme")})
        app = create_app(
            {"support": ServedSystem(support_team, principal_deps=lambda p: {"user_id": p.id})},
            authenticator=auth,
        )
        # uvicorn my_module:app --host 0.0.0.0 --port 8000
        ```
    """
    from awa.models.replay import refuse_replay

    refuse_replay("The HTTP server")

    if max_body_bytes is not None and max_body_bytes <= 0:
        raise ValueError("max_body_bytes must be positive or None")
    from awa._internal.version import __version__
    from awa.server._router import build_router

    options = ServingOptions(**serving)
    server = build_server(systems, options, allow_empty=data is not None)

    data_api: Any = None

    @asynccontextmanager
    async def draining(app: FastAPI) -> AsyncIterator[Any]:
        async with lifespan(app) if lifespan is not None else nullcontext() as state:
            with server.subscribed():
                try:
                    yield state
                finally:
                    if data_api is not None:
                        await data_api.aclose()
                    await server.drain(server.drain_timeout)

    app = FastAPI(
        title=title,
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=draining,
    )
    app.state.awa_server = server
    _install_middleware(
        app, max_body_bytes, cors_origins, security_headers, access_log, allowed_hosts, data
    )
    if docs:

        @app.get("/openapi.json", include_in_schema=False)
        async def openapi(request: Request) -> JSONResponse:
            await server.authenticate(request)
            return JSONResponse(app.openapi())

    install_app_error_handlers(app)
    # The app drains in its own lifespan (inside the caller's), so the router gets none.
    app.include_router(build_router(server, prefix=API_PREFIX, drain_on_shutdown=False))
    if data is not None:
        from awa.server._data_api import mount_data_api

        data_api = mount_data_api(app, server, data, API_PREFIX)
    if automations is not None:
        from awa.server._automation import create_automation_router

        # Whoever may answer the approvals of a system may also answer those of automated runs.
        reviewers = {
            role
            for served in server.systems.values()
            if served.approver_roles is not None
            for role in served.approver_roles
        }
        app.include_router(create_automation_router(automations, approver_roles=sorted(reviewers)))
    return app


def _install_middleware(
    app: FastAPI,
    max_body_bytes: int | None,
    cors_origins: Sequence[str],
    security_headers: bool | Mapping[str, str],
    access_log: bool,
    allowed_hosts: Sequence[str] | None,
    data: DataOptions | None = None,
) -> None:
    if allowed_hosts is not None:
        app.add_middleware(HostGuard, hosts=allowed_hosts)
    if max_body_bytes is not None:
        # The upload endpoints take files: they get the limit of `data`, everything else `max_body_bytes`.
        wide = (
            {}
            if data is None
            else {
                f"{API_PREFIX}/uploads": data.max_request_bytes + 1024 * 1024,
                f"{API_PREFIX}/data/profile": data.max_request_bytes + 1024 * 1024,
            }
        )
        app.add_middleware(BodyLimit, limit=max_body_bytes, paths=wide)
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Retry-After", "Location", "Idempotent-Replayed"],
            allow_credentials=False,
        )
    if security_headers is True:
        headers: Mapping[str, str] = DEFAULT_SECURITY_HEADERS
    elif security_headers is False:
        headers = {}
    else:
        headers = security_headers
    # Added last, so it is the outermost middleware and covers every response.
    app.add_middleware(RequestContext, security_headers=headers, access_log=access_log)


def build_server(
    systems: SystemsArg,
    options: ServingOptions,
    *,
    allow_empty: bool = False,
    auth_dependency: bool = False,
) -> Server:
    """The serving state of `create_app` and `create_router`."""

    # One api_key, or no authentication at all: every caller is the same principal.
    single_caller = options.api_key is not None or (
        options.authenticator is None and not auth_dependency
    )
    authenticator = (
        ApiKeyAuthenticator({options.api_key: API_KEY_PRINCIPAL})
        if options.api_key is not None
        else options.authenticator
    )
    served = normalise_systems(systems, allow_empty=allow_empty)
    thread_index = thread_index_for(
        [options.checkpointer, *(system_checkpointer(entry.system) for entry in served.values())],
        options.thread_index,
        several_callers=not single_caller,
        where="The server",
    )
    return Server(
        served,
        options,
        authenticator=authenticator,
        thread_index=thread_index,
        single_caller=single_caller,
    )


async def drain(app: FastAPI | APIRouter, *, timeout: float | None = None) -> int:
    """Stop an app from accepting runs and wait for the runs in flight.

    New runs get 503 and `GET /v1/ready` reports `draining`. Runs in flight get up to `timeout`
    seconds (default: the app's `drain_timeout`); the rest are cancelled. Returns how many were
    cancelled. `serve()`, `awa serve` and the app's lifespan drain on shutdown.
    """

    server = server_of(app)
    return await server.drain(server.drain_timeout if timeout is None else timeout)


def server_of(target: Any) -> Server:
    """The serving state behind an app of `create_app` or a router of `create_router`."""

    server = getattr(target, "awa_server", None)
    if server is None:
        server = getattr(getattr(target, "state", None), "awa_server", None)
    if not isinstance(server, Server):
        raise TypeError("Expected an app built by create_app or a router built by create_router")
    return server


class _AppKw(ServingKw, total=False):
    """The keywords of `create_app` that `serve` passes on."""

    cors_origins: Sequence[str]
    title: str
    docs: bool
    allowed_hosts: Sequence[str] | None
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[Any]] | None
    max_body_bytes: int | None
    security_headers: bool | Mapping[str, str]
    access_log: bool
    automations: Sequence[Automation] | Mapping[str, Automation] | None
    data: DataOptions | None


def serve(
    systems: SystemsArg,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: str = "info",
    json_logs: bool = False,
    allow_unauthenticated: bool = False,
    **options: Unpack[_AppKw],
) -> None:
    """Serve systems over HTTP with uvicorn until interrupted; see `create_app`.

    A single process (local connections only by default) that drains on SIGTERM or Ctrl+C. For
    several processes run an ASGI server on a module calling `create_app`, with a shared
    checkpointer.

    Args:
        json_logs: Log JSON lines with request ids (`json_log_config`).
        allow_unauthenticated: Serve on a non-loopback `host` without `api_key` or
            `authenticator`.
        **options: The options of `create_app`.

    Raises:
        ConfigurationError: If `host` is not a loopback address, neither `api_key` nor
            `authenticator` is set and `allow_unauthenticated` is false.

    Example:
        ```python
        from awa.server import serve

        serve(support_team, port=8080, api_key=os.environ["AWA_API_KEY"])
        ```
    """

    unauthenticated = options.get("api_key") is None and options.get("authenticator") is None
    if host not in LOOPBACK_HOSTS and unauthenticated and not allow_unauthenticated:
        raise ConfigurationError(
            f"Serving on {host} without authentication would let anyone who can reach the port "
            "run the systems; pass api_key or an authenticator, or allow_unauthenticated=True"
        )
    uvicorn = import_uvicorn()
    if json_logs:
        options.setdefault("access_log", True)
    app = create_app(systems, **options)
    run_uvicorn(uvicorn, app, host=host, port=port, log_level=log_level, json_logs=json_logs)


def run_uvicorn(
    uvicorn: Any,
    app: FastAPI,
    *,
    host: str,
    port: int,
    log_level: str,
    json_logs: bool = False,
    selector_loop: bool = False,
) -> None:
    """Run `app` with a uvicorn server that drains the app before shutting down.

    `selector_loop` serves on a `SelectorEventLoop` on Windows (psycopg's async driver needs one).
    """

    config_options: dict[str, Any] = {"host": host, "port": port, "log_level": log_level}
    if json_logs:
        config_options["log_config"] = json_log_config(log_level)
    server: Server = app.state.awa_server
    if server.drain_timeout is not None:
        # Connections still open after the drain are closed after this long.
        config_options["timeout_graceful_shutdown"] = int(server.drain_timeout) + 5

    class DrainingServer(uvicorn.Server):  # type: ignore[misc]  # uvicorn is imported lazily
        async def shutdown(self, sockets: Any = None) -> None:
            await drain(app)
            await super().shutdown(sockets=sockets)

    served = DrainingServer(uvicorn.Config(app, **config_options))
    if selector_loop:
        from awa._internal import aio as asyncio_compat

        asyncio_compat.run(served.serve(), selector_loop=True)
    else:
        served.run()


def import_uvicorn() -> Any:
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise OptionalDependencyError(server_extra_message()) from exc
    return uvicorn
