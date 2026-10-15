"""Core proof, scenario 10: cancellation and timeout stop the whole run tree and leave nothing behind."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.tools import tool

import awa.runtime
from _helpers.proof_helpers import delegate, of_type, other_tasks, scripted, settle, tool_results
from awa.agents import Agent
from awa.contracts import ErrorEvent, RunCancelledEvent, RunEvent, UsageLimits
from awa.errors import RunCancelledError, RunTimeoutError
from awa.team import Team, TeamLimits
from awa.testing import FakeChatModel, tool_call

STATE: dict[str, Any] = {"entered": asyncio.Event, "finished": False}


@pytest.fixture(autouse=True)
async def _isolated() -> AsyncIterator[None]:
    STATE["entered"] = asyncio.Event()
    STATE["finished"] = False
    yield
    await settle(5, 0)
    assert other_tasks() == [] and awa.runtime.active_runs() == []


@tool
async def hang() -> str:
    """Wait for a very long time."""

    STATE["entered"].set()
    await asyncio.sleep(30)
    STATE["finished"] = True
    return "never"


def hanging_team(events: list[RunEvent], *lead_answers: str, **limits: Any) -> Team:
    """A team whose member hangs in a tool; the lead answers `lead_answers` once it gets control back."""

    worker = Agent(
        "worker", description="Hangs.", model=FakeChatModel([tool_call("hang")]), tools=[hang]
    )
    return Team(
        "desk",
        orchestrator=scripted("lead", delegate("worker"), *lead_answers),
        members=[worker],
        limits=TeamLimits(**limits) if limits else None,
        hooks=[events.append],
    )


async def test_s10_timeout_stops_a_delegated_tool_and_raises_run_timeout_error() -> None:
    """Claim: a run timeout of 0.5 s ends a team whose member hangs in a tool: RunTimeoutError
    names the limit, the tool never finished, one run_cancelled event with reason "timeout" and
    then an error event are emitted, and no task or active run is left."""

    events: list[RunEvent] = []

    with pytest.raises(RunTimeoutError) as caught:
        await hanging_team(events).run("go", timeout=0.5)

    assert caught.value.limit_name == "timeout" and caught.value.limit == 0.5
    assert caught.value.elapsed >= 0.5
    assert STATE["entered"].is_set() and STATE["finished"] is False
    cancelled = [e for e in events if isinstance(e, RunCancelledEvent) and e.depth == 0]
    assert [e.reason for e in cancelled] == ["timeout"]
    assert any(isinstance(e, ErrorEvent) for e in events)


async def test_s10_max_duration_is_the_same_hard_stop() -> None:
    """Claim: UsageLimits(max_duration=...) stops the tree like a timeout and names max_duration."""

    with pytest.raises(RunTimeoutError) as caught:
        await hanging_team([]).run("go", usage_limits=UsageLimits(max_duration=0.3))

    assert caught.value.limit_name == "max_duration" and STATE["finished"] is False


async def test_s10_cancel_by_run_id_stops_the_tree_and_reports_who_and_why() -> None:
    """Claim: awa.runtime.cancel(run_id) from another task stops the member inside its tool;
    the caller gets RunCancelledError with the reason and the name given; the tool did not finish."""

    events: list[RunEvent] = []
    team = hanging_team(events)
    task = asyncio.create_task(team.run("go"))
    await asyncio.wait_for(STATE["entered"].wait(), 10)
    [root] = awa.runtime.active_runs()

    assert awa.runtime.cancel(root, "stop pressed", cancelled_by="alice")

    with pytest.raises(RunCancelledError) as caught:
        await task
    assert (caught.value.reason, caught.value.cancelled_by) == ("stop pressed", "alice")
    assert STATE["finished"] is False
    [top] = [e for e in of_type(events, RunCancelledEvent) if e.depth == 0]
    assert (top.reason, top.cancelled_by) == ("stop pressed", "alice")


async def test_s10_cancel_of_an_unknown_run_does_nothing() -> None:
    """Claim: cancelling an id that is not running returns False and disturbs no run."""

    assert awa.runtime.cancel("run_does_not_exist") is False


async def test_s10_a_run_that_finishes_in_time_is_not_affected_by_its_timeout() -> None:
    """Claim: a timeout that is not reached changes nothing: normal result, no cancel event."""

    events: list[RunEvent] = []
    team = Team(
        "desk",
        orchestrator=scripted("lead", delegate("worker"), "lead done"),
        members=[scripted("worker", "quick")],
        hooks=[events.append],
    )

    result = await team.run("go", timeout=30)

    assert result.output == "lead done" and of_type(events, RunCancelledEvent) == []


async def test_s10_delegation_timeout_fails_only_that_delegation() -> None:
    """Claim: TeamLimits(delegation_timeout=...) is a soft timer: the member is stopped, the lead
    receives status="error" and still gives its final answer; the run does not fail."""

    events: list[RunEvent] = []

    result = await hanging_team(events, "lead done", delegation_timeout=0.3).run("go")

    [report] = tool_results(result)
    assert 'agent="worker" status="error"' in report
    assert result.output == "lead done" and STATE["finished"] is False
