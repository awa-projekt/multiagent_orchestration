"""The engine port: agents behave the same on every engine; the LangGraph engine meets the port."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, TypedDict

import pytest

from _helpers.simple_engine import SimpleEngine
from awa.agents import Agent
from awa.errors import MaxStepsExceededError
from awa.persistence import StateInMemorySaver
from awa.runtime import Engine, EngineOutcome, Pause, Resume, RunOptions, current_engine, use_engine
from awa.testing import FakeChatModel, tool_call
from awa.tools import tool


@pytest.fixture(params=["langgraph", "simple"])
def engine(request: pytest.FixtureRequest) -> Iterator[Engine]:
    chosen = current_engine() if request.param == "langgraph" else SimpleEngine()
    with use_engine(chosen):
        yield chosen


@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@tool(approval=True)
def send_email(to: str) -> str:
    """Send an e-mail."""
    return f"sent to {to}"


async def test_an_agent_answers(engine: Engine) -> None:
    agent = Agent("greeter", model=FakeChatModel(["Hello."]))

    assert (await agent.run("Hi")).output == "Hello."


async def test_an_agent_calls_a_tool_and_answers(engine: Engine) -> None:
    model = FakeChatModel([tool_call("add", {"a": 2, "b": 3}), "It is 5."])
    agent = Agent("calculator", model=model, tools=[add])

    result = await agent.run("2 + 3?")

    assert result.output == "It is 5."
    assert [str(m.content) for m in result.messages if m.type == "tool"] == ["5"]


async def test_a_gated_tool_pauses_the_agent_until_it_is_resumed(engine: Engine) -> None:
    model = FakeChatModel([tool_call("send_email", {"to": "ops"}), "Sent."])
    agent = Agent("mailer", model=model, tools=[send_email], checkpointer=StateInMemorySaver())

    paused = await agent.run("Mail ops", thread_id="t1")
    done = await agent.resume("t1", {"action": "approve"})

    assert paused.is_interrupted and paused.output is None
    assert paused.interrupts[0]["value"]["tool"] == "send_email"
    assert done.output == "Sent."
    assert any(str(m.content) == "sent to ops" for m in done.messages)


async def test_the_agent_builds_again_for_another_engine() -> None:
    agent = Agent("greeter", model=FakeChatModel(["first", "second"]))
    simple = SimpleEngine()

    with use_engine(simple):
        assert (await agent.run("Hi")).output == "first"
    assert (await agent.run("Hi")).output == "second"
    assert [spec.name for spec in simple.specs] == ["greeter"]


async def test_a_step_limit_of_an_agent_is_max_steps() -> None:
    class Looping(SimpleEngine):
        def is_step_limit(self, error: BaseException) -> bool:
            return isinstance(error, RecursionError)

        async def run(self, handle: Any, input: Any, options: RunOptions) -> EngineOutcome:
            assert options.step_limit == 10 * (2 + 1)
            raise RecursionError

    agent = Agent("loop", model=FakeChatModel(["unused"]), max_steps=2)
    with use_engine(Looping()), pytest.raises(MaxStepsExceededError):
        await agent.run("Go")


class _State(TypedDict, total=False):
    answer: str
    count: int


def _graph(node: Any, *, until: int | None = None) -> Any:
    from awa._internal.upstream.graph import END, START, StateGraph

    def route(state: _State) -> str:
        return "node" if until is not None and state.get("count", 0) < until else END

    builder: StateGraph[Any, Any, Any, Any] = StateGraph(_State)
    builder.add_node("node", node)
    builder.add_edge(START, "node")
    builder.add_conditional_edges("node", route)
    return builder.compile(checkpointer=StateInMemorySaver())


def _count(state: _State) -> _State:
    return {"count": state.get("count", 0) + 1}


async def test_the_langgraph_engine_pauses_and_resumes_by_pending_id() -> None:
    engine = current_engine()
    graph = _graph(lambda state: {"answer": str(engine.pause({"question": "Send it?"}))})
    options = RunOptions(run_name="approval", thread_id="t1", durable_writes=False)

    paused = await engine.run(graph, {}, options)
    saved = await engine.state(graph, "t1")
    done = await engine.run(graph, Resume("yes", by_pending_id=True), options)

    [pause] = paused.pending
    assert pause.value == {"question": "Send it?"}
    assert saved.pending == paused.pending and saved.has_next
    assert done.values == {"answer": "yes"} and not done.pending


async def test_the_langgraph_engine_reports_its_step_limit_and_continues_the_thread() -> None:
    engine = current_engine()
    graph = _graph(_count, until=5)

    with pytest.raises(Exception) as caught:
        await engine.run(graph, {}, RunOptions(run_name="loop", thread_id="t2", step_limit=2))
    stopped = await engine.state(graph, "t2")
    finished = await engine.run(graph, None, RunOptions(run_name="loop", thread_id="t2"))

    assert engine.is_step_limit(caught.value)
    assert stopped.has_next and not stopped.pending
    assert finished.values == {"count": 5}


class _Recorder:
    """A compiled graph stand-in that records how it was invoked and returns `state`."""

    def __init__(self, state: dict[str, Any] | None = None, error: BaseException | None = None):
        self.state = state or {}
        self.error = error
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    async def ainvoke(self, graph_input: Any, config: Any = None, **keywords: Any) -> Any:
        self.calls.append((config, keywords))
        if self.error is not None:
            raise self.error
        return self.state


@pytest.mark.parametrize(
    ("durable_writes", "keywords"),
    [(None, {}), (False, {"durability": "async"}), (True, {"durability": "sync"})],
)
async def test_the_langgraph_engine_passes_the_run_options(
    durable_writes: bool | None, keywords: dict[str, str]
) -> None:
    graph = _Recorder({"answer": "ok"})
    options = RunOptions(
        run_name="job",
        thread_id="t3",
        step_limit=7,
        durable_writes=durable_writes,
        max_concurrency=2,
    )

    outcome = await current_engine().run(graph, {}, options)

    [(config, passed)] = graph.calls
    assert outcome == EngineOutcome(values={"answer": "ok"})
    assert passed == keywords
    assert config == {
        "run_name": "job",
        "recursion_limit": 7,
        "max_concurrency": 2,
        "configurable": {"thread_id": "t3"},
    }


def _interrupts(*ids: str) -> list[Any]:
    from awa._internal.upstream.graph import Interrupt

    return [Interrupt(value=f"ask {id_}", id=id_) for id_ in ids]


async def test_the_langgraph_engine_returns_pauses_in_pause_order() -> None:
    graph = _Recorder({"__interrupt__": _interrupts("b", "a")})
    options = RunOptions(run_name="job", pause_order={"a": 0, "b": 1}.__getitem__)

    outcome = await current_engine().run(graph, {}, options)

    assert outcome.values == {}
    assert outcome.pending == (Pause("a", "ask a"), Pause("b", "ask b"))


async def test_a_nested_pause_propagates_in_pause_order() -> None:
    from awa._internal.upstream.graph import GraphInterrupt

    engine = current_engine()
    graph = _Recorder(error=GraphInterrupt(_interrupts("b", "a")))
    options = RunOptions(run_name="job", pause_order={"a": 0, "b": 1}.__getitem__)

    with pytest.raises(engine.control_flow) as caught:
        await engine.run(graph, {}, options)

    assert [pause.id for pause in engine.pauses(caught.value)] == ["a", "b"]
    assert engine.pauses(ValueError("no")) == ()


def test_detached_and_outside_run_leave_the_enclosing_run() -> None:
    from awa._internal.upstream.runnables import ensure_config, var_child_runnable_config

    engine = current_engine()
    callbacks: list[Any] = ["enclosing callback"]
    token = var_child_runnable_config.set(
        {"callbacks": callbacks, "configurable": {"thread_id": "t4"}}
    )
    try:
        inside = engine.in_thread()
        detached = engine.detached().run(engine.in_thread)
        with engine.outside_run():
            outside = engine.in_thread()
            carried = ensure_config().get("callbacks")
    finally:
        var_child_runnable_config.reset(token)

    assert inside and not detached and not outside
    assert carried == callbacks
