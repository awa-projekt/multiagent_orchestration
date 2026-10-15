"""Core proof, scenario 5: limits end the run with the documented error and nothing continues.

"Nothing continues" is measured: after the error, no further model call and no further tool
execution happens, and no task of the run is left.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from _helpers.proof_helpers import Census, delegate, model_of, other_tasks, settle
from awa.agents import Agent
from awa.contracts import ModelPrice, PriceTable, UsageLimits
from awa.errors import MaxStepsExceededError, UsageLimitExceededError
from awa.runtime import active_runs
from awa.team import Team
from awa.testing import FakeChatModel, tool_call

EFFECTS: list[str] = []


@tool
def work(item: str) -> str:
    """Do one unit of work."""

    EFFECTS.append(item)
    return f"did {item}"


@pytest.fixture(autouse=True)
def _clean() -> None:
    EFFECTS.clear()


def reply(
    text: str = "", calls: list[Any] | None = None, tokens: tuple[int, int] = (100, 20)
) -> Any:
    return AIMessage(
        content=text,
        tool_calls=calls or [],
        usage_metadata={
            "input_tokens": tokens[0],
            "output_tokens": tokens[1],
            "total_tokens": sum(tokens),
        },
    )


def looping(**options: Any) -> Agent:
    """An agent whose model asks for the tool forever."""

    model = FakeChatModel(
        [reply(calls=[tool_call("work", {"item": "x"})])], repeat_last=True, model_name="priced"
    )
    return Agent("looper", model=model, tools=[work], **options)


async def assert_stopped(agent: Agent, census: Census) -> None:
    census.mark()
    await settle()
    assert census.frozen(), "a model call or a tool ran after the run had ended"
    assert other_tasks() == [] and active_runs() == []


async def test_s5_max_steps_ends_the_run_after_exactly_that_many_model_calls() -> None:
    """Claim: an agent that never answers stops with MaxStepsExceededError after max_steps=4 model
    calls and 4 tool runs (the 5th model call is refused before it is made); nothing continues."""

    agent = looping(max_steps=4)
    census = Census(agent, effects=EFFECTS)

    with pytest.raises(MaxStepsExceededError, match="max_steps=4"):
        await agent.run("go")

    assert model_of(agent).call_count == 4
    assert len(EFFECTS) == 4
    await assert_stopped(agent, census)


async def test_s5_a_run_that_needs_exactly_max_steps_is_not_cut_off() -> None:
    """Claim: the limit is not off by one: a run that finishes in exactly max_steps calls succeeds."""

    agent = Agent(
        "exact",
        model=FakeChatModel([reply(calls=[tool_call("work", {"item": "a"})]), reply("done")]),
        tools=[work],
        max_steps=2,
    )

    assert (await agent.run("go")).output == "done"


async def test_s5_request_limit_ends_the_run_at_the_limit() -> None:
    """Claim: max_requests=3 stops a looping agent with UsageLimitExceededError on the 4th request
    attempt at the latest; the model was called exactly 3 times and the error names the limit."""

    agent = looping()
    census = Census(agent, effects=EFFECTS)

    with pytest.raises(UsageLimitExceededError, match="max_requests=3"):
        await agent.run("go", usage_limits=UsageLimits(max_requests=3))

    assert model_of(agent).call_count == 3
    await assert_stopped(agent, census)


async def test_s5_token_limit_ends_the_run_after_the_response_that_crosses_it() -> None:
    """Claim: with 120 tokens per response and max_total_tokens=300 the run stops after the third
    response (360 tokens) with UsageLimitExceededError naming total_tokens."""

    agent = looping()
    census = Census(agent, effects=EFFECTS)

    with pytest.raises(UsageLimitExceededError, match="total_tokens"):
        await agent.run("go", usage_limits=UsageLimits(max_total_tokens=300))

    assert model_of(agent).call_count == 3
    await assert_stopped(agent, census)


async def test_s5_cost_limit_ends_the_run_when_the_priced_usage_crosses_it() -> None:
    """Claim: at 2.0 per million input and 8.0 per million output tokens one response costs
    0.00036; max_cost=0.0005 stops the run after the second response with an error naming the cost."""

    prices = PriceTable(prices={"priced": ModelPrice(input_per_mtok=2.0, output_per_mtok=8.0)})
    agent = looping()
    census = Census(agent, effects=EFFECTS)

    with pytest.raises(UsageLimitExceededError, match="cost"):
        await agent.run("go", usage_limits=UsageLimits(max_cost=0.0005, prices=prices))

    assert model_of(agent).call_count == 2
    await assert_stopped(agent, census)


async def test_s5_tool_call_limit_ends_the_run() -> None:
    """Claim: max_tool_calls=2 stops a looping agent with UsageLimitExceededError and at most 2
    tool executions happened."""

    agent = looping()
    census = Census(agent, effects=EFFECTS)

    with pytest.raises(UsageLimitExceededError, match="tool_calls"):
        await agent.run("go", usage_limits=UsageLimits(max_tool_calls=2))

    assert len(EFFECTS) <= 2
    await assert_stopped(agent, census)


async def test_s5_a_limit_covers_the_whole_team_not_one_agent() -> None:
    """Claim: usage limits are shared by the lead and every delegated member: with max_requests=4
    a lead that keeps delegating stops the team; lead + member calls together are exactly 4."""

    member_model = FakeChatModel(["ok"], repeat_last=True)
    worker = Agent("worker", description="Works.", model=member_model)
    lead = Agent(
        "lead",
        description="Delegates forever.",
        model=FakeChatModel([delegate("worker")], repeat_last=True),
    )
    team = Team("desk", orchestrator=lead, members=[worker])

    with pytest.raises(UsageLimitExceededError, match="max_requests=4"):
        await team.run("go", usage_limits=UsageLimits(max_requests=4))

    assert model_of(lead).call_count + member_model.call_count == 4
    census = Census(lead, worker)
    await settle()
    assert census.frozen() and other_tasks() == [] and active_runs() == []


async def test_s5_the_failed_run_still_reports_its_usage() -> None:
    """Claim: the error of a breached limit carries what was used, so the cost of a failed run is
    not lost (the usage of the failure equals the scripted tokens of the calls made)."""

    from awa.runtime._run import usage_of_failure

    agent = looping()
    with pytest.raises(UsageLimitExceededError) as caught:
        await agent.run("go", usage_limits=UsageLimits(max_requests=2))

    usage = usage_of_failure(caught.value)
    assert usage is not None and usage.requests == 2 and usage.total_tokens == 240
