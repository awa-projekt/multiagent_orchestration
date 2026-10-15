"""LangGraph `BaseStore` backends selected from `Settings`, for what must outlive a single thread.

Items written with a `ttl` expire: the PostgreSQL store deletes them when it opens and every
`STORE_SWEEP_INTERVAL_MINUTES`; with the in-memory store, `AgentMemory` drops expired memories.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from awa._internal.upstream.checkpoint import BaseStore, InMemoryStore, postgres_store_class
from awa.core.settings import Settings
from awa.errors import ConfigurationError
from awa.persistence.checkpointer import _open_postgres

STORE_SWEEP_INTERVAL_MINUTES = 60
"""How often an open PostgreSQL store deletes its expired items."""

_STORE_TTL: dict[str, Any] = {
    # A memory keeps the lifetime it was written with; reading it does not extend it.
    "refresh_on_read": False,
    "sweep_interval_minutes": STORE_SWEEP_INTERVAL_MINUTES,
}


@asynccontextmanager
async def open_store(settings: Settings) -> AsyncIterator[BaseStore]:
    """Open the configured store (`store_backend`) for the duration of a block.

    `postgres` creates its tables if needed and sweeps expired items while open; on Windows, run the
    caller with `awa.ext.run_sync(..., selector_loop=True)`.

    Raises:
        MissingConfigurationError: If `postgres` is selected but `postgres_dsn` is unset.
        OptionalDependencyError: If `postgres` is selected but its extra is not installed.
        ConfigurationError: For an unknown backend.
    """

    if settings.store_backend == "memory":
        yield InMemoryStore()
        return
    if settings.store_backend != "postgres":
        raise ConfigurationError(
            f"Unknown store backend {settings.store_backend!r}; set AWA_STORE_BACKEND to "
            "'memory' or 'postgres'"
        )
    async with _open_postgres(
        settings, "store", postgres_store_class, ttl=dict(_STORE_TTL)
    ) as store:
        await store.sweep_ttl()
        await store.start_ttl_sweeper()
        try:
            yield store
        finally:
            await store.stop_ttl_sweeper(timeout=10)


__all__ = ["STORE_SWEEP_INTERVAL_MINUTES", "open_store"]
