"""Persisted formats carry a version: older data loads, newer is refused; files of the previous
format still open and resume."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from awa.agents import Agent
from awa.errors import ConfigurationError
from awa.persistence import Scope, SqliteCheckpointer, sqlite
from awa.persistence.ledger import load_migrations
from awa.persistence.runlog import SqliteRunLog
from awa.persistence.sql import SqliteDatabase
from awa.testing import FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper


def _user_version(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def test_checkpoint_database_is_stamped_with_the_format(tmp_path: Path) -> None:
    path = tmp_path / "cp.db"
    SqliteCheckpointer(path).close()

    assert _user_version(path) == sqlite.FORMAT_VERSION


def test_checkpoint_database_without_a_version_loads_and_is_stamped(tmp_path: Path) -> None:
    path = tmp_path / "cp.db"
    SqliteCheckpointer(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=0")  # written before the version was set

    SqliteCheckpointer(path).close()

    assert _user_version(path) == sqlite.FORMAT_VERSION


def test_checkpoint_database_runs_the_migration_of_an_older_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cp.db"
    SqliteCheckpointer(path).close()
    called: list[sqlite3.Connection] = []
    monkeypatch.setattr(sqlite, "FORMAT_VERSION", 2)
    monkeypatch.setattr(sqlite, "_MIGRATIONS", {1: called.append})

    SqliteCheckpointer(path).close()

    assert len(called) == 1 and _user_version(path) == 2


def test_checkpoint_database_of_a_newer_format_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cp.db"
    SqliteCheckpointer(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=99")

    with pytest.raises(ConfigurationError, match=r"format version 99.*reads up to 1"):
        SqliteCheckpointer(path)
    assert _user_version(path) == 99


# Written by the first persisted format (format version 1): `checkpoints.db` holds thread `t-1` of
# an agent paused on an approval (`send` is waiting); `awa.db` holds one process run (`run-1`,
# tenant `acme`, project `invoices`) with a step that waits for approval, one attempt, one result
# and one approval that nobody has decided yet.
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "persisted_v1"
SCOPE = Scope("acme", "invoices")
SENT: list[str] = []


@tool
def send(text: str) -> str:
    """Send a text to the customer."""

    SENT.append(text)
    return f"sent {text!r}"


def mailer(saver: Any) -> Agent:
    # The agent of `make.py`: it decides from the conversation, so a new process continues.
    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "mailer done"
        return tool_call("send", {"text": "hello"})

    return Agent(
        "mailer",
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(send)],
        checkpointer=saver,
    )


@pytest.fixture
def files(tmp_path: Path) -> Path:
    """A copy of the fixtures, so opening them (WAL files, a resume) never changes them."""

    SENT.clear()
    for name in ("awa.db", "checkpoints.db"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    return tmp_path


def test_the_data_file_of_format_1_needs_no_migration_and_its_migrations_are_unchanged(
    files: Path,
) -> None:
    database = SqliteDatabase(files / "awa.db", upgrade=False)  # raises when one is pending
    try:
        found = database.status()
    finally:
        database.close()

    applied = [(s.version, s.name, s.state) for s in found]
    assert applied == [(1, "runs", "applied"), (2, "threads", "applied"), (3, "run_log", "applied")]
    with sqlite3.connect(FIXTURES / "awa.db") as db:
        recorded = dict(db.execute("SELECT version, checksum FROM awa_schema_migrations"))
    for migration in load_migrations("sqlite"):
        assert recorded[migration.version] == migration.checksum


async def test_a_run_of_format_1_reads_back_and_its_approval_can_still_be_decided(
    files: Path,
) -> None:
    database = SqliteDatabase(files / "awa.db")
    log = SqliteRunLog(database)
    try:
        [step] = await log.steps(SCOPE, "run-1")
        [attempt] = await log.attempts(SCOPE, "run-1")
        [result] = await log.results(SCOPE, "run-1")
        [approval] = await log.approvals(SCOPE, "run-1")
        decided = await log.decide_approval(
            SCOPE,
            approval.approval_id,
            decision="approved",
            reviewer="rita",
            reason="checked",
            now=datetime(2026, 1, 16, tzinfo=UTC),
        )

        assert (step.step_key, step.status, step.attempts) == ("extract", "needs_approval", 1)
        assert (attempt.status, result.kind, result.output) == (
            "succeeded",
            "final",
            {"total": 6200},
        )
        assert approval.decision is None and approval.reasons == ("Large.",)
        assert decided.decision == "approved" and decided.reviewer == "rita"
        assert await log.head(Scope("globex", "invoices"), "run-1") is None
    finally:
        await log.close()


async def test_checkpoints_of_format_1_open_and_the_paused_thread_resumes_to_the_end(
    files: Path,
) -> None:
    with sqlite3.connect(files / "checkpoints.db") as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
    saver = SqliteCheckpointer(files / "checkpoints.db", retention=None)
    try:
        done = await mailer(saver).resume("t-1", {"action": "approve"})

        assert done.output == "mailer done" and SENT == ["hello"]
        assert not done.interrupts
    finally:
        saver.close()
    with sqlite3.connect(files / "checkpoints.db") as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)


def test_a_json_column_holds_nan_and_infinities_as_valid_json() -> None:
    import math

    from awa.persistence.runlog.rows import dumped

    text = dumped({"score": math.nan, "rows": [math.inf, -math.inf, 1.5]})

    assert sqlite3.connect(":memory:").execute("SELECT json_valid(?)", (text,)).fetchone() == (1,)
    assert json.loads(text) == {"score": "nan", "rows": ["inf", "-inf", 1.5]}
