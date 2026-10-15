"""Agent streams, threads and `stream_run`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import interrupt
from pydantic import BaseModel

from awa.agents import Agent, AgentHooks, RunTrackingMiddleware
from awa.contracts import (
    RUN_EVENT_ADAPTER,
    AgentFinishedEvent,
    AgentStartedEvent,
    ErrorEvent,
    ModelResponseEvent,
    RunCompletedEvent,
    RunEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
    UsageLimits,
)
from awa.errors import AgentRunError, AwaError, UsageLimitExceededError
from awa.runtime import RunContext, new_run_id, use_run_context
from awa.runtime._streaming import stream_run
from awa.testing import FakeChatModel, ModelCall, structured_output, tool_call


def streaming_model(responses: list[Any]) -> FakeChatModel:
    """A scripted model that streams its text in three-character chunks."""
    return FakeChatModel(responses, stream_chunk_size=3)


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def _types(events: list[RunEvent]) -> list[str]:
    return [event.type for event in events]


async def _collect(stream: AsyncIterator[RunEvent]) -> list[RunEvent]:
    return [event async for event in stream]


async def test_stream_event_order_for_a_tool_loop() -> None:
    model = FakeChatModel([tool_call("add", {"a": 2, "b": 3}), "Five."])
    agent = Agent("calc", model=model, tools=[add])

    events = await _collect(agent.stream("2+3?"))

    assert _types(events) == [
        "agent_started",
        "model_response",
        "tool_call",
        "tool_result",
        "model_response",
        "agent_finished",
        "run_completed",
    ]
    started, first, call, result, _, finished, completed = events
    assert isinstance(started, AgentStartedEvent) and started.input_preview == "2+3?"
    assert isinstance(first, ModelResponseEvent) and first.tool_call_names == ("add",)
    assert isinstance(call, ToolCallEvent) and call.arguments == {"a": 2, "b": 3}
    assert isinstance(result, ToolResultEvent)
    assert (result.content, result.call_id, result.is_error) == ("5", call.call_id, False)
    assert isinstance(finished, AgentFinishedEvent) and finished.output_preview == "Five."
    assert isinstance(completed, RunCompletedEvent)
    assert completed.result.output == "Five."
    assert {e.run_id for e in events} == {completed.result.run_id}
    assert all(e.depth == 0 and e.agent == "calc" for e in events)


async def test_stream_yields_text_deltas_from_streaming_models() -> None:
    agent = Agent("talker", model=streaming_model(["Hello streaming world"]))

    events = await _collect(agent.stream("hi"))

    deltas = [e for e in events if isinstance(e, TextDeltaEvent)]
    assert "".join(d.delta for d in deltas) == "Hello streaming world"
    assert len(deltas) > 1
    assert _types(events).index("text_delta") < _types(events).index("model_response")
    completed = events[-1]
    assert isinstance(completed, RunCompletedEvent)
    assert completed.result.output == "Hello streaming world"


async def test_run_does_not_emit_text_deltas() -> None:
    received: list[RunEvent] = []
    agent = Agent("talker", model=streaming_model(["Hello"]), hooks=[received.append])

    await agent.run("hi")

    assert "text_delta" not in _types(received)


async def test_structured_output_calls_are_not_reported_as_tool_calls() -> None:
    class Answer(BaseModel):
        value: int

    agent = Agent(
        "typed", model=FakeChatModel([structured_output(Answer, {"value": 1})]), output_type=Answer
    )

    events = await _collect(agent.stream("go"))

    assert "tool_call" not in _types(events)
    completed = events[-1]
    assert isinstance(completed, RunCompletedEvent) and completed.result.output == Answer(value=1)


async def test_stream_includes_nested_runs_with_their_depth() -> None:
    inner = Agent(
        "inner",
        description="Adds numbers.",
        model=streaming_model([tool_call("add", {"a": 1, "b": 2}), "three"]),
        tools=[add],
    )
    outer = Agent(
        "outer",
        model=streaming_model([tool_call("inner", {"task": "1+2"}), "It is three."]),
        tools=[inner.as_tool()],
    )

    events = await _collect(outer.stream("go"))

    inner_events = [e for e in events if e.agent == "inner"]
    assert {e.depth for e in inner_events} == {1}
    assert _types(inner_events) == [
        "agent_started",
        "model_response",
        "tool_call",
        "tool_result",
        "text_delta",
        "text_delta",
        "model_response",
        "agent_finished",
    ]
    assert "".join(e.delta for e in inner_events if isinstance(e, TextDeltaEvent)) == "three"
    outer_deltas = [e for e in events if isinstance(e, TextDeltaEvent) and e.agent == "outer"]
    assert "".join(d.delta for d in outer_deltas) == "It is three."
    started = next(e for e in inner_events if isinstance(e, AgentStartedEvent))
    completed = events[-1]
    assert isinstance(completed, RunCompletedEvent)
    assert started.parent_run_id == completed.result.run_id
    assert started.root_run_id == completed.result.run_id


async def test_stream_raises_after_yielding_the_error_event() -> None:
    model = FakeChatModel([tool_call("add", {"a": 1, "b": 1})], repeat_last=True)
    agent = Agent("loop", model=model, tools=[add])
    events: list[RunEvent] = []

    with pytest.raises(UsageLimitExceededError):
        async for event in agent.stream("go", usage_limits=UsageLimits(max_requests=1)):
            events.append(event)

    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.error_type == "UsageLimitExceededError"
    assert "run_completed" not in _types(events)


async def test_closing_the_stream_early_cancels_the_run() -> None:
    gate = asyncio.Event()
    started = asyncio.Event()
    finished: list[bool] = []

    async def wait() -> str:
        """Wait for the gate."""
        started.set()
        try:
            await gate.wait()
        finally:
            finished.append(gate.is_set())
        return "done"

    agent = Agent("waiter", model=FakeChatModel([tool_call("wait")]), tools=[wait])
    stream = agent.stream("go")

    async for event in stream:
        if isinstance(event, ToolCallEvent):
            await started.wait()
            break
    await stream.aclose()  # type: ignore[attr-defined]

    assert finished == [False]


async def test_hooks_receive_every_event_of_the_run() -> None:
    class Recorder(AgentHooks):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def on_agent_start(self, event: AgentStartedEvent) -> None:
            self.calls.append(f"start:{event.agent}")

        async def on_model_response(self, event: ModelResponseEvent) -> None:
            self.calls.append("model")

        async def on_tool_call(self, event: ToolCallEvent) -> None:
            self.calls.append(f"call:{event.tool_name}")

        async def on_tool_result(self, event: ToolResultEvent) -> None:
            self.calls.append(f"result:{event.content}")

        async def on_agent_end(self, event: AgentFinishedEvent) -> None:
            self.calls.append(f"end:{event.agent}")

    recorder = Recorder()
    plain: list[RunEvent] = []
    agent = Agent(
        "calc",
        model=FakeChatModel([tool_call("add", {"a": 1, "b": 1}), "2"]),
        tools=[add],
        hooks=[recorder, plain.append],
    )

    await agent.run("1+1")

    assert recorder.calls == ["start:calc", "model", "call:add", "result:2", "model", "end:calc"]
    assert _types(plain)[0] == "agent_started" and _types(plain)[-1] == "agent_finished"


async def test_hooks_receive_errors() -> None:
    errors: list[ErrorEvent] = []

    class OnError(AgentHooks):
        async def on_error(self, event: ErrorEvent) -> None:
            errors.append(event)

    def explode() -> str:
        """Fails."""
        raise RuntimeError("boom")

    agent = Agent(
        "fragile", model=FakeChatModel([tool_call("explode")]), tools=[explode], hooks=[OnError()]
    )

    with pytest.raises(RuntimeError):
        await agent.run("go")

    assert [e.message for e in errors] == ["boom"]


async def test_failing_tool_reports_an_error_result() -> None:
    def explode() -> str:
        """Fails."""
        raise RuntimeError("boom")

    events: list[RunEvent] = []
    agent = Agent(
        "fragile",
        model=FakeChatModel([tool_call("explode")]),
        tools=[explode],
        hooks=[events.append],
    )

    with pytest.raises(RuntimeError):
        await agent.run("go")

    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.is_error and "boom" in result.content
    assert _types(events)[-1] == "error"


async def test_tool_error_messages_are_flagged() -> None:
    events: list[RunEvent] = []
    agent = Agent(
        "caller",
        model=FakeChatModel([tool_call("add", {"a": "not a number"}), "sorry"]),
        tools=[add],
        hooks=[events.append],
    )

    await agent.run("go")

    result = next(e for e in events if isinstance(e, ToolResultEvent))
    assert result.is_error


async def test_hook_failures_do_not_fail_the_run() -> None:
    def broken(event: RunEvent) -> None:
        raise RuntimeError("observer bug")

    agent = Agent("robust", model=FakeChatModel(["fine"]), hooks=[broken])

    assert (await agent.run("go")).output == "fine"


async def test_events_serialise_with_the_run_event_adapter() -> None:
    agent = Agent(
        "calc", model=FakeChatModel([tool_call("add", {"a": 1, "b": 1}), "2"]), tools=[add]
    )

    events = await _collect(agent.stream("go"))

    for event in events[:-1]:
        assert RUN_EVENT_ADAPTER.validate_json(RUN_EVENT_ADAPTER.dump_json(event)) == event


def _streaming_inner() -> Agent:
    return Agent("inner", model=streaming_model(["abcdef"]))


async def test_stream_inside_a_tool_of_a_plain_run() -> None:
    inner = _streaming_inner()
    inner_events: list[RunEvent] = []

    async def consult(task: str) -> str:
        """Stream the inner agent."""
        async for event in inner.stream(task):
            inner_events.append(event)
        return "consulted"

    outer_events: list[RunEvent] = []
    outer = Agent(
        "outer",
        model=streaming_model([tool_call("consult", {"task": "t"}), "done"]),
        tools=[consult],
        hooks=[outer_events.append],
    )

    await outer.run("go")

    inner_deltas = [e.delta for e in inner_events if isinstance(e, TextDeltaEvent)]
    assert inner_deltas == ["abc", "def"]
    assert {e.agent for e in inner_events} == {"inner"}
    assert [e.agent for e in outer_events if isinstance(e, TextDeltaEvent)] == ["inner", "inner"]


async def test_nested_streams_emit_each_delta_once() -> None:
    inner = _streaming_inner()

    async def consult(task: str) -> str:
        """Stream the inner agent."""
        return "".join([e.delta async for e in inner.stream(task) if isinstance(e, TextDeltaEvent)])

    outer = Agent(
        "outer",
        model=streaming_model([tool_call("consult", {"task": "t"}), "done"]),
        tools=[consult],
    )

    events = await _collect(outer.stream("go"))

    deltas = [(e.agent, e.delta) for e in events if isinstance(e, TextDeltaEvent)]
    assert deltas == [("inner", "abc"), ("inner", "def"), ("outer", "don"), ("outer", "e")]
    completed = events[-1]
    assert isinstance(completed, RunCompletedEvent)
    assert completed.result.messages[2].content == "abcdef"


def approve(action: str) -> str:
    """Ask a human to approve an action."""
    decision = interrupt({"action": action})
    return f"decision: {decision}"


class HoldFirstCall(AgentMiddleware):
    """Runs the turn's first tool call only after the later call finished (paused)."""

    def __init__(self) -> None:
        super().__init__()
        self._other_done = asyncio.Event()

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        if request.tool_call["args"].get("action") != "first":
            try:
                return await handler(request)
            finally:
                self._other_done.set()
        async with asyncio.timeout(10):
            await self._other_done.wait()
        return await handler(request)


def parallel_approvals() -> list[Any]:
    return [tool_call("approve", {"action": "first"}), tool_call("approve", {"action": "second"})]


async def test_parallel_interrupts_are_reported_in_tool_call_order() -> None:
    agent = Agent(
        "gate",
        model=FakeChatModel([parallel_approvals()]),
        tools=[approve],
        middleware=[HoldFirstCall()],
        checkpointer=InMemorySaver(),
    )

    result = await agent.run("go", thread_id="t1")

    assert [i["value"]["action"] for i in result.interrupts] == ["first", "second"]


async def test_parallel_interrupts_of_a_team_member_are_reported_in_tool_call_order() -> None:
    from awa.team import Team

    lead = Agent(
        "lead",
        model=FakeChatModel([parallel_approvals()]),
        tools=[approve],
        middleware=[HoldFirstCall()],
    )
    team = Team(
        "desk",
        orchestrator=lead,
        members=[Agent("helper", model=FakeChatModel(["ok"]))],
        checkpointer=InMemorySaver(),
    )

    result = await team.run("go", thread_id="t1")

    assert [i["value"]["action"] for i in result.interrupts] == ["first", "second"]


# --------------------------------------------------------------------------- threads


async def test_thread_id_requires_a_checkpointer() -> None:
    agent = Agent("stateless", model=FakeChatModel(["x"]))

    with pytest.raises(ValueError, match="checkpointer"):
        await agent.run("hi", thread_id="t1")
    with pytest.raises(ValueError, match="checkpointer"):
        await agent.resume("t1", "yes")


async def test_threads_persist_the_conversation() -> None:
    model = FakeChatModel(["first answer", "second answer"])
    agent = Agent("chat", model=model, checkpointer=InMemorySaver())

    first = await agent.run("first question", thread_id="t1")
    second = await agent.run("second question", thread_id="t1")

    assert first.thread_id == second.thread_id == "t1"
    assert [m.content for m in model.calls[1].messages] == [
        "first question",
        "first answer",
        "second question",
    ]
    assert second.new_messages_start == 2
    assert [m.content for m in second.new_messages()] == ["second question", "second answer"]


async def test_a_checkpointer_without_thread_id_starts_a_new_thread() -> None:
    model = FakeChatModel(["a", "b"])
    agent = Agent("chat", model=model, checkpointer=InMemorySaver())

    first = await agent.run("one")
    second = await agent.run("two")

    assert first.thread_id and second.thread_id and first.thread_id != second.thread_id
    assert len(model.calls[1].messages) == 1


async def test_store_is_available_to_the_graph() -> None:
    store = InMemoryStore()
    agent = Agent("memory", model=FakeChatModel(["ok"]), store=store)

    assert (await agent.run("go")).output == "ok"


# --------------------------------------------------------------------------- interrupts


async def test_interrupt_is_returned_and_resume_completes_the_run() -> None:
    model = FakeChatModel([tool_call("approve", {"action": "deploy"}), "Deployed."])
    agent = Agent("operator", model=model, tools=[approve], checkpointer=InMemorySaver())

    paused = await agent.run("deploy it")

    assert paused.is_interrupted
    assert paused.output is None
    assert paused.interrupts[0]["value"] == {"action": "deploy"}
    assert paused.interrupts[0]["id"]
    assert paused.thread_id is not None

    resumed = await agent.resume(paused.thread_id, "approved")

    assert not resumed.is_interrupted
    assert resumed.output == "Deployed."
    assert resumed.messages[2].content == "decision: approved"
    assert [m.content for m in resumed.new_messages()] == ["decision: approved", "Deployed."]


async def test_resume_emits_events() -> None:
    events: list[RunEvent] = []
    model = FakeChatModel([tool_call("approve", {"action": "x"}), "ok"])
    agent = Agent(
        "operator",
        model=model,
        tools=[approve],
        checkpointer=InMemorySaver(),
        hooks=[events.append],
    )

    paused = await agent.run("go")
    assert paused.thread_id is not None
    events.clear()
    await agent.resume(paused.thread_id, "yes")

    types = [e.type for e in events]
    assert types[0] == "agent_started" and types[-1] == "agent_finished"
    assert "tool_result" in types


# --------------------------------------------------------------------------- middleware


async def test_tracking_middleware_works_in_a_hand_built_graph() -> None:
    graph = create_agent(
        FakeChatModel([tool_call("add", {"a": 1, "b": 1}), "2"]),
        tools=[add],
        middleware=[RunTrackingMiddleware()],
    )
    events: list[RunEvent] = []
    ctx: RunContext[Any] = RunContext(run_id=new_run_id(), agent="manual", deps=None)
    ctx.events.subscribe(events.append)

    with use_run_context(ctx):
        await graph.ainvoke({"messages": [HumanMessage("1+1")]})

    assert [type(e) for e in events] == [
        ModelResponseEvent,
        ToolCallEvent,
        ToolResultEvent,
        ModelResponseEvent,
    ]
    assert ctx.usage.requests == 2 and ctx.usage.tool_calls == 1
    assert ctx.usage.by_agent["manual"].requests == 2


async def test_tracking_middleware_passes_through_without_a_run_context() -> None:
    graph = create_agent(FakeChatModel(["plain"]), middleware=[RunTrackingMiddleware()])

    result = await graph.ainvoke({"messages": [HumanMessage("hi")]})

    assert result["messages"][-1].content == "plain"


async def test_tracking_middleware_checks_the_request_limit_before_calling() -> None:
    calls: list[ModelCall] = []

    def respond(call: ModelCall) -> str:
        calls.append(call)
        return "x"

    graph = create_agent(FakeChatModel([respond]), middleware=[RunTrackingMiddleware()])
    ctx: RunContext[Any] = RunContext(
        run_id=new_run_id(), agent="manual", deps=None, limits=UsageLimits(max_requests=0)
    )

    with use_run_context(ctx), pytest.raises(UsageLimitExceededError, match="max_requests=0"):
        await graph.ainvoke({"messages": [HumanMessage("hi")]})

    assert calls == []


async def test_user_supplied_tracking_middleware_is_not_duplicated() -> None:
    agent = Agent("custom", model=FakeChatModel(["ok"]), middleware=[RunTrackingMiddleware()])

    result = await agent.run("go")

    assert result.usage.requests == 1


async def test_resuming_a_finished_thread_says_what_to_do() -> None:
    model = FakeChatModel([tool_call("approve", {"action": "deploy"}), "Deployed."])
    agent = Agent("operator", model=model, tools=[approve], checkpointer=InMemorySaver())
    paused = await agent.run("deploy it")
    assert paused.thread_id is not None
    await agent.resume(paused.thread_id, "approved")

    with pytest.raises(AgentRunError, match="start a new run") as caught:
        await agent.resume(paused.thread_id, "approved")
    assert "pending approval" in str(caught.value)


def _stream(agent: Agent[Any, Any], text: str) -> Any:
    ctx: RunContext[Any] = RunContext(run_id="stream-run", agent=agent._name, deps=None)

    def start(subscriber: Any) -> Any:
        return agent._execute(
            ctx, *agent._graph_input(text, None), thread_id=None, timeout=None,
            stream_text=True, subscribers=(subscriber,),
        )  # fmt: skip

    return stream_run(start, ctx.event_fields)


async def test_text_deltas_arrive_in_order_and_the_result_comes_last() -> None:
    agent = Agent("talker", model=FakeChatModel(["Hello streaming world"], stream_chunk_size=4))

    events = [event async for event in _stream(agent, "hi")]

    deltas = [e.delta for e in events if isinstance(e, TextDeltaEvent)]
    assert len(deltas) > 1 and "".join(deltas) == "Hello streaming world"
    assert isinstance(events[-1], RunCompletedEvent)
    assert events[-1].result.output == "Hello streaming world"
    assert [e.type for e in events].count("run_completed") == 1


async def test_a_consumer_that_stops_early_cancels_the_run_task() -> None:
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def work(subscriber: Any) -> Any:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    stream = stream_run(work, lambda: {})
    consumer = asyncio.create_task(anext(stream))  # waits for the first event
    await started.wait()
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await stream.aclose()

    assert cancelled == [True]
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


async def test_the_events_before_a_failure_are_delivered_and_the_error_propagates() -> None:
    seen: list[RunEvent] = []

    class Boom(AwaError):
        pass

    async def work(subscriber: Any) -> Any:
        await asyncio.sleep(0)
        raise Boom("the run failed")

    with pytest.raises(Boom, match="the run failed"):
        async for event in stream_run(work, lambda: {}):
            seen.append(event)

    assert seen == []
