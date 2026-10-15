"""Run timeouts, `awa.runtime.cancel`, `RunHandle` and the cancellation events of an `Agent`."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

import awa.runtime
from _helpers.polling import TIMEOUT_FACTOR, until
from awa import Agent
from awa.contracts import ErrorEvent, RunEvent, RunResult, UsageLimits
from awa.contracts.events import RunCancelledEvent
from awa.errors import AwaError, ConfigurationError, RunCancelledError, RunTimeoutError
from awa.runtime import RunContext, RunHandle, current_run_context, use_run_id
from awa.runtime._run import run_observed
from awa.runtime.termination import ceilings, tree_guard
from awa.testing import FakeChatModel, tool_call
from awa.tools.approval import create_tool_approval_wrapper


@pytest.fixture(autouse=True)
async def _no_leaks() -> AsyncIterator[None]:
    """Run each test with asyncio debug mode and assert it leaves no task or run behind."""

    loop = asyncio.get_running_loop()
    debug = loop.get_debug()
    loop.set_debug(True)
    try:
        yield
        await _settle()
        current = asyncio.current_task()
        assert [t for t in asyncio.all_tasks() if t is not current] == []
        assert awa.runtime.active_runs() == []
        assert current is not None and current.cancelling() == 0
    finally:
        loop.set_debug(debug)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@tool
async def hang() -> str:
    """Wait for a very long time."""
    await asyncio.sleep(30)
    return "never"


def hanging_agent(name: str = "worker", **options: Any) -> Agent:
    return Agent(name, model=FakeChatModel([tool_call("hang")]), tools=[hang], **options)


async def start_hanging(name: str = "worker") -> RunHandle[Any]:
    """Start a hanging agent and return once it is inside its tool call."""

    events: list[RunEvent] = []
    handle = hanging_agent(name, hooks=[events.append]).start("go")
    await wait_for(events, "tool_call")
    return handle


def types(events: list[RunEvent]) -> list[str]:
    return [event.type for event in events]


async def wait_for(events: list[RunEvent], kind: str) -> RunEvent:
    for _ in range(500):
        for event in events:
            if event.type == kind:
                return event
        await asyncio.sleep(0.005)
    raise AssertionError(f"no {kind} event")


# ----------------------------------------------------------------------------- timeouts


async def test_timeout_stops_a_hanging_tool_and_raises_run_timeout_error() -> None:
    events: list[RunEvent] = []
    agent = hanging_agent(hooks=[events.append])

    # Room for the model step on a busy machine; the tool then hangs until the timeout.
    limit = 3.0 * TIMEOUT_FACTOR
    with pytest.raises(RunTimeoutError) as caught:
        await agent.run("go", timeout=limit)

    error = caught.value
    assert isinstance(error, TimeoutError) and isinstance(error, AwaError)
    assert error.retryable
    assert error.limit == limit and error.limit_name == "timeout"
    assert error.elapsed >= limit
    assert error.details["run_id"] == error.run_id
    assert types(events) == [
        "agent_started",
        "model_response",
        "tool_call",
        "run_cancelled",
        "error",
    ]
    cancelled, failed = events[-2], events[-1]
    assert isinstance(cancelled, RunCancelledEvent)
    assert cancelled.reason == "timeout" and cancelled.cancelled_by is None
    assert cancelled.elapsed_ms >= limit * 1000
    assert isinstance(failed, ErrorEvent) and failed.error_type == "RunTimeoutError"
    assert cancelled.run_id == failed.run_id == error.run_id


async def test_max_duration_bounds_the_run_like_a_timeout() -> None:
    with pytest.raises(RunTimeoutError) as caught:
        await hanging_agent().run("go", usage_limits=UsageLimits(max_duration=0.1))

    assert caught.value.limit_name == "max_duration"
    assert "max_duration=0.1s" in str(caught.value)


async def test_the_tighter_of_timeout_and_max_duration_applies() -> None:
    with pytest.raises(RunTimeoutError) as caught:
        await hanging_agent().run("go", timeout=0.1, usage_limits=UsageLimits(max_duration=20))

    assert caught.value.limit_name == "timeout"


@pytest.mark.parametrize("slow_event", ["agent_started", "agent_finished"])
async def test_a_timeout_inside_a_slow_subscriber_raises_run_timeout_error(slow_event: str) -> None:
    async def slow(event: RunEvent) -> None:
        if event.type == slow_event:
            await asyncio.sleep(5)

    agent = Agent("watched", model=FakeChatModel(["done"]), hooks=[slow])

    with pytest.raises(RunTimeoutError):
        await agent.run("go", timeout=0.1)


async def test_a_run_that_finishes_in_time_is_unaffected() -> None:
    agent = Agent("quick", model=FakeChatModel(["hi"]))

    result = await agent.run("hello", timeout=5)

    assert result.output == "hi"


@pytest.mark.parametrize("value", [0, -1.0])
async def test_timeout_must_be_positive(value: float) -> None:
    with pytest.raises(ValueError, match="timeout must be a positive"):
        await Agent("quick", model=FakeChatModel(["hi"])).run("hello", timeout=value)


def test_max_duration_must_be_positive() -> None:
    with pytest.raises(ValueError):
        UsageLimits(max_duration=0)


def test_run_sync_accepts_a_timeout() -> None:
    with pytest.raises(RunTimeoutError):
        hanging_agent().run_sync("go", timeout=0.1)


async def test_nested_runs_share_the_deadline_and_cannot_extend_it() -> None:
    events: list[RunEvent] = []
    inner = hanging_agent("inner")
    seen: dict[str, float | None] = {}

    @tool
    async def ask_inner() -> str:
        """Ask the inner agent."""
        ctx = current_run_context()
        assert ctx is not None
        seen["remaining"] = ctx.remaining_time()
        result = await inner.run("go", timeout=60)  # longer than what is left: ignored
        return result.text

    outer = Agent(
        "outer",
        model=FakeChatModel([tool_call("ask_inner")]),
        tools=[ask_inner],
        hooks=[events.append],
    )

    limit = 1.5 * TIMEOUT_FACTOR
    with pytest.raises(RunTimeoutError) as caught:
        await outer.run("go", timeout=limit)

    remaining = seen["remaining"]
    assert remaining is not None and 0 < remaining <= limit
    outer_started = next(e for e in events if e.type == "agent_started" and e.agent == "outer")
    assert caught.value.run_id == outer_started.run_id
    cancelled = [e for e in events if isinstance(e, RunCancelledEvent)]
    assert [e.agent for e in cancelled] == ["outer"]
    inner_errors = [e for e in events if isinstance(e, ErrorEvent) and e.agent == "inner"]
    assert [e.error_type for e in inner_errors] == ["CancelledError"]


async def test_a_nested_run_can_shorten_the_deadline() -> None:
    events: list[RunEvent] = []
    inner = hanging_agent("inner")

    @tool
    async def ask_inner() -> str:
        """Ask the inner agent."""
        result = await inner.run("go", timeout=0.1)
        return result.text

    outer = Agent(
        "outer",
        model=FakeChatModel([tool_call("ask_inner")]),
        tools=[ask_inner],
        hooks=[events.append],
    )

    with pytest.raises(RunTimeoutError) as caught:
        await outer.run("go", timeout=20 * TIMEOUT_FACTOR)

    inner_started = next(e for e in events if e.type == "agent_started" and e.agent == "inner")
    assert caught.value.run_id == inner_started.run_id
    cancelled = [e for e in events if isinstance(e, RunCancelledEvent)]
    assert [e.agent for e in cancelled] == ["inner"]


async def test_remaining_time_without_a_timeout_is_the_time_ceiling() -> None:
    # No run is unbounded: without a timeout the run tree gets AWA_RUN_MAX_SECONDS.
    seen: list[float | None] = []

    @tool
    async def probe() -> str:
        """Report the time left."""
        ctx = current_run_context()
        assert ctx is not None
        seen.append(ctx.remaining_time())
        return "ok"

    agent = Agent("probe", model=FakeChatModel([tool_call("probe"), "done"]), tools=[probe])
    await agent.run("go")

    [left] = seen
    assert left is not None and 3500 < left <= ceilings().run_max_seconds


async def test_remaining_time_works_in_a_sync_tool_on_a_worker_thread() -> None:
    seen: list[tuple[float | None, bool]] = []

    @tool
    def probe() -> str:
        """Report the time left."""
        ctx = current_run_context()
        assert ctx is not None
        seen.append((ctx.remaining_time(), threading.current_thread() is threading.main_thread()))
        return "ok"

    agent = Agent("probe", model=FakeChatModel([tool_call("probe"), "done"]), tools=[probe])
    await agent.run("go", timeout=30)

    [(remaining, on_main_thread)] = seen
    assert not on_main_thread
    assert remaining is not None and 0 < remaining <= 30


# ----------------------------------------------------------------------------- cancel by id


async def test_cancel_by_id_from_another_task_during_a_tool() -> None:
    events: list[RunEvent] = []
    agent = hanging_agent(hooks=[events.append])
    run = asyncio.create_task(agent.run("go"))

    started = await wait_for(events, "tool_call")
    assert awa.runtime.active_runs() == [started.root_run_id]
    assert awa.runtime.cancel(started.root_run_id, "stop pressed", cancelled_by="alice")

    with pytest.raises(RunCancelledError) as caught:
        await run
    error = caught.value
    assert (error.run_id, error.reason, error.cancelled_by) == (
        started.root_run_id,
        "stop pressed",
        "alice",
    )
    assert "by 'alice': stop pressed" in str(error)
    assert types(events)[-2:] == ["run_cancelled", "error"]
    cancelled = events[-2]
    assert isinstance(cancelled, RunCancelledEvent)
    assert (cancelled.reason, cancelled.cancelled_by) == ("stop pressed", "alice")
    assert isinstance(events[-1], ErrorEvent) and events[-1].error_type == "RunCancelledError"


async def test_cancel_while_the_model_is_running() -> None:
    gate = asyncio.Event()

    @tool
    async def fast() -> str:
        """A quick tool."""
        return "ok"

    class SlowModel(FakeChatModel):
        async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
            gate.set()
            await asyncio.sleep(30)
            return await super()._agenerate(*args, **kwargs)

    agent = Agent("thinker", model=SlowModel(["x"]), tools=[fast])
    handle = agent.start("go")
    await gate.wait()

    assert handle.cancel()
    with pytest.raises(RunCancelledError):
        await handle.result()


async def test_cancel_by_a_nested_run_id_stops_the_whole_tree() -> None:
    events: list[RunEvent] = []
    inner = hanging_agent("inner")

    @tool
    async def ask_inner() -> str:
        """Ask the inner agent."""
        return (await inner.run("go")).text

    outer = Agent(
        "outer",
        model=FakeChatModel([tool_call("ask_inner")]),
        tools=[ask_inner],
        hooks=[events.append],
    )
    run = asyncio.create_task(outer.run("go"))
    await until(lambda: any(e.type == "tool_call" and e.agent == "inner" for e in events))
    nested = next(e for e in events if e.agent == "inner")

    assert awa.runtime.cancel(nested.run_id)

    with pytest.raises(RunCancelledError) as caught:
        await run
    assert caught.value.run_id == nested.root_run_id
    assert [e.agent for e in events if isinstance(e, RunCancelledEvent)] == ["outer"]


async def test_cancel_returns_false_for_unknown_finished_or_already_stopping_runs() -> None:
    assert not awa.runtime.cancel("run_unknown")
    result = await Agent("quick", model=FakeChatModel(["hi"])).run("hello")
    assert not awa.runtime.cancel(result.run_id)

    handle = await start_hanging()
    assert handle.cancel()
    assert not awa.runtime.cancel(handle.run_id)
    with pytest.raises(RunCancelledError):
        await handle
    assert not handle.cancel()


async def test_cancel_from_another_thread() -> None:
    handle = await start_hanging()
    outcome: list[bool] = []
    thread = threading.Thread(target=lambda: outcome.append(awa.runtime.cancel(handle.run_id)))
    thread.start()
    thread.join()

    assert outcome == [True]
    with pytest.raises(RunCancelledError):
        await handle.result()


async def test_stream_consumers_see_the_cancel() -> None:
    agent = hanging_agent()
    seen: list[str] = []

    with pytest.raises(RunCancelledError):
        async for event in agent.stream("go"):
            seen.append(event.type)
            if event.type == "tool_call":
                assert awa.runtime.cancel(event.root_run_id, "user left")

    assert seen[-2:] == ["run_cancelled", "error"]


async def test_stream_timeout_ends_with_run_cancelled_and_error_events() -> None:
    seen: list[RunEvent] = []

    with pytest.raises(RunTimeoutError):
        async for event in hanging_agent().stream("go", timeout=0.1):
            seen.append(event)

    assert types(seen)[-2:] == ["run_cancelled", "error"]


async def test_closing_a_stream_is_a_plain_cancellation_without_run_cancelled_event() -> None:
    events: list[RunEvent] = []
    stream = hanging_agent(hooks=[events.append]).stream("go")
    async for event in stream:
        if event.type == "tool_call":
            break
    await stream.aclose()

    assert "run_cancelled" not in types(events)
    assert isinstance(events[-1], ErrorEvent) and events[-1].error_type == "CancelledError"


async def test_cancelling_the_caller_task_stays_a_plain_cancellation() -> None:
    events: list[RunEvent] = []
    run = asyncio.create_task(hanging_agent(hooks=[events.append]).run("go"))
    await wait_for(events, "tool_call")
    run.cancel()

    with pytest.raises(asyncio.CancelledError):
        await run


async def test_a_swallowed_cancellation_is_enforced_at_the_next_checkpoint() -> None:
    swallowed = asyncio.Event()
    running = asyncio.Event()

    @tool
    async def stubborn() -> str:
        """Ignore cancellation."""
        running.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            swallowed.set()
        return "kept going"

    agent = Agent("stubborn", model=FakeChatModel([tool_call("stubborn")]), tools=[stubborn])
    handle = agent.start("go")
    await running.wait()
    handle.cancel()

    with pytest.raises(RunCancelledError):
        await handle.result()
    assert swallowed.is_set()


async def test_checkpoint_is_a_no_op_outside_a_run() -> None:
    await awa.runtime.checkpoint()


# ----------------------------------------------------------------------------- run_observed


async def test_run_observed_cleans_up_when_setup_fails() -> None:
    ctx: RunContext[Any] = RunContext(run_id="setup-fails", agent="a", deps=None)

    async def work(started: float) -> RunResult[Any]:
        raise AssertionError("the work must not start")

    with (
        patch.object(RunContext, "settle_session", side_effect=RuntimeError("setup")),
        pytest.raises(RuntimeError, match="setup"),
    ):
        await run_observed(
            ctx,
            work,
            kind="agent",
            input_text="x",
            output_text=str,
            timeout=30,
            subscribers=[lambda event: None],
        )

    assert awa.runtime.active_runs() == []
    assert tree_guard(ctx) is None
    assert ctx.events._subscribers == []


# ----------------------------------------------------------------------------- RunHandle


async def test_start_returns_a_handle_with_the_run_id_and_result() -> None:
    handle = Agent("quick", model=FakeChatModel(["hi"])).start("hello")

    assert isinstance(handle, RunHandle)
    result = await handle
    assert result.run_id == handle.run_id and result.output == "hi"
    assert handle.done()
    assert repr(handle) == f"RunHandle({handle.run_id!r}, done)"


async def test_a_handle_cancelled_before_the_run_starts() -> None:
    events: list[RunEvent] = []
    handle = hanging_agent(hooks=[events.append]).start("go")

    assert handle.cancel("changed my mind", cancelled_by="bob")
    with pytest.raises(RunCancelledError) as caught:
        await handle.result()

    assert (caught.value.reason, caught.value.cancelled_by) == ("changed my mind", "bob")
    assert events == []


async def test_handle_cancel_from_a_worker_thread() -> None:
    handle = hanging_agent().start("go")  # the task has not started yet
    results: list[bool] = []
    listed: list[list[str]] = []

    def from_a_thread() -> None:
        results.append(handle.cancel("from a thread"))
        listed.append(awa.runtime.active_runs())

    worker = threading.Thread(target=from_a_thread)
    worker.start()
    worker.join()

    assert results == [True] and listed == [[]]
    with pytest.raises(RunCancelledError) as caught:
        await handle.result()
    assert caught.value.reason == "from a thread"


async def test_cancel_on_a_closed_loop_reports_false() -> None:
    loop = asyncio.new_event_loop()
    pending: Any = loop.create_future()  # a task stand-in: `RunHandle` only needs done/get_loop
    handle: RunHandle[Any] = RunHandle("r-closed", pending)
    loop.close()

    assert handle.cancel() is False


async def test_start_is_refused_inside_a_run() -> None:
    inner = Agent("inner", model=FakeChatModel(["hi"]))
    refused: list[str] = []

    @tool
    async def spawn() -> str:
        """Try to start a top-level run."""
        try:
            inner.start("x")
        except ConfigurationError as exc:
            refused.append(str(exc))
        return "ok"

    agent = Agent("outer", model=FakeChatModel([tool_call("spawn"), "done"]), tools=[spawn])
    await agent.run("go")

    assert refused and "Agent.start()" in refused[0]


def test_start_needs_a_running_loop() -> None:
    with pytest.raises(RuntimeError):
        Agent("quick", model=FakeChatModel(["hi"])).start("hello")


async def test_use_run_id_assigns_the_top_level_run_id() -> None:
    agent = Agent("quick", model=FakeChatModel(["a", "b"]))
    with use_run_id("request-42"):
        first = await agent.run("hello")
        second = await agent.run("again")  # sequential runs may reuse the id

    assert first.run_id == second.run_id == "request-42"


async def test_use_run_id_refuses_overlapping_runs() -> None:
    with use_run_id("request-7"):
        handle = hanging_agent().start("go")  # start assigns its own id
        events: list[RunEvent] = []
        running = asyncio.create_task(hanging_agent("second", hooks=[events.append]).run("go"))
        await wait_for(events, "tool_call")
        with pytest.raises(ConfigurationError, match="already active"):
            await Agent("third", model=FakeChatModel(["x"])).run("go")
    assert awa.runtime.cancel("request-7")
    with pytest.raises(RunCancelledError):
        await running
    handle.cancel()
    with pytest.raises(RunCancelledError):
        await handle


def test_use_run_id_rejects_an_empty_id() -> None:
    with pytest.raises(ValueError), use_run_id(""):
        pass


# ----------------------------------------------------------------------------- threads


APPROVALS: dict[str, bool] = {"slow": True}
TRANSFER_STARTED: list[asyncio.Event] = []


@tool
async def transfer(amount: int) -> str:
    """Transfer money."""
    for started in TRANSFER_STARTED:
        started.set()
    if APPROVALS["slow"]:
        await asyncio.sleep(30)
    return f"sent {amount}"


async def test_a_paused_run_is_not_active_and_a_cancelled_resume_stays_resumable() -> None:
    APPROVALS["slow"] = True
    agent = Agent(
        "payer",
        model=FakeChatModel([tool_call("transfer", {"amount": 5}), "Done."]),
        tools=[create_tool_approval_wrapper(transfer)],
        checkpointer=InMemorySaver(),
    )
    paused = await agent.run("pay")
    assert paused.is_interrupted
    assert paused.thread_id is not None
    assert not awa.runtime.cancel(paused.run_id)  # waiting for a human: nothing runs

    # Approved, but the tool hangs: the resumed run times out mid-tool.
    with pytest.raises(RunTimeoutError):
        await agent.resume(paused.thread_id, {"action": "approve"}, timeout=0.2)

    APPROVALS["slow"] = False
    final = await agent.resume(paused.thread_id, {"action": "approve"})
    assert final.output == "Done." and not final.is_interrupted


async def test_resume_continues_a_thread_that_was_cancelled_mid_run() -> None:
    APPROVALS["slow"] = True
    events: list[RunEvent] = []
    agent = Agent(
        "worker",
        model=FakeChatModel([tool_call("transfer", {"amount": 7}), "Finished."]),
        tools=[transfer],
        checkpointer=InMemorySaver(),
        hooks=[events.append],
    )
    started = asyncio.Event()
    TRANSFER_STARTED[:] = [started]
    handle = agent.start("go", thread_id="t-cancel")
    await started.wait()
    TRANSFER_STARTED.clear()
    handle.cancel()
    with pytest.raises(RunCancelledError):
        await handle

    APPROVALS["slow"] = False
    result = await agent.resume("t-cancel", None)

    assert result.output == "Finished."
    assert [m.type for m in result.new_messages()] == ["tool", "ai"]


# ----------------------------------------------------------------------------- configuration


async def test_thread_id_without_checkpointer_is_a_configuration_error() -> None:
    agent = Agent("quick", model=FakeChatModel(["hi"]))
    with pytest.raises(ConfigurationError, match="checkpointer"):
        await agent.run("hello", thread_id="t1")
    with pytest.raises(ValueError, match="checkpointer"):
        await agent.resume("t1", "x")


async def test_nested_usage_limits_are_a_configuration_error() -> None:
    inner = Agent("inner", model=FakeChatModel(["hi"]))
    raised: list[BaseException] = []

    @tool
    async def nested() -> str:
        """Run a nested agent with its own limits."""
        try:
            await inner.run("x", usage_limits=UsageLimits(max_requests=1))
        except ValueError as exc:
            raised.append(exc)
        return "ok"

    agent = Agent("outer", model=FakeChatModel([tool_call("nested"), "done"]), tools=[nested])
    await agent.run("go")

    assert len(raised) == 1 and isinstance(raised[0], ConfigurationError)


async def test_metadata_reaches_tools_and_nested_runs() -> None:
    seen: list[dict[str, Any]] = []

    @tool
    async def whoami() -> str:
        """Report the request metadata."""
        ctx = current_run_context()
        assert ctx is not None
        seen.append(
            {k: ctx.metadata[k] for k in ("awa.request_id", "awa.tenant") if k in ctx.metadata}
        )
        return "ok"

    inner = Agent("inner", model=FakeChatModel([tool_call("whoami"), "done"]), tools=[whoami])
    outer = Agent(
        "outer",
        model=FakeChatModel([tool_call("whoami"), tool_call("inner", {"task": "x"}), "done"]),
        tools=[whoami, inner.as_tool()],
    )

    await outer.run("go", metadata={"awa.request_id": "req-1", "awa.tenant": "acme"})

    assert seen == [{"awa.request_id": "req-1", "awa.tenant": "acme"}] * 2


async def test_runtime_errors_derive_from_awa_error() -> None:
    from awa.errors import AwaError, CallCycleError

    assert issubclass(CallCycleError, AwaError) and issubclass(CallCycleError, ValueError)
    with pytest.raises(AwaError, match="timeout must be a positive"):
        await Agent("quick", model=FakeChatModel(["hi"])).run("hello", timeout=0)
