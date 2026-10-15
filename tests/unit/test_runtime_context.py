"""Run context, case directories, global subscribers, the clock and id ports and their doubles."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from awa import Agent, Team
from awa._internal.ids import new_id
from awa.automation import InMemoryEventQueue
from awa.contracts import AgentStartedEvent, RunEvent, Usage, UsageLimits
from awa.contracts.events import AgentFinishedEvent
from awa.errors import ConfigurationError, UsageLimitExceededError
from awa.runtime import (
    Clock,
    EventEmitter,
    IdSource,
    RunContext,
    SystemClock,
    UuidIds,
    active_case_dir,
    as_clock,
    case_dir,
    current_run_context,
    global_subscribers,
    new_run_id,
    require_run_context,
    subscribe_globally,
    use_case_dir,
    use_global_subscriber,
    use_run_context,
)
from awa.runtime import ids as ids_module
from awa.runtime.case import in_case_dir
from awa.runtime.context import new_run_context
from awa.runtime.ids import Uuid7Ids
from awa.testing import FakeChatModel, FakeClock, FixedIds, FunctionChatModel, ModelCall, tool_call
from awa.tools import tool


def _event(agent: str = "a") -> AgentStartedEvent:
    return AgentStartedEvent(run_id="r", root_run_id="r", agent=agent)


def test_new_run_ids_are_unique() -> None:
    assert new_run_id() != new_run_id()
    assert new_run_id().startswith("run_")


def test_root_context_defaults_root_run_id() -> None:
    context = RunContext(run_id="r1", agent="a", deps=None)
    assert context.root_run_id == "r1"
    assert context.event_fields() == {"run_id": "r1", "root_run_id": "r1", "agent": "a", "depth": 0}


def test_child_shares_accounting_and_inherits_deps() -> None:
    parent = RunContext(
        run_id="r1", agent="lead", deps={"db": 1}, limits=UsageLimits(max_requests=5)
    )
    parent.metadata["tenant"] = "t"
    child = parent.child("worker")
    assert child.agent == "worker"
    assert child.parent_run_id == "r1"
    assert child.root_run_id == "r1"
    assert child.depth == 1
    assert child.deps == {"db": 1}
    assert child.usage is parent.usage
    assert child.limits is parent.limits
    assert child.events is parent.events
    child.metadata["local"] = True
    assert "local" not in parent.metadata
    assert parent.child("x", deps="other", run_id="r9").deps == "other"
    assert child.child("deeper").depth == 2


def test_check_limits_uses_the_shared_usage() -> None:
    context = RunContext(run_id="r", agent="a", deps=None, limits=UsageLimits(max_requests=1))
    context.child("b").usage.record_model_call("b")
    context.check_limits()
    context.usage.record_model_call("a")
    with pytest.raises(UsageLimitExceededError):
        context.check_limits()
    RunContext(run_id="r", agent="a", deps=None, usage=Usage()).check_limits()


def test_use_run_context_binds_and_restores() -> None:
    assert current_run_context() is None
    with pytest.raises(RuntimeError):
        require_run_context()
    outer = RunContext(run_id="r1", agent="a", deps=None)
    inner = outer.child("b")
    with use_run_context(outer):
        assert require_run_context() is outer
        with use_run_context(inner):
            assert current_run_context() is inner
        assert current_run_context() is outer
    assert current_run_context() is None


async def test_context_is_visible_in_child_tasks() -> None:
    context = RunContext(run_id="r1", agent="a", deps="deps")

    async def read() -> object:
        found = current_run_context()
        return found.deps if found else None

    with use_run_context(context):
        assert await asyncio.create_task(read()) == "deps"


async def test_emitter_delivers_to_sync_and_async_subscribers_in_order() -> None:
    emitter = EventEmitter()
    received: list[str] = []

    def sync_subscriber(event: RunEvent) -> None:
        received.append(f"sync:{event.agent}")

    async def async_subscriber(event: RunEvent) -> None:
        received.append(f"async:{event.agent}")

    emitter.subscribe(sync_subscriber)
    unsubscribe = emitter.subscribe(async_subscriber)
    await emitter.emit(_event("a"))
    unsubscribe()
    unsubscribe()
    await emitter.emit(_event("b"))
    assert received == ["sync:a", "async:a", "sync:b"]


async def test_failing_subscriber_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    emitter = EventEmitter()
    received: list[RunEvent] = []

    def broken(event: RunEvent) -> None:
        raise RuntimeError("boom")

    emitter.subscribe(broken)
    emitter.subscribe(received.append)
    context = RunContext(run_id="r", agent="a", deps=None, events=emitter)
    with caplog.at_level(logging.ERROR, logger="awa.runtime"):
        await context.emit(_event())
    assert len(received) == 1
    assert "subscriber failed" in caplog.text


async def test_flush_requeues_the_undelivered_events_when_a_delivery_is_cancelled() -> None:
    emitter = EventEmitter()
    delivered: list[str] = []

    async def subscriber(event: RunEvent) -> None:
        delivered.append(event.agent)
        if event.agent == "b" and delivered.count("b") == 1:
            raise asyncio.CancelledError

    emitter.subscribe(subscriber)
    for name in ("a", "b", "c", "d"):
        emitter.emit_later(_event(name))

    with pytest.raises(asyncio.CancelledError):
        await emitter.flush()
    await emitter.flush()

    assert delivered == ["a", "b", "c", "d"]


def test_a_nested_run_cannot_change_the_session() -> None:
    parent = new_run_context("outer", None, None, {"awa.session_id": "sess-1"})
    with use_run_context(parent):
        assert new_run_context(
            "inner", None, None, {"awa.session_id": "sess-1"}
        ).ids.session_id == ("sess-1")
        with pytest.raises(ConfigurationError, match="cannot change the session"):
            new_run_context("inner", None, None, {"awa.session_id": "sess-2"})


async def test_subtree_subscriber_forwards_only_the_runs_below_its_root() -> None:
    from awa.runtime._run import SubtreeSubscriber

    seen: list[str] = []

    async def collect(event: RunEvent) -> None:
        seen.append(event.run_id)

    subscriber = SubtreeSubscriber("root", collect)
    started = [
        AgentStartedEvent(run_id="root", root_run_id="root", agent="a"),
        AgentStartedEvent(run_id="child", root_run_id="root", parent_run_id="root", agent="b"),
        AgentStartedEvent(run_id="grand", root_run_id="root", parent_run_id="child", agent="c"),
        AgentStartedEvent(run_id="other", root_run_id="x", agent="d"),
        AgentStartedEvent(run_id="sibling", root_run_id="x", parent_run_id="other", agent="e"),
    ]
    for event in started:
        outcome = subscriber(event)
        if outcome is not None:
            await outcome

    assert seen == ["root", "child", "grand"]


def test_outside_a_case_the_default_or_the_working_directory_applies(tmp_path: Path) -> None:
    assert active_case_dir() is None
    assert case_dir(tmp_path) == tmp_path
    assert case_dir() == Path.cwd()


def test_use_case_dir_sets_and_restores_the_folder(tmp_path: Path) -> None:
    with use_case_dir(tmp_path):
        assert active_case_dir() == tmp_path
        assert case_dir("elsewhere") == tmp_path
    assert active_case_dir() is None


def test_use_case_dir_none_leaves_the_current_folder(tmp_path: Path) -> None:
    with use_case_dir(tmp_path), use_case_dir(None):
        assert active_case_dir() == tmp_path
    with use_case_dir(None):
        assert active_case_dir() is None


def test_use_case_dir_nests(tmp_path: Path) -> None:
    inner = tmp_path / "inner"
    with use_case_dir(tmp_path):
        with use_case_dir(inner):
            assert active_case_dir() == inner
        assert active_case_dir() == tmp_path


def test_in_case_dir_prefers_the_case_copy_when_it_exists(tmp_path: Path) -> None:
    base = tmp_path / "base"
    case = tmp_path / "case"
    (case / "data").mkdir(parents=True)
    assert in_case_dir(base, "data") == base / "data"
    with use_case_dir(case):
        assert in_case_dir(base, "data") == case / "data"
        assert in_case_dir(base, "spec.yaml") == base / "spec.yaml"


async def test_the_case_folder_is_isolated_per_task(tmp_path: Path) -> None:
    seen: dict[str, Path | None] = {}

    async def case(name: str) -> None:
        with use_case_dir(tmp_path / name):
            await asyncio.sleep(0)
            seen[name] = active_case_dir()

    await asyncio.gather(case("a"), case("b"))

    assert seen == {"a": tmp_path / "a", "b": tmp_path / "b"}
    assert active_case_dir() is None


@tool(approval=True, risk=["external_action"])
def refund(order_id: str) -> str:
    """Refund an order."""
    return f"refunded {order_id}"


def _names(events: list[RunEvent]) -> list[tuple[str, str]]:
    return [(event.type, event.agent) for event in events]


async def test_a_global_subscriber_sees_every_run_and_is_removed() -> None:
    events: list[RunEvent] = []
    unsubscribe = subscribe_globally(events.append)
    try:
        await Agent("one", model=FakeChatModel(["a"])).run("hi")
        await Agent("two", model=FakeChatModel(["b"])).run("hi")
    finally:
        unsubscribe()
    await Agent("three", model=FakeChatModel(["c"])).run("hi")
    unsubscribe()  # a second call does nothing

    started = [e.agent for e in events if isinstance(e, AgentStartedEvent)]
    assert started == ["one", "two"]
    assert events.append not in global_subscribers()


async def test_nested_runs_are_seen_once() -> None:
    member = Agent("member", description="Answers.", model=FakeChatModel(["member answer"]))

    def lead_model(call: ModelCall) -> Any:
        if not any(m.type == "tool" for m in call.messages):
            return tool_call("delegate_task", {"agent": "member", "task": "answer it"})
        return "final"

    team = Team(
        "desk", orchestrator=Agent("lead", model=FunctionChatModel(lead_model)), members=[member]
    )
    events: list[RunEvent] = []

    with use_global_subscriber(events.append):
        streamed = [event async for event in team.stream("question")]

    started = [e.agent for e in events if isinstance(e, AgentStartedEvent)]
    assert started.count("member") == 1 and started.count("lead") == 1
    assert _names(events) == _names([e for e in streamed if e.type != "run_completed"])


async def test_global_subscribers_come_after_the_runs_own() -> None:
    order: list[str] = []
    agent = Agent("ordered", model=FakeChatModel(["x"]), hooks=[lambda e: order.append("own")])

    with use_global_subscriber(lambda event: order.append("global")):
        await agent.run("hi")

    assert order[:2] == ["own", "global"]


async def test_resumed_runs_reach_global_subscribers() -> None:
    agent = Agent(
        "clerk",
        model=FakeChatModel([tool_call("refund", {"order_id": "A-1"}), "Refunded."]),
        tools=[refund],
        checkpointer=InMemorySaver(),
    )
    paused = await agent.run("refund A-1")
    assert paused.is_interrupted and paused.thread_id is not None

    events: list[RunEvent] = []
    with use_global_subscriber(events.append):
        done = await agent.resume(paused.thread_id, {"action": "approve"})

    assert done.output == "Refunded."
    assert [e.type for e in events] == [
        "agent_started",
        "approval_decided",
        "tool_result",
        "model_response",
        "agent_finished",
    ]
    assert isinstance(events[-1], AgentFinishedEvent) and events[-1].agent == "clerk"


async def test_failing_and_async_subscribers_never_fail_a_run() -> None:
    seen: list[str] = []

    def broken(event: RunEvent) -> None:
        raise RuntimeError("observer bug")

    async def recording(event: RunEvent) -> None:
        seen.append(event.type)

    with use_global_subscriber(broken), use_global_subscriber(recording):
        result = await Agent("safe", model=FakeChatModel(["ok"])).run("hi")

    assert result.output == "ok"
    assert "agent_started" in seen and "agent_finished" in seen


async def test_the_same_subscriber_can_be_registered_twice() -> None:
    events: list[RunEvent] = []
    first = subscribe_globally(events.append)
    second = subscribe_globally(events.append)
    try:
        await Agent("twice", model=FakeChatModel(["x"])).run("hi")
        doubled = len(events)
        first()
        events.clear()
        await Agent("once", model=FakeChatModel(["x"])).run("hi")
    finally:
        second()

    assert doubled == 2 * len(events)


def test_registration_is_thread_safe() -> None:
    subscribers = [lambda event, i=i: None for i in range(200)]
    removers: list[Any] = []
    lock = threading.Lock()

    def register(chunk: list[Any]) -> None:
        for subscriber in chunk:
            remove = subscribe_globally(subscriber)
            with lock:
                removers.append(remove)

    threads = [threading.Thread(target=register, args=(subscribers[i::8],)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len([s for s in global_subscribers() if s in subscribers]) == 200

    threads = [threading.Thread(target=lambda r=r: r()) for r in removers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not [s for s in global_subscribers() if s in subscribers]


def test_a_subscriber_must_be_callable() -> None:
    with pytest.raises(TypeError, match="callable"):
        subscribe_globally("audit")  # type: ignore[arg-type]


def test_the_system_clock_reads_utc_and_a_monotonic_counter() -> None:
    clock = SystemClock()

    assert clock.now().tzinfo is UTC
    first = clock.monotonic()
    assert clock.monotonic() >= first


async def test_the_system_clock_really_waits_and_never_for_a_negative_time() -> None:
    clock = SystemClock()
    before = clock.monotonic()

    await clock.sleep(0.02)
    await clock.sleep(-5)

    assert 0.015 <= clock.monotonic() - before < 2


def test_as_clock_accepts_a_clock_or_nothing() -> None:
    fake = FakeClock()

    assert isinstance(as_clock(None), SystemClock)
    assert as_clock(fake) is fake
    assert isinstance(SystemClock(), Clock) and isinstance(fake, Clock)
    assert not isinstance(lambda: datetime.now(UTC), Clock)


def test_a_fake_clock_moves_only_when_told_and_waiting_advances_it() -> None:
    clock = FakeClock(datetime(2026, 5, 1, 12, 0, tzinfo=UTC))

    assert clock.now() == clock() == datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    clock.advance(minutes=5)
    clock.advance(timedelta(seconds=30), hours=1)
    assert clock.now() == datetime(2026, 5, 1, 13, 5, 30, tzinfo=UTC)
    assert clock.monotonic() == 3930.0


async def test_a_fake_clock_sleep_advances_instead_of_waiting() -> None:
    clock = FakeClock()
    start = clock.now()

    await clock.sleep(3600)
    await clock.sleep(0)

    assert clock.now() - start == timedelta(hours=1, milliseconds=1)


def test_a_fake_clock_refuses_a_naive_start_and_going_back() -> None:
    with pytest.raises(ValueError, match="aware"):
        FakeClock(datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="backwards"):
        FakeClock().advance(seconds=-1)


async def test_a_fake_clock_drives_an_existing_clock_parameter() -> None:
    clock = FakeClock()
    queue = InMemoryEventQueue(clock=clock)
    await queue.send("later", delay="10m")

    assert await queue.receive() == []
    clock.advance(minutes=10)
    assert [m.payload for m in await queue.receive()] == ["later"]


def test_fixed_ids_count_per_prefix_and_look_like_the_real_ones() -> None:
    ids: IdSource = FixedIds()

    assert [ids.new_id("evt"), ids.new_id("evt"), ids.new_id("run")] == [
        "evt_000000000001",
        "evt_000000000002",
        "run_000000000001",
    ]
    assert FixedIds().new_id("evt") == "evt_000000000001"
    assert len(UuidIds().new_id("evt")) == len(new_id("evt")) == len("evt_000000000001")
    assert UuidIds().new_id("evt") != UuidIds().new_id("evt")


def test_fake_transport_is_the_connector_mock_and_loads_on_first_use() -> None:
    import awa.testing
    from awa.connectors import MockTransport, connector_tools, mock_response

    assert awa.testing.FakeTransport is MockTransport
    transport = awa.testing.FakeTransport(
        {"GET /rest/api/3/search/jql": mock_response(json={"issues": [{"key": "OPS-1"}]})}
    )
    env = {
        "JIRA_URL": "https://example.atlassian.net",
        "JIRA_EMAIL": "a@b.c",
        "JIRA_API_TOKEN": "t",
    }
    tools = connector_tools("jira", env=env, transport=transport)

    assert tools and transport.requests == []
    with pytest.raises(AttributeError, match="no attribute 'Nope'"):
        _ = awa.testing.Nope  # type: ignore[attr-defined]


def test_importing_the_testing_package_does_not_load_the_connector_catalog() -> None:
    import subprocess

    code = "import sys, awa.testing; sys.exit('awa.connectors' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], check=False)

    assert result.returncode == 0


def test_new_id_is_prefixed() -> None:
    assert new_id("run").startswith("run_")


def test_new_id_generates_unique_values() -> None:
    assert new_id("run") != new_id("run")


def test_the_layout_is_rfc_9562_version_7(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ids_module.time, "time_ns", lambda: 0x0123456789AB * 1_000_000)
    text = Uuid7Ids().new_hex()
    value = uuid.UUID(hex=text)

    assert value.version == 7
    assert value.variant == uuid.RFC_4122
    assert text[:12] == "0123456789ab"
    assert text[13:16] == "000"


def test_ids_made_in_one_millisecond_still_sort_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ids_module.time, "time_ns", lambda: 1_790_000_000_000 * 1_000_000)
    source = Uuid7Ids()

    made = [source.new_id("res") for _ in range(50)]

    assert made == sorted(made)
    assert len(set(made)) == 50
    assert all(value.startswith("res_") and len(value) == 36 for value in made)


def test_a_clock_that_steps_back_does_not_break_the_order(monkeypatch: pytest.MonkeyPatch) -> None:
    readings = iter([2_000, 1_000, 1_000])
    monkeypatch.setattr(ids_module.time, "time_ns", lambda: next(readings) * 1_000_000)
    source = Uuid7Ids()

    made = [source.new_id("x") for _ in range(3)]

    assert made == sorted(made)


def test_the_counter_borrows_the_next_millisecond_when_it_runs_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ids_module.time, "time_ns", lambda: 5_000 * 1_000_000)
    source = Uuid7Ids()

    made = [source.new_id("x") for _ in range(4097)]

    assert made == sorted(made)
    assert int(made[-1][2:14], 16) == 5_001


def test_the_module_level_uuid7_is_monotonic_within_one_millisecond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ids_module.time, "time_ns", lambda: 1_790_000_000_000 * 1_000_000)

    made = [ids_module.uuid7() for _ in range(100)]

    assert made == sorted(made)
    assert len(set(made)) == 100
    assert all(value.version == 7 for value in made)
