"""`Sequential`, `Parallel` and `Loop` with scripted test doubles, structured outputs and real `create_agent` graphs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from awa.agents import Agent
from awa.contracts import (
    AgentCard,
    AgentFinishedEvent,
    AgentInput,
    AgentLike,
    AgentStartedEvent,
    ErrorEvent,
    RunEvent,
    RunResult,
    UsageLimits,
)
from awa.errors import AgentRunError, UsageLimitExceededError
from awa.runtime.context import (
    RunContext,
    current_run_context,
    new_run_id,
    require_run_context,
    use_run_context,
)
from awa.testing import FakeChatModel, structured_output, tool_call
from awa.workflows import Loop, Parallel, Sequential, Workflow


@dataclass
class Scripted:
    """An `AgentLike` double that records its calls and one model call of usage."""

    name: str
    reply: Callable[[str], str] | str = "ok"
    capabilities: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    error: Exception | None = None
    delay: float = 0.0
    tokens: int = 10
    interrupted: bool = False
    inputs: list[AgentInput] = field(default_factory=list)
    deps_seen: list[Any] = field(default_factory=list)
    contexts: list[RunContext[Any] | None] = field(default_factory=list)
    cancelled: bool = False

    @property
    def card(self) -> AgentCard:
        return AgentCard(
            name=self.name,
            description=f"{self.name} double",
            capabilities=self.capabilities,
            skills=self.skills,
            input_schema={"type": "string"} if self.name == "first" else None,
            output_schema={"type": "object"} if self.name == "last" else None,
        )

    async def run(
        self,
        input: AgentInput,
        *,
        deps: Any = None,
        usage_limits: UsageLimits | None = None,
        thread_id: str | None = None,
        history: Any = None,
    ) -> RunResult[Any]:
        self.inputs.append(input)
        self.deps_seen.append(deps)
        ctx = current_run_context()
        self.contexts.append(ctx)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if ctx is not None:
            ctx.usage.record_model_call(self.name, input_tokens=self.tokens, output_tokens=1)
        if self.error is not None:
            raise self.error
        prompt = input if isinstance(input, str) else str(input[-1])
        text = self.reply(prompt) if callable(self.reply) else self.reply
        return RunResult(
            run_id=new_run_id(),
            agent=self.name,
            output=text,
            text=text,
            interrupts=[{"value": "approve?"}] if self.interrupted else [],
        )


class Gate(Scripted):
    """A double whose reply is awaited (for synchronisation between branches)."""

    def __init__(self, name: str, wait: Callable[[str], Awaitable[str]]) -> None:
        super().__init__(name)
        self.wait = wait

    async def run(self, input: AgentInput, **kwargs: Any) -> RunResult[Any]:
        text = await self.wait(input if isinstance(input, str) else "")
        return RunResult(run_id=new_run_id(), agent=self.name, output=text, text=text)


def record_events(ctx: RunContext[Any]) -> list[RunEvent]:
    events: list[RunEvent] = []
    ctx.events.subscribe(events.append)
    return events


def root(name: str = "caller", **kwargs: Any) -> RunContext[Any]:
    return RunContext(run_id=new_run_id(), agent=name, deps=kwargs.pop("deps", None), **kwargs)


# --------------------------------------------------------------------------- #
# Protocol and cards
# --------------------------------------------------------------------------- #


def test_workflows_satisfy_agent_like_and_build_cards() -> None:
    a = Scripted("first", capabilities=("research",), skills=("citations",))
    b = Scripted("last", capabilities=("writing", "research"))
    sequential = Sequential("pipeline", [a, b])
    parallel = Parallel("fanout", [a, b])
    loop = Loop("refine", b, until=lambda _: True, max_iterations=2)

    for workflow in (sequential, parallel, loop):
        assert isinstance(workflow, AgentLike)
        assert isinstance(workflow, Workflow)
        assert workflow.card.role == "workflow"
        assert "workflow" in workflow.card.tags

    card = sequential.card
    assert card.name == "pipeline"
    assert card.capabilities == ("research", "writing")
    assert card.skills == ("citations",)
    assert card.tags == ("workflow", "sequential")
    assert card.description == "Runs first, then last."
    assert card.input_schema == {"type": "string"}
    assert card.output_schema == {"type": "object"}
    assert parallel.card.description == "Runs first, last concurrently."
    assert parallel.card.input_schema is None and parallel.card.output_schema is None
    assert loop.card.description.startswith("Repeats last until")
    assert Sequential("x", [a], description="Custom.").card.description == "Custom."
    assert sequential.steps == (a, b) and parallel.branches == (a, b) and loop.body is b
    assert repr(sequential) == "Sequential('pipeline', [first, last])"


def test_constructor_validation() -> None:
    a = Scripted("a")
    with pytest.raises(ValueError, match="Invalid agent name"):
        Sequential("bad name", [a])
    with pytest.raises(ValueError, match="at least one step"):
        Sequential("s", [])
    with pytest.raises(ValueError, match="carry"):
        Sequential("s", [a], carry="none")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one branch"):
        Parallel("p", [])
    with pytest.raises(ValueError, match="Duplicate branch names: a"):
        Parallel("p", [a, Scripted("a")])
    with pytest.raises(ValueError, match="max_concurrency"):
        Parallel("p", [a], max_concurrency=0)
    with pytest.raises(ValueError, match="max_iterations"):
        Loop("l", a, until=lambda _: True, max_iterations=0)


# --------------------------------------------------------------------------- #
# Sequential
# --------------------------------------------------------------------------- #


async def test_sequential_carries_previous_text() -> None:
    a = Scripted("a", reply=lambda p: f"A({p})")
    b = Scripted("b", reply=lambda p: f"B({p})")
    c = Scripted("c", reply=lambda p: f"C({p})")
    result = await Sequential("chain", [a, b, c]).run("x", deps={"k": 1})

    assert a.inputs == ["x"] and b.inputs == ["A(x)"] and c.inputs == ["B(A(x))"]
    assert result.output == result.text == "C(B(A(x)))"
    assert result.agent == "chain"
    assert result.handoff_path == ()
    assert result.usage.requests == 3
    assert set(result.usage.by_agent) == {"a", "b", "c"}
    assert a.deps_seen == [{"k": 1}]
    assert all(ctx is not None and ctx.agent == "chain" for ctx in a.contexts + c.contexts)


async def test_sequential_carry_all_labels_every_step() -> None:
    a = Scripted("a", reply="alpha")
    b = Scripted("b", reply="beta")
    c = Scripted("c", reply="gamma")
    result = await Sequential("chain", [a, b, c], carry="all").run(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "the request"}]
    )

    assert b.inputs == ["the request\n\n[a]: alpha"]
    assert c.inputs == ["the request\n\n[a]: alpha\n\n[b]: beta"]
    assert result.text == "gamma"


async def test_sequential_stops_on_failure_and_emits_error_event() -> None:
    a = Scripted("a", error=RuntimeError("boom"))
    b = Scripted("b")
    ctx = root()
    events = record_events(ctx)
    with use_run_context(ctx), pytest.raises(RuntimeError, match="boom"):
        await Sequential("chain", [a, b]).run("x")

    assert b.inputs == []
    assert [type(event) for event in events] == [AgentStartedEvent, ErrorEvent]
    error = events[-1]
    assert isinstance(error, ErrorEvent)
    assert error.error_type == "RuntimeError" and error.agent == "chain"


async def test_member_interrupt_raises_agent_run_error() -> None:
    workflow = Sequential("chain", [Scripted("a", interrupted=True), Scripted("b")])
    with pytest.raises(AgentRunError, match="cannot resume"):
        await workflow.run("x")


# --------------------------------------------------------------------------- #
# Parallel
# --------------------------------------------------------------------------- #


async def test_parallel_runs_branches_on_the_same_input() -> None:
    a = Scripted("a", reply="alpha", delay=0.02)
    b = Scripted("b", reply="beta")
    result = await Parallel("fan", [a, b]).run("q")

    assert a.inputs == b.inputs == ["q"]
    assert result.output == {"a": "alpha", "b": "beta"}
    assert result.text == "[a]: alpha\n\n[b]: beta"
    assert result.usage.requests == 2
    assert "errors" not in result.metadata


async def test_parallel_branches_actually_overlap() -> None:
    barrier = asyncio.Barrier(4)

    async def meet(prompt: str) -> str:
        # Only branches running at the same time get past the barrier.
        async with asyncio.timeout(10):
            await barrier.wait()
        return "met"

    branches = [Gate(f"b{i}", meet) for i in range(4)]
    result = await Parallel("fan", branches).run("q")
    assert result.output == {f"b{i}": "met" for i in range(4)}


async def test_parallel_respects_max_concurrency() -> None:
    running = 0
    peak = 0

    def track(prompt: str) -> str:
        return prompt

    class Counting(Scripted):
        async def run(self, input: AgentInput, **kwargs: Any) -> RunResult[Any]:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            try:
                return await super().run(input, **kwargs)
            finally:
                running -= 1

    branches = [Counting(f"b{i}", reply=track, delay=0.02) for i in range(5)]
    result = await Parallel("fan", branches, max_concurrency=2).run("q")
    assert peak == 2
    assert list(result.output) == [f"b{i}" for i in range(5)]


async def test_parallel_aggregate() -> None:
    a = Scripted("a", reply="1")
    b = Scripted("b", reply="2")

    def total(results: Any) -> int:
        return sum(int(result.output) for result in results.values())

    result = await Parallel("sum", [a, b], aggregate=total).run("q")
    assert result.output == 3
    assert result.text == "[a]: 1\n\n[b]: 2"

    joined = await Parallel(
        "join", [a, b], aggregate=lambda results: "+".join(r.text for r in results.values())
    ).run("q")
    assert joined.output == joined.text == "1+2"


async def test_parallel_fail_fast_cancels_siblings() -> None:
    slow = Scripted("slow", delay=5)
    failing = Scripted("failing", error=ValueError("bad input"), delay=0.01)
    with pytest.raises(ValueError, match="bad input"):
        await Parallel("fan", [slow, failing]).run("q")
    assert slow.cancelled


async def test_parallel_collects_errors_without_fail_fast() -> None:
    ok = Scripted("ok", reply="fine")
    failing = Scripted("failing", error=ValueError("bad input"))
    result = await Parallel("fan", [failing, ok], fail_fast=False).run("q")

    assert result.output == {"ok": "fine"}
    assert result.text == "[ok]: fine"
    assert result.metadata["errors"] == {"failing": "ValueError: bad input"}


async def test_parallel_raises_when_every_branch_fails() -> None:
    branches = [Scripted("a", error=ValueError("one")), Scripted("b", error=KeyError("two"))]
    with pytest.raises(AgentRunError, match="Every branch") as info:
        await Parallel("fan", branches, fail_fast=False).run("q")
    assert "a: ValueError: one" in str(info.value)
    assert isinstance(info.value.__cause__, ValueError)


async def test_parallel_usage_limit_propagates_even_without_fail_fast() -> None:
    slow = Scripted("slow", delay=5)
    limited = Scripted("limited", error=UsageLimitExceededError("too many tokens"))
    with pytest.raises(UsageLimitExceededError):
        await Parallel("fan", [slow, limited], fail_fast=False).run("q")
    assert slow.cancelled


# --------------------------------------------------------------------------- #
# Loop
# --------------------------------------------------------------------------- #


async def test_loop_stops_on_condition() -> None:
    body = Scripted("editor", reply=lambda p: p + "!")
    result = await Loop(
        "refine", body, until=lambda r: r.text.endswith("!!!"), max_iterations=10
    ).run("draft")

    assert body.inputs == ["draft", "draft!", "draft!!"]
    assert result.text == result.output == "draft!!!"
    assert result.metadata == {"iterations": 3, "stopped_reason": "condition"}
    assert result.usage.requests == 3


async def test_loop_stops_at_max_iterations_with_custom_next_input() -> None:
    body = Scripted("editor", reply="again")
    result = await Loop(
        "refine",
        body,
        until=lambda _: False,
        max_iterations=2,
        next_input=lambda r: f"improve: {r.text}",
    ).run("draft")

    assert body.inputs == ["draft", "improve: again"]
    stop = result.metadata.pop("stop")
    assert result.metadata == {"iterations": 2, "stopped_reason": "max_iterations"}
    assert stop["bound"] == "max_iterations" and stop["suggested_fix"]


# --------------------------------------------------------------------------- #
# Run context, events and limits
# --------------------------------------------------------------------------- #


async def test_top_level_run_creates_root_context_and_emits_events() -> None:
    body = Scripted("a", reply="done")
    workflow = Sequential("chain", [body])
    result = await workflow.run("hello")

    ctx = body.contexts[0]
    assert ctx is not None
    assert ctx.run_id == result.run_id == ctx.root_run_id
    assert ctx.depth == 0 and ctx.parent_run_id is None
    assert result.duration_ms >= 0
    assert current_run_context() is None


async def test_nested_run_uses_child_context_and_shared_usage() -> None:
    parent = root(deps="parent-deps", limits=UsageLimits(max_requests=10))
    events = record_events(parent)
    body = Scripted("a", reply="done")
    inner = Sequential("inner", [body])
    outer = Sequential("outer", [inner])
    with use_run_context(parent):
        result = await outer.run("hello")

    ctx = body.contexts[0]
    assert ctx is not None
    assert ctx.agent == "inner" and ctx.depth == 2
    assert ctx.root_run_id == parent.run_id
    assert ctx.usage is parent.usage and ctx.events is parent.events
    assert ctx.deps == "parent-deps"
    assert parent.usage.requests == 1
    assert result.usage.requests == 1 and result.usage is not parent.usage

    started = [e for e in events if isinstance(e, AgentStartedEvent)]
    finished = [e for e in events if isinstance(e, AgentFinishedEvent)]
    assert [(e.agent, e.depth) for e in started] == [("outer", 1), ("inner", 2)]
    assert started[0].parent_run_id == parent.run_id
    assert started[0].input_preview == "hello"
    assert [e.agent for e in finished] == ["inner", "outer"]
    assert finished[-1].output_preview == "done"
    assert finished[-1].usage.requests == 1


async def test_nested_run_rejects_usage_limits() -> None:
    with use_run_context(root()), pytest.raises(ValueError, match="top-level run"):
        await Sequential("s", [Scripted("a")]).run("x", usage_limits=UsageLimits())


async def test_usage_limits_are_enforced_between_steps() -> None:
    a = Scripted("a")
    b = Scripted("b")
    workflow = Sequential("chain", [a, b])
    with pytest.raises(UsageLimitExceededError):
        await workflow.run("x", usage_limits=UsageLimits(max_requests=0))
    assert b.inputs == []


def test_run_sync() -> None:
    result = Loop("l", Scripted("a", reply="x"), until=lambda _: True, max_iterations=1).run_sync(
        "hi"
    )
    assert result.text == "x"
    assert result.metadata["iterations"] == 1


async def test_run_sync_inside_event_loop_raises() -> None:
    with pytest.raises(RuntimeError, match="running event loop"):
        Sequential("s", [Scripted("a")]).run_sync("hi")


class Facts(BaseModel):
    city: str


def extractor() -> Agent[None, Facts]:
    return Agent(
        "extractor",
        model=FakeChatModel([structured_output(Facts, {"city": "Oslo"})], repeat_last=True),
        output_type=Facts,
    )


async def test_sequential_passes_structured_output_as_json() -> None:
    writer_model = FakeChatModel(["Oslo it is."])
    pipeline = Sequential("pipeline", [extractor(), Agent("writer", model=writer_model)])

    result = await pipeline.run("Where?")

    assert writer_model.calls[0].last_message.content == '{"city":"Oslo"}'
    assert result.output == "Oslo it is."


async def test_sequential_carry_all_labels_structured_output_as_json() -> None:
    writer_model = FakeChatModel(["done"])
    pipeline = Sequential(
        "pipeline", [extractor(), Agent("writer", model=writer_model)], carry="all"
    )

    await pipeline.run("Where?")

    assert writer_model.calls[0].last_message.content == 'Where?\n\n[extractor]: {"city":"Oslo"}'


async def test_loop_feeds_structured_output_to_the_next_iteration() -> None:
    body = extractor()
    model = FakeChatModel([structured_output(Facts, {"city": "Oslo"})], repeat_last=True)

    with body.override(model=model):
        result = await Loop("loop", body, until=lambda _: False, max_iterations=2).run("Where?")

    assert model.calls[1].last_message.content == '{"city":"Oslo"}'
    stop = result.metadata.pop("stop")
    assert result.metadata == {"iterations": 2, "stopped_reason": "max_iterations"}
    assert stop["bound"] == "max_iterations" and stop["suggested_fix"]


async def test_parallel_text_renders_structured_branches_as_json() -> None:
    plain = Agent("plain", model=FakeChatModel(["Bergen"]))

    result = await Parallel("both", [extractor(), plain]).run("Where?")

    assert result.text == '[extractor]: {"city":"Oslo"}\n\n[plain]: Bergen'
    assert result.output == {"extractor": Facts(city="Oslo"), "plain": "Bergen"}


class GraphAgent:
    """Minimal `AgentLike` around a `create_agent` graph, recording usage like `Agent`."""

    def __init__(self, name: str, model: FakeChatModel, tools: list[Any] | None = None) -> None:
        self.name = name
        self.model = model
        self._graph = create_agent(model, tools=tools or [])

    @property
    def card(self) -> AgentCard:
        return AgentCard(name=self.name, capabilities=(self.name,))

    async def run(
        self,
        input: AgentInput,
        *,
        deps: Any = None,
        usage_limits: UsageLimits | None = None,
        thread_id: str | None = None,
        history: Any = None,
    ) -> RunResult[Any]:
        ctx = require_run_context().child(self.name, deps=deps)
        messages: list[BaseMessage] = (
            [HumanMessage(content=input)] if isinstance(input, str) else []
        )
        with use_run_context(ctx):
            state = await self._graph.ainvoke({"messages": messages})
        for message in state["messages"]:
            if isinstance(message, AIMessage):
                ctx.usage.record_usage_metadata(
                    self.name,
                    dict(message.usage_metadata or {}),
                    tool_calls=len(message.tool_calls),
                )
        text = str(state["messages"][-1].text)
        return RunResult(
            run_id=ctx.run_id, agent=self.name, output=text, text=text, messages=state["messages"]
        )


@tool
def lookup(topic: str) -> str:
    """Look up a fact about a topic."""

    ctx = current_run_context()
    return f"{topic} fact (seen by {ctx.agent if ctx else 'nobody'})"


async def test_sequential_of_create_agent_graphs() -> None:
    researcher = GraphAgent(
        "researcher",
        FakeChatModel([tool_call("lookup", {"topic": "rivers"}), "Rivers flow downhill."]),
        tools=[lookup],
    )
    writer = GraphAgent("writer", FakeChatModel(["A poem about rivers."]))
    result = await Sequential("pipeline", [researcher, writer]).run("Write about rivers.")

    assert result.text == "A poem about rivers."
    assert writer.model.calls[0].last_message.text == "Rivers flow downhill."
    tool_result = researcher.model.calls[1].last_message
    assert tool_result.text == "rivers fact (seen by researcher)"
    assert result.usage.requests == 3
    assert result.usage.tool_calls == 1
    assert result.usage.by_agent["researcher"].requests == 2
    assert result.usage.total_tokens > 0
    assert result.messages[-1].text == "A poem about rivers."


async def test_parallel_of_create_agent_graphs() -> None:
    a = GraphAgent("optimist", FakeChatModel(["It will work."]))
    b = GraphAgent("pessimist", FakeChatModel(["It will fail."]))
    result = await Parallel("debate", [a, b]).run("Will it work?")

    assert result.output == {"optimist": "It will work.", "pessimist": "It will fail."}
    assert a.model.calls[0].last_message.text == "Will it work?"
    assert result.usage.requests == 2
    assert result.text == "[optimist]: It will work.\n\n[pessimist]: It will fail."
