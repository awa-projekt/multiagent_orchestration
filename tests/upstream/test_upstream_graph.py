"""Contracts of LangGraph control flow awa relies on: interrupts, resume, Send, errors."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphBubbleUp, GraphInterrupt, GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Interrupt, Send, interrupt


def broken(awa_module: str, what: str) -> str:
    return f"Upstream contract broken: {what}. awa depends on it in {awa_module}."


class _State(TypedDict, total=False):
    items: list[str]
    answers: Annotated[list[str], operator.add]
    item: str


def _ask(state: _State) -> dict[str, Any]:
    answer = interrupt({"question": f"approve {state['item']}?"})
    return {"answers": [f"{state['item']}={answer}"]}


def _fan_out(state: _State) -> list[Send]:
    return [Send("ask", {"item": item}) for item in state["items"]]


def _graph() -> Any:
    builder = StateGraph(_State)
    builder.add_node("ask", _ask)
    builder.add_conditional_edges(START, _fan_out, ["ask"])
    builder.add_edge("ask", END)
    return builder.compile(checkpointer=InMemorySaver())


async def test_an_interrupt_pauses_the_run_and_command_resume_continues_it() -> None:
    graph = _graph()
    config = {"configurable": {"thread_id": "t1"}}
    paused = await graph.ainvoke({"items": ["a"]}, config)

    interrupts = paused.get("__interrupt__")
    assert interrupts and isinstance(interrupts[0], Interrupt), broken(
        "awa.agents.agent, awa.tools.approval", "ainvoke returns '__interrupt__' with Interrupts"
    )
    assert interrupts[0].value == {"question": "approve a?"} and interrupts[0].id, broken(
        "awa.contracts (RunResult.interrupts)", "Interrupt.value and a non-empty Interrupt.id"
    )
    snapshot = await graph.aget_state(config)
    assert snapshot.interrupts and snapshot.next == ("ask",), broken(
        "awa.agents.agent (resume)", "StateSnapshot.interrupts and .next of a paused run"
    )

    done = await graph.ainvoke(Command(resume="yes"), config)
    assert done["answers"] == ["a=yes"], broken(
        "awa.agents.agent.resume", "Command(resume=value) answers the pending interrupt"
    )


async def test_several_interrupts_in_one_step_resume_by_id() -> None:
    graph = _graph()
    config = {"configurable": {"thread_id": "t2"}}
    paused = await graph.ainvoke({"items": ["a", "b"]}, config)

    interrupts = paused["__interrupt__"]
    assert len(interrupts) == 2 and len({i.id for i in interrupts}) == 2, broken(
        "awa.tools.approval (parallel approvals)", "one Interrupt with a distinct id per task"
    )
    decisions = {i.id: ("yes" if "a?" in i.value["question"] else "no") for i in interrupts}
    done = await graph.ainvoke(Command(resume=decisions), config)
    assert sorted(done["answers"]) == ["a=yes", "b=no"], broken(
        "awa.agents.agent.resume", "Command(resume={interrupt_id: value}) resumes each interrupt"
    )


async def test_command_goto_and_update_route_the_graph() -> None:
    def first(state: _State) -> Command[Any]:
        return Command(goto="second", update={"answers": ["first"]})

    def second(state: _State) -> dict[str, Any]:
        return {"answers": ["second"]}

    builder = StateGraph(_State)
    builder.add_node("first", first, destinations=("second",))
    builder.add_node("second", second)
    builder.add_edge(START, "first")
    builder.add_edge("second", END)
    result = await builder.compile().ainvoke({"answers": []})
    assert result["answers"] == ["first", "second"], broken(
        "awa.team (handoffs)", "Command(goto=..., update=...)"
    )
    assert Command.PARENT == "__parent__", broken("awa.team._handoffs", "Command.PARENT")


def test_graph_errors_keep_their_hierarchy() -> None:
    assert issubclass(GraphInterrupt, GraphBubbleUp), broken(
        "awa.agents.agent, awa.tools.hardening",
        "GraphInterrupt is a GraphBubbleUp (awa re-raises bubble-ups untouched)",
    )
    assert issubclass(GraphRecursionError, RecursionError), broken(
        "awa.agents.agent (MaxStepsExceededError)", "GraphRecursionError is a RecursionError"
    )


async def test_recursion_limit_raises_graph_recursion_error() -> None:
    builder = StateGraph(_State)
    builder.add_node("loop", lambda state: {"answers": ["x"]})
    builder.add_edge(START, "loop")
    builder.add_conditional_edges("loop", lambda state: "loop", ["loop"])
    try:
        await builder.compile().ainvoke({"answers": []}, {"recursion_limit": 3})
    except GraphRecursionError:
        return
    raise AssertionError(
        broken("awa.agents.agent, awa.testing", "recursion_limit raises GraphRecursionError")
    )


def test_add_node_takes_timeout_and_error_handler() -> None:
    import inspect

    add_node = inspect.signature(StateGraph.add_node).parameters
    assert "timeout" in add_node
    assert "error_handler" in add_node


async def test_a_resumed_node_sees_its_answer_in_the_task_config() -> None:
    from langgraph._internal._constants import CONFIG_KEY_SCRATCHPAD
    from langgraph.config import get_config

    seen: list[bool] = []

    def ask(state: _State) -> dict[str, Any]:
        pad = get_config()["configurable"][CONFIG_KEY_SCRATCHPAD]
        seen.append(bool(pad.resume) or pad.get_null_resume(False) is not None)
        return {"answers": [str(interrupt("ok?"))]}

    builder = StateGraph(_State)
    builder.add_node("ask", ask)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t-scratch"}}
    await graph.ainvoke({"items": []}, config)
    await graph.ainvoke(Command(resume="yes"), config)

    assert seen == [False, True], broken(
        "awa.runtime._langgraph (has_decision)",
        "CONFIG_KEY_SCRATCHPAD holds 'resume' answers and 'get_null_resume'",
    )
