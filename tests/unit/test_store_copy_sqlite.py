"""Copying the data between two stores, offline: two SQLite files stand in for SQLite and
PostgreSQL, so the copy, its conflicts, its resume and its verification run without a database."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from test_run_log_recording import automation, run_to_end

from awa.automation import SqliteRunStore
from awa.errors import StoreConflictError
from awa.persistence import Scope
from awa.persistence.checkpointer import SqliteCheckpointer
from awa.persistence.sql import SqliteDatabase
from awa.persistence.threads import SqliteThreadIndex, ThreadOwner
from awa.server import _store_copy
from awa.server._store_copy import (
    SQL_TABLES,
    CopyReport,
    SqliteSide,
    _checkpoint_keys,
    _checkpoints,
    _checksum,
    _threads_of,
    copy_data,
    logical,
    verify,
)


class Source:
    """A SQLite data file with one recorded run, a thread owner and an answered decision."""

    def __init__(self, database: SqliteDatabase, runs: SqliteRunStore, run_id: str) -> None:
        self.database, self.runs, self.run_id = database, runs, run_id
        self.side = SqliteSide(database)


async def source(folder: Path) -> Source:
    auto, log = automation(folder / "source.db", capture=True)
    run = await run_to_end(auto)
    index = SqliteThreadIndex(log.database)
    await index.claim("desk", "t-1", ThreadOwner("acme", "ana", datetime(2026, 3, 2, tzinfo=UTC)))
    await index.claim_decision("desk", "t-1", "v1")
    return Source(log.database, auto.store, run.id)  # type: ignore[arg-type]


class Target:
    def __init__(self, folder: Path, name: str = "target.db") -> None:
        self.database = SqliteDatabase(folder / name)
        self.runs = SqliteRunStore(self.database)
        self.side = SqliteSide(self.database)

    async def table(self, name: str) -> list[tuple[Any, ...]]:
        found: list[tuple[Any, ...]] = await self.database.read(
            lambda conn: conn.execute(f"SELECT * FROM {name}").fetchall()  # noqa: S608
        )
        return found

    async def change(self, statement: str, *values: Any) -> None:
        await self.database.write(lambda conn: conn.execute(statement, values))


def normalized(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[list[Any]]:
    found = [[logical(c, v) for c, v in zip(columns, row, strict=True)] for row in rows]
    return sorted(found, key=repr)


async def copy(src: Source, dst: Target, **options: Any) -> CopyReport:
    return await copy_data(
        source_side=src.side,
        target_side=dst.side,
        source_runs=src.runs,
        target_runs=dst.runs,
        **options,
    )


def by_name(report: CopyReport) -> dict[str, Any]:
    return {table.name: table for table in report.tables}


async def test_a_copy_is_complete_and_verified(tmp_path: Path) -> None:
    src, dst = await source(tmp_path), Target(tmp_path)

    report = await copy(src, dst)

    tables = by_name(report)
    assert report.verified and all(t.checksum_ok for t in report.tables)
    assert tables["process_runs"].copied == 1 and tables["run_step"].copied == 2
    assert tables["server_threads"].copied == 1 and tables["server_decisions"].copied == 1
    for table, _ in SQL_TABLES:
        assert tables[table].target == tables[table].source
    assert await dst.runs.get(src.run_id) == await src.runs.get(src.run_id)


async def test_a_second_run_copies_nothing_and_reports_every_row_present(tmp_path: Path) -> None:
    src, dst = await source(tmp_path), Target(tmp_path)
    first = await copy(src, dst)

    second = await copy(src, dst)

    assert second.verified
    assert all(t.copied == 0 for t in second.tables)
    assert {t.name: t.present for t in second.tables} == {t.name: t.copied for t in first.tables}


async def test_a_different_row_under_a_source_key_is_a_conflict_and_the_target_stays(
    tmp_path: Path,
) -> None:
    src, dst = await source(tmp_path), Target(tmp_path)
    await dst.database.write(
        lambda conn: conn.execute(
            "INSERT INTO run_log_head (run_id, tenant_id, project_id, capture, opened_at)"
            " VALUES (?, 'globex', 'other', 0, '2020-01-01T00:00:00.000000Z')",
            (src.run_id,),
        )
    )
    before = await dst.table("run_log_head")

    with pytest.raises(StoreConflictError, match="run_log_head"):
        await copy(src, dst)

    assert await dst.table("run_log_head") == before
    assert await dst.table("run_step") == []


async def test_a_row_refused_by_another_unique_column_is_a_conflict() -> None:
    class Side:
        async def count(self, table: str) -> int:
            return 1

        async def rows(self, *args: Any) -> list[tuple[Any, ...]]:
            return [("r-1", "s-1")]

        async def insert(self, *args: Any) -> tuple[int, list[Any]]:
            return 0, [None]  # ON CONFLICT DO NOTHING on another unique column, no row has the key

        def normal(self, column: str, value: Any) -> Any:
            return value

        stored = normal

    with pytest.raises(StoreConflictError, match="run_step row"):
        await _store_copy._copy_table(
            Side(),  # type: ignore[arg-type]
            Side(),  # type: ignore[arg-type]
            "run_step",
            ("run_id", "step_key"),
            ("run_id", "step_key"),
            _store_copy.TableReport("run_step"),
            dry_run=False,
            on_batch=None,
        )


async def test_a_conflicting_process_run_is_refused(tmp_path: Path) -> None:
    src, dst = await source(tmp_path), Target(tmp_path)
    stored = await src.runs.get(src.run_id)
    assert stored is not None
    await dst.runs.create(stored.model_copy(update={"input": "something else"}))

    with pytest.raises(StoreConflictError, match="process run"):
        await copy(src, dst)


async def test_rows_are_copied_in_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src, dst = await source(tmp_path), Target(tmp_path)
    monkeypatch.setattr(_store_copy, "BATCH", 2)
    index = SqliteThreadIndex(src.database)
    for number in range(2, 6):  # five thread owners in all, with the first one
        await index.claim("desk", f"t-{number}", ThreadOwner("acme", "ana"))
    for number in range(2, 5):  # four answered decisions in all
        await index.claim_decision("desk", "t-1", f"v{number}")

    report = await copy(src, dst)

    tables = by_name(report)
    assert report.verified
    assert tables["server_threads"].copied == 5 and tables["server_decisions"].copied == 4
    assert len(await dst.table("server_threads")) == 5


async def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    src, dst = await source(tmp_path), Target(tmp_path)

    report = await copy(src, dst, dry_run=True)

    assert report.dry_run and not report.verified
    assert by_name(report)["run_step"].source == 2
    for table, _ in SQL_TABLES:
        assert await dst.table(table) == []
    assert await dst.runs.list(limit=10) == []


async def test_a_corrupted_target_row_fails_the_verification(tmp_path: Path) -> None:
    src, dst = await source(tmp_path), Target(tmp_path)
    report = await copy(src, dst)
    assert report.verified

    await dst.change("UPDATE run_step SET message = 'tampered' WHERE step_key = 'book'")
    await verify(
        report,
        source_side=src.side,
        target_side=dst.side,
        source_runs=src.runs,
        target_runs=dst.runs,
        source_checkpointer=None,
        target_checkpointer=None,
    )

    assert by_name(report)["run_step"].checksum_ok is False
    assert by_name(report)["run_log_head"].checksum_ok is True
    assert not report.verified


def test_the_logical_form_of_times_json_and_booleans() -> None:
    moment = datetime(2026, 3, 2, 9, 30, 1, 5, tzinfo=UTC)

    assert logical("created_at", moment) == "2026-03-02T09:30:01.000005Z"
    assert logical("created_at", "2026-03-02T09:30:01.000005Z") == "2026-03-02T09:30:01.000005Z"
    assert logical("output", '{"b": 1, "a": [2]}') == '{"a": [2], "b": 1}'
    assert logical("output", {"b": 1, "a": [2]}, decoded=True) == '{"a": [2], "b": 1}'
    assert logical("capture", 1) == 1 and logical("capture", True) == 1
    assert logical("title", "text") == "text" and logical("ended_at", None) is None


class Shuffled:
    """A side that returns the rows of a table in a different order, as another collation."""

    def __init__(self, side: SqliteSide, reverse: bool) -> None:
        self._side, self._reverse = side, reverse

    async def rows(
        self, table: str, columns: Sequence[str], key: Sequence[str], after: Any
    ) -> list[Any]:
        found: list[Any] = await self._side.rows(table, columns, key, after)
        return found[::-1] if self._reverse else found

    def normal(self, column: str, value: Any) -> Any:
        return self._side.normal(column, value)


async def test_the_table_checksum_does_not_depend_on_the_order_of_the_rows(tmp_path: Path) -> None:
    src = await source(tmp_path)
    columns = _store_copy._columns()["run_step"]

    straight = await _checksum(
        Shuffled(src.side, False), "run_step", columns, ("run_id", "step_key")
    )  # type: ignore[arg-type]
    reversed_ = await _checksum(
        Shuffled(src.side, True), "run_step", columns, ("run_id", "step_key")
    )  # type: ignore[arg-type]

    assert straight == reversed_


async def test_the_pending_writes_of_one_task_arrive_in_order_and_are_verified(
    tmp_path: Path,
) -> None:
    source_saver = InMemorySaver()
    base: Any = {"configurable": {"thread_id": "t-9", "checkpoint_ns": ""}}
    checkpoint = {
        "v": 4,
        "id": "0001",
        "ts": "2026-03-02T09:00:00+00:00",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": [],
    }
    saved = await source_saver.aput(base, checkpoint, {"source": "loop", "step": 1}, {})  # type: ignore[arg-type]
    await source_saver.aput_writes(saved, [("a", 1), ("b", 2), ("c", 3)], "task-1")
    await source_saver.aput_writes(saved, [("a", 10)], "task-2")
    target_saver = SqliteCheckpointer(tmp_path / "ckpt.db", retention=None)
    src, dst = await source(tmp_path), Target(tmp_path)

    report = await copy(
        src, dst, source_checkpointer=source_saver, target_checkpointer=target_saver
    )

    checkpoints = by_name(report)["checkpoints"]
    assert checkpoints.copied == 1 and checkpoints.checksum_ok and report.verified
    [copied] = await _checkpoints(target_saver, {"t-9"})
    assert [w[1:] for w in copied.pending_writes if w[0] == "task-1"] == [
        ("a", 1),
        ("b", 2),
        ("c", 3),
    ]
    assert [w[1:] for w in copied.pending_writes if w[0] == "task-2"] == [("a", 10)]
    target_saver.close()


async def test_a_checkpoint_whose_writes_differ_fails_the_verification(tmp_path: Path) -> None:
    source_saver, target_saver = InMemorySaver(), InMemorySaver()
    base: Any = {"configurable": {"thread_id": "t-9", "checkpoint_ns": ""}}
    checkpoint = {
        "v": 4,
        "id": "0001",
        "ts": "2026-03-02T09:00:00+00:00",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": [],
    }
    for saver, value in ((source_saver, 1), (target_saver, 2)):
        saved = await saver.aput(base, checkpoint, {"source": "loop", "step": 1}, {})  # type: ignore[arg-type]
        await saver.aput_writes(saved, [("a", value)], "task-1")
    src, dst = await source(tmp_path), Target(tmp_path)
    report = await copy(src, dst)

    await verify(
        report,
        source_side=src.side,
        target_side=dst.side,
        source_runs=src.runs,
        target_runs=dst.runs,
        source_checkpointer=source_saver,
        target_checkpointer=target_saver,
    )

    assert by_name(report)["checkpoints"].checksum_ok is False


async def test_the_checkpoint_helpers_list_threads_and_keys() -> None:
    saver = InMemorySaver()
    checkpoint = {
        "v": 4,
        "id": "0001",
        "ts": "2026-03-02T09:00:00+00:00",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
        "updated_channels": [],
    }
    for thread in ("t-2", "t-1", "t-3"):
        config: Any = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
        await saver.aput(config, checkpoint, {"source": "loop", "step": 1}, {})  # type: ignore[arg-type]

    everything = await _checkpoints(saver)
    chosen = await _checkpoints(saver, {"t-1", "t-3", "t-missing"})

    assert _threads_of(everything) == {"t-1", "t-2", "t-3"}
    assert _checkpoint_keys(chosen) == [("t-1", "", "0001"), ("t-3", "", "0001")]


async def test_an_interrupted_copy_resumes_and_a_round_trip_keeps_the_content(
    tmp_path: Path,
) -> None:
    src, middle = await source(tmp_path), Target(tmp_path, "middle.db")

    async def stop(table: str, done: int) -> None:
        if table == "run_step":
            raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        await copy(src, middle, on_batch=stop)
    resumed = await copy(src, middle)
    last = Target(tmp_path, "last.db")
    back = await copy_data(
        source_side=middle.side,
        target_side=last.side,
        source_runs=middle.runs,
        target_runs=last.runs,
    )

    tables = by_name(resumed)
    assert resumed.verified and back.verified
    assert tables["process_runs"].present == 1 and tables["run_step"].present == 2
    columns = _store_copy._columns()
    for table, _ in SQL_TABLES:
        mine = await src.database.read(
            lambda conn, name=table: conn.execute(f"SELECT * FROM {name}").fetchall()  # noqa: S608
        )
        assert normalized(columns[table], await last.table(table)) == normalized(
            columns[table], mine
        ), table


async def test_tenants_stay_apart_after_a_copy(tmp_path: Path) -> None:
    from awa.persistence.runlog import SqliteRunLog

    src, dst = await source(tmp_path), Target(tmp_path)
    await copy(src, dst)
    log = SqliteRunLog(dst.database)

    assert [s.step_key for s in await log.steps(Scope("acme", "invoices"), src.run_id)] == [
        "extract",
        "book",
    ]
    assert await log.steps(Scope("globex", "invoices"), src.run_id) == []
