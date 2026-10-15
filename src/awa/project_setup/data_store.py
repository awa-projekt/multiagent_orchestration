"""Switch a project's data between the local SQLite file and PostgreSQL, and look at it.

`use_store` is the one-step path (it starts the local PostgreSQL in Docker without a DSN),
`migrate_store` the same move with every option, `store_status` the active store. A move copies
every table, verifies counts and a sha256 per table, and only then writes `AWA_DATA_BACKEND`;
the old store stays as a backup. Connection strings are never printed.
"""

from __future__ import annotations

import glob
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from awa.core.settings import DATA_BACKEND_VARIABLE, Settings
from awa.core.settings import POSTGRES_DSN_VARIABLE as DSN_VARIABLE
from awa.errors import ConfigurationError, MissingConfigurationError
from awa.persistence.sql import describe_dsn

Backend = Literal["sqlite", "postgres"]
"""Where a project's data is kept."""


@dataclass
class MigrationResult:
    """What a move did.

    Attributes:
        report: Per table: source rows, copied, already there, target rows, checksum match.
        verified: Every count and checksum matched.
        switched: `AWA_DATA_BACKEND` was written (only after verification, never on a dry run).
    """

    source: Backend
    target: Backend
    report: dict[str, Any]
    verified: bool
    switched: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "verified": self.verified,
            "switched": self.switched,
            **self.report,
        }


@dataclass
class StoreStatus:
    """The active store of a project."""

    backend: Backend
    where: str
    schema: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    last_migration: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _settings(project: Path) -> Settings:
    env = project / ".env"
    return Settings(_env_file=env if env.is_file() else None)


def _dsn(settings: Settings, dsn_env: str, folder: Path) -> str | None:
    if dsn_env == DSN_VARIABLE:
        value = settings.postgres_dsn.get_secret_value() if settings.postgres_dsn else None
    else:
        from awa.core.config import env_value
        from awa.core.dotenv import read_env_file

        value = env_value(dsn_env) or read_env_file(folder / ".env").get(dsn_env)
    return (value or "").strip() or None


def active_backend(settings: Settings) -> Backend:
    """`sqlite` or `postgres`: `AWA_DATA_BACKEND`, else `postgres` when a DSN is set."""

    if settings.data_backend is not None:
        return settings.data_backend
    return "postgres" if settings.postgres_dsn is not None else "sqlite"


async def _open_postgres(dsn: str, schema: str, settings: Settings) -> Any:
    from awa.server._data_store import PostgresData

    data = PostgresData(dsn, schema, settings=settings)
    await data.open()
    return data


def _local(project: Path, settings: Settings, *, key: bool = False) -> Any:
    """The local stores. The pseudonym key is made (and written to `.env`) only with `key=True`,
    for a person's erasure and for a move; otherwise the key that is set is used, if any."""

    from awa.server._data_store import ensure_pseudonym_key, open_local_data, pseudonym_key

    return open_local_data(
        project / ".awa" / "awa.db",
        pseudonym=ensure_pseudonym_key(project, settings) if key else pseudonym_key(settings),
    )


def _require_data(folder: Path, settings: Settings) -> None:
    """Refuse a project without data (or, for PostgreSQL, without a DSN) instead of creating one."""

    if active_backend(settings) == "postgres":
        if _dsn(settings, DSN_VARIABLE, folder) is None:
            raise ConfigurationError(f"no awa data in {folder}: {DSN_VARIABLE} is not set in .env")
        return
    if not (folder / ".awa" / "awa.db").is_file():
        raise ConfigurationError(f"no awa data in {folder}: run an automation first")


def _local_checkpointer(project: Path, *, create: bool) -> Any:
    from awa.persistence import SqliteCheckpointer

    path = project / ".awa" / "checkpoints.db"
    if not create and not path.is_file():
        return None
    return SqliteCheckpointer(path, keep_checkpoints=None, retention=None)


async def migrate_store(
    project: str | os.PathLike[str],
    to: Backend,
    *,
    dsn_env: str = DSN_VARIABLE,
    schema: str = "awa",
    dry_run: bool = False,
    switch: bool = True,
    say: Callable[[str], None] = lambda line: None,
    on_batch: Any = None,
) -> MigrationResult:
    """Move the project's data to `to` (`postgres` or `sqlite`).

    Run it on a selector event loop on Windows (`awa.ext.run_sync(..., selector_loop=True)`).

    Args:
        dsn_env: The variable holding the PostgreSQL DSN (never a DSN itself).
        dry_run: Only count what would move: no row is copied and nothing is switched. Opening
            the stores still prepares them (the local data files, the PostgreSQL schema and,
            when missing, the pseudonym key in `.env`).
        switch: Write `AWA_DATA_BACKEND=<to>` into `.env` after a verified move.
        say: Receives one line per stage.
        on_batch: Awaited after every batch (`(table, rows_done)`), for progress or tests.

    Raises:
        MissingConfigurationError: If the DSN variable is not set.
        ConfigurationError: If the data is already in `to`.
        StoreConflictError: If the target holds different data under the same keys.
    """

    from awa.server._store_copy import PostgresSide, SqliteSide, copy_data

    folder = Path(project)
    settings = _settings(folder)
    source: Backend = active_backend(settings)
    if source == to:
        raise ConfigurationError(f"The data is already in {to}; nothing to move.")
    dsn = _dsn(settings, dsn_env, folder)
    if dsn is None:
        raise MissingConfigurationError(
            f"{dsn_env} is not set. Run `awa store use postgres` (starts a local PostgreSQL) "
            f"or set {dsn_env} in .env."
        )
    say(
        f"source: {source}, target: {to} ({describe_dsn(dsn) if to == 'postgres' else '.awa/awa.db'})"
    )
    postgres = await _open_postgres(dsn, schema, settings)
    local = _local(folder, settings, key=True)
    local_saver = _local_checkpointer(folder, create=to == "sqlite")
    try:
        sqlite_side, pg_side = SqliteSide(local.database), PostgresSide(postgres.pool, schema)
        if to == "postgres":
            arguments = {
                "source_side": sqlite_side, "target_side": pg_side,
                "source_runs": local.runs, "target_runs": postgres.runs,
                "source_checkpointer": local_saver, "target_checkpointer": postgres.checkpointer,
            }  # fmt: skip
        else:
            arguments = {
                "source_side": pg_side, "target_side": sqlite_side,
                "source_runs": postgres.runs, "target_runs": local.runs,
                "source_checkpointer": postgres.checkpointer, "target_checkpointer": local_saver,
            }  # fmt: skip
        say(
            "copying runs, steps, results, thread owners and checkpoints"
            if not dry_run
            else "counting (dry run)"
        )
        report = await copy_data(**arguments, dry_run=dry_run, on_batch=on_batch)
    finally:
        await local.close()
        await postgres.close()
        if local_saver is not None:
            local_saver.close()
    verified = report.verified
    if not dry_run:
        say(
            "verified: counts and checksums match"
            if verified
            else "verification FAILED: not switched"
        )
    switched = False
    if verified and switch:
        from awa.core.dotenv import write_env_file
        from awa.core.settings import get_settings

        write_env_file(folder / ".env", {DATA_BACKEND_VARIABLE: to}, confirm_overwrite=True)
        get_settings.cache_clear()
        switched = True
        say(f"switched: {DATA_BACKEND_VARIABLE}={to} in .env (the {source} data stays as a backup)")
    return MigrationResult(source, to, report.to_dict(), verified, switched)


async def use_store(
    project: str | os.PathLike[str],
    to: Backend,
    *,
    schema: str = "awa",
    say: Callable[[str], None] = lambda line: None,
    start_database: Callable[[Path], None] | None = None,
) -> MigrationResult | None:
    """Make `to` the project's store in one step; `None` when the data was already in `to`.

    Raises:
        ConfigurationError: If the local PostgreSQL cannot be started (Docker missing).
        StoreConflictError: See `migrate_store`.
    """

    folder = Path(project)
    settings = _settings(folder)
    if active_backend(settings) == to:
        say(f"the data is already in {to}")
        return None
    if to == "postgres" and settings.postgres_dsn is None:
        say("starting the local PostgreSQL in Docker (awa setup)")
        (start_database or _start_local_postgres)(folder)
    return await migrate_store(folder, to, schema=schema, say=say)


def _start_local_postgres(folder: Path) -> None:
    from awa.project_setup import setup

    setup(database="postgres-docker", project=folder, start_services=True)


async def store_status(project: str | os.PathLike[str], *, schema: str = "awa") -> StoreStatus:
    """The active store of `project`: where it is, its schema state and row counts."""

    from awa.server._store_copy import SQL_TABLES, PostgresSide, SqliteSide

    folder = Path(project)
    settings = _settings(folder)
    _require_data(folder, settings)
    backend = active_backend(settings)
    tables = ["process_runs", *(name for name, _ in SQL_TABLES)]
    if backend == "postgres":
        dsn = _dsn(settings, DSN_VARIABLE, folder)
        if dsn is None:
            raise MissingConfigurationError(f"{DSN_VARIABLE} is not set in .env.")
        data = await _open_postgres(dsn, schema, settings)
        try:
            from awa.persistence.ledger import PostgresLedger, load_migrations

            found = await PostgresLedger(
                data.pool, schema=schema, migrations=load_migrations("postgres")
            ).status()
            side: Any = PostgresSide(data.pool, schema)
            counts = {table: await side.count(table) for table in tables}
            where = f"{describe_dsn(dsn)} (schema {schema})"
        finally:
            await data.close()
    else:
        local = _local(folder, settings)
        try:
            found = local.database.status()
            side = SqliteSide(local.database)
            counts = {table: await side.count(table) for table in tables}
            where = str(local.database.path)
        finally:
            await local.close()
    applied = [s for s in found if s.state == "applied"]
    return StoreStatus(
        backend=backend,
        where=where,
        schema=[s.__dict__ for s in found],
        counts=counts,
        last_migration=(
            f"{applied[-1].component}/{applied[-1].version:04d}_{applied[-1].name}"
            f" at {applied[-1].applied_at}"
            if applied
            else None
        ),
    )


@dataclass
class ErasureResult:
    """What an erasure or a retention sweep deleted: runs, checkpoint threads, trace files."""

    runs: list[str] = field(default_factory=list)
    threads: list[str] = field(default_factory=list)
    traces: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


async def _open_active(
    folder: Path, settings: Settings, schema: str, *, key: bool = False
) -> tuple[Any, Any]:
    """The active data and its checkpointer (`None` when the project has none yet)."""

    _require_data(folder, settings)
    if active_backend(settings) == "postgres":
        dsn = _dsn(settings, DSN_VARIABLE, folder)
        assert dsn is not None  # `_require_data` checked it
        data = await _open_postgres(dsn, schema, settings)
        return data, data.checkpointer
    return _local(folder, settings, key=key), _local_checkpointer(folder, create=False)


async def _close_active(data: Any, saver: Any) -> None:
    await data.close()
    if saver is not None and getattr(data, "database", None) is not None:
        saver.close()  # the local checkpoint file; PostgreSQL's closes with the pool


async def _delete(
    folder: Path, data: Any, saver: Any, by_scope: dict[tuple[str, str], list[str]]
) -> ErasureResult:
    """Delete the runs of the named tenant and automation physically, then compact the storage."""

    from awa.persistence.runlog import Scope

    result = ErasureResult()
    gone: list[Any] = []
    for (tenant, project), run_ids in by_scope.items():
        wanted = list(dict.fromkeys(run_ids))
        runs = [run for run in [await data.runs.get(i) for i in wanted] if run is not None]
        owned = [
            run for run in runs if run.automation == project and (run.tenant or "local") == tenant
        ]
        await data.log.delete_runs(Scope(tenant, project), wanted)
        await data.runs.delete([run.id for run in owned])
        result.runs += wanted
        gone += owned
    kept = {run.thread_id for run in await data.runs.list(limit=10**9) if run.thread_id}
    for thread in sorted({run.thread_id for run in gone if run.thread_id} - kept):
        if saver is not None:
            await saver.adelete_thread(thread)
        result.threads.append(thread)
    result.traces = _delete_traces(folder, result.runs)
    await _compact(data)
    return result


def _delete_traces(folder: Path, run_ids: Iterable[str]) -> int:
    """Delete the trace files of the runs (`.awa/traces/<run>.a<N>.jsonl`); return how many."""

    traces = folder / ".awa" / "traces"
    deleted = 0
    if traces.is_dir():
        for run_id in run_ids:
            if not run_id or any(c in run_id for c in "/\\") or run_id.startswith("."):
                continue  # never a path: only a trace file name in this folder
            for path in traces.glob(f"{glob.escape(run_id)}.a*.jsonl"):
                path.unlink(missing_ok=True)
                deleted += 1
    return deleted


_VACUUMED = (
    "process_runs",
    "run_log_head",
    "run_step",
    "step_attempt",
    "step_result",
    "approval",
    "captured_content",
    "data_subject_link",
)


async def _compact(data: Any) -> None:
    import asyncio

    database = getattr(data, "database", None)
    if database is not None:
        await asyncio.to_thread(database.compact)
        return
    async with data.pool.connection() as conn:  # PostgreSQL frees the space when it vacuums
        for table in _VACUUMED:
            await conn.execute(f"VACUUM {data.schema}.{table}")


async def erase(
    project: str | os.PathLike[str],
    *,
    automation: str,
    tenant: str = "local",
    run: str | None = None,
    everything: bool = False,
    person: str | None = None,
    schema: str = "awa",
) -> ErasureResult:
    """Delete runs for good: one run, every run of an automation (`everything`), or a `person`.

    A person goes through `awa.persistence.erase_subject` (run log rows found through the HMAC
    link, process runs, threads and trace files). The storage is compacted afterwards; backups
    made before keep the data until they are deleted.

    Raises:
        ConfigurationError: If not exactly one of `run`, `everything`, `person` is given, the
            project has no data, the run is not known in that tenant and automation, or the
            erasure of a person could not be completed (the message lists why).
    """

    from awa.persistence.runlog import Scope

    if sum((run is not None, everything, person is not None)) != 1:
        raise ConfigurationError("Name exactly one of: a run, the whole automation, a person.")
    folder = Path(project)
    settings = _settings(folder)
    scope = Scope(tenant, automation)
    data, saver = await _open_active(folder, settings, schema, key=person is not None)
    try:
        if person is not None:
            return await _erase_person(folder, data, saver, scope, person)
        if run is not None:
            await _require_run(data, scope, run)
            ids = [run]
        else:
            stored = await data.runs.list(automation=automation, limit=10**9)
            ids = [
                *await data.log.project_runs(scope),
                *(r.id for r in stored if (r.tenant or "local") == tenant),
            ]
        return await _delete(folder, data, saver, {(tenant, automation): ids})
    finally:
        await _close_active(data, saver)


async def _require_run(data: Any, scope: Any, run: str) -> None:
    """The run must exist in this tenant and automation (its record, or its run log rows)."""

    stored = await data.runs.get(run)
    if stored is not None:
        known = stored.automation == scope.project and (stored.tenant or "local") == scope.tenant
    else:
        known = run in {*await data.log.project_runs(scope)}
    if not known:
        raise ConfigurationError(f"No run {run!r} in automation {scope.project!r}.")


async def _erase_person(
    folder: Path, data: Any, saver: Any, scope: Any, person: str
) -> ErasureResult:
    from awa.persistence import erase_subject

    report = await erase_subject(
        person,
        checkpointer=saver,
        run_log=data.log,
        scopes=[scope],
        threads_index=data.threads,
        run_store=data.runs,
        project=folder,
    )
    runs = sorted({*report.ids["runs"], *report.ids["process_runs"]})
    result = ErasureResult(
        runs=runs, threads=report.ids["threads"], traces=_delete_traces(folder, runs)
    )
    await _compact(data)
    if not report.ok:
        raise ConfigurationError(
            f"The erasure of {person!r} is not complete: " + "; ".join(report.errors)
        )
    return result


async def sweep(
    project: str | os.PathLike[str],
    *,
    days: int | None = None,
    now: Any = None,
    schema: str = "awa",
) -> ErasureResult:
    """Delete the runs older than the retention (`days`, else `AWA_RUN_RETENTION_DAYS`).

    A run counts from when it started; a run that is not finished is kept.

    Raises:
        ConfigurationError: If no retention is set, `days` is below 1, or there is no data.
    """

    from datetime import UTC, datetime, timedelta

    from awa.persistence import RetentionPolicy, apply_retention

    folder = Path(project)
    settings = _settings(folder)
    keep = days if days is not None else settings.run_retention_days
    if keep is None:
        raise ConfigurationError(
            "No retention is set: pass --days or set AWA_RUN_RETENTION_DAYS in .env."
        )
    if keep < 1:
        raise ConfigurationError("days must be at least 1")
    data, saver = await _open_active(folder, settings, schema)
    try:
        report = await apply_retention(
            RetentionPolicy(runs=timedelta(days=keep)),
            checkpointer=saver,
            run_log=data.log,
            run_store=data.runs,
            project=folder,
            now=now or datetime.now(UTC),
        )
        runs = sorted({*report.ids["runs"], *report.ids["process_runs"]})
        result = ErasureResult(
            runs=runs, threads=report.ids["threads"], traces=_delete_traces(folder, runs)
        )
        await _compact(data)
        return result
    finally:
        await _close_active(data, saver)


def backup_store(project: str | os.PathLike[str], target: str | os.PathLike[str]) -> list[Path]:
    """Back up the local data (`checkpoints.db`, then `awa.db`) into the folder `target`.

    Uses SQLite's online backup API, never a raw file copy; PostgreSQL is backed up with its own
    tools.

    Raises:
        ConfigurationError: If the project's data is in PostgreSQL.
        DataStoreError: If a backup file exists already.
    """

    import contextlib

    from awa.errors import DataStoreError
    from awa.persistence import SqliteCheckpointer
    from awa.persistence.runlog import SqliteDatabase

    folder = Path(project)
    if active_backend(_settings(folder)) == "postgres":
        raise ConfigurationError(
            "The data is in PostgreSQL: back it up with pg_dump (see 'The data store')."
        )
    if not (folder / ".awa" / "awa.db").is_file():
        raise ConfigurationError(f"no awa data in {folder}: run an automation first")
    out = Path(target)
    written: list[Path] = []
    for name in ("checkpoints.db", "awa.db"):
        if (out / name).exists():
            raise DataStoreError(f"{out / name} exists; choose a new folder for the backup")
    checkpoints = folder / ".awa" / "checkpoints.db"
    if checkpoints.is_file():
        saver = SqliteCheckpointer(checkpoints, keep_checkpoints=None, retention=None)
        with contextlib.closing(saver):
            written.append(saver.backup(out / "checkpoints.db"))
    database = SqliteDatabase(folder / ".awa" / "awa.db")
    try:
        written.append(database.backup(out / "awa.db"))
    finally:
        database.close()
    return written


async def upgrade_store(
    project: str | os.PathLike[str], *, schema: str = "awa"
) -> list[dict[str, Any]]:
    """Apply pending migrations to the active store (PostgreSQL through the direct connection
    `AWA_POSTGRES_ADMIN_DSN` when set; SQLite with a backup first)."""

    folder = Path(project)
    settings = _settings(folder)
    if active_backend(settings) == "postgres":
        from awa.persistence.ledger import PostgresLedger, load_migrations
        from awa.persistence.sql import shared_pool

        dsn = _dsn(settings, DSN_VARIABLE, folder)
        if dsn is None:
            raise MissingConfigurationError(f"{DSN_VARIABLE} is not set in .env.")
        admin = settings.postgres_admin_dsn
        direct = admin.get_secret_value() if admin is not None else dsn
        pool = shared_pool(direct, max_size=1)
        await pool.open(wait=True)
        try:
            found = await PostgresLedger(
                pool, schema=schema, migrations=load_migrations("postgres")
            ).upgrade()
        finally:
            await pool.close()
    else:
        from awa.persistence.runlog import SqliteDatabase

        database = SqliteDatabase(folder / ".awa" / "awa.db")
        try:
            found = database.upgrade()
        finally:
            database.close()
    return [s.__dict__ for s in found]


__all__ = [
    "ErasureResult",
    "MigrationResult",
    "StoreStatus",
    "backup_store",
    "erase",
    "migrate_store",
    "store_status",
    "sweep",
    "upgrade_store",
    "use_store",
]
