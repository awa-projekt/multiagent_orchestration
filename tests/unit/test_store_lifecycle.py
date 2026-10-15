"""Retention and erasure with physical deletion, the SQLite backup, and the `awa store` commands."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_run_log_recording import ANSWER, PROMPT, automation

from awa.errors import ConfigurationError
from awa.persistence import Scope
from awa.persistence.runlog import SqliteDatabase
from awa.project_setup.data_store import backup_store, erase, store_status, sweep

SCOPE = Scope("acme", "invoices")


async def project_with_runs(folder: Path) -> dict[str, str]:
    """Two finished runs (started by ana and bob, both decided by rita) and one waiting run."""

    (folder / ".awa").mkdir(parents=True)
    # The same pseudonym key the test automation links people with.
    (folder / ".env").write_text(
        "AWA_DATA_BACKEND=sqlite\nAWA_PSEUDONYM_KEY=test-pseudonym-key-of-32-bytes!!\n",
        encoding="utf-8",
    )
    ids: dict[str, str] = {}
    for who in ("ana", "bob", "cid"):  # a fresh system each time: its models are scripted
        auto, log = automation(folder / ".awa" / "awa.db", capture=True)
        submitted = await auto.submit(
            {"text": f"Check this invoice. {PROMPT}"}, actor=who, tenant="acme"
        )
        ids[who] = submitted.run.id
        [paused] = await auto.run_pending()
        assert paused.status == "waiting"
        if who != "cid":
            await auto.decide(ids[who], {"action": "approve", "note": "ok"}, actor="rita")
            [done] = await auto.run_pending()
            assert done.status == "succeeded"
        await log.close()
        await auto.store.close()
    traces = folder / ".awa" / "traces"
    traces.mkdir()
    for run_id in ids.values():
        (traces / f"{run_id}.a1.jsonl").write_text("{}\n", encoding="utf-8")
    return ids


def rows(folder: Path, table: str, run_id: str) -> int:
    conn = sqlite3.connect(folder / ".awa" / "awa.db")
    try:
        column = "id" if table == "process_runs" else "run_id"
        query = f"SELECT COUNT(*) FROM {table} WHERE {column} = ?"  # noqa: S608
        return int(conn.execute(query, (run_id,)).fetchone()[0])
    finally:
        conn.close()


async def test_erasing_a_person_deletes_only_their_runs(tmp_path: Path) -> None:
    ids = await project_with_runs(tmp_path)

    erased = await erase(tmp_path, automation="invoices", tenant="acme", person="ana")

    assert erased.runs == [ids["ana"]] and erased.traces == 1
    for table in ("process_runs", "run_log_head", "run_step", "step_result", "approval"):
        assert rows(tmp_path, table, ids["ana"]) == 0, table
        assert rows(tmp_path, table, ids["bob"]) >= 1, table
    assert not (tmp_path / ".awa" / "traces" / f"{ids['ana']}.a1.jsonl").exists()
    assert (tmp_path / ".awa" / "traces" / f"{ids['bob']}.a1.jsonl").exists()


async def test_erasing_a_reviewer_deletes_every_run_they_decided(tmp_path: Path) -> None:
    ids = await project_with_runs(tmp_path)

    erased = await erase(tmp_path, automation="invoices", tenant="acme", person="rita")

    assert sorted(erased.runs) == sorted([ids["ana"], ids["bob"]])
    assert rows(tmp_path, "process_runs", ids["cid"]) == 1


async def test_erasing_an_automation_leaves_no_trace_of_its_content_in_the_file(
    tmp_path: Path,
) -> None:
    ids = await project_with_runs(tmp_path)
    data = b"".join(p.read_bytes() for p in (tmp_path / ".awa").glob("awa.db*"))
    assert ANSWER.encode() in data  # captured, before

    erased = await erase(tmp_path, automation="invoices", tenant="acme", everything=True)

    assert sorted(erased.runs) == sorted(ids.values())
    data = b"".join(p.read_bytes() for p in (tmp_path / ".awa").glob("awa.db*"))
    for sentinel in (PROMPT, ANSWER):
        assert sentinel.encode() not in data, sentinel


async def test_erasing_one_run(tmp_path: Path) -> None:
    ids = await project_with_runs(tmp_path)

    erased = await erase(tmp_path, automation="invoices", tenant="acme", run=ids["bob"])

    assert erased.runs == [ids["bob"]]
    assert rows(tmp_path, "process_runs", ids["bob"]) == 0
    assert rows(tmp_path, "process_runs", ids["ana"]) == 1


@pytest.mark.parametrize("run", ["*", "../x", "unknown"])
async def test_erasing_an_unknown_run_or_a_pattern_deletes_nothing(
    tmp_path: Path, run: str
) -> None:
    await project_with_runs(tmp_path)
    traces = sorted((tmp_path / ".awa" / "traces").glob("*"))

    with pytest.raises(ConfigurationError, match="No run"):
        await erase(tmp_path, automation="invoices", tenant="acme", run=run)

    assert sorted((tmp_path / ".awa" / "traces").glob("*")) == traces and traces


async def test_erase_needs_exactly_one_choice(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="exactly one"):
        await erase(tmp_path, automation="invoices", run="x", everything=True)


async def test_the_sweep_deletes_old_finished_runs_and_keeps_waiting_ones(tmp_path: Path) -> None:
    ids = await project_with_runs(tmp_path)

    kept = await sweep(tmp_path, days=30, now=datetime(2026, 3, 2, 10, tzinfo=UTC))
    later = await sweep(
        tmp_path, days=30, now=datetime(2026, 3, 2, tzinfo=UTC) + timedelta(days=31)
    )

    assert kept.runs == []
    assert sorted(later.runs) == sorted([ids["ana"], ids["bob"]])
    assert rows(tmp_path, "process_runs", ids["cid"]) == 1  # still waiting for a decision
    with pytest.raises(ConfigurationError, match="AWA_RUN_RETENTION_DAYS"):
        await sweep(tmp_path)


async def test_backup_writes_consistent_copies(tmp_path: Path) -> None:
    ids = await project_with_runs(tmp_path)

    written = await asyncio.to_thread(backup_store, tmp_path, tmp_path / "backup")

    assert [p.name for p in written] == ["awa.db"]
    conn = sqlite3.connect(tmp_path / "backup" / "awa.db")
    assert conn.execute("SELECT COUNT(*) FROM process_runs").fetchone() == (len(ids),)
    conn.close()
    with pytest.raises(Exception, match="exists"):
        await asyncio.to_thread(backup_store, tmp_path, tmp_path / "backup")


async def test_awa_store_status_and_backup_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from awa.cli import main

    await project_with_runs(tmp_path)
    capsys.readouterr()

    code = await asyncio.to_thread(main, ["store", "status", str(tmp_path)])
    out = capsys.readouterr().out
    status = await store_status(tmp_path)

    assert code == 0 and out.startswith("store: sqlite")
    assert status.counts["process_runs"] == 3 and status.counts["run_step"] >= 4
    assert status.last_migration is not None and "0003_run_log" in status.last_migration
    code = await asyncio.to_thread(main, ["store", "backup", str(tmp_path / "b"), str(tmp_path)])
    assert code == 0 and (tmp_path / "b" / "awa.db").is_file()


async def test_erasing_many_runs_needs_confirm_and_a_sweep_at_least_one_day(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from awa.cli import main

    ids = await project_with_runs(tmp_path)
    erase_all = ["store", "erase", "--automation", "invoices", "--tenant", "acme", "--all"]

    assert await asyncio.to_thread(main, [*erase_all, str(tmp_path)]) == 3
    assert "--confirm" in capsys.readouterr().err
    assert rows(tmp_path, "process_runs", ids["ana"]) == 1
    with pytest.raises(SystemExit):
        main(["store", "sweep", "--days", "0", str(tmp_path)])
    assert await asyncio.to_thread(main, [*erase_all, "--confirm", str(tmp_path)]) == 0
    assert rows(tmp_path, "process_runs", ids["ana"]) == 0


def files(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*"))


async def test_the_read_commands_refuse_a_folder_without_data_and_create_nothing(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError, match="no awa data in"):
        await store_status(tmp_path)
    with pytest.raises(ConfigurationError, match="no awa data in"):
        await sweep(tmp_path, days=30)
    with pytest.raises(ConfigurationError, match="no awa data in"):
        await erase(tmp_path, automation="invoices", everything=True)
    with pytest.raises(ConfigurationError, match="no awa data in"):
        await asyncio.to_thread(backup_store, tmp_path, tmp_path / "backup")

    assert files(tmp_path) == []


async def test_reading_a_project_never_makes_the_pseudonym_key(tmp_path: Path) -> None:
    (tmp_path / ".awa").mkdir()
    SqliteDatabase(tmp_path / ".awa" / "awa.db").close()

    status = await store_status(tmp_path)
    swept = await sweep(tmp_path, days=30)
    await asyncio.to_thread(backup_store, tmp_path, tmp_path / "backup")

    assert status.counts["process_runs"] == 0 and swept.runs == []
    assert not (tmp_path / ".env").exists()


async def test_erasing_a_person_makes_the_key_once(tmp_path: Path) -> None:
    (tmp_path / ".awa").mkdir()
    SqliteDatabase(tmp_path / ".awa" / "awa.db").close()

    await erase(tmp_path, automation="invoices", person="ana")
    first = (tmp_path / ".env").read_text(encoding="utf-8")
    await erase(tmp_path, automation="invoices", person="ana")

    assert first.count("AWA_PSEUDONYM_KEY=") == 1
    assert (tmp_path / ".env").read_text(encoding="utf-8") == first


async def test_a_run_of_another_tenant_or_automation_is_refused_and_kept(tmp_path: Path) -> None:
    ids = await project_with_runs(tmp_path)

    with pytest.raises(ConfigurationError, match="No run"):
        await erase(tmp_path, automation="invoices", tenant="other", run=ids["ana"])
    with pytest.raises(ConfigurationError, match="No run"):
        await erase(tmp_path, automation="payments", tenant="acme", run=ids["ana"])

    assert rows(tmp_path, "process_runs", ids["ana"]) == 1
    assert rows(tmp_path, "run_log_head", ids["ana"]) == 1


@pytest.mark.parametrize("days", [0, -1])
async def test_a_sweep_needs_at_least_one_day(tmp_path: Path, days: int) -> None:
    await project_with_runs(tmp_path)

    with pytest.raises(ConfigurationError, match="days must be at least 1"):
        await sweep(tmp_path, days=days)


async def test_the_global_project_option_is_honoured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from awa.cli import main

    await project_with_runs(tmp_path)
    capsys.readouterr()

    code = await asyncio.to_thread(main, ["--project", str(tmp_path), "store", "status"])

    assert code == 0 and capsys.readouterr().out.startswith("store: sqlite")


async def test_status_of_a_folder_without_data_says_so_through_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from awa.cli import main

    code = await asyncio.to_thread(main, ["--project", str(tmp_path), "store", "status"])

    assert code != 0 and "no awa data in" in capsys.readouterr().err
    assert files(tmp_path) == []
