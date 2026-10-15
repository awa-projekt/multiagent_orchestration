"""Run traces: the recorder, the store, the tree view, redaction, retention, erasure, and what a trace may leak, how it ends and how it reads."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from _helpers.automation import ScriptedSystem
from _helpers.fake_secrets import fake_secret
from _helpers.trace_helpers import PUBLISHED, SECRET, desk_team, run_desk, scrub
from awa import Agent
from awa._internal import aio as asyncio_compat
from awa.automation import Automation, InMemoryRunStore, ProcessRun, SqliteRunStore
from awa.automation.trace_cases import add_cases, cases_from_runs
from awa.cli import main
from awa.contracts import AgentFinishedEvent, ModelPrice, PriceTable, ToolCallEvent
from awa.contracts.events import (
    AgentStartedEvent,
    ApprovalDecidedEvent,
    ApprovalRequestedEvent,
    DelegationEvent,
    HandoffEvent,
    ModelResponseEvent,
)
from awa.errors import AwaError, EvalError
from awa.evals import Dataset, EvalCase
from awa.kernel.redaction import SECRETS_REDACTOR, RegexRedactor
from awa.models import ModelRegistry
from awa.observability import EventRecorder, TraceRecorder, TraceStore, read_trace, render_trace
from awa.observability import trace_store as trace_store_module
from awa.observability._trace_encode import TraceEncoder
from awa.observability.trace_store import DEFAULT_MAX_BYTES, TraceDeleteError, resolve_level
from awa.observability.trace_view import build_tree
from awa.persistence.erasure import erase_subject
from awa.persistence.retention import RetentionPolicy, SubjectIndex, apply_retention
from awa.runtime import use_global_subscriber
from awa.testing import FakeChatModel, FakeClock, FunctionChatModel, tool_call

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "trace_tree.golden.txt"
PRICES = PriceTable(default=ModelPrice(input_per_mtok=2.0, output_per_mtok=8.0))


@pytest.fixture(autouse=True)
def _clear() -> None:
    PUBLISHED.clear()


@tool
def lookup(password: str, note: str, nested: dict[str, str], free: str) -> str:
    """Look something up.

    Args:
        password: A password.
        note: A note.
        nested: Nested values.
        free: Free text.
    """

    return "found"


@tool
def search(query: str) -> str:
    """Search.

    Args:
        query: The query.
    """

    return "ok"


async def recorded(
    path: Path, *, level: Any = "summary", prices: PriceTable | None = None, **options: Any
) -> tuple[Any, EventRecorder, TraceRecorder]:
    recorder = TraceRecorder(path, level=level, prices=prices, **options)
    events = EventRecorder()
    result = await run_desk(desk_team(), recorder=recorder, events=events)
    recorder.close("ok")
    return result, events, recorder


async def test_the_tree_of_a_team_with_delegation_consult_handoff_and_approval(
    tmp_path: Path,
) -> None:
    path = tmp_path / "desk.jsonl"
    result, _, _ = await recorded(path, prices=None)

    text = scrub(render_trace(path), path)

    assert result.output == "final summary" and PUBLISHED == ["Debian 1993", "Debian 1993"]
    if os.environ.get("UPDATE_TRACE_GOLDEN"):  # after a deliberate prompt or trace change
        GOLDEN.write_text(text, encoding="utf-8")
    assert text == GOLDEN.read_text(encoding="utf-8").replace("\r\n", "\n")


async def test_the_trace_has_exactly_the_events_of_an_in_memory_recorder(tmp_path: Path) -> None:
    path = tmp_path / "desk.jsonl"
    _, events, _ = await recorded(path)

    totals = build_tree(read_trace(path)).totals
    data = read_trace(path)

    delegations = events.of_type(DelegationEvent)
    assert totals["delegations"] == sum(e.mode == "delegate" for e in delegations) == 2
    assert totals["consults"] == sum(e.mode == "consult" for e in delegations) == 1
    assert totals["handoffs"] == len(events.of_type(HandoffEvent)) == 1
    assert totals["tool_calls"] == len(events.of_type(ToolCallEvent)) == 8
    assert totals["model_calls"] == len(events.of_type(ModelResponseEvent)) == 12
    assert totals["approvals_requested"] == len(events.of_type(ApprovalRequestedEvent)) == 2
    assert totals["approvals_decided"] == len(events.of_type(ApprovalDecidedEvent)) == 2
    assert totals["input_tokens"] == sum(e.input_tokens for e in events.of_type(ModelResponseEvent))
    assert totals["output_tokens"] == sum(
        e.output_tokens for e in events.of_type(ModelResponseEvent)
    )
    # Every recorded event is in the file (streamed text and the final result are not).
    kept = [e for e in events if e.type not in ("text_delta", "run_completed")]
    assert [r["type"] for r in data.events] == [e.type for e in kept]
    assert [r["tool"] for r in data.events if r["type"] == "tool_call"] == [
        e.tool_name for e in events.of_type(ToolCallEvent)
    ]
    # desk, lead, researcher (twice, once as a consult), analyst, writer
    assert totals["agents"] == 6


async def test_the_json_view_has_the_numbers_of_the_tree(tmp_path: Path) -> None:
    path = tmp_path / "desk.jsonl"
    await recorded(path, prices=PRICES)

    document = build_tree(read_trace(path)).to_dict()

    [root] = document["roots"]
    assert (root["agent"], root["kind"]) == ("desk", "team")
    lead = next(i["run"] for i in root["items"] if i["type"] == "run")
    assert lead["agent"] == "lead" and lead["model_calls"] == 3
    delegations = [i for i in lead["items"] if i["type"] == "delegation"]
    assert [(d["mode"], d["target"], d["status"]) for d in delegations] == [
        ("delegate", "researcher", "ok"),
        ("delegate", "analyst", "ok"),
    ]
    researcher = delegations[0]["run"]
    tools = [i for i in researcher["items"] if i["type"] == "tool"]
    assert [t["tool"] for t in tools] == ["web_search", "publish"]
    assert tools[1]["arguments_preview"].count("[REDACTED") == 1
    assert document["totals"]["tokens"] == sum(n["tokens"] for n in _walk(document["roots"]))
    assert json.loads(json.dumps(document)) == document


def _walk(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for node in nodes:
        found.append(node)
        found.extend(_walk([i["run"] for i in node["items"] if "run" in i]))
    return found


async def test_cost_comes_from_the_prices_in_the_header_or_the_ones_you_pass(
    tmp_path: Path,
) -> None:
    path = tmp_path / "desk.jsonl"
    _, events, _ = await recorded(path, prices=PRICES)
    replies = events.of_type(ModelResponseEvent)
    tokens_in, tokens_out = (
        sum(e.input_tokens for e in replies),
        sum(e.output_tokens for e in replies),
    )
    expected = (tokens_in * 2.0 + tokens_out * 8.0) / 1_000_000

    tree = build_tree(read_trace(path))
    assert tree.totals["cost"] == pytest.approx(expected) and tree.totals["cost_complete"]
    assert f"cost {expected:.4f}" in tree.render()
    assert sum(n.cost for r in tree.roots for n in r.walk()) == pytest.approx(expected)

    free = tmp_path / "free.jsonl"
    await recorded(free, prices=None)
    assert build_tree(read_trace(free)).totals["cost"] is None
    other = PriceTable(default=ModelPrice(input_per_mtok=1.0, output_per_mtok=1.0))
    assert build_tree(read_trace(free), prices=other).totals["cost"] == pytest.approx(
        (tokens_in + tokens_out) / 1_000_000
    )
    assert "no prices" in render_trace(free)

    # A model without a price is said so, and the rest is still counted.
    nameless = PriceTable(prices={"gpt-test": ModelPrice(input_per_mtok=1.0, output_per_mtok=1.0)})
    partial = build_tree(read_trace(free), prices=nameless)
    assert partial.totals["cost"] == 0.0 and not partial.totals["cost_complete"]
    assert "some models have no price" in partial.render()


def test_a_named_model_is_priced_by_name_prefix_and_per_agent(tmp_path: Path) -> None:
    path = tmp_path / "named.jsonl"
    lines: list[dict[str, Any]] = [
        {"type": "agent_started", "run_id": "r1", "agent": "clerk", "depth": 0, "kind": "agent"},
        {
            "type": "model_response",
            "run_id": "r1",
            "agent": "clerk",
            "depth": 0,
            "model": "gpt-test-2026-01-01",
            "input_tokens": 1000,
            "output_tokens": 200,
        },
        {"type": "agent_finished", "run_id": "r1", "agent": "clerk", "depth": 0, "duration_ms": 40},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    prices = PriceTable(prices={"gpt-test": ModelPrice(input_per_mtok=2.0, output_per_mtok=8.0)})

    tree = build_tree(read_trace(path), prices=prices)

    assert tree.roots[0].cost == pytest.approx(0.0036) == tree.totals["cost"]
    assert "clerk  [1 model call, 1,200 tokens, cost 0.0036, 40 ms]" in tree.render()


async def test_a_run_that_was_resumed_is_one_tree_and_pending_approvals_show(
    tmp_path: Path,
) -> None:
    path = tmp_path / "desk.jsonl"
    team = desk_team()
    recorder = TraceRecorder(path)
    with use_global_subscriber(recorder):
        paused = await team.run("Brief on Debian", thread_id="t-9")
    recorder.close("paused")

    text = render_trace(path)

    assert paused.is_interrupted
    assert "Result: paused, waiting for an approval" in text
    assert "approval for publish: still waiting for a decision" in text
    assert text.count("agent researcher") == 1


async def test_a_secret_in_a_tool_argument_is_redacted_in_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAYMENT_API_TOKEN", "tok-0123456789abcdef")
    for level in ("summary", "full"):
        path = tmp_path / f"{level}.jsonl"
        recorder = TraceRecorder(path, level=level)
        agent = Agent(
            "clerk",
            model=FakeChatModel(
                [
                    tool_call(
                        "lookup",
                        {
                            "password": "hunter2-hunter2",
                            "note": "Authorization: Bearer abcdefghij1234567890klmnop",
                            "nested": {"api_key": SECRET, "url": "https://u:s3cret@db.example/x"},
                            "free": "pay with tok-0123456789abcdef please",
                        },
                    ),
                    "done",
                ]
            ),
            tools=[lookup],
        )
        with use_global_subscriber(recorder):
            await agent.run("Look it up")
        recorder.close()

        text = path.read_text(encoding="utf-8")

        for leaked in (
            "hunter2-hunter2",
            "abcdefghij1234567890klmnop",
            SECRET,
            "s3cret",
            "tok-0123456789abcdef",
        ):
            assert leaked not in text, f"{leaked!r} leaked at level {level}"
        assert "[REDACTED" in text and "lookup" in text


async def test_a_name_with_a_line_break_cannot_forge_a_line_of_the_text_view(
    tmp_path: Path,
) -> None:
    path = tmp_path / "trace.jsonl"
    recorder = TraceRecorder(path)
    model = FakeChatModel([tool_call("search", {"query": "q"}), "done"])
    agent = Agent("clerk", model=model, tools=[search])
    with use_global_subscriber(recorder):
        await agent.run("Look it up")
    recorder.close()
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace('"search"', '"search\\nagent forged"'), encoding="utf-8")

    lines = render_trace(path).splitlines()

    assert any("search agent forged" in line for line in lines)
    assert not any(line.lstrip().startswith("agent forged") for line in lines)


async def test_summary_keeps_no_message_bodies_and_full_keeps_them_cut(tmp_path: Path) -> None:
    summary, full = tmp_path / "summary.jsonl", tmp_path / "full.jsonl"
    await recorded(summary, level="summary")
    await recorded(full, level="full")

    brief, detailed = summary.read_text(encoding="utf-8"), full.read_text(encoding="utf-8")

    for body in (
        "Brief on Debian",
        "research the topic",
        "3 results for debian",
        "researcher findings",
    ):
        assert body not in brief, body
        assert body in detailed, body
    assert "debian first release" in brief  # the arguments are previewed at every level
    assert read_trace(summary).level == "summary" and read_trace(full).level == "full"
    assert len(detailed) > len(brief)


async def test_arguments_are_previewed_in_summary_and_whole_in_full(tmp_path: Path) -> None:
    long_text = "word " * 400
    for level, key in (("summary", "arguments_preview"), ("full", "arguments")):
        path = tmp_path / f"{level}.jsonl"
        recorder = TraceRecorder(path, level=level)
        agent = Agent(
            "clerk",
            model=FakeChatModel([tool_call("search", {"query": long_text}), "done"]),
            tools=[search],
        )
        with use_global_subscriber(recorder):
            await agent.run("go")
        recorder.close()

        [call] = [e for e in read_trace(path).events if e["type"] == "tool_call"]

        assert key in call
        if level == "summary":
            assert len(call[key]) <= 200 and call[key].endswith("...")
        else:
            assert call[key] == {"query": long_text}


async def test_a_trace_is_capped_and_says_so(tmp_path: Path) -> None:
    path = tmp_path / "capped.jsonl"
    recorder = TraceRecorder(path, level="full", max_bytes=10_000)
    events = EventRecorder()
    team = desk_team()
    with use_global_subscriber(recorder), use_global_subscriber(events):
        for round_number in range(8):
            result = await team.run("Brief on Debian", thread_id=f"t-{round_number}")
            while result.is_interrupted:
                result = await team.resume(f"t-{round_number}", {"action": "approve"})
    recorder.close()

    data = read_trace(path)

    assert path.stat().st_size <= 10_000 + 400  # the closing line is always written
    assert data.truncated and data.dropped > 0
    assert len(data.events) + data.dropped == len(
        [e for e in events if e.type not in ("text_delta", "run_completed")]
    )
    assert "cut at its size limit" in render_trace(data)
    with pytest.raises(ValueError, match="at least 10000"):
        TraceRecorder(tmp_path / "x.jsonl", max_bytes=100)
    assert DEFAULT_MAX_BYTES == 5_000_000


async def test_level_off_writes_nothing_and_unknown_levels_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "off.jsonl"
    recorder = TraceRecorder(path, level="off")
    await run_desk(desk_team(), recorder=recorder)
    recorder.close()

    assert not path.exists()
    assert resolve_level(None) == "summary" and resolve_level(" FULL ") == "full"
    with pytest.raises(ValueError, match="Unknown trace level"):
        resolve_level("verbose")
    with pytest.raises(ValueError, match="Unknown trace level"):
        TraceRecorder(path, level="verbose")  # type: ignore[arg-type]


async def test_the_recorder_follows_only_its_own_run_tree(tmp_path: Path) -> None:
    path = tmp_path / "one.jsonl"
    first = Agent("first", model=FakeChatModel(["one"]))
    second = Agent("second", model=FakeChatModel(["two"]))
    wanted = await first.run("go", metadata=None)  # a run to learn a root id from
    recorder = TraceRecorder(path, root_run_id=wanted.run_id)
    with use_global_subscriber(recorder):
        await second.run("go")  # another run tree: not recorded
    recorder.close()

    assert read_trace(path).events == []
    assert read_trace(path).header["root_run_id"] == wanted.run_id


async def test_a_failing_run_leaves_a_readable_trace(tmp_path: Path) -> None:
    def broken(call: Any) -> Any:
        raise RuntimeError("the model service is down, key=" + SECRET)

    path = tmp_path / "failed.jsonl"
    recorder = TraceRecorder(path)
    agent = Agent("clerk", model=FunctionChatModel(broken))
    with use_global_subscriber(recorder), pytest.raises(Exception, match="model service is down"):
        await agent.run("go")
    recorder.close("failed", "RuntimeError: the model service is down")

    data = read_trace(path)
    tree = build_tree(data)

    assert [e["type"] for e in data.events][-1] == "error"
    assert SECRET not in path.read_text(encoding="utf-8")
    assert tree.roots[0].errors and "model service is down" in tree.roots[0].errors[0]
    text = tree.render()
    assert "Result: failed" in text and "What went wrong:" in text


def test_a_trace_cut_in_the_middle_of_a_line_is_still_readable(tmp_path: Path) -> None:
    path = tmp_path / "crashed.jsonl"
    lines = [
        {"type": "trace", "format": "awa.trace/v1", "level": "summary", "meta": {"attempt": 1}},
        {"type": "agent_started", "run_id": "r1", "agent": "clerk", "depth": 0, "kind": "agent"},
        {"type": "tool_call", "run_id": "r1", "agent": "clerk", "tool": "lookup", "call_id": "c1"},
    ]
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + '\n{"type":"tool_resu', encoding="utf-8"
    )

    data = read_trace(path)
    text = render_trace(data)

    assert (len(data.events), data.damaged, data.ends) == (2, 1, [])
    assert "not finished" in text and "tool lookup" in text and "no result recorded" in text
    assert "could not be read" in text


def test_a_missing_file_and_an_empty_one(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_trace(tmp_path / "nothing.jsonl")
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")

    assert "no events were recorded" in render_trace(empty)


def make_trace(store: TraceStore, name: str, *, age: timedelta, **meta: Any) -> Path:
    recorder = store.recorder(name, **meta)
    recorder.close("ok")
    path = store.path(name)
    stamp = (datetime.now(UTC) - age).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_retention_removes_old_and_excess_files(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "traces", max_age=timedelta(days=30), max_files=3)
    for name, days in (("a", 90), ("b", 40), ("c", 5), ("d", 4), ("e", 3), ("f", 2)):
        make_trace(store, name, age=timedelta(days=days))

    preview = store.prune(dry_run=True)
    pruned = store.prune()

    assert preview == pruned == ["a.jsonl", "b.jsonl", "c.jsonl"]  # two too old, one over the limit
    assert [p.name for p in store.files()] == ["d.jsonl", "e.jsonl", "f.jsonl"]
    assert store.prune() == []


def test_a_trace_being_written_is_never_pruned(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "traces", max_age=timedelta(days=1))
    live = store.recorder("live")
    live.flush()
    old = store.path("live")
    stamp = (datetime.now(UTC) - timedelta(days=9)).timestamp()
    os.utime(old, (stamp, stamp))

    assert store.prune() == []
    live.close()
    os.utime(old, (stamp, stamp))
    assert store.prune() == ["live.jsonl"]


async def test_the_existing_retention_applies_to_traces(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "traces")
    for name, days in (("old", 40), ("recent-1", 3), ("recent-2", 2), ("recent-3", 1)):
        make_trace(store, name, age=timedelta(days=days))

    policy = RetentionPolicy(max_age=timedelta(days=30), max_trace_files=2)
    preview = await apply_retention(policy, traces=store, dry_run=True)
    report = await apply_retention(policy, traces=store)

    assert preview.ids["traces"] == report.ids["traces"] == ["old.jsonl", "recent-1.jsonl"]
    assert report.deleted["traces"] == 2 and report.ok
    assert [p.name for p in store.files()] == ["recent-2.jsonl", "recent-3.jsonl"]
    only_traces = RetentionPolicy(traces=timedelta(hours=1))
    assert (await apply_retention(only_traces, traces=store)).ids["traces"] == [
        "recent-2.jsonl",
        "recent-3.jsonl",
    ]
    with pytest.raises(ValueError, match="max_trace_files"):
        RetentionPolicy(max_trace_files=0)


def test_a_store_prunes_by_itself_when_a_new_trace_starts(tmp_path: Path) -> None:
    folder = tmp_path / "traces"
    for name, days in (("a", 3), ("b", 2), ("c", 1)):
        make_trace(TraceStore(folder), name, age=timedelta(days=days))

    TraceStore(folder, max_files=2).recorder("d").close()

    assert sorted(p.name for p in TraceStore(folder).files()) == ["b.jsonl", "c.jsonl", "d.jsonl"]


def test_the_stores_arguments_are_checked(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_files"):
        TraceStore(tmp_path, max_files=0)
    with pytest.raises(ValueError, match="max_age"):
        TraceStore(tmp_path, max_age=timedelta(0))
    with pytest.raises(ValueError, match="Unknown trace level"):
        TraceStore(tmp_path, level="loud")  # type: ignore[arg-type]
    assert TraceStore(tmp_path).path("proc_1/../x").name == "proc_1%2F..%2Fx.jsonl"


async def test_erasure_removes_a_subjects_traces_and_nothing_else(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "traces")
    make_trace(store, "own.a1", age=timedelta(0), actor="u-42", thread_id="t-own")
    make_trace(store, "named", age=timedelta(0), subject="u-42")
    make_trace(store, "thread", age=timedelta(0), thread_id="t-linked", actor="clerk-bot")
    make_trace(store, "other", age=timedelta(0), actor="u-7", thread_id="t-other")
    make_trace(store, "u-420", age=timedelta(0), actor="u-420")

    preview = await erase_subject("u-42", traces=store, dry_run=True)
    report = await erase_subject("u-42", traces=store)
    again = await erase_subject("u-42", traces=store)

    assert preview.dry_run and preview.ids["traces"] == report.ids["traces"]
    assert sorted(report.ids["traces"]) == ["named.jsonl", "own.a1.jsonl"]
    assert report.deleted["traces"] == 2 and report.ok
    assert sorted(p.name for p in store.files()) == ["other.jsonl", "thread.jsonl", "u-420.jsonl"]
    assert again.deleted["traces"] == 0
    # A thread of the subject is enough, whoever started the run.
    assert store.erase("u-42", threads=["t-linked"], dry_run=True) == ["thread.jsonl"]
    assert store.erase("u-42", threads=["t-linked"]) == ["thread.jsonl"]
    with pytest.raises(ValueError, match="subject_id"):
        store.erase("")


async def test_erasure_finds_a_threads_traces_through_the_subject_index(tmp_path: Path) -> None:
    store, traces = InMemoryStore(), TraceStore(tmp_path / "traces")
    make_trace(traces, "run", age=timedelta(0), thread_id="t-1")
    await SubjectIndex(store).link("u-9", threads=["t-1"])

    report = await erase_subject("u-9", store=store, checkpointer=InMemorySaver(), traces=traces)

    assert report.ids["traces"] == ["run.jsonl"] and traces.files() == []


def test_a_failed_run_is_timed_by_its_own_events_and_each_error_is_said_once(
    tmp_path: Path,
) -> None:
    def line(event_type: str, second: int, run: str, agent: str, depth: int, **fields: Any) -> str:
        stamp = f"2026-10-05T10:00:{second:02d}.000+00:00"
        record = {"type": event_type, "ts": stamp, "run_id": run, "agent": agent, "depth": depth}
        return json.dumps({**record, **fields})

    error = {"error_type": "UsageLimitExceededError", "message": "requests=21 > max_requests=20"}
    lines = [
        line("agent_started", 0, "r0", "desk", 0, kind="team"),
        line("agent_started", 1, "r1", "lead", 1, parent_run_id="r0", kind="agent"),
        line("limit_exceeded", 7, "r1", "lead", 1, limit="max_requests", value=21.0, maximum=20.0),
        line("error", 7, "r1", "lead", 1, **error),
        line("error", 8, "r0", "desk", 0, **error),
    ]
    path = tmp_path / "failed.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tree = build_tree(read_trace(path))
    text = tree.render()

    assert tree.totals["duration_ms"] == 8000
    assert tree.roots[0].children()[0].duration_ms == 6000
    assert tree.problems == [
        "lead: limit max_requests reached (21 > 20)",
        "lead: UsageLimitExceededError: requests=21 > max_requests=20",
    ]
    assert (
        "team desk  [no model calls, 8.0 s]" in text
        and "agent lead  [no model calls, 6.0 s]" in text
    )


def test_attempt_files_are_numbered_and_a_resumed_attempt_continues_its_file(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "traces")

    first = store.attempt_path("proc_1")
    first.parent.mkdir(parents=True)
    first.write_text("{}\n", encoding="utf-8")

    assert first.name == "proc_1.a1.jsonl"
    assert store.attempt_path("proc_1", resume=True) == first  # after an approval: same file
    second = store.attempt_path("proc_1")  # a new attempt
    second.write_text("{}\n", encoding="utf-8")
    assert second.name == "proc_1.a2.jsonl"
    assert store.attempt_path("proc_1", resume=True) == second
    # nothing to continue
    assert store.attempt_path("proc_2", resume=True).name == "proc_2.a1.jsonl"
    assert [p.name for p in store.attempts("proc_1")] == ["proc_1.a1.jsonl", "proc_1.a2.jsonl"]
    assert store.attempts("proc_10") == [] and store.find("proc_1") == [first, second]


def test_names_differing_only_by_case_never_share_a_file(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    upper, lower = store.path("Run1"), store.path("run1")
    assert upper.name.lower() != lower.name.lower()
    assert lower.name == "run1.jsonl"


PII = RegexRedactor.pii()
KEY = fake_secret("sk-", "abcdefghijklmnopqrstuvwxyz123456")


def drain() -> None:
    trace_store_module._WRITER.drain()


async def test_erasure_finds_a_person_whom_the_pii_redactor_masks_in_the_header(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "traces", redactor=PII)
    path = make_trace(store, "run", age=timedelta(0), subject="jane@example.com", thread_id="t-1")
    make_trace(store, "other", age=timedelta(0), subject="john@example.com")

    header = read_trace(path).meta
    text = path.read_text(encoding="utf-8")
    assert header["subject"] == "[REDACTED:email]" and "jane@example.com" not in text
    # Masked text is no id: it must not match anybody, least of all everybody.
    assert store.erase("[REDACTED:email]", dry_run=True) == []
    report = await erase_subject("jane@example.com", traces=store)

    assert report.ok and report.ids["traces"] == ["run.jsonl"]
    assert [p.name for p in store.files()] == ["other.jsonl"]
    assert store.erase("x", threads=["t-9"]) == []


async def test_a_trace_that_is_still_written_is_reported_as_an_error_not_as_deleted(
    tmp_path: Path,
) -> None:
    store = TraceStore(tmp_path / "traces")
    make_trace(store, "done", age=timedelta(0), actor="u-1")
    running = store.recorder("running", actor="u-1")  # still being written
    running.flush()

    report = await erase_subject("u-1", traces=store)

    assert not report.ok and "traces" in report.errors[0] and "running.jsonl" in report.errors[0]
    assert report.ids["traces"] == ["done.jsonl"]  # only what is gone
    assert store.path("running").exists()
    running.close("ok")
    assert store.erase("u-1") == ["running.jsonl"]


async def test_a_trace_the_system_refuses_to_delete_is_an_error_in_erasure_and_in_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TraceStore(tmp_path / "traces")
    make_trace(store, "stuck", age=timedelta(days=9), actor="u-2")
    make_trace(store, "fine", age=timedelta(days=9), actor="u-2")
    real = Path.unlink

    def refuse(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name == "stuck.jsonl":
            raise PermissionError("locked")
        real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)

    with pytest.raises(TraceDeleteError, match="stuck.jsonl") as raised:
        store.erase("u-2")
    assert raised.value.deleted == ["fine.jsonl"] and raised.value.failed == ["stuck.jsonl"]
    make_trace(store, "fine", age=timedelta(days=9), actor="u-2")
    report = await erase_subject("u-2", traces=store)
    assert not report.ok and report.ids["traces"] == ["fine.jsonl"]
    make_trace(store, "fine", age=timedelta(days=9), actor="u-2")
    pruned = await apply_retention(RetentionPolicy(traces=timedelta(days=1)), traces=store)
    assert pruned.ids["traces"] == ["stuck.jsonl", "fine.jsonl"][1:] and len(pruned.errors) == 1
    assert "stuck.jsonl" in pruned.errors[0] and store.path("stuck").exists()


def scripted_run(tmp_path: Path, topic: str, answer: str | None = None) -> ProcessRun:
    """A succeeded run of a system that answers `answer` (default: the input), in a run store."""

    class Echo(ScriptedSystem):
        async def run(self, input: Any, **options: Any) -> Any:
            result = await super().run(input, **options)
            return result.model_copy(update={"output": answer or result.output})

    async def go() -> ProcessRun:
        store = SqliteRunStore(tmp_path / ".awa" / "awa.db")
        try:
            auto = Automation(Echo(), store=store, clock=FakeClock(), input="{payload.topic}")
            await auto.submit({"topic": topic})
            return (await auto.run_pending())[-1]
        finally:
            await store.close()

    return asyncio_compat.run(go())


def export(tmp_path: Path, run: ProcessRun, out: Path, *extra: str) -> int:
    store = str(tmp_path / ".awa" / "awa.db")
    return main(["runs", "export-cases", run.id, "--out", str(out), "--store", store, *extra])


def test_an_exported_case_holds_no_secret_and_says_what_was_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PAYMENT_API_TOKEN", "tok-0123456789abcdef")
    run = scripted_run(tmp_path, f"Refund jane, api_key={KEY}, paid with tok-0123456789abcdef")
    out = tmp_path / "cases.yaml"

    assert export(tmp_path, run, out) == 0

    text = out.read_text(encoding="utf-8")
    assert KEY not in text and "tok-0123456789abcdef" not in text and "Refund jane" in text
    err = capsys.readouterr().err
    assert f"warning {run.id}: secrets or personal data were masked" in err
    [case] = Dataset.load(out).cases
    assert case.input == case.expected.removeprefix("done: ")  # type: ignore[union-attr]


def test_an_answer_cut_by_the_run_record_is_reported_not_passed_off_as_the_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = scripted_run(tmp_path, "long", answer="word " * 1500)
    out = tmp_path / "cases.yaml"

    assert export(tmp_path, run, out) == 0

    assert f"warning {run.id}: the answer is cut" in capsys.readouterr().err
    [case] = Dataset.load(out).cases
    assert (
        case.expected is not None and case.expected.endswith("...") and len(case.expected) == 4000
    )


def test_exporting_again_adds_cases_without_rewriting_the_file(tmp_path: Path) -> None:
    first, second = scripted_run(tmp_path, "one"), scripted_run(tmp_path, "two")
    out = tmp_path / "cases.yaml"
    assert export(tmp_path, first, out, "--no-judge") == 0
    hand_edited = "# Our regression suite. Reviewed by Ana.\n" + out.read_text(encoding="utf-8")
    hand_edited = hand_edited.replace("input: one", "input: one  # the simple one")
    out.write_text(hand_edited, encoding="utf-8")

    assert export(tmp_path, second, out, "--no-judge") == 0
    assert export(tmp_path, first, out, "--no-judge") == 0  # nothing new

    text = out.read_text(encoding="utf-8")
    assert text.startswith(hand_edited)  # every byte of the old file is still there
    assert text.count("- id:") == 2
    assert [c.id for c in Dataset.load(out).cases] == [f"from-{first.id}", f"from-{second.id}"]


def test_cases_are_added_to_a_new_file_and_to_an_indented_list(tmp_path: Path) -> None:
    def case(name: str) -> EvalCase:
        return EvalCase(id=name, input=name)

    fresh = tmp_path / "cases.yaml"
    assert add_cases(fresh, Dataset(name="s", cases=(case("a"),))) == (["a"], [])
    assert add_cases(fresh, Dataset(name="s", cases=(case("a"), case("b")))) == (["b"], ["a"])
    assert [c.id for c in Dataset.load(fresh).cases] == ["a", "b"]

    nested = tmp_path / "nested.yaml"
    nested.write_text(
        "name: mine\ncases:\n  # the first\n  - id: a\n    input: a\n", encoding="utf-8"
    )
    assert add_cases(nested, Dataset(name="x", cases=(case("b"),))) == (["b"], [])
    assert yaml.safe_load(nested.read_text(encoding="utf-8"))["cases"][1]["id"] == "b"
    assert nested.read_text(encoding="utf-8").startswith("name: mine\ncases:\n  # the first\n")


def test_a_file_that_cannot_be_added_to_in_place_is_refused_and_left_alone(tmp_path: Path) -> None:
    flow = tmp_path / "flow.yaml"
    flow.write_text("name: x\ncases: [{id: a, input: a}]\n", encoding="utf-8")
    after = tmp_path / "after.yaml"
    after.write_text("cases:\n- id: a\n  input: a\nname: mine\n", encoding="utf-8")
    new = Dataset(name="x", cases=(EvalCase(id="b", input="b"),))

    for file in (flow, after):
        before = file.read_bytes()
        with pytest.raises(EvalError, match="Cannot add cases to"):
            add_cases(file, new)
        assert file.read_bytes() == before


def test_trace_files_and_their_folder_are_made_for_the_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modes: list[tuple[str, int]] = []
    real_open, real_mkdir = os.open, Path.mkdir

    def spy_open(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        modes.append(("file", mode))
        return real_open(path, flags, mode, **kwargs)

    def spy_mkdir(self: Path, mode: int = 0o777, *args: Any, **kwargs: Any) -> None:
        modes.append(("folder", mode))
        real_mkdir(self, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(Path, "mkdir", spy_mkdir)

    recorder = TraceStore(tmp_path / "traces").recorder("run")
    recorder.close("ok")

    assert ("file", 0o600) in modes and ("folder", 0o700) in modes


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_trace_files_are_not_readable_by_others_whatever_the_umask(tmp_path: Path) -> None:
    previous = os.umask(0o022)
    try:
        recorder = TraceStore(tmp_path / "traces").recorder("run")
        recorder.close("ok")
    finally:
        os.umask(previous)

    assert stat.S_IMODE((tmp_path / "traces").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "traces" / "run.jsonl").stat().st_mode) == 0o600


ESCAPES = "\x1b[2J\x1b[31mFAKE\x1b]0;pwned\x07\x9b31m\r"


def write_trace(path: Path, *events: dict[str, Any]) -> Path:
    header = {"type": "trace", "format": "awa.trace/v1", "level": "full", "meta": {"x": ESCAPES}}
    stamp = "2026-01-01T00:00:00.000+00:00"
    base = {"ts": stamp, "run_id": "r1", "agent": "a", "depth": 0}
    lines = [header, {**base, "type": "agent_started", "kind": "agent"}]
    lines += [{**base, **event} for event in events]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def test_the_text_view_never_passes_terminal_controls_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_trace(
        tmp_path / "esc.a1.jsonl",
        {"type": "tool_call", "tool": f"x{ESCAPES}", "call_id": "c", "arguments_preview": ESCAPES},
        {"type": "tool_result", "tool": "x", "call_id": "c", "is_error": True, "error": ESCAPES},
        {"type": "approval_decided", "tool": "x", "interrupt_id": "i", "note": ESCAPES},
        {"type": "error", "error_type": ESCAPES, "message": ESCAPES},
        {"type": "model_response", "content": ESCAPES},
    )

    text = render_trace(path)
    assert main(["runs", "trace", str(path)]) == 0
    printed = capsys.readouterr().out

    for shown in (text, printed):
        assert not any(c in shown for c in ("\x1b", "\x07", "\x9b", "\r", "\x00"))
        assert "FAKE" in shown and "\n" in shown  # the words stay, line breaks stay


def test_a_damaged_or_hostile_trace_is_read_as_far_as_it_can_be(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stamp = "2026-01-01T00:00:00.000+00:00"
    path = write_trace(
        tmp_path / "bad.a1.jsonl",
        {"type": "model_response", "input_tokens": "abc", "output_tokens": [1], "depth": "x"},
        {"type": "model_response", "input_tokens": 1e999},
        {"type": "agent_finished", "duration_ms": "long"},
        {"type": "run_cancelled", "elapsed_ms": {}, "reason": "r"},
        {"type": "approval_decided", "interrupt_id": "i", "decision": "approve", "latency_ms": "x"},
        {"type": "tool_call", "tool": "t", "call_id": "c", "ts": "2026-01-01T00:00:00"},  # naive
        {"type": "tool_result", "tool": "t", "call_id": "c", "ts": stamp},
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("[" * 100_000 + "\n")  # nested deeper than the JSON reader goes

    for words in (["runs", "trace", str(path)], ["runs", "trace", str(path), "--json"]):
        assert main(words) == 0, capsys.readouterr().err
    assert "1 line(s) of the file could not be read" in render_trace(path)


def test_a_trace_nested_thousands_deep_is_shown_flat_not_as_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stamp = "2026-01-01T00:00:00.000+00:00"
    lines: list[dict[str, Any]] = [{"type": "trace", "format": "awa.trace/v1", "level": "summary"}]
    for i in range(3000):
        lines.append(
            {
                "type": "agent_started",
                "ts": stamp,
                "run_id": f"r{i}",
                "parent_run_id": f"r{i - 1}" if i else None,
                "agent": f"a{i}",
                "depth": i,
            }
        )
    path = tmp_path / "deep.a1.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

    assert main(["runs", "trace", str(path)]) == 0
    assert main(["runs", "trace", str(path), "--json"]) == 0
    assert "agents 3000" in capsys.readouterr().out


def encode(secret: str, level: str, **arguments: Any) -> str:
    event = ToolCallEvent(
        run_id="r", root_run_id="r", agent="a", tool_name="t", call_id="c", arguments=arguments
    )
    record = TraceEncoder(level, SECRETS_REDACTOR, [secret], with_root=False).encode(event)
    return json.dumps(record)


@pytest.mark.parametrize("level", ["summary", "full"])
def test_a_credential_with_quotes_or_backslashes_is_removed_from_tool_arguments(level: str) -> None:
    secret = 'p"ss\\word-123456789'

    stored = encode(secret, level, note=f"login with {secret} now", nested={"n": [secret]})

    assert "word-123456789" not in stored and "***" in stored


def test_a_private_key_cut_by_the_length_limit_is_still_masked() -> None:
    encoder = TraceEncoder("full", SECRETS_REDACTOR, [], with_root=False)
    key_block = fake_secret("-----BEGIN ", "PRIVATE KEY-----") + " MIIEbody" * 50

    assert "MIIEbody" not in encoder.clean(key_block, 100)


def test_free_text_reasons_in_a_summary_trace_are_redacted_too() -> None:
    event = ApprovalRequestedEvent(
        run_id="r",
        root_run_id="r",
        agent="a",
        tool="pay",
        interrupt_id="i",
        reason=f"check api_key={KEY} first",
    )

    record = TraceEncoder("summary", SECRETS_REDACTOR, [], with_root=False).encode(event)

    assert record is not None and KEY not in json.dumps(record)


def test_a_numeric_argument_equal_to_a_secret_does_not_break_the_line() -> None:
    secret = "123456789012"
    event = ToolCallEvent(
        run_id="r",
        root_run_id="r",
        agent="a",
        tool_name="t",
        call_id="c",
        arguments={"n": int(secret)},
    )

    record = TraceEncoder("full", SECRETS_REDACTOR, [secret], with_root=False).encode(event)

    assert record is not None and record["arguments"] == {"n": int(secret)}


def test_trace_delete_error_is_an_awa_error() -> None:
    error = TraceDeleteError(["a.jsonl"], ["b.jsonl"])

    assert isinstance(error, AwaError) and isinstance(error, OSError)
    assert error.deleted == ["a.jsonl"] and "b.jsonl" in str(error)


def test_drain_returns_after_the_pending_writes(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    recorder = store.recorder("barrier", level="summary")
    recorder(AgentStartedEvent(run_id="r", root_run_id="r", agent="a", input_preview="x"))

    drain()

    assert "agent_started" in recorder.path.read_text(encoding="utf-8")


def automation_in(
    tmp_path: Path, system: Any, *, in_memory: bool = False, **options: Any
) -> Automation:
    store = InMemoryRunStore() if in_memory else SqliteRunStore(tmp_path / ".awa" / "awa.db")
    return Automation(system, store=store, clock=FakeClock(), input="{payload.topic}", **options)


async def test_a_failure_before_the_system_starts_still_closes_its_trace(tmp_path: Path) -> None:
    def no_deps(event: Any) -> Any:
        raise ValueError("cannot build the dependencies, password=hunter2hunter2")

    auto = automation_in(tmp_path, ScriptedSystem(), deps=no_deps)
    await auto.submit({"topic": "go"})
    [run] = await auto.run_pending()

    assert auto.traces is not None
    [path] = auto.traces.files()
    data = read_trace(path)
    assert run.status == "failed" and run.trace == path.name
    assert data.outcome == "failed" and "cannot build the dependencies" in (data.error or "")
    assert "hunter2hunter2" not in path.read_text(encoding="utf-8")
    assert auto.traces._active == set()  # not counted as open for ever  # noqa: SLF001


@pytest.mark.parametrize("in_memory", [True, False], ids=["in memory", "in a file"])
async def test_an_attempt_cancelled_before_it_began_still_closes_its_trace(
    tmp_path: Path, in_memory: bool
) -> None:
    # In memory the cancel arrives when the attempt is about to start, with a run file while
    # the trace is being noted on the run.
    options: dict[str, Any] = {"trace": TraceStore(tmp_path / "traces")} if in_memory else {}
    auto = automation_in(tmp_path, ScriptedSystem(), in_memory=in_memory, **options)
    await auto.submit({"topic": "go"})
    run = await auto.claim("w1")
    assert run is not None and auto.traces is not None
    task = asyncio.ensure_future(auto.execute(run, worker="w1"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    drain()
    [path] = auto.traces.files()
    assert read_trace(path).outcome == "cancelled"
    assert auto.traces._active == set()  # noqa: SLF001


async def test_closing_a_trace_never_waits_for_the_disk_on_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed_on: list[str] = []
    real_close = TraceRecorder.close

    def close(self: TraceRecorder, *args: Any, **kwargs: Any) -> None:
        if not self._closed:  # noqa: SLF001 - a second call does nothing and is no work
            closed_on.append(threading.current_thread().name)
        real_close(self, *args, **kwargs)

    monkeypatch.setattr(TraceRecorder, "close", close)
    auto = automation_in(tmp_path, ScriptedSystem())
    await auto.submit({"topic": "go"})
    await auto.run_pending()

    assert closed_on and threading.main_thread().name not in closed_on


def test_awa_run_says_where_the_trace_is_also_when_the_run_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(call: Any) -> Any:
        raise RuntimeError("provider is down")

    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(broken))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "desk.yaml").write_text(
        "name: desk\nagents:\n  - name: clerk\n    description: Answers.\n", encoding="utf-8"
    )
    target = tmp_path / "failed.jsonl"

    assert main(["run", "desk.yaml", "Hello", "--trace", str(target)]) == 1

    err = capsys.readouterr().err
    assert f"trace written to {target}" in err and "provider is down" in err


def test_different_trace_names_never_share_a_file(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)
    names = ["a:b", "a_b", "a/b", "a%3Ab", "a b", "proc_1.a1"]

    files = [store.path(name).name for name in names]

    assert len(set(files)) == len(names)
    assert store.path("proc_1.a1").name == "proc_1.a1.jsonl"  # run ids stay readable
    make_trace(store, "x_1.a1", age=timedelta(0))
    assert store.attempts("x:1") == [] and [p.name for p in store.attempts("x_1")] == [
        "x_1.a1.jsonl"
    ]


def test_a_recorder_that_cannot_be_made_does_not_stay_counted_as_open(tmp_path: Path) -> None:
    store = TraceStore(tmp_path)

    with pytest.raises(ValueError, match="Unknown trace level"):
        store.recorder("x", level="loud")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_bytes"):
        TraceStore(tmp_path, max_bytes=10)

    assert store._active == set()  # noqa: SLF001


def test_the_event_loop_only_hands_an_event_over_and_the_writer_does_the_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoded_on: list[str] = []
    real_encode = TraceEncoder.encode

    def encode_spy(self: TraceEncoder, event: Any) -> Any:
        encoded_on.append(threading.current_thread().name)
        return real_encode(self, event)

    monkeypatch.setattr(TraceEncoder, "encode", encode_spy)
    recorder = TraceRecorder(tmp_path / "busy.jsonl", level="full")
    event = AgentFinishedEvent(run_id="r", root_run_id="r", agent="a", output_preview="x" * 500)
    for _ in range(300):
        recorder(event)  # called from this thread, as the event loop would
    recorder.close()

    assert len(read_trace(tmp_path / "busy.jsonl").events) == 300
    assert set(encoded_on) == {"awa-trace-writer"}  # nothing was encoded or redacted here


def test_an_approval_that_expired_is_exported_as_a_rejection(tmp_path: Path) -> None:
    traces = TraceStore(tmp_path)
    run = ProcessRun(id="proc_1", automation="desk", status="succeeded", input="x", output="y")
    expired = {"type": "approval_decided", "tool": "pay", "interrupt_id": "i", "expired": True}
    write_trace(
        traces.path("proc_1.a1"),
        {**expired, "decision": "reject", "note": "the approval request expired after 60 seconds"},
        # No run writes this (an expired decision is always a rejection), but whatever a damaged
        # or hand-made trace says, an expired approval must never become an approval.
        {**expired, "decision": "approve", "interrupt_id": "j"},
        {"type": "approval_decided", "tool": "pay", "decision": "approve", "interrupt_id": "k"},
    )

    [case] = cases_from_runs([run], traces, judge=False).dataset.cases

    assert case.decisions == (
        {
            "action": "reject",
            "tool": "pay",
            "note": "the approval request expired after 60 seconds",
        },
        {"action": "reject", "tool": "pay", "note": "Rejected."},
        {"action": "approve", "tool": "pay"},
    )


def test_the_former_import_path_of_the_trace_cases_still_works() -> None:
    from awa.automation import trace_cases
    from awa.observability import trace_cases as former

    assert former.__all__ == trace_cases.__all__
    assert all(getattr(former, name) is getattr(trace_cases, name) for name in former.__all__)
