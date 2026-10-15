"""Moving a project's data between SQLite and PostgreSQL (AWA_TEST_POSTGRES_DSN): a round trip
keeps the logical content, an interrupted move resumes, a conflicting target is refused, tenants
stay apart, and the store setting changes only after verification."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from test_run_log_recording import automation, run_to_end

from awa.core.dotenv import read_env_file
from awa.errors import StoreConflictError
from awa.persistence import Scope, SqliteCheckpointer
from awa.persistence.runlog import PostgresRunLog
from awa.project_setup.data_store import migrate_store, store_status, use_store
from awa.server._store_copy import SQL_TABLES

pytestmark = pytest.mark.postgres


@pytest.fixture
async def schema(postgres_dsn: str) -> AsyncIterator[str]:
    import psycopg

    name = f"awa_test_m{uuid4().hex[:10]}"
    yield name
    async with await psycopg.AsyncConnection.connect(postgres_dsn, autocommit=True) as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


async def local_project(folder: Path, dsn: str) -> str:
    """A project with one recorded run, a thread owner and a checkpoint in its SQLite files."""

    (folder / ".awa").mkdir(parents=True)
    (folder / ".env").write_text(
        f"AWA_POSTGRES_DSN={dsn}\nAWA_DATA_BACKEND=sqlite\n", encoding="utf-8"
    )
    auto, log = automation(folder / ".awa" / "awa.db", capture=True)
    run = await run_to_end(auto)
    await log.close()
    from awa.agents import Agent
    from awa.persistence.threads import SqliteThreadIndex, ThreadOwner
    from awa.testing import FakeChatModel

    # The checkpointer's tables are shared by the whole test database: a thread of its own.
    thread = f"thread-{folder.name}-{uuid4().hex[:8]}"
    index = SqliteThreadIndex(folder / ".awa" / "awa.db")
    await index.claim("desk", thread, ThreadOwner("acme", "ana"))
    await index.claim_decision("desk", thread, "v1")
    await index.close()
    saver = SqliteCheckpointer(folder / ".awa" / "checkpoints.db")
    await Agent("echo", model=FakeChatModel(["one", "two"]), checkpointer=saver).run(
        "hi", thread_id=thread
    )
    saver.close()
    return run.id


def sqlite_content(path: Path) -> dict[str, list[Any]]:
    from awa.server._store_copy import _columns, logical

    columns = _columns()
    conn = sqlite3.connect(path)
    try:
        found: dict[str, list[Any]] = {}
        for table, key in SQL_TABLES:
            rows = conn.execute(
                f"SELECT {', '.join(columns[table])} FROM {table} ORDER BY {', '.join(key)}"  # noqa: S608
            ).fetchall()
            found[table] = [
                [logical(c, v) for c, v in zip(columns[table], row, strict=True)] for row in rows
            ]
        runs = conn.execute("SELECT id, data FROM process_runs ORDER BY id").fetchall()
        found["process_runs"] = [[i, __import__("json").loads(d)] for i, d in runs]
        return found
    finally:
        conn.close()


def backend(folder: Path) -> str | None:
    return read_env_file(folder / ".env").get("AWA_DATA_BACKEND")


async def test_a_round_trip_keeps_the_logical_content(
    tmp_path: Path, postgres_dsn: str, schema: str
) -> None:
    first = tmp_path / "one"
    run_id = await local_project(first, postgres_dsn)

    there = await migrate_store(first, "postgres", schema=schema)

    assert there.verified and there.switched and backend(first) == "postgres"
    copied = {t["name"]: t for t in there.report["tables"]}
    assert copied["process_runs"]["copied"] == 1
    assert copied["run_step"]["copied"] == 2 and copied["checkpoints"]["copied"] >= 1
    assert all(t["checksum_ok"] for t in there.report["tables"])
    status = await store_status(first, schema=schema)
    assert status.backend == "postgres" and status.counts["step_result"] == 3
    assert postgres_dsn.split("@")[0] not in status.where  # no user or password shown
    log = PostgresRunLog(postgres_dsn, schema=schema)
    assert [s.step_key for s in await log.steps(Scope("acme", "invoices"), run_id)] == [
        "extract",
        "book",
    ]
    assert await log.steps(Scope("globex", "invoices"), run_id) == []  # tenants stay apart
    await log.close()

    second = tmp_path / "two"
    (second / ".awa").mkdir(parents=True)
    (second / ".env").write_text(
        f"AWA_POSTGRES_DSN={postgres_dsn}\nAWA_DATA_BACKEND=postgres\n", encoding="utf-8"
    )
    back = await migrate_store(second, "sqlite", schema=schema)

    assert back.verified and back.switched and backend(second) == "sqlite"
    one, two = sqlite_content(first / ".awa" / "awa.db"), sqlite_content(second / ".awa" / "awa.db")
    assert one == two
    assert one["captured_content"] and one["server_threads"] and one["data_subject_link"]
    saver = SqliteCheckpointer(second / ".awa" / "checkpoints.db")
    assert len(list(saver.list(None))) == copied["checkpoints"]["copied"]
    saver.close()


async def test_an_interrupted_move_resumes_and_switches_only_when_verified(
    tmp_path: Path, postgres_dsn: str, schema: str
) -> None:
    folder = tmp_path / "p"
    await local_project(folder, postgres_dsn)

    async def stop(table: str, done: int) -> None:
        if table == "run_step":
            raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        await migrate_store(folder, "postgres", schema=schema, on_batch=stop)
    assert backend(folder) == "sqlite"

    resumed = await migrate_store(folder, "postgres", schema=schema)

    tables = {t["name"]: t for t in resumed.report["tables"]}
    assert resumed.verified and backend(folder) == "postgres"
    assert tables["process_runs"]["present"] == 1 and tables["run_step"]["present"] == 2


async def test_a_target_with_different_data_is_refused_and_nothing_switches(
    tmp_path: Path, postgres_dsn: str, schema: str
) -> None:
    folder = tmp_path / "p"
    run_id = await local_project(folder, postgres_dsn)
    log = PostgresRunLog(postgres_dsn, schema=schema)
    from datetime import UTC, datetime

    await log.open_run(Scope("globex", "other"), run_id, now=datetime(2020, 1, 1, tzinfo=UTC))
    await log.close()

    with pytest.raises(StoreConflictError, match="run_log_head"):
        await migrate_store(folder, "postgres", schema=schema)
    assert backend(folder) == "sqlite"


async def test_a_dry_run_writes_nothing(tmp_path: Path, postgres_dsn: str, schema: str) -> None:
    folder = tmp_path / "p"
    await local_project(folder, postgres_dsn)

    plan = await migrate_store(folder, "postgres", schema=schema, dry_run=True)

    tables = {t["name"]: t for t in plan.report["tables"]}
    assert not plan.switched and backend(folder) == "sqlite"
    assert tables["run_step"]["source"] == 2 and tables["run_step"]["copied"] == 0
    assert tables["run_step"]["present"] == 0


async def test_use_store_starts_the_database_when_no_dsn_is_set(
    tmp_path: Path, postgres_dsn: str, schema: str
) -> None:
    folder = tmp_path / "p"
    await local_project(folder, postgres_dsn)
    (folder / ".env").write_text("AWA_DATA_BACKEND=sqlite\n", encoding="utf-8")
    started: list[Path] = []
    lines: list[str] = []

    def start(project: Path) -> None:  # stands in for `awa setup` starting Docker
        started.append(project)
        with (project / ".env").open("a", encoding="utf-8") as env:
            env.write(f"AWA_POSTGRES_DSN={postgres_dsn}\n")

    done = await use_store(
        folder, "postgres", schema=schema, say=lines.append, start_database=start
    )

    assert started == [folder] and done is not None and done.switched
    assert any(line.startswith("verified") for line in lines)
    assert any(line.startswith("switched") for line in lines)
    assert all("@" not in line for line in lines)
    assert await use_store(folder, "postgres", schema=schema, say=lines.append) is None
