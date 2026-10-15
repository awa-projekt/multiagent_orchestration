"""Move a project's data between the local SQLite file and PostgreSQL (`copy_data`)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from awa.errors import StoreConflictError
from awa.persistence.runlog.rows import stamp, when

SQL_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("process_state", ("key",)),
    ("server_threads", ("system", "thread_id")),
    ("server_decisions", ("system", "thread_id", "version")),
    ("run_log_head", ("run_id",)),
    ("run_step", ("run_id", "step_key")),
    ("step_attempt", ("run_id", "step_key", "attempt")),
    ("step_result", ("result_id",)),
    ("approval", ("approval_id",)),
    ("captured_content", ("content_ref",)),
    ("captured_content_read", ("read_id",)),
    ("data_subject_link", ("tenant_id", "project_id", "subject_hmac", "ref_kind", "ref_id")),
)
"""The tables copied row by row, parents first, with their keys."""

_TIME = frozenset(
    {
        "created_at", "updated_at", "opened_at", "started_at", "ended_at", "requested_at",
        "decided_at", "read_at", "claimed_at",
    }
)  # fmt: skip
_JSON = frozenset({"output", "tool_calls", "checks", "reasons", "body"})
_BOOL = frozenset({"capture"})
BATCH = 500


def logical(column: str, value: Any, *, decoded: bool = False) -> Any:
    """A stored value in the form both dialects agree on; `decoded`: JSON already parsed."""

    if value is None:
        return None
    if column in _TIME:
        return stamp(when(value))
    if column in _JSON:
        data = value if decoded else json.loads(value)
        return json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    if column in _BOOL:
        return int(bool(value))
    return value


def _columns() -> dict[str, list[str]]:
    """The columns of each copied table (the same names in both dialects)."""

    from awa.persistence.ledger import SqliteLedger, load_migrations

    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        SqliteLedger(conn, load_migrations("sqlite")).upgrade()
        return {
            table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for table, _ in SQL_TABLES
        }
    finally:
        conn.close()


@dataclass
class TableReport:
    """What happened to one table (or the runs, or the checkpoints).

    Attributes:
        present: Rows the target held already, equal to the source's.
        target: Rows in the target after the copy (`None` on a dry run).
        checksum_ok: Whether the per-table sha256 matched (`None` before verification).
    """

    name: str
    source: int = 0
    copied: int = 0
    present: int = 0
    target: int | None = None
    checksum_ok: bool | None = None


@dataclass
class CopyReport:
    """The result of a copy: per table, and whether everything was verified."""

    tables: list[TableReport] = field(default_factory=list)
    dry_run: bool = False

    @property
    def verified(self) -> bool:
        return not self.dry_run and all(
            t.checksum_ok and t.target is not None and t.target >= t.source for t in self.tables
        )

    def table(self, name: str) -> TableReport:
        for item in self.tables:
            if item.name == name:
                return item
        item = TableReport(name)
        self.tables.append(item)
        return item

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "verified": self.verified,
            "tables": [t.__dict__ for t in self.tables],
        }


Rows = list[tuple[Any, ...]]


class _Side(ABC):
    """The SQL of a copy source or target; a subclass runs it on its database."""

    mark = "?"
    prefix = ""

    def _values(self, columns: Sequence[str]) -> str:
        return ", ".join(self.mark for _ in columns)

    @abstractmethod
    async def _fetch(self, query: str, params: Sequence[Any] = ()) -> Rows:
        """The rows `query` returns."""

    @abstractmethod
    async def _insert(
        self, insert: str, select: str, rows: Sequence[tuple[tuple[Any, ...], tuple[Any, ...]]]
    ) -> Any:
        """Run `insert` per `(row, key)` in one transaction: `(copied, present rows by select)`."""

    async def rows(
        self, table: str, columns: Sequence[str], key: Sequence[str], after: tuple[Any, ...] | None
    ) -> Rows:
        order = ", ".join(key)
        where = f" WHERE ({order}) > ({', '.join(self.mark for _ in key)})" if after else ""
        return await self._fetch(
            f"SELECT {', '.join(columns)} FROM {self.prefix}{table}{where} "  # noqa: S608
            f"ORDER BY {order} LIMIT {BATCH}",
            after or (),
        )

    async def insert(
        self, table: str, columns: Sequence[str], key: Sequence[str], rows: Sequence[Sequence[Any]]
    ) -> tuple[int, Rows]:
        names = ", ".join(columns)
        positions = [columns.index(k) for k in key]
        insert = (
            f"INSERT INTO {self.prefix}{table} ({names}) VALUES ({self._values(columns)})"  # noqa: S608
            " ON CONFLICT DO NOTHING"
        )
        select = f"SELECT {names} FROM {self.prefix}{table} WHERE " + " AND ".join(  # noqa: S608
            f"{k} = {self.mark}" for k in key
        )
        keyed = [(tuple(row), tuple(row[i] for i in positions)) for row in rows]
        done: tuple[int, Rows] = await self._insert(insert, select, keyed)
        return done

    async def count(self, table: str) -> int:
        [row] = await self._fetch(f"SELECT COUNT(*) FROM {self.prefix}{table}")  # noqa: S608
        return int(row[0])


class SqliteSide(_Side):
    """The local data file as a copy source or target."""

    def __init__(self, database: Any) -> None:
        self.database = database

    async def _fetch(self, query: str, params: Sequence[Any] = ()) -> Rows:
        found: Rows = await self.database.read(
            lambda conn: conn.execute(query, tuple(params)).fetchall()
        )
        return found

    async def _insert(
        self, insert: str, select: str, rows: Sequence[tuple[tuple[Any, ...], tuple[Any, ...]]]
    ) -> Any:
        def work(conn: sqlite3.Connection) -> tuple[int, Rows]:
            copied, present = 0, []
            for row, key in rows:
                if conn.execute(insert, row).rowcount:
                    copied += 1
                else:
                    present.append(conn.execute(select, key).fetchone())
            return copied, present

        return await self.database.write(work)

    def stored(self, column: str, value: Any) -> Any:
        return value  # the logical form is SQLite's own

    def normal(self, column: str, value: Any) -> Any:
        return logical(column, value)


class PostgresSide(_Side):
    """A PostgreSQL schema as a copy source or target (on the shared pool)."""

    mark = "%s"

    def __init__(self, pool: Any, schema: str) -> None:
        self.pool = pool
        self.prefix = f"{schema}."

    def _values(self, columns: Sequence[str]) -> str:
        return ", ".join("%s::jsonb" if c in _JSON else "%s" for c in columns)

    async def _fetch(self, query: str, params: Sequence[Any] = ()) -> Rows:
        from awa.persistence.sql import fetch_all

        async with self.pool.connection() as conn:
            return await fetch_all(conn, query, tuple(params))

    async def _insert(
        self, insert: str, select: str, rows: Sequence[tuple[tuple[Any, ...], tuple[Any, ...]]]
    ) -> Any:
        from awa.persistence.sql import fetch_one

        copied, present = 0, []
        async with self.pool.connection() as conn, conn.transaction():
            for row, key in rows:
                if (await conn.execute(insert, row)).rowcount:
                    copied += 1
                else:
                    present.append(await fetch_one(conn, select, key))
        return copied, present

    def normal(self, column: str, value: Any) -> Any:
        return logical(column, value, decoded=True)

    def stored(self, column: str, value: Any) -> Any:
        if value is None:
            return None
        if column in _TIME:
            return when(value)
        if column in _BOOL:
            return bool(value)
        return value


Side = SqliteSide | PostgresSide


async def _checksum(side: Side, table: str, columns: Sequence[str], key: Sequence[str]) -> str:
    """One sha256 over the digests of the rows, sorted here, so the order the database returns
    them in (its collation) does not matter."""

    digests: list[bytes] = []
    after: tuple[Any, ...] | None = None
    while True:
        batch = await side.rows(table, columns, key, after)
        for row in batch:
            normal = [side.normal(c, v) for c, v in zip(columns, row, strict=True)]
            digests.append(hashlib.sha256(json.dumps(normal, default=str).encode("utf-8")).digest())
        if len(batch) < BATCH:
            return hashlib.sha256(b"".join(sorted(digests))).hexdigest()
        after = tuple(batch[-1][columns.index(k)] for k in key)


def _conflict(
    table: str, row: Sequence[Any] | None, key: Sequence[str], columns: Sequence[str]
) -> Any:
    """`row` of the target differs from the copy; `None`: a row clashes on another unique column."""

    shown = f" { ({k: row[columns.index(k)] for k in key}) }" if row is not None else ""
    return StoreConflictError(
        f"The target already holds a different {table} row{shown}. Nothing was switched; "
        "empty the target schema or choose another one, then run the move again."
    )


async def _copy_table(
    source: Side,
    target: Side,
    table: str,
    key: Sequence[str],
    columns: Sequence[str],
    report: TableReport,
    *,
    dry_run: bool,
    on_batch: Callable[[str, int], Awaitable[None]] | None,
) -> None:
    report.source = await source.count(table)
    if dry_run:
        report.present = await target.count(table)
        return
    after: tuple[Any, ...] | None = None
    while True:
        batch = await source.rows(table, columns, key, after)
        if not batch:
            break
        normal = [[source.normal(c, v) for c, v in zip(columns, row, strict=True)] for row in batch]
        written = [
            [target.stored(c, v) for c, v in zip(columns, row, strict=True)] for row in normal
        ]
        copied, present = await target.insert(table, columns, key, written)
        report.copied += copied
        wanted = {tuple(r[columns.index(k)] for k in key): r for r in normal}
        for existing in present:
            if existing is None:  # refused by another unique column: no row has the key
                raise _conflict(table, None, key, columns)
            mine = [target.normal(c, v) for c, v in zip(columns, existing, strict=True)]
            ident = tuple(mine[columns.index(k)] for k in key)
            if wanted.get(ident) != mine:
                raise _conflict(table, mine, key, columns)
            report.present += 1
        if on_batch is not None:
            await on_batch(table, report.copied + report.present)
        if len(batch) < BATCH:
            break
        after = tuple(batch[-1][columns.index(k)] for k in key)


async def _copy_runs(
    source_runs: Any,
    target_runs: Any,
    report: TableReport,
    *,
    dry_run: bool,
    on_batch: Callable[[str, int], Awaitable[None]] | None,
) -> list[str]:
    from awa.errors import ProcessRunStateError

    runs = sorted(await source_runs.list(limit=10**9), key=lambda run: (run.tenant or "", run.id))
    report.source = len(runs)
    if dry_run:
        report.present = len(await target_runs.list(limit=10**9))
        return [run.id for run in runs]
    for position, run in enumerate(runs, start=1):
        try:
            await target_runs.create(run)
            report.copied += 1
        except ProcessRunStateError:
            existing = await target_runs.get(run.id)
            if existing is None or _run_text(existing) != _run_text(run):
                raise StoreConflictError(
                    f"The target already holds a different process run {run.id!r}. Nothing was "
                    "switched; empty the target or choose another one, then run the move again."
                ) from None
            report.present += 1
        if on_batch is not None and position % BATCH == 0:
            await on_batch("process_runs", position)
    return [run.id for run in runs]


def _run_text(run: Any) -> str:
    return json.dumps(json.loads(run.model_dump_json()), sort_keys=True)


async def _runs_checksum(runs: Any) -> tuple[int, str]:
    found = sorted(await runs.list(limit=10**9), key=lambda run: run.id)
    digest = hashlib.sha256()
    for run in found:
        digest.update(_run_text(run).encode("utf-8"))
    return len(found), digest.hexdigest()


async def _checkpoints(saver: Any, threads: Collection[str] | None = None) -> list[Any]:
    """The checkpoints of `saver`, only of `threads` when given (a PostgreSQL checkpointer
    holds every project's threads of its database)."""

    if threads is None:
        found = [item async for item in saver.alist(None)]
    else:
        found = [
            item
            for thread in sorted(threads)
            async for item in saver.alist({"configurable": {"thread_id": thread}})
        ]
    # Parents before children: checkpoint ids grow with time within a thread.
    return sorted(
        found,
        key=lambda t: (
            t.config["configurable"]["thread_id"],
            t.config["configurable"].get("checkpoint_ns", ""),
            t.config["configurable"]["checkpoint_id"],
        ),
    )


def _threads_of(items: Sequence[Any]) -> set[str]:
    return {str(t.config["configurable"]["thread_id"]) for t in items}


async def project_threads(side: Side, runs: Any) -> set[str]:
    """The threads of a project: those of its process runs and those its server recorded."""

    found = {run.thread_id for run in await runs.list(limit=10**9) if run.thread_id}
    after: tuple[Any, ...] | None = None
    while True:
        batch = await side.rows(
            "server_threads", ["system", "thread_id"], ["system", "thread_id"], after
        )
        found.update(str(row[1]) for row in batch)
        if len(batch) < BATCH:
            return found
        after = tuple(batch[-1])


def _writes_by_task(item: Any) -> dict[str, list[tuple[str, Any]]]:
    """The pending writes of a checkpoint grouped by task, each task's in their order (one
    `aput_writes` call per task numbers them from 0, as the original run did)."""

    grouped: dict[str, list[tuple[str, Any]]] = {}
    for task_id, channel, value in item.pending_writes or ():
        grouped.setdefault(task_id, []).append((channel, value))
    return grouped


def _checkpoint_keys(items: Sequence[Any]) -> list[tuple[str, str, str]]:
    return [
        (
            str(t.config["configurable"]["thread_id"]),
            str(t.config["configurable"].get("checkpoint_ns", "")),
            str(t.config["configurable"]["checkpoint_id"]),
        )
        for t in items
    ]


async def _copy_checkpoints(
    source: Any, target: Any, report: TableReport, *, dry_run: bool, threads: Collection[str] | None
) -> None:
    items = await _checkpoints(source, threads)
    report.source = len(items)
    known = set(_checkpoint_keys(await _checkpoints(target, _threads_of(items))))
    if dry_run:
        report.present = len(known)
        return
    for item, ident in zip(items, _checkpoint_keys(items), strict=True):
        if ident in known:
            report.present += 1
            continue
        configurable = item.config["configurable"]
        parent = (item.parent_config or {}).get("configurable", {}).get("checkpoint_id")
        config: dict[str, Any] = {
            "configurable": {
                "thread_id": configurable["thread_id"],
                "checkpoint_ns": configurable.get("checkpoint_ns", ""),
                **({"checkpoint_id": parent} if parent else {}),
            }
        }
        saved = await target.aput(
            config, item.checkpoint, item.metadata, dict(item.checkpoint["channel_versions"])
        )
        for task_id, writes in _writes_by_task(item).items():
            await target.aput_writes(saved, writes, task_id)
        report.copied += 1


async def copy_data(
    *,
    source_side: Side,
    target_side: Side,
    source_runs: Any,
    target_runs: Any,
    source_checkpointer: Any = None,
    target_checkpointer: Any = None,
    dry_run: bool = False,
    on_batch: Callable[[str, int], Awaitable[None]] | None = None,
) -> CopyReport:
    """Copy the process runs, the thread owners, decisions and run log, and the checkpoints.

    Tables go parents first. Equal rows are skipped and a different one is a
    `StoreConflictError`, so an interrupted copy resumes by running again; verification compares
    row counts and a sha256 per table over one logical form. The source is never changed.

    Args:
        on_batch: Awaited after each batch with the table and the rows done.

    Raises:
        StoreConflictError: If the target holds a different row under a source key.
    """

    report = CopyReport(dry_run=dry_run)
    columns = _columns()
    await _copy_runs(
        source_runs, target_runs, report.table("process_runs"), dry_run=dry_run, on_batch=on_batch
    )
    for table, key in SQL_TABLES:
        await _copy_table(
            source_side, target_side, table, key, columns[table], report.table(table),
            dry_run=dry_run, on_batch=on_batch,
        )  # fmt: skip
    threads: set[str] | None = None
    if isinstance(source_side, PostgresSide):  # the database may hold other projects' threads
        threads = await project_threads(source_side, source_runs)
    if source_checkpointer is not None and target_checkpointer is not None:
        await _copy_checkpoints(
            source_checkpointer,
            target_checkpointer,
            report.table("checkpoints"),
            dry_run=dry_run,
            threads=threads,
        )
    if not dry_run:
        await verify(
            report,
            source_side=source_side,
            target_side=target_side,
            source_runs=source_runs,
            target_runs=target_runs,
            source_checkpointer=source_checkpointer,
            target_checkpointer=target_checkpointer,
            columns=columns,
            threads=threads,
        )
    return report


async def verify(
    report: CopyReport,
    *,
    source_side: Side,
    target_side: Side,
    source_runs: Any,
    target_runs: Any,
    source_checkpointer: Any,
    target_checkpointer: Any,
    columns: dict[str, list[str]] | None = None,
    threads: set[str] | None = None,
) -> None:
    """Fill in the target counts and compare the checksums (the target may hold more rows than
    the source only when it held them before; then the checksum cannot match and fails)."""

    columns = columns or _columns()
    _, digest = await _runs_checksum(source_runs)
    target_count, target_digest = await _runs_checksum(target_runs)
    runs = report.table("process_runs")
    runs.target, runs.checksum_ok = target_count, digest == target_digest
    for table, key in SQL_TABLES:
        item = report.table(table)
        item.target = await target_side.count(table)
        item.checksum_ok = await _checksum(source_side, table, columns[table], key) == (
            await _checksum(target_side, table, columns[table], key)
        )
    if source_checkpointer is not None and target_checkpointer is not None:
        item = report.table("checkpoints")
        mine = await _checkpoints(source_checkpointer, threads)
        # The checkpointer's tables may hold other projects' threads (one per database).
        theirs = await _checkpoints(target_checkpointer, _threads_of(mine))
        item.target = len(theirs)
        item.checksum_ok = _fingerprints(mine) == _fingerprints(theirs)


def _fingerprints(
    items: Sequence[Any],
) -> list[tuple[tuple[str, str, str], dict[str, list[tuple[str, Any]]]]]:
    """Each checkpoint's key with its pending writes by task (what `verify` compares)."""

    return [
        (key, _writes_by_task(item))
        for key, item in zip(_checkpoint_keys(items), items, strict=True)
    ]
