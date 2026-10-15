"""SQL backend kit: connections, transactions and schema setup for PostgreSQL and SQLite.

Concurrent `CREATE ... IF NOT EXISTS` from several processes fails with unique violations, so
plain DDL runs in one transaction under `pg_advisory_xact_lock`; migrations that cannot run in a
transaction (`CREATE INDEX CONCURRENTLY`) poll a session-level lock instead and give up after
`POSTGRES_SETUP_LOCK_TIMEOUT` seconds with `PostgresSetupTimeoutError`.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import re
import sqlite3
import sys
import threading
import time
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar

from awa._internal.aio import LoopLocal
from awa._internal.install import install_hint
from awa._internal.retry import backoff_delay
from awa.errors import (
    ConfigurationError,
    DataStoreError,
    OptionalDependencyError,
    PostgresSetupTimeoutError,
)
from awa.persistence.ledger import (
    MigrationStatus,
    PostgresLedger,
    SqliteLedger,
    load_migrations,
    require_current,
)
from awa.runtime.clock import Clock, as_clock

if TYPE_CHECKING:
    from typing import Self

POSTGRES_SETUP_LOCK_KEY = 0x6177615F73657475  # "awa_setu"
"""Advisory lock key held while a PostgreSQL backend creates or migrates its tables."""

POSTGRES_SETUP_LOCK_TIMEOUT = 120.0
"""Seconds a setup waits for the lock before raising `PostgresSetupTimeoutError`."""

MIN_SQLITE_VERSION = (3, 38, 0)
"""The oldest SQLite library accepted (JSON functions built in, `RETURNING`)."""

_SCHEMA_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,39}")
_MAX_NAME_CHARS = 200
_FIRST_RETRY_SECONDS = 0.02
_MAX_RETRY_SECONDS = 0.5
_BACKUPS_KEPT = 3
_NETWORK_FILESYSTEMS = frozenset(
    {"nfs", "nfs4", "cifs", "smbfs", "smb3", "sshfs", "fuse.sshfs", "9p", "afs"}
)

T = TypeVar("T")


def import_psycopg() -> tuple[Any, Any]:
    """Return the `psycopg` and `psycopg_pool` modules.

    Raises:
        OptionalDependencyError: If the `postgres` extra is not installed.
    """

    try:
        import psycopg
        import psycopg_pool
    except ImportError as exc:
        raise OptionalDependencyError(
            f"The PostgreSQL backends require the optional extra: {install_hint('postgres')}"
        ) from exc
    return psycopg, psycopg_pool


def connect_once(dsn: str, timeout: float) -> None:
    """Open one connection to `dsn` within `timeout` seconds and run `SELECT 1`.

    Raises:
        OptionalDependencyError: If the `postgres` extra is not installed.
    """

    psycopg, _ = import_psycopg()
    with psycopg.connect(dsn, connect_timeout=max(1, int(timeout))) as connection:
        connection.execute("SELECT 1")


def describe_dsn(dsn: str) -> str:
    """`host:port/database` of a DSN, without user or password (safe to print)."""

    try:
        from psycopg.conninfo import conninfo_to_dict

        parts = conninfo_to_dict(dsn)
    except Exception:  # noqa: BLE001 - never echo a DSN that does not parse
        return "a PostgreSQL database"
    return f"{parts.get('host', 'localhost')}:{parts.get('port', 5432)}/{parts.get('dbname', '')}"


def check_event_loop() -> None:
    """Refuse Windows' `ProactorEventLoop`, which psycopg's async driver cannot use.

    Raises:
        ConfigurationError: On Windows, on a `ProactorEventLoop`.
    """

    proactor = getattr(asyncio, "ProactorEventLoop", None)
    if sys.platform != "win32" or proactor is None:
        return
    if isinstance(asyncio.get_running_loop(), proactor):
        raise ConfigurationError(
            "The PostgreSQL backends cannot run on Windows' default ProactorEventLoop "
            "(psycopg's async driver needs a SelectorEventLoop). Start the program with "
            "awa.ext.run_sync(main(), selector_loop=True) or "
            "asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)."
        )


def check_schema(schema: str) -> str:
    """Validate a schema name for DDL: lowercase letters, digits and `_`, at most 40 characters.

    Raises:
        ConfigurationError: If `schema` is not such a name.
    """

    if not isinstance(schema, str) or not _SCHEMA_PATTERN.fullmatch(schema):
        raise ConfigurationError(
            f"schema must be a lowercase SQL identifier of at most 40 characters "
            f"(letters, digits, '_'), got {schema!r}"
        )
    return schema


def check_name(name: str, what: str) -> str:
    """Validate a namespace name (board, blackboard, bus or automation name) stored as data.

    Raises:
        ConfigurationError: If `name` is blank, longer than 200 characters or has a NUL.
    """

    if not isinstance(name, str) or not name.strip():
        raise ConfigurationError(f"{what} must not be empty")
    if len(name) > _MAX_NAME_CHARS or "\x00" in name:
        raise ConfigurationError(f"{what} must be at most {_MAX_NAME_CHARS} characters without NUL")
    return name


def check_conninfo(conninfo: Any) -> Any:
    """Validate the first argument of a PostgreSQL backend: a DSN or an `AsyncConnectionPool`.

    Raises:
        OptionalDependencyError: If the `postgres` extra is not installed.
        ConfigurationError: If the DSN is blank.
    """

    _, psycopg_pool = import_psycopg()
    if isinstance(conninfo, str):
        if not conninfo.strip():
            raise ConfigurationError("The DSN must not be empty")
    elif not isinstance(conninfo, psycopg_pool.AsyncConnectionPool):
        raise TypeError(
            "The first argument must be a PostgreSQL DSN or a psycopg_pool.AsyncConnectionPool, "
            f"got {type(conninfo).__name__}"
        )
    return conninfo


async def fetch_all(
    conn: Any, query: str, params: Sequence[Any] | Mapping[str, Any] = ()
) -> list[Any]:
    """Run `query` (positional `%s` or named `%(name)s` parameters); every row as a tuple."""

    from psycopg.rows import tuple_row

    async with conn.cursor(row_factory=tuple_row) as cursor:
        await cursor.execute(query, params)
        return list(await cursor.fetchall())


async def fetch_one(conn: Any, query: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Any:
    """Run `query` and return its first row as a tuple, or `None`."""

    from psycopg.rows import tuple_row

    async with conn.cursor(row_factory=tuple_row) as cursor:
        await cursor.execute(query, params)
        return await cursor.fetchone()


async def setup_schema(
    conn: Any, schema: str, statements: Iterable[str], *, lock_key: int | None
) -> None:
    """Create `schema` and run the idempotent DDL `statements` in one transaction.

    `lock_key` names the advisory lock that serializes concurrent setups (`None`: no lock).
    """

    async with conn.transaction():
        if lock_key is not None:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {check_schema(schema)}")
        for statement in statements:
            await conn.execute(statement)


async def acquire_setup_lock(conn: Any, *, timeout: float | None = None) -> None:
    """Take the setup lock on the autocommit connection `conn`, polling up to `timeout` seconds.

    Raises:
        PostgresSetupTimeoutError: If the lock was not free within `timeout`.
    """

    limit = POSTGRES_SETUP_LOCK_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + limit
    for attempt in itertools.count():
        row = await fetch_one(conn, "SELECT pg_try_advisory_lock(%s)", (POSTGRES_SETUP_LOCK_KEY,))
        if row is not None and row[0]:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PostgresSetupTimeoutError(
                f"Timed out after {limit:g}s waiting for the PostgreSQL setup lock "
                f"(advisory lock {POSTGRES_SETUP_LOCK_KEY}); another session is still creating "
                "or migrating awa's tables. Check pg_locks for the holder.",
                details={"timeout": limit, "lock_key": POSTGRES_SETUP_LOCK_KEY},
            )
        delay = backoff_delay(attempt, initial=_FIRST_RETRY_SECONDS, cap=_MAX_RETRY_SECONDS)
        await asyncio.sleep(min(delay, remaining))


async def release_setup_lock(conn: Any) -> None:
    """Release the setup lock taken with `acquire_setup_lock`."""

    await conn.execute("SELECT pg_advisory_unlock(%s)", (POSTGRES_SETUP_LOCK_KEY,))


@asynccontextmanager
async def postgres_setup_lock(dsn: str, *, timeout: float | None = None) -> AsyncIterator[None]:
    """Hold the setup lock (on a connection of its own) for the block.

    Raises:
        PostgresSetupTimeoutError: See `acquire_setup_lock`.
    """

    psycopg, _ = import_psycopg()
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await acquire_setup_lock(conn, timeout=timeout)
        try:
            yield
        finally:
            with suppress(Exception):
                await release_setup_lock(conn)


class _PoolState:
    """The pool of one event loop, and whether it is ready for use."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.pool: Any = None
        self.ready = False


class LoopLocalPool:
    """A connection pool opened on first use, one per event loop.

    A psycopg pool runs background tasks in the loop that opened it, so every loop gets its own pool
    built from the DSN, closed by `close()` or when its loop shuts down. A pool passed in is used as
    it is.

    Args:
        prepare: Awaited with the pool before its first use in a loop (for example to create
            tables); a failure is raised and retried on the next use.
    """

    def __init__(
        self,
        conninfo: Any,
        *,
        max_size: int,
        prepare: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        self._dsn: str | None = conninfo if isinstance(conninfo, str) else None
        self._shared: Any = None if isinstance(conninfo, str) else conninfo
        self._max_size = max_size
        self._prepare = prepare
        self._states: LoopLocal[_PoolState] = LoopLocal(_PoolState, aclose=self._close_state)

    async def get(self) -> Any:
        """Return the running loop's pool, opening and preparing it on first use."""

        state = self._states.get()
        if not state.ready:
            async with state.lock:
                if not state.ready:
                    if state.pool is None:
                        state.pool = (
                            self._shared if self._shared is not None else await self._open()
                        )
                    if self._prepare is not None:
                        await self._prepare(state.pool)
                    state.ready = True
        return state.pool

    async def close(self) -> None:
        """Close the running loop's pool if this object opened it; a later use opens a new one."""

        state = self._states.peek()
        if state is not None:
            await self._release(state)

    async def _open(self) -> Any:
        _, psycopg_pool = import_psycopg()
        check_event_loop()
        pool = psycopg_pool.AsyncConnectionPool(
            self._dsn, min_size=1, max_size=self._max_size, open=False, kwargs={"autocommit": True}
        )
        try:
            await pool.open(wait=True, timeout=30.0)
        except BaseException:
            await pool.close()  # stop its connection workers; the next use tries again
            raise
        return pool

    async def _release(self, state: _PoolState) -> None:
        pool, state.pool, state.ready = state.pool, None, False
        if pool is not None and self._shared is None:
            await pool.close()

    async def _close_state(self, state: _PoolState) -> None:
        # The loop is shutting down and has already cancelled the pool's workers,
        # so the pool may report that cancellation while closing.
        with suppress(asyncio.CancelledError):
            await self._release(state)


def shared_pool(
    dsn: str,
    *,
    max_size: int = 10,
    pooler: Literal["none", "pgbouncer"] = "none",
    application_name: str = "awa",
) -> Any:
    """The one connection pool of a process, not yet opened (`await pool.open(wait=True)`).

    Every PostgreSQL part of awa shares it, so the process holds at most `max_size` connections.
    Connections are autocommit; a multi-statement unit runs in `conn.transaction()`, so `SET LOCAL`
    lasts for it, also behind PgBouncer. With `pooler="pgbouncer"` no statements are prepared.

    Raises:
        OptionalDependencyError: If the `postgres` extra is not installed.
        ConfigurationError: If the DSN is blank, `max_size` is below 1 or `pooler` is unknown.
    """

    _, psycopg_pool = import_psycopg()
    check_conninfo(dsn)
    if max_size < 1:
        raise ConfigurationError(f"max_size must be at least 1, got {max_size!r}")
    if pooler not in ("none", "pgbouncer"):
        raise ConfigurationError(f"pooler must be 'none' or 'pgbouncer', got {pooler!r}")
    kwargs: dict[str, Any] = {"autocommit": True, "application_name": application_name}
    if pooler == "pgbouncer":
        kwargs["prepare_threshold"] = None
    return psycopg_pool.AsyncConnectionPool(
        dsn,
        min_size=1,
        max_size=max_size,
        open=False,
        kwargs=kwargs,
        check=psycopg_pool.AsyncConnectionPool.check_connection,
        max_lifetime=1800.0,
        max_idle=300.0,
        timeout=10.0,
    )


class PostgresTables:
    """Base class of PostgreSQL stores owning a few tables in one schema.

    The pool opens on first use, one per event loop, and the tables are created on first use unless
    `setup=False`. Subclasses return idempotent DDL from `_ddl()` and may set `_setup_lock_key`.

    Args:
        conninfo: A DSN, or a `psycopg_pool.AsyncConnectionPool` with `autocommit=True` connections.
        setup: Create the schema and tables on first use.
        pool_size: Size of the pool opened for a DSN.

    Raises:
        OptionalDependencyError: If the `postgres` extra is not installed.
        ConfigurationError: If the schema, DSN or pool size is invalid.
    """

    _setup_lock_key: ClassVar[int | None] = None
    """Advisory lock key taken while the tables are created; `None` for none."""

    def __init__(
        self, conninfo: Any, *, schema: str = "awa", setup: bool = True, pool_size: int = 4
    ) -> None:
        import_psycopg()
        self._schema = check_schema(schema)
        if pool_size < 1:
            raise ConfigurationError(f"pool_size must be at least 1, got {pool_size!r}")
        check_conninfo(conninfo)
        self._setup = setup
        self._tables_ready = False
        # One pool per event loop: the object may outlive the `asyncio.run` that opened it.
        self._pools = LoopLocalPool(conninfo, max_size=pool_size, prepare=self._prepare)

    @property
    def schema(self) -> str:
        """The PostgreSQL schema holding the tables."""

        return self._schema

    def _ddl(self) -> list[str]:
        raise NotImplementedError

    async def _connection(self) -> Any:
        """A pool connection context (`async with await self._connection() as conn`)."""

        return (await self._pools.get()).connection()

    async def _prepare(self, pool: Any) -> None:
        if self._setup and not self._tables_ready:
            await self._create_tables(pool)

    async def _create_tables(self, pool: Any) -> None:
        async with pool.connection() as conn:
            await setup_schema(conn, self._schema, self._ddl(), lock_key=self._setup_lock_key)
        self._tables_ready = True

    def use_pool(self, pool: Any) -> None:
        """Use `pool` (a fresh shared pool, after the old one was closed) from now on."""

        self._pools = LoopLocalPool(pool, max_size=1, prepare=self._prepare)

    async def setup(self) -> None:
        """Create the schema and tables now instead of on first use."""

        await self._create_tables(await self._pools.get())

    async def ping(self) -> None:
        """Run `SELECT 1`; raises if the database is unreachable."""

        async with await self._connection() as conn:
            await conn.execute("SELECT 1")

    async def close(self) -> None:
        """Close the pool opened in the running event loop (a pool passed in stays open)."""

        await self._pools.close()

    async def __aenter__(self) -> Self:
        await self._pools.get()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


class PostgresMigratedTables(PostgresTables):
    """A `PostgresTables` whose tables come from the versioned migration files (one ledger)."""

    async def _create_tables(self, pool: Any) -> None:
        await PostgresLedger(
            pool, schema=self._schema, migrations=load_migrations("postgres")
        ).upgrade()
        self._tables_ready = True


def create_private_file(path: Path) -> None:
    """Create `path` empty and owner-only (0600) in one step; `FileExistsError` if it exists."""

    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))


def restrict_sqlite_files(path: Path, *, create: bool = False) -> None:
    """Make a SQLite file and its `-wal` and `-shm` files owner-only (best effort, like `.env`).

    With `create`, missing files are first created empty and private, before SQLite connects.
    """

    from awa.core.dotenv import restrict_to_owner

    for target in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if create:
            with suppress(FileExistsError):
                create_private_file(target)
        if target.exists():
            with suppress(OSError):
                restrict_to_owner(target)


def sqlite_version() -> tuple[int, int, int]:
    """The version of the SQLite library Python uses."""

    return sqlite3.sqlite_version_info


def _windows_drive_is_remote(path: Path) -> bool:
    import ctypes

    drive = path.drive
    if not drive or drive.startswith("\\\\"):
        return bool(drive)
    kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009 - Windows only
    return int(kernel32.GetDriveTypeW(f"{drive}\\")) == 4  # DRIVE_REMOTE


def _posix_filesystem(path: Path) -> str | None:
    mounts = Path("/proc/mounts")
    if not mounts.is_file():
        return None
    best: tuple[int, str] | None = None
    for line in mounts.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        point, kind = parts[1], parts[2]
        inside = str(path) == point or str(path).startswith(point.rstrip("/") + "/")
        if inside and (best is None or len(point) > best[0]):
            best = (len(point), kind)
    return None if best is None else best[1]


def is_network_path(path: Path) -> bool:
    """Whether `path` is on a network drive or share (UNC path, mapped drive, NFS, SMB, ...)."""

    resolved = path.resolve()
    if str(path).startswith(("\\\\", "//")) or str(resolved).startswith("\\\\"):
        return True
    if sys.platform == "win32":
        return _windows_drive_is_remote(resolved)
    return (_posix_filesystem(resolved) or "") in _NETWORK_FILESYSTEMS


class SqliteConnection:
    """One connection to a SQLite file, shared by threads under a lock.

    A missing file is created with its folder, owner-only (`":memory:"` opens a private database).
    The connection is in autocommit mode with the write-ahead log, and waits up to `timeout`
    seconds for another writer.

    Args:
        pragmas: Further `PRAGMA` settings, such as `"foreign_keys=ON"`.
    """

    def __init__(
        self, path: str | os.PathLike[str], *, timeout: float, pragmas: Iterable[str] = ()
    ) -> None:
        if str(path) != ":memory:":
            file = Path(path)
            file.parent.mkdir(parents=True, exist_ok=True)
            if not file.exists():
                restrict_sqlite_files(file, create=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(
            path, timeout=timeout, isolation_level=None, check_same_thread=False
        )
        try:
            for pragma in ("journal_mode=WAL", f"busy_timeout={round(timeout * 1000)}", *pragmas):
                self.conn.execute(f"PRAGMA {pragma}")
        except BaseException:
            self.conn.close()
            raise

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        """The connection, under the lock (no transaction of its own)."""

        with self.lock:
            yield self.conn

    @contextmanager
    def writing(self) -> Iterator[sqlite3.Connection]:
        """The connection inside `BEGIN IMMEDIATE` ... `COMMIT` (`ROLLBACK` on any error)."""

        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                if self.conn.in_transaction:
                    with suppress(sqlite3.Error):
                        self.conn.execute("ROLLBACK")
                raise

    def backup(self, target: str | os.PathLike[str]) -> Path:
        """Copy the database consistently to the new, owner-only file `target` (online backup API).

        Raises:
            FileExistsError: If `target` exists.
        """

        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        create_private_file(destination)
        copy = sqlite3.connect(destination)
        try:
            with self.lock:
                self.conn.backup(copy)
        finally:
            copy.close()
        restrict_sqlite_files(destination)
        return destination

    def close(self) -> None:
        """Close the connection."""

        with self.lock:
            self.conn.close()


class SqliteDatabase:
    """One SQLite data file, opened safely and kept at the current schema.

    Args:
        clock: Where the backup file names get their time.
        upgrade: Apply pending migrations when opening (backing up a file with data first);
            `False` only checks that the schema is current.
        migrations_folder: Read the SQLite migration files from this folder instead.

    Raises:
        DataStoreError: If the path is on a network drive, the SQLite library is too old, the file
            cannot be opened, or a call comes after `close()`.
        SchemaVersionError: If the schema is not current.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Clock | None = None,
        upgrade: bool = True,
        migrations_folder: Path | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = as_clock(clock)
        if sqlite_version() < MIN_SQLITE_VERSION:
            found = ".".join(str(part) for part in sqlite_version())
            raise DataStoreError(
                f"The SQLite library is {found}; awa's data store needs 3.38 or newer. Use a "
                "newer Python build, or keep the data in PostgreSQL (AWA_POSTGRES_DSN)."
            )
        if is_network_path(self.path):
            raise DataStoreError(
                f"{self.path} is on a network drive. SQLite's write-ahead log does not work "
                "over a network file system; keep the data file on a local disk, or share "
                "PostgreSQL between machines (AWA_POSTGRES_DSN)."
            )
        try:
            self._db = SqliteConnection(
                self.path,
                timeout=5.0,
                pragmas=("foreign_keys=ON", "secure_delete=ON", "journal_size_limit=67108864"),
            )
        except (OSError, sqlite3.Error) as exc:
            raise DataStoreError(f"Cannot open the data file {self.path}: {exc}") from exc
        self._closed = False
        self._ledger = SqliteLedger(
            self._db.conn, load_migrations("sqlite", folder=migrations_folder)
        )
        try:
            if upgrade:
                self._upgrade()
            else:
                require_current(self._ledger.status())
        except BaseException:
            self._db.close()
            raise

    def __repr__(self) -> str:
        return f"SqliteDatabase({str(self.path)!r})"

    def _upgrade(self) -> None:
        found = self._ledger.status()
        pending = [s for s in found if s.state == "pending"]
        if pending and any(s.state == "applied" for s in found):
            self._backup_before_upgrade(max(s.version for s in found if s.state == "applied"))
        self._ledger.upgrade()

    def _backup_before_upgrade(self, version: int) -> None:
        folder = self.path.parent / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        moment = self._clock.now().strftime("%Y%m%dT%H%M%S%fZ")
        target = folder / f"{self.path.stem}-v{version:04d}-{moment}.db"
        create_private_file(target)
        self._db.conn.execute("VACUUM INTO ?", (str(target),))
        restrict_sqlite_files(target)
        kept = sorted(folder.glob(f"{self.path.stem}-v*.db"), key=lambda p: p.name, reverse=True)
        for old in kept[_BACKUPS_KEPT:]:
            with suppress(OSError):
                old.unlink()

    def _locked(self, work: Callable[[sqlite3.Connection], T], write: bool) -> T:
        with self._db.lock:
            if self._closed:
                raise DataStoreError(f"The data file {self.path} is closed")
            with self._db.writing() if write else self._db.reading() as conn:
                return work(conn)

    async def read(self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Run `work(conn)` in a worker thread (no transaction of its own)."""

        return await asyncio.to_thread(self._locked, work, False)

    async def write(self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Run `work(conn)` in a worker thread inside `BEGIN IMMEDIATE` ... `COMMIT`."""

        return await asyncio.to_thread(self._locked, work, True)

    def status(self) -> list[MigrationStatus]:
        """Where every migration stands."""

        with self._db.lock:
            return self._ledger.status()

    def upgrade(self) -> list[MigrationStatus]:
        """Apply pending migrations now (with a backup first when the file holds data)."""

        with self._db.lock:
            self._upgrade()
            return self._ledger.status()

    def backup(self, target: str | os.PathLike[str]) -> Path:
        """Copy the whole database consistently to `target` (online backup API, safe in use).

        Raises:
            DataStoreError: If `target` exists or cannot be written.
        """

        try:
            return self._db.backup(target)
        except FileExistsError:
            raise DataStoreError(
                f"{Path(target)} exists; choose a new file for the backup"
            ) from None
        except (OSError, sqlite3.Error) as exc:
            raise DataStoreError(f"Cannot write the backup {Path(target)}: {exc}") from exc

    def compact(self) -> None:
        """Write the log into the file and truncate it, so deleted records leave no copy there."""

        with self._db.reading() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        """Close the connection."""

        with self._db.lock:
            self._closed = True
            self._db.close()
