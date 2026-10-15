"""Checkpointers selected from `Settings`: in memory, in a local SQLite file, or in PostgreSQL.

Attach the checkpointer to the outermost graph only; subgraphs and nested agents share it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from awa._internal.install import install_hint
from awa._internal.upstream.checkpoint import (
    BaseCheckpointSaver,
    StateInMemorySaver,
    postgres_saver_class,
    state_serde,
)
from awa.core.settings import Settings
from awa.errors import ConfigurationError, MissingConfigurationError, OptionalDependencyError
from awa.persistence.sql import postgres_setup_lock
from awa.persistence.sqlite import SqliteCheckpointer

DEFAULT_CHECKPOINTER_PATH = ".awa/checkpoints.db"
"""File of the `sqlite` checkpointer when `checkpointer_path` is unset."""


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    """Open the configured checkpointer (`checkpointer_backend`) for the duration of a block.

    `postgres` creates its tables if needed, safe when several processes start at once; on Windows,
    run the caller with `awa.ext.run_sync(..., selector_loop=True)`.

    Raises:
        MissingConfigurationError: If `postgres` is selected but `postgres_dsn` is unset.
        OptionalDependencyError: If `postgres` is selected but its extra is not installed.
        ConfigurationError: For an unknown backend.

    Example:
        ```python
        async with open_checkpointer(settings) as checkpointer:
            agent = Agent("assistant", checkpointer=checkpointer)
            await agent.run("Hello", thread_id="t-1")
        ```
    """

    if settings.checkpointer_backend == "memory":
        yield StateInMemorySaver()
        return
    if settings.checkpointer_backend == "sqlite":
        saver = SqliteCheckpointer(settings.checkpointer_path or DEFAULT_CHECKPOINTER_PATH)
        try:
            yield saver
        finally:
            saver.close()
        return
    if settings.checkpointer_backend != "postgres":
        raise ConfigurationError(
            f"Unknown checkpointer backend {settings.checkpointer_backend!r}; set "
            "AWA_CHECKPOINTER_BACKEND to 'memory', 'sqlite' or 'postgres'"
        )
    async with _open_postgres(
        settings, "checkpointer", postgres_saver_class, serde=state_serde()
    ) as checkpointer:
        yield checkpointer


@asynccontextmanager
async def _open_postgres(
    settings: Settings, what: str, load_class: Callable[[], Any], **options: Any
) -> AsyncIterator[Any]:
    """Open LangGraph's Postgres checkpointer or store on `postgres_dsn` and migrate it."""

    if not settings.postgres_dsn:
        raise MissingConfigurationError(
            f"AWA_POSTGRES_DSN must be set for the postgres {what} backend"
        )
    try:
        backend_class = load_class()
    except ImportError as exc:
        raise OptionalDependencyError(
            f"The postgres {what} requires the optional extra: " + install_hint("postgres")
        ) from exc

    dsn = settings.postgres_dsn.get_secret_value()
    async with backend_class.from_conn_string(dsn, **options) as backend:
        # Several processes may start at once; their migrations must not overlap.
        async with postgres_setup_lock(dsn):
            await backend.setup()
        yield backend


def checkpoint_time(stamp: str) -> datetime:
    """The time in a checkpoint's `ts` field, as an aware UTC datetime."""

    moment = datetime.fromisoformat(stamp)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def postgres_thread_activity(
    checkpointer: BaseCheckpointSaver[Any],
) -> dict[str, datetime] | None:
    """The latest checkpoint time of every thread in one query; `None` for a non-PostgreSQL saver."""

    try:
        postgres_saver = postgres_saver_class()
    except ImportError:
        return None
    if not isinstance(checkpointer, postgres_saver):
        return None
    async with checkpointer._cursor() as cursor:
        await cursor.execute(
            "SELECT thread_id, max(checkpoint->>'ts') AS ts FROM checkpoints GROUP BY thread_id"
        )
        rows = await cursor.fetchall()
    return {str(row["thread_id"]): checkpoint_time(str(row["ts"])) for row in rows if row["ts"]}


__all__ = [
    "DEFAULT_CHECKPOINTER_PATH",
    "SqliteCheckpointer",
    "StateInMemorySaver",
    "checkpoint_time",
    "open_checkpointer",
    "postgres_thread_activity",
]
