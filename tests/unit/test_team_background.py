from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from awa.agents import Agent
from awa.contracts import AgentMessageEvent, DelegationResultEvent, RunEvent, UsageLimits
from awa.errors import UsageLimitExceededError
from awa.runtime import current_run_context
from awa.team import Team, TeamLimits, is_team_note
from awa.team._run import TeamRun
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call


def background(agent: str, task: str = "work") -> Any:
    return tool_call("delegate_task", {"agent": agent, "task": task, "background": True})


def worker(
    name: str, delay: float, barrier: asyncio.Barrier | None = None, *, finishes: bool = True
) -> Agent:
    """A member that works for `delay` seconds; it answers only if its run `finishes`."""

    @tool
    async def work() -> str:
        """Do slow work."""
        if barrier is not None:
            # Every worker must be inside its tool before any may finish.
            async with asyncio.timeout(10):
                await barrier.wait()
        await asyncio.sleep(delay)
        return f"{name} worked"

    return Agent(
        name,
        description=f"The {name} agent.",
        model=FakeChatModel(
            [tool_call("work"), f"{name} result"][: 2 if finishes else 1], repeat_last=True
        ),
        tools=[work],
    )


@tool
async def pause(seconds: float) -> str:
    """Wait a moment."""
    await asyncio.sleep(seconds)
    return "paused"


@tool
async def wait_until_background_done() -> str:
    """Wait until every background task of the team run has finished."""

    ctx = current_run_context()
    assert ctx is not None
    run = next(value for value in ctx.internal.values() if isinstance(value, TeamRun))
    async with asyncio.timeout(10):
        while any(not task.done for task in run.background.values()):
            await asyncio.sleep(0.005)
    return "paused"


def deliveries(messages: tuple[BaseMessage, ...] | list[BaseMessage]) -> list[str]:
    return [
        str(m.content)
        for m in messages
        if isinstance(m, HumanMessage) and is_team_note(m) and "<handback" in str(m.content)
    ]


def lead(respond: Any, **kwargs: Any) -> Agent:
    return Agent("lead", model=FunctionChatModel(respond), tools=[pause], **kwargs)


async def test_background_tasks_run_concurrently_and_are_handed_back_once() -> None:
    barrier = asyncio.Barrier(2)

    def respond(call: ModelCall) -> Any:
        if call.index == 0:
            return [background("a"), background("b")]
        if call.index == 1:
            return tool_call("wait_until_background_done")
        if call.index == 2:
            return tool_call("pause", {"seconds": 0.0})
        return "synthesised"

    model = FunctionChatModel(respond)
    team = Team(
        "desk",
        orchestrator=Agent("lead", model=model, tools=[pause, wait_until_background_done]),
        members=[worker("a", 0.0, barrier), worker("b", 0.0, barrier)],
    )

    result = await team.run("go")

    assert result.output == "synthesised"
    started = [m for m in result.messages if isinstance(m, ToolMessage)][:2]
    assert all(str(m.content).startswith('<task_started id="dlg_') for m in started)
    [delivery] = deliveries(model.calls[2].messages)
    assert delivery.count("<handback") == 2
    assert 'agent="a" status="ok"' in delivery and "a result" in delivery
    assert deliveries(model.calls[3].messages) == [delivery]
    assert model.call_count == 4


async def test_wait_for_delegations_returns_results_and_marks_them_delivered() -> None:
    def respond(call: ModelCall) -> Any:
        if call.index == 0:
            return background("a")
        if call.index == 1:
            return tool_call("wait_for_delegations", {})
        return "done"

    model = FunctionChatModel(respond)
    team = Team("desk", orchestrator=Agent("lead", model=model), members=[worker("a", 0.05)])

    result = await team.run("go")

    waited = str(model.calls[2].messages[-1].content)
    assert waited.startswith('<handback task_id="dlg_') and "a result" in waited
    assert deliveries(model.calls[2].messages) == []
    assert result.output == "done" and model.call_count == 3


async def test_check_and_cancel_delegation() -> None:
    task_ids: list[str] = []

    def respond(call: ModelCall) -> Any:
        last = call.last_message
        if call.index == 0:
            return background("slow")
        if call.index == 1:
            task_ids.append(str(last.content).split('"')[1])
            return tool_call("check_delegations", {})
        if call.index == 2:
            return tool_call("cancel_delegation", {"task_id": task_ids[0]})
        if call.index == 3:
            return tool_call("check_delegations", {})
        return "gave up"

    model = FunctionChatModel(respond)
    team = Team(
        "desk",
        orchestrator=Agent("lead", model=model),
        members=[worker("slow", 10, finishes=False)],
    )

    result = await team.run("go")

    running = json.loads(str(model.calls[2].messages[-1].content))
    assert running["tasks"][0]["status"] == "running"
    assert str(model.calls[3].messages[-1].content) == f"Cancelled task {task_ids[0]}."
    cancelled = json.loads(str(model.calls[4].messages[-1].content))
    assert cancelled["tasks"][0]["status"] == "cancelled"
    assert result.output == "gave up" and model.call_count == 5


async def test_team_waits_for_outstanding_tasks_and_gives_the_lead_another_turn() -> None:
    def respond(call: ModelCall) -> Any:
        if call.index == 0:
            return background("a")
        if call.index == 1:
            return "interim"
        [delivery] = deliveries(call.messages)
        return "final with " + ("a result" if "a result" in delivery else "nothing")

    model = FunctionChatModel(respond)
    team = Team("desk", orchestrator=Agent("lead", model=model), members=[worker("a", 0.05)])

    result = await team.run("go")

    assert result.output == "final with a result"
    assert result.handoff_path == ("lead",)
    assert [m.content for m in result.new_messages() if m.type == "ai" and m.content] == [
        "interim",
        "final with a result",
    ]


async def test_handback_rounds_are_bounded(caplog: pytest.LogCaptureFixture) -> None:
    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "interim"
        return background("a")

    team = Team(
        "desk",
        orchestrator=Agent("lead", model=FunctionChatModel(respond)),
        members=[worker("a", 0.01)],
        limits=TeamLimits(max_handback_rounds=1),
    )

    with caplog.at_level(logging.WARNING, logger="awa.team"):
        result = await team.run("go")

    assert result.output == "interim"
    assert "were dropped because max_handback_rounds=1 was reached" in caplog.text
    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


async def test_no_background_task_leaks_when_the_run_fails() -> None:
    asyncio.get_running_loop().set_debug(True)

    def explode() -> str:
        """Fails."""
        raise RuntimeError("lead crashed")

    def respond(call: ModelCall) -> Any:
        return [background("slow"), background("slow")] if call.index == 0 else tool_call("explode")

    team = Team(
        "desk",
        orchestrator=Agent("lead", model=FunctionChatModel(respond), tools=[explode]),
        members=[worker("slow", 10)],
    )

    with pytest.raises(RuntimeError, match="lead crashed"):
        await team.run("go")

    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


async def test_cancelling_the_team_run_cancels_background_tasks() -> None:
    # The member's tool, the lead's tool and the test meet here: both runs are busy.
    busy = asyncio.Barrier(3)

    @tool
    async def hold() -> str:
        """Wait until cancelled."""
        await busy.wait()
        await asyncio.sleep(10)
        return "held"

    def respond(call: ModelCall) -> Any:
        return background("slow") if call.index == 0 else tool_call("hold")

    team = Team(
        "desk",
        orchestrator=Agent("lead", model=FunctionChatModel(respond), tools=[hold]),
        members=[worker("slow", 10, busy, finishes=False)],
    )

    run = asyncio.create_task(team.run("go"))
    async with asyncio.timeout(10):
        await busy.wait()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert [t for t in asyncio.all_tasks() if t is not asyncio.current_task()] == []


async def test_background_limit_and_failed_background_task() -> None:
    def broken_tool() -> str:
        """Fails."""
        raise RuntimeError("no data")

    broken = Agent("broken", model=FakeChatModel([tool_call("broken_tool")]), tools=[broken_tool])
    events: list[RunEvent] = []

    def respond(call: ModelCall) -> Any:
        if call.index == 0:
            return [background("broken"), background("broken")]
        if call.index == 1:
            return tool_call("wait_for_delegations", {})
        return "done"

    model = FunctionChatModel(respond)
    team = Team(
        "desk",
        orchestrator=Agent("lead", model=model),
        members=[broken],
        limits=TeamLimits(max_background_tasks=1),
        hooks=[events.append],
    )

    await team.run("go")

    started, rejected = (
        str(m.content) for m in model.calls[1].messages if isinstance(m, ToolMessage)
    )
    assert started.startswith("<task_started")
    assert "max_background_tasks=1" in rejected
    waited = str(model.calls[2].messages[-1].content)
    assert 'status="error"' in waited and "RuntimeError: no data" in waited
    [returned] = [e for e in events if isinstance(e, DelegationResultEvent)]
    assert returned.status == "error"
    [handback] = [e for e in events if isinstance(e, AgentMessageEvent)]
    assert handback.message.sender == "broken" and handback.message.recipient == "lead"
    assert handback.message.data["handback"]["status"] == "error"


async def test_background_limit_counts_tasks_admitted_in_the_same_turn() -> None:
    def respond(call: ModelCall) -> Any:
        if call.index == 0:
            return [background("helper", "one"), background("helper", "two")]
        if call.index == 1:
            return tool_call("wait_for_delegations", {})
        return "done"

    model = FunctionChatModel(respond)
    team = Team(
        "desk",
        orchestrator=Agent("lead", model=model),
        members=[Agent("helper", model=FakeChatModel(["helped"]))],
        limits=TeamLimits(max_background_tasks=1),
    )

    await team.run("go")

    # The finished task may already be handed back after the tool results.
    first, second = (str(m.content) for m in model.calls[1].messages if isinstance(m, ToolMessage))
    assert first.startswith("<task_started")
    assert "max_background_tasks=1" in second


async def test_member_background_results_are_appended_to_its_delegated_result() -> None:
    def respond(call: ModelCall) -> Any:
        return background("helper") if call.index == 0 else "coordinator done early"

    coordinator = Agent("coordinator", model=FunctionChatModel(respond))
    team = Team(
        "desk",
        orchestrator=Agent(
            "lead",
            model=FakeChatModel(
                [tool_call("delegate_task", {"agent": "coordinator", "task": "go"}), "done"]
            ),
        ),
        members=[coordinator, worker("helper", 0.05)],
        delegates={"lead": ["coordinator"], "coordinator": ["helper"]},
    )

    result = await team.run("go")

    [delegated] = [str(m.content) for m in result.messages if isinstance(m, ToolMessage)]
    assert "coordinator done early" in delegated
    assert "Results of background tasks coordinator started:" in delegated
    assert "helper result" in delegated


async def test_usage_limit_in_a_background_task_fails_the_run() -> None:
    def respond(call: ModelCall) -> Any:
        return background("a") if call.index == 0 else "interim"

    team = Team(
        "desk",
        orchestrator=Agent("lead", model=FunctionChatModel(respond)),
        members=[worker("a", 0.05, finishes=False)],
    )

    with pytest.raises(UsageLimitExceededError):
        await team.run("go", usage_limits=UsageLimits(max_requests=3))


async def test_waiting_for_an_unknown_task_says_background_work_does_not_survive_a_pause() -> None:
    # After a resume from an approval, a background task of the paused run is gone: the answer
    # tells the lead to delegate again.
    def respond(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call("wait_for_delegations", {"task_ids": ["dlg_before_pause"]})
        return "done"

    model = FunctionChatModel(respond)
    team = Team("desk", orchestrator=Agent("lead", model=model), members=[worker("helper", 0)])

    await team.run("go")

    answer = str(model.calls[1].messages[-1].content)
    assert "Unknown task ids: dlg_before_pause." in answer
    assert "do not survive a pause for a human decision; delegate again" in answer
