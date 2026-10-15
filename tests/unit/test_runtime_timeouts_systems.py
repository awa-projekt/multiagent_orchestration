"""Timeouts and cancellation of teams, networks and workflows: the whole run tree stops."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver

import awa.runtime
from _helpers.polling import TIMEOUT_FACTOR, until
from awa import Agent, AgentNetwork, Loop, Parallel, Sequential, Team
from awa.contracts import AgentStatus, ErrorEvent, RunEvent, UsageLimits
from awa.contracts.events import RunCancelledEvent
from awa.errors import (
    ConfigurationError,
    NoPendingInterruptError,
    RunCancelledError,
    RunTimeoutError,
)
from awa.tasks import TaskStatus
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call


@pytest.fixture(autouse=True)
async def _no_leaks() -> AsyncIterator[None]:
    loop = asyncio.get_running_loop()
    debug = loop.get_debug()
    loop.set_debug(True)
    try:
        yield
        for _ in range(5):
            await asyncio.sleep(0)
        current = asyncio.current_task()
        assert [t for t in asyncio.all_tasks() if t is not current] == []
        assert awa.runtime.active_runs() == []
    finally:
        loop.set_debug(debug)


def hang_tool(started: list[str] | None = None, name: str = "hang") -> BaseTool:
    @tool(name)
    async def hang() -> str:
        """Work for a very long time."""
        if started is not None:
            started.append(name)
        await asyncio.sleep(30)
        return "never"

    return hang


def slow_member(name: str, started: list[str] | None = None) -> Agent:
    return Agent(
        name,
        description=f"The {name} agent.",
        model=FakeChatModel([tool_call("hang")]),
        tools=[hang_tool(started)],
    )


def quick(name: str, reply: str = "ok") -> Agent:
    return Agent(name, description=f"The {name} agent.", model=FakeChatModel([reply]))


def delegate(agent: str, *, background: bool = False) -> Any:
    return tool_call("delegate_task", {"agent": agent, "task": "work", "background": background})


def stopped_events(events: list[RunEvent]) -> list[str]:
    return [e.agent for e in events if isinstance(e, RunCancelledEvent)]


def all_idle(directory: Any, names: list[str]) -> bool:
    return all(directory.presence(n).status is AgentStatus.AVAILABLE for n in names)


# ----------------------------------------------------------------------------- teams


async def test_team_timeout_stops_a_slow_delegated_member() -> None:
    events: list[RunEvent] = []
    started: list[str] = []
    lead = Agent("lead", model=FakeChatModel([delegate("slow")]))
    team = Team(
        "desk", orchestrator=lead, members=[slow_member("slow", started)], hooks=[events.append]
    )

    with pytest.raises(RunTimeoutError) as caught:
        await team.run("go", timeout=5.0)

    assert started == ["hang"]
    assert caught.value.run_id == events[0].run_id
    assert stopped_events(events) == ["desk"]
    assert [e.type for e in events][-2:] == ["run_cancelled", "error"]
    member_errors = [e for e in events if isinstance(e, ErrorEvent) and e.agent == "slow"]
    assert [e.error_type for e in member_errors] == ["CancelledError"]
    assert all_idle(team.directory, ["lead", "slow"])


async def test_team_timeout_cancels_background_delegations() -> None:
    started: list[str] = []

    def lead_model(call: ModelCall) -> Any:
        if call.index == 0:
            return delegate("slow", background=True)
        if call.index == 1:
            return tool_call("wait_for_delegations", {"timeout": 60})
        return "done"

    lead = Agent("lead", model=FunctionChatModel(lead_model))
    team = Team("desk", orchestrator=lead, members=[slow_member("slow", started)])

    # Room for the lead's two steps on a busy machine before the background member hangs.
    with pytest.raises(RunTimeoutError):
        await team.run("go", timeout=4.0)

    assert started == ["hang"]  # the background member was running, and was cancelled
    assert all_idle(team.directory, ["lead", "slow"])


async def test_team_cancel_by_id_and_stream() -> None:
    started: list[str] = []
    lead = Agent("lead", model=FakeChatModel([delegate("slow")]))
    team = Team("desk", orchestrator=lead, members=[slow_member("slow", started)])
    seen: list[str] = []

    with pytest.raises(RunCancelledError) as caught:
        async for event in team.stream("go"):
            seen.append(event.type)
            if event.type == "tool_call" and event.agent == "slow":
                awa.runtime.cancel(event.root_run_id, "operator stop", cancelled_by="ops")

    assert caught.value.cancelled_by == "ops"
    assert seen[-2:] == ["run_cancelled", "error"]
    assert all_idle(team.directory, ["lead", "slow"])


async def test_team_handle_and_max_duration() -> None:
    lead = Agent("lead", model=FakeChatModel([delegate("slow")]))
    team = Team("desk", orchestrator=lead, members=[slow_member("slow")])

    handle = team.start("go", usage_limits=UsageLimits(max_duration=2.0))
    with pytest.raises(RunTimeoutError) as caught:
        await handle

    assert caught.value.limit_name == "max_duration"
    assert caught.value.run_id == handle.run_id


async def test_team_configuration_errors_keep_value_error_compatibility() -> None:
    team = Team("desk", orchestrator=quick("lead"), members=[quick("member")])
    with pytest.raises(ConfigurationError, match="checkpointer"):
        await team.run("go", thread_id="t1")

    saved = Team(
        "saved", orchestrator=quick("boss"), members=[quick("helper")], checkpointer=InMemorySaver()
    )
    result = await saved.run("go")
    assert result.thread_id is not None
    with pytest.raises(NoPendingInterruptError) as caught:
        await saved.resume(result.thread_id, "yes")
    assert isinstance(caught.value, ValueError)


async def test_team_resume_continues_a_thread_that_was_stopped_mid_run() -> None:
    gate = {"slow": True}

    @tool
    async def hang() -> str:
        """Work."""
        if gate["slow"]:
            await asyncio.sleep(30)
        return "worked"

    member = Agent(
        "member",
        description="Works.",
        model=FakeChatModel([tool_call("hang"), "member done"], repeat_last=True),
        tools=[hang],
    )
    lead = Agent("lead", model=FakeChatModel([delegate("member"), "lead done"], repeat_last=True))
    team = Team("desk", orchestrator=lead, members=[member], checkpointer=InMemorySaver())

    with pytest.raises(RunTimeoutError):
        await team.run("go", thread_id="team-t", timeout=2.0)

    gate["slow"] = False
    result = await team.resume("team-t", None)
    assert result.output == "lead done"


# ----------------------------------------------------------------------------- networks


def network_lead() -> Agent:
    def respond(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call("create_task", {"title": "Research", "owner": "mate"})
        if call.index == 1:
            return "Planned."
        return "Final."

    return Agent("lead", description="Plans.", model=FunctionChatModel(respond))


async def test_network_timeout_stops_every_worker_and_releases_the_task() -> None:
    events: list[RunEvent] = []
    started: list[str] = []
    network = AgentNetwork(
        "net", lead=network_lead(), teammates=[slow_member("mate", started)], hooks=[events.append]
    )

    with pytest.raises(RunTimeoutError):
        await network.run("go", timeout=2.5)

    assert started == ["hang"]
    assert stopped_events(events) == ["net"]
    tasks = network.task_board.list()
    assert [t.status for t in tasks] == [TaskStatus.PENDING]  # released, not left in progress
    assert all_idle(network.directory, ["lead", "mate"])
    assert network._running == 0  # free for the next goal


async def test_network_cancel_by_id() -> None:
    started: list[str] = []
    network = AgentNetwork("net", lead=network_lead(), teammates=[slow_member("mate", started)])
    handle = network.start("go")
    while not started:
        await asyncio.sleep(0.01)

    assert awa.runtime.cancel(handle.run_id, "enough")
    with pytest.raises(RunCancelledError) as caught:
        await handle
    assert caught.value.reason == "enough"
    assert network._running == 0
    assert all_idle(network.directory, ["lead", "mate"])


async def test_network_rejects_threads_with_a_configuration_error() -> None:
    network = AgentNetwork("net", lead=quick("boss"), teammates=[quick("mate")])
    with pytest.raises(ConfigurationError, match="thread_id"):
        await network.run("go", thread_id="t")


# ----------------------------------------------------------------------------- workflows


async def test_sequential_timeout_stops_the_running_step() -> None:
    events: list[RunEvent] = []
    started: list[str] = []
    flow = Sequential("seq", [quick("first"), slow_member("second", started)])

    # Room for the first step on a busy machine; the second step hangs until the timeout.
    with pytest.raises(RunTimeoutError):
        async for event in flow.stream("go", timeout=3.0):
            events.append(event)

    assert started == ["hang"]
    assert stopped_events(events) == ["seq"]
    finished = [e.agent for e in events if e.type == "agent_finished"]
    assert finished == ["first"]


async def test_parallel_timeout_is_not_collected_as_a_branch_error() -> None:
    flow = Parallel("par", [quick("fast"), slow_member("slow")], fail_fast=False)

    with pytest.raises(RunTimeoutError):
        await flow.run("go", timeout=1.0)


def hanging_loop(started: list[str]) -> Loop:
    body = Agent("body", model=FakeChatModel([tool_call("hang")]), tools=[hang_tool(started)])
    return Loop("loop", body, until=lambda _: False, max_iterations=5)


async def test_loop_timeout_and_cancel() -> None:
    started: list[str] = []
    with pytest.raises(RunTimeoutError):
        await hanging_loop(started).run("go", timeout=1.0 * TIMEOUT_FACTOR)
    assert started == ["hang"]

    started.clear()
    handle = hanging_loop(started).start("go")
    await until(lambda: bool(started))
    assert handle.cancel()
    with pytest.raises(RunCancelledError):
        await handle


async def test_a_workflow_inside_a_timed_agent_run_inherits_the_deadline() -> None:
    flow = Sequential("seq", [slow_member("inner")])

    @tool
    async def run_flow() -> str:
        """Run the workflow."""
        return (await flow.run("go", timeout=120)).text

    agent = Agent("outer", model=FakeChatModel([tool_call("run_flow")]), tools=[run_flow])

    with pytest.raises(RunTimeoutError) as caught:
        await agent.run("go", timeout=1.0)
    assert caught.value.limit == 1.0


async def test_workflows_reject_threads_with_a_configuration_error() -> None:
    flow = Sequential("seq", [quick("a")])
    with pytest.raises(ConfigurationError, match="thread_id"):
        await flow.run("go", thread_id="t")
    with pytest.raises(ConfigurationError, match="history"):
        await flow.run("go", history=[])
