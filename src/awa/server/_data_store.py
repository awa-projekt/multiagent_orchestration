"""A project's data store as `awa serve`, `awa worker`, `awa runs` and `awa store` open it,
one SQLite file `.awa/awa.db` or a PostgreSQL schema."""

from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awa._internal.install import install_hint
from awa.core.settings import PSEUDONYM_KEY_VARIABLE
from awa.errors import DataStoreError, OptionalDependencyError
from awa.persistence.sql import describe_dsn

if TYPE_CHECKING:
    from awa.automation.store import RunStore
    from awa.core.settings import Settings
    from awa.persistence.runlog import PseudonymKey, RunLog, SqliteDatabase
    from awa.persistence.threads import BaseThreadIndex


def pseudonym_key(settings: Settings | None = None) -> PseudonymKey | None:
    """The key that links people to their runs (`AWA_PSEUDONYM_KEY`), or `None` when unset."""

    from awa.core.settings import get_settings
    from awa.persistence.runlog import PseudonymKey

    settings = settings or get_settings()
    if settings.pseudonym_key is None:
        return None
    return PseudonymKey(
        settings.pseudonym_key.get_secret_value().encode("utf-8"), settings.pseudonym_key_id
    )


def ensure_pseudonym_key(folder: Path, settings: Settings | None = None) -> PseudonymKey:
    """The pseudonym key of a local project: `AWA_PSEUDONYM_KEY` from the settings, else from
    `folder/.env`, else a new random one written into `folder/.env` (never printed)."""

    from awa.core.dotenv import read_env_file, write_env_file
    from awa.core.settings import get_settings
    from awa.persistence.runlog import PseudonymKey

    settings = settings or get_settings()
    known = pseudonym_key(settings)
    if known is not None:
        return known
    env = folder / ".env"
    value = (read_env_file(env).get(PSEUDONYM_KEY_VARIABLE) or "").strip()
    if not value:
        value = secrets.token_urlsafe(32)
        write_env_file(env, {PSEUDONYM_KEY_VARIABLE: value})
        get_settings.cache_clear()
    return PseudonymKey(value.encode("utf-8"), settings.pseudonym_key_id)


def uses_postgres(dsn: str | None, settings: Settings | None = None) -> bool:
    """Whether the data lives in PostgreSQL: a DSN is given and `AWA_DATA_BACKEND` is not
    `sqlite`."""

    from awa.core.settings import get_settings

    backend = (settings or get_settings()).data_backend
    return bool(dsn) and backend != "sqlite"


@dataclass
class LocalData:
    """The local data of a project: the data file and the stores on it."""

    database: SqliteDatabase
    runs: RunStore
    log: RunLog
    threads: BaseThreadIndex

    async def close(self) -> None:
        await self.runs.close()
        await asyncio.to_thread(self.database.close)


def open_local_data(
    data_file: str | os.PathLike[str], *, pseudonym: PseudonymKey | None = None
) -> LocalData:
    """Open the local stores in the SQLite file `data_file`.

    Without `pseudonym` (the HMAC key linking people to runs) erasure per person is refused.

    Raises:
        DataStoreError: If the data file cannot be opened (network drive, old SQLite, ...).
        SchemaVersionError: If the file was written by a newer awa with a breaking change.
    """

    from awa.automation import SqliteRunStore
    from awa.persistence.runlog import SqliteDatabase, SqliteRunLog
    from awa.persistence.threads import SqliteThreadIndex

    database = SqliteDatabase(data_file)
    return LocalData(
        database=database,
        runs=SqliteRunStore(database),
        log=SqliteRunLog(database, pseudonym=pseudonym),
        threads=SqliteThreadIndex(database),
    )


class PostgresData:
    """The PostgreSQL data of `awa serve --dsn` and `awa worker --dsn`, on one shared pool.

    The stores and checkpointer exist from the start; `open()` opens a fresh pool on the serving
    event loop and moves every store onto it. `dsn` may go through PgBouncer; migrations use
    `AWA_POSTGRES_ADMIN_DSN` when it is set.
    """

    def __init__(self, dsn: str, schema: str, *, settings: Settings | None = None) -> None:
        from awa.automation import PostgresRunStore
        from awa.core.settings import get_settings
        from awa.persistence.runlog import PostgresRunLog
        from awa.persistence.threads import PostgresIdempotencyCache, PostgresThreadIndex

        self.settings = settings or get_settings()
        admin = self.settings.postgres_admin_dsn
        self.dsn = dsn
        self.admin_dsn = admin.get_secret_value() if admin is not None else dsn
        self.schema = schema
        self.pool = self._new_pool()
        self.runs = PostgresRunStore(self.pool, schema=schema)
        self.log = PostgresRunLog(self.pool, schema=schema, pseudonym=pseudonym_key(self.settings))
        self.threads = PostgresThreadIndex(self.pool, schema=schema)
        self.idempotency = PostgresIdempotencyCache(self.pool, schema=schema)
        self._opened = False
        self.checkpointer = _saver_on(self.pool)

    def _new_pool(self) -> Any:
        from awa.persistence.sql import shared_pool

        try:
            return shared_pool(
                self.dsn,
                max_size=self.settings.postgres_pool_max,
                pooler=self.settings.postgres_pooler,
                application_name="awa",
            )
        except OptionalDependencyError as exc:
            raise OptionalDependencyError(_postgres_extra_message()) from exc

    async def open(self) -> None:
        """Open the pool on the running loop, apply pending migrations and set up the
        checkpointer's tables (under the setup lock, on the direct connection)."""

        from awa.persistence.sql import check_event_loop, postgres_setup_lock

        check_event_loop()
        if self._opened:
            self.pool = self._new_pool()
            stores: tuple[Any, ...] = (self.runs, self.log, self.threads, self.idempotency)
            for store in stores:
                store.use_pool(self.pool)
            self.checkpointer.conn = self.pool
        self._opened = True
        # The saver runs its synchronous methods on the loop it was created in, and its
        # lock must belong to the serving loop.
        self.checkpointer.loop = asyncio.get_running_loop()
        self.checkpointer.lock = asyncio.Lock()
        try:
            await self.pool.open(wait=True, timeout=30.0)
        except Exception as exc:
            await self.pool.close()
            raise DataStoreError(
                f"Cannot connect to the PostgreSQL database {describe_dsn(self.dsn)}: "
                f"{type(exc).__name__}. Check that it is running and that the address and "
                "credentials in AWA_POSTGRES_DSN are right."
            ) from exc
        await self.runs.setup()
        # Several servers and workers may start at once; their migrations must not overlap.
        async with postgres_setup_lock(self.admin_dsn):
            await self.checkpointer.setup()

    async def close(self) -> None:
        """Close the pool (every store and the checkpointer stop with it)."""

        await self.pool.close()


def _saver_on(pool: Any) -> Any:
    """A PostgreSQL checkpointer on `pool`, created outside the serving event loop."""

    from awa._internal import aio as asyncio_compat
    from awa._internal.upstream.checkpoint import postgres_saver_class, state_serde

    try:
        saver_class = postgres_saver_class()
    except ImportError as exc:
        raise OptionalDependencyError(_postgres_extra_message()) from exc

    async def create() -> Any:
        return saver_class(conn=pool, serde=state_serde())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # The saver insists on being created in an event loop; `open` rebinds it.
        return asyncio_compat.run(create())
    return saver_class(conn=pool, serde=state_serde())


def _postgres_extra_message() -> str:
    return "Serving with a PostgreSQL DSN requires the optional extra: " + install_hint("postgres")
