"""Traces of process runs and the commands that use them: `awa run --trace`, `awa runs trace`,
`awa runs export-cases`; and the evaluation of an exported case."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from _helpers.automation import ScriptedSystem
from _helpers.fake_secrets import fake_secret
from _helpers.trace_helpers import PUBLISHED, desk_team
from awa import Agent
from awa._internal import aio as asyncio_compat
from awa.automation import Automation, InMemoryRunStore, ProcessRun, RetryPolicy, SqliteRunStore
from awa.automation.trace_cases import JUDGE_CRITERIA
from awa.cli import build_parser, main
from awa.errors import ConfigurationError
from awa.evals import Dataset, evaluate
from awa.models import ModelRegistry
from awa.observability import EventRecorder, TraceStore, read_trace
from awa.observability.trace_view import build_tree
from awa.runtime import use_global_subscriber
from awa.testing import FakeChatModel, FakeClock, FunctionChatModel, structured_output


@pytest.fixture(autouse=True)
def _clear() -> None:
    PUBLISHED.clear()


def automation(system: Any, store: Any, **options: Any) -> Automation:
    options.setdefault("clock", FakeClock())
    options.setdefault("input", "{payload.topic}")
    return Automation(system, store=store, **options)


async def finish(auto: Automation, *, approver: str = "alice") -> ProcessRun:
    """Submit one run and see it through every approval."""

    await auto.submit({"topic": "Brief on Debian"})
    run = (await auto.run_pending())[-1]
    while run.status == "waiting":
        await auto.decide(run.id, {"action": "approve"}, actor=approver)
        run = (await auto.run_pending())[-1]
    return run


def store_in(tmp_path: Path) -> SqliteRunStore:
    return SqliteRunStore(tmp_path / ".awa" / "awa.db")


def traces_of(tmp_path: Path) -> TraceStore:
    return TraceStore(tmp_path / ".awa" / "traces")


async def test_every_attempt_has_its_own_trace_next_to_the_run_store(tmp_path: Path) -> None:
    attempts = {"n": 0}

    def flaky(call: Any) -> Any:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("the model service hiccuped")
        return "answered"

    clock = FakeClock()
    auto = automation(
        Agent("clerk", model=FunctionChatModel(flaky)),
        store_in(tmp_path),
        retry=RetryPolicy(max_attempts=2, initial=1, retry_on_types=(Exception,)),
        clock=clock,
    )
    await auto.submit({"topic": "go"})
    [first] = await auto.run_pending()
    clock.advance(seconds=5)
    [second] = await auto.run_pending()

    files = traces_of(tmp_path).attempts(first.id)

    assert (first.status, second.status) == ("queued", "succeeded")
    assert [f.name for f in files] == [f"{first.id}.a1.jsonl", f"{first.id}.a2.jsonl"]
    assert first.trace == files[0].name and second.trace == files[1].name
    one, two = read_trace(files[0]), read_trace(files[1])
    assert [d.meta["attempt"] for d in (one, two)] == [1, 2]
    assert one.outcome == "failed" and "hiccuped" in (one.error or "")
    assert any(e["type"] == "error" for e in one.events)
    assert two.outcome == "ok" and any(e["type"] == "agent_finished" for e in two.events)
    stored = await auto.store.get(first.id)
    assert stored is not None and stored.trace == files[1].name  # survives in the run file


async def test_a_run_resumed_after_approvals_continues_its_trace_and_counts_like_a_recorder(
    tmp_path: Path,
) -> None:
    auto = automation(desk_team(), store_in(tmp_path))
    events = EventRecorder()
    with use_global_subscriber(events):
        run = await finish(auto)

    [path] = traces_of(tmp_path).files()
    data = read_trace(path)
    totals = build_tree(data).totals

    assert run.status == "succeeded" and run.trace == path.name == f"{run.id}.a1.jsonl"
    assert [e.get("outcome") for e in data.ends] == ["paused", "paused", "ok"]
    assert data.meta["automation"] == "desk" and "actor" not in data.meta
    assert data.header["root_run_id"] == run.id
    assert totals["tool_calls"] == len([e for e in events if e.type == "tool_call"]) == 8
    assert totals["delegations"] + totals["consults"] == len(
        [e for e in events if e.type == "delegation"]
    )
    assert totals["approvals_decided"] == 2 and totals["agents"] == 6
    assert "Result: finished" in build_tree(data).render()


async def test_a_failure_outside_the_run_still_leaves_a_trace_with_the_reason(
    tmp_path: Path,
) -> None:
    system = ScriptedSystem(errors=[ValueError("bad payload, password=hunter2hunter2")])
    auto = automation(system, store_in(tmp_path))
    await auto.submit({"topic": "go"})
    [run] = await auto.run_pending()

    [path] = traces_of(tmp_path).files()
    data = read_trace(path)

    assert run.status == "failed" and run.trace == path.name
    assert data.outcome == "failed" and "bad payload" in (data.error or "")
    assert "hunter2hunter2" not in path.read_text(encoding="utf-8")
    assert "Result: failed (ValueError: bad payload" in build_tree(data).render()


async def test_the_trace_records_who_and_what_for_erasure(tmp_path: Path) -> None:
    auto = automation(
        ScriptedSystem(), store_in(tmp_path), tenant="acme", thread_id="case-{payload.topic}"
    )
    await auto.submit({"topic": "go"}, actor="u-42")
    [run] = await auto.run_pending()

    meta = read_trace(traces_of(tmp_path).find(run.id)[0]).meta

    assert (meta["actor"], meta["tenant"], meta["thread_id"]) == ("u-42", "acme", "case-go")
    assert (meta["automation"], meta["process_run"], meta["trigger"]) == ("desk", run.id, "api")


async def test_runs_in_memory_are_not_traced_and_every_other_store_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    memory = automation(ScriptedSystem(), InMemoryRunStore())
    filed = automation(ScriptedSystem(), store_in(tmp_path))

    assert memory.traces is None
    assert filed.traces is not None
    assert (filed.traces.directory, filed.traces.level) == (tmp_path / ".awa" / "traces", "summary")
    assert filed.traces.max_age is not None and filed.traces.max_files == 2000
    await memory.submit({"topic": "go"})
    await memory.run_pending()
    assert not (tmp_path / ".awa" / "traces").exists()


async def test_the_level_comes_from_the_argument_then_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    store = store_in(tmp_path)

    assert automation(ScriptedSystem(), store, trace="off").traces is None
    full = automation(ScriptedSystem(), InMemoryRunStore(), trace="full").traces
    assert full is not None and full.level == "full" and full.directory == Path(".awa/traces")
    own = TraceStore(tmp_path / "mine", level="summary")
    assert automation(ScriptedSystem(), store, trace=own).traces is own
    monkeypatch.setenv("AWA_TRACE_LEVEL", "full")
    env = automation(ScriptedSystem(), store).traces
    assert env is not None and env.level == "full"
    monkeypatch.setenv("AWA_TRACE_LEVEL", "off")
    assert automation(ScriptedSystem(), store).traces is None
    monkeypatch.setenv("AWA_TRACE_LEVEL", "loud")
    with pytest.raises(ConfigurationError, match="AWA_TRACE_LEVEL: Unknown trace level"):
        automation(ScriptedSystem(), store)
    monkeypatch.delenv("AWA_TRACE_LEVEL")
    with pytest.raises(ConfigurationError, match="trace: Unknown trace level"):
        automation(ScriptedSystem(), store, trace="loud")  # type: ignore[arg-type]


async def test_an_unwritable_trace_folder_never_fails_the_run(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the folder should be", encoding="utf-8")
    auto = automation(ScriptedSystem(), store_in(tmp_path), trace=TraceStore(blocked / "traces"))

    await auto.submit({"topic": "go"})
    [run] = await auto.run_pending()

    assert run.status == "succeeded"


def test_old_run_records_load_without_a_trace() -> None:
    run = ProcessRun.model_validate({"id": "proc_old", "automation": "desk", "status": "queued"})

    assert run.trace is None
    assert ProcessRun(automation="desk", trace="proc_1.a1.jsonl").trace == "proc_1.a1.jsonl"


def make_run(tmp_path: Path, *, retry: bool = False) -> ProcessRun:
    """A finished run of the desk team in `tmp_path/.awa/awa.db`."""

    async def go() -> ProcessRun:
        store = store_in(tmp_path)
        try:
            return await finish(automation(desk_team(), store))
        finally:
            await store.close()

    return asyncio_compat.run(go())


def runs_in(tmp_path: Path, *words: str) -> list[str]:
    return ["runs", *words, "--store", str(tmp_path / ".awa" / "awa.db")]


def test_awa_runs_trace_shows_the_tree_in_plain_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = make_run(tmp_path)

    assert main(runs_in(tmp_path, "trace", run.id)) == 0

    out = capsys.readouterr().out
    assert out.startswith(f"Trace of run {run.id}, attempt 1\n")
    assert "Result: finished" in out
    for line in (
        "team desk",
        "agent lead",
        "delegates to researcher -> ok",
        "consults researcher -> ok",
        "hands over to writer",
        'tool web_search {"query": "debian first release"} -> ok',
        "approval for publish: approved by alice",
    ):
        assert line in out, line
    assert "REDACTED" in out and fake_secret("sk-", "abcdefghijklmnopqrstuvwxyz123456") not in out


def test_awa_runs_trace_json_has_the_same_numbers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = make_run(tmp_path)

    assert main(runs_in(tmp_path, "trace", run.id, "--json")) == 0

    document = json.loads(capsys.readouterr().out)
    assert document["format"] == "awa.trace.tree/v1" and document["outcome"] == "ok"
    assert document["totals"]["model_calls"] == 12
    [path] = traces_of(tmp_path).files()
    replies = [e for e in read_trace(path).events if e["type"] == "model_response"]
    assert document["totals"]["tokens"] == sum(
        e["input_tokens"] + e["output_tokens"] for e in replies
    )
    assert document["roots"][0]["agent"] == "desk"
    assert document["meta"]["process_run"] == run.id


def test_awa_runs_trace_picks_an_attempt_or_shows_all(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = make_run(tmp_path)
    store = traces_of(tmp_path)
    first = store.attempts(run.id)[0]
    second = first.with_name(f"{run.id}.a2.jsonl")
    second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")

    assert main(runs_in(tmp_path, "trace", run.id)) == 0
    last = capsys.readouterr().out
    assert "This run has 2 attempts; this is the last one" in last
    assert main(runs_in(tmp_path, "trace", run.id, "--attempt", "1")) == 0
    assert "This run has" not in capsys.readouterr().out
    assert main(runs_in(tmp_path, "trace", run.id, "--all")) == 0
    assert capsys.readouterr().out.count("Trace of run") == 2
    assert main(runs_in(tmp_path, "trace", run.id, "--all", "--json")) == 0
    assert len(json.loads(capsys.readouterr().out)) == 2
    assert main(runs_in(tmp_path, "trace", run.id, "--attempt", "7")) == 1
    assert "has no attempt 7" in capsys.readouterr().err


def test_awa_runs_trace_explains_where_it_looked_when_there_is_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(runs_in(tmp_path, "trace", "proc_missing")) == 1

    err = capsys.readouterr().err
    assert "No trace for 'proc_missing' in" in err and "--traces DIR" in err


def test_awa_runs_trace_reads_a_file_and_a_folder_you_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = make_run(tmp_path)
    [path] = traces_of(tmp_path).files()

    assert main(["runs", "trace", str(path)]) == 0
    assert f"Trace of run {run.id}" in capsys.readouterr().out
    assert main(["runs", "trace", run.id, "--traces", str(path.parent)]) == 0
    assert "Result: finished" in capsys.readouterr().out


def test_awa_runs_trace_costs_a_run_with_your_prices(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = make_run(tmp_path)
    prices = tmp_path / "prices.yaml"
    prices.write_text("unknown: {input_per_mtok: 2, output_per_mtok: 8}\n", encoding="utf-8")

    assert main(runs_in(tmp_path, "trace", run.id, "--prices", str(prices))) == 0

    out = capsys.readouterr().out
    assert "cost 0." in out and "no prices" not in out


def test_the_worker_takes_a_trace_level() -> None:
    parsed = build_parser().parse_args(["worker", "x.yaml", "--trace-level", "off"])

    assert parsed.trace_level == "off"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["worker", "x.yaml", "--trace-level", "loud"])


def test_awa_runs_export_cases_writes_a_dataset_the_eval_loader_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = make_run(tmp_path)
    out = tmp_path / "evals" / "from_runs.yaml"

    assert main(runs_in(tmp_path, "export-cases", run.id, "--out", str(out))) == 0

    assert f"wrote 1 case(s) to {out}" in capsys.readouterr().out
    dataset = Dataset.load(out)
    [case] = dataset.cases
    assert case.id == f"from-{run.id}" and case.input == "Brief on Debian"
    assert case.expected == "final summary"
    assert case.decisions == (
        {"action": "approve", "tool": "publish"},
        {"action": "approve", "tool": "publish"},
    )
    assert case.scorers == (
        "tool:web_search",
        "tool:publish",
        "delegated:researcher",
        "delegated:analyst",
        "handoff:writer",
        f"judge:{JUDGE_CRITERIA}",
    )
    assert case.tags == ("from-runs", "desk") and case.metadata["run_id"] == run.id


def test_exporting_again_keeps_the_cases_already_in_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = make_run(tmp_path)
    out = tmp_path / "from_runs.yaml"
    assert main(runs_in(tmp_path, "export-cases", run.id, "--out", str(out), "--no-judge")) == 0
    edited = out.read_text(encoding="utf-8").replace("name: from_runs", "name: my-suite", 1)
    out.write_text(edited, encoding="utf-8")
    capsys.readouterr()

    assert main(runs_in(tmp_path, "export-cases", run.id, "--out", str(out))) == 0

    assert f"kept the case from-{run.id} already in" in capsys.readouterr().err
    dataset = Dataset.load(out)
    assert len(dataset) == 1 and dataset.name == "my-suite"
    assert not any(s.startswith("judge:") for s in dataset.cases[0].scorers)


def test_export_skips_runs_it_cannot_make_a_case_of(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def go() -> ProcessRun:
        store = store_in(tmp_path)
        try:
            auto = automation(ScriptedSystem(errors=[ValueError("boom")]), store)
            await auto.submit({"topic": "go"})
            return (await auto.run_pending())[-1]
        finally:
            await store.close()

    failed = asyncio_compat.run(go())
    out = tmp_path / "cases.yaml"

    assert main(runs_in(tmp_path, "export-cases", failed.id, "--out", str(out))) == 1

    err = capsys.readouterr().err
    assert f"skipped {failed.id}: status is failed" in err and "no case written" in err
    assert not out.exists()
    assert main(runs_in(tmp_path, "export-cases", "proc_nope", "--out", str(out))) == 1
    assert "No process run 'proc_nope'" in capsys.readouterr().err


async def test_an_exported_case_passes_on_the_system_it_came_from(tmp_path: Path) -> None:
    run = await asyncio.to_thread(make_run, tmp_path)
    out = tmp_path / "from_runs.yaml"
    exit_code = await asyncio.to_thread(
        main, runs_in(tmp_path, "export-cases", run.id, "--out", str(out))
    )
    assert exit_code == 0
    PUBLISHED.clear()
    judge = FakeChatModel([_verdict(1.0)], repeat_last=True)

    report = await evaluate(desk_team, Dataset.load(out), judge_model=judge)

    [result] = report.cases
    assert result.status == "passed", report.to_markdown()
    assert result.decisions == ["approve:publish", "approve:publish"]
    assert result.tool_calls.count("publish") == 2 and PUBLISHED == ["Debian 1993"] * 2
    assert judge.call_count == 1
    # The same case on a system that no longer does the work fails its expectations.
    broken = Agent("desk", model=FakeChatModel(["I cannot help."], repeat_last=True))
    failing = await evaluate(lambda: broken, Dataset.load(out), judge_model=judge)
    assert failing.pass_rate == 0.0


def _verdict(score: float) -> Any:
    from awa.evals import JudgeVerdict

    return structured_output(JudgeVerdict, {"score": score, "reasoning": "same facts"})


DESK = """
name: desk
agents:
  - name: clerk
    description: Answers questions.
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    registry = ModelRegistry()
    registry.register("default", FakeChatModel(["It is done."], repeat_last=True))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "desk.yaml").write_text(DESK, encoding="utf-8")
    return tmp_path


def test_awa_run_writes_a_trace_when_asked(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = workspace / "run.trace.jsonl"

    assert main(["run", "desk.yaml", "Hello", "--trace", str(target)]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == "It is done."
    assert f"trace written to {target}" in captured.err
    data = read_trace(target)
    assert data.level == "summary" and data.meta["blueprint"] == "desk" and data.outcome == "ok"
    assert [e["type"] for e in data.events] == ["agent_started", "model_response", "agent_finished"]
    assert "Hello" not in target.read_text(encoding="utf-8")
    assert main(["runs", "trace", str(target)]) == 0
    assert "agent clerk  [1 model call" in capsys.readouterr().out


def test_awa_run_names_the_trace_after_the_run_and_can_record_everything(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "desk.yaml", "Hello there", "--trace", "--trace-level", "full"]) == 0

    [path] = (workspace / ".awa" / "traces").glob("run_*.jsonl")
    data = read_trace(path)
    assert data.header["root_run_id"] == path.stem and data.level == "full"
    assert any(e.get("input") == "Hello there" for e in data.events)
    assert str(Path(".awa") / "traces" / path.name) in capsys.readouterr().err


def test_awa_run_without_trace_writes_no_file(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "desk.yaml", "Hello"]) == 0

    assert not (workspace / ".awa").exists()
    assert "trace written" not in capsys.readouterr().err


def test_awa_run_closes_the_trace_of_a_failing_run(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(call: Any) -> Any:
        raise RuntimeError("provider is down")

    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(broken))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    target = workspace / "failed.jsonl"

    assert main(["run", "desk.yaml", "Hello", "--trace", str(target)]) == 1

    data = read_trace(target)
    assert data.outcome == "failed" and "provider is down" in (data.error or "")
