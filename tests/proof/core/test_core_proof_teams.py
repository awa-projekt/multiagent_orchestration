"""Core proof, scenarios 1, 2, 3 and 9: delegation, handoff, peer consult, nested teams.

Each test drives the real agent loop with a scripted model (technical flow, not answer quality)
and asserts measured numbers: model calls per agent, events, usage, the text the next agent saw.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from _helpers.proof_helpers import delegate, model_of, of_type, scripted, tool_results, transfer
from awa.agents import Agent
from awa.contracts import (
    AgentStartedEvent,
    DelegationEvent,
    DelegationResultEvent,
    HandoffEvent,
    RunEvent,
)
from awa.errors import HandoffError
from awa.team import Team, TeamLimits
from awa.testing import FunctionChatModel, ModelCall, tool_call

# ------------------------------------------------------------------ scenario 1: orchestrator


async def test_s1_orchestrator_delegates_to_two_task_agents_and_answers() -> None:
    """Claim: the lead delegates to two task agents one after the other, each result comes back
    framed into the lead's next turn, the second brief contains the first result, and the final
    answer is the lead's. Measured: 3 lead calls + 1 + 1 member call = 5 requests, split by agent."""

    def lead_script(call: ModelCall) -> Any:
        framed = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]
        if not framed:
            return delegate("researcher", "Find the carrier of order A-7.")
        if len(framed) == 1:
            return delegate("writer", f"Write one sentence from: {framed[0]}")
        return "Final: order A-7 ships with DHL."

    lead = Agent("lead", description="Coordinates.", model=FunctionChatModel(lead_script))
    researcher = scripted("researcher", "Order A-7 ships with DHL.")
    writer = scripted("writer", "Your order A-7 ships with DHL.")
    events: list[RunEvent] = []
    team = Team("desk", orchestrator=lead, members=[researcher, writer], hooks=[events.append])

    result = await team.run("Where is order A-7?")

    assert result.output == "Final: order A-7 ships with DHL."
    assert result.agent == "lead"
    assert (model_of(lead).call_count, model_of(researcher).call_count) == (3, 1)
    assert model_of(writer).call_count == 1
    first, second = tool_results(result)
    assert first.startswith('<result agent="researcher" status="ok"')
    assert "Order A-7 ships with DHL." in first
    assert second.startswith('<result agent="writer" status="ok"')
    writer_brief = str(model_of(writer).calls[0].messages[-1].content)
    assert "Order A-7 ships with DHL." in writer_brief  # the first result reached the second brief
    assert "Where is order A-7?" not in str(model_of(researcher).calls[0].messages)  # brief only
    assert result.usage.requests == 5
    assert {a: u.requests for a, u in result.usage.by_agent.items()} == {
        "lead": 3,
        "researcher": 1,
        "writer": 1,
    }
    assert [(e.target, e.mode) for e in of_type(events, DelegationEvent)] == [
        ("researcher", "delegate"),
        ("writer", "delegate"),
    ]


async def test_s1_unknown_member_is_refused_as_tool_text_and_nothing_runs() -> None:
    """Claim: delegating to an agent that is not a member does not start any agent; the lead gets
    the refusal as text and can still answer."""

    researcher = scripted("researcher", "never asked")
    team = Team(
        "desk",
        orchestrator=scripted("lead", delegate("ghost"), "I could not delegate."),
        members=[researcher],
    )

    result = await team.run("go")

    [refusal] = tool_results(result)
    assert "ghost" in refusal and "<result" not in refusal
    assert model_of(researcher).call_count == 0
    assert result.output == "I could not delegate."


# ------------------------------------------------------------------ scenario 2: handoff


def desk(lead: Agent, writer: Agent, **kwargs: Any) -> Team:
    kwargs.setdefault("handoffs", {"lead": ["writer"], "writer": ["lead"]})
    return Team("desk", orchestrator=lead, members=[writer], **kwargs)


async def test_s2_handoff_moves_control_and_the_new_agent_answers() -> None:
    """Claim: after a handoff the target agent receives the conversation and answers the user, the
    source agent makes no further model call, the path records both, one HandoffEvent is emitted."""

    lead = scripted(
        "lead", AIMessage(content="Getting the writer.", tool_calls=[transfer("writer", "prose")])
    )
    writer = scripted("writer", "Here is your poem.")
    events: list[RunEvent] = []

    result = await desk(lead, writer, hooks=[events.append]).run("Write a poem.")

    assert (result.agent, result.output) == ("writer", "Here is your poem.")
    assert result.handoff_path == ("lead", "writer")
    assert (model_of(lead).call_count, model_of(writer).call_count) == (1, 1)
    [handoff] = of_type(events, HandoffEvent)
    assert (handoff.source, handoff.target, handoff.reason) == ("lead", "writer", "prose")
    seen = [str(m.content) for m in model_of(writer).calls[0].messages]
    assert any("Write a poem." in text for text in seen)


async def test_s2_handoff_to_an_agent_the_topology_does_not_allow_is_refused() -> None:
    """Claim: a handoff that the declared topology does not list is not executed; the target
    agent is never called."""

    lead = scripted("lead", transfer("writer"), "I will do it myself.")
    writer = scripted("writer", "should not run")
    team = Team("desk", orchestrator=lead, members=[writer], handoffs={"writer": ["lead"]})

    result = await team.run("go")

    assert model_of(writer).call_count == 0
    assert result.output == "I will do it myself."
    assert result.handoff_path == ("lead",)


async def test_s2_handoff_over_budget_is_refused_as_text_and_the_agent_answers_itself() -> None:
    """Claim: with max_handoffs=1 the second transfer is refused with the limit's name as tool
    text, the third agent never runs, and the refused agent can still answer."""

    polite = scripted("a", transfer("b"), "a answers itself")
    bottom = scripted("b", "must not run")
    team = Team(
        "desk",
        orchestrator=scripted("lead", transfer("a")),
        members=[polite, bottom],
        handoffs={"lead": ["a"], "a": ["b"]},
        limits=TeamLimits(max_handoffs=1),
    )

    result = await team.run("go")

    assert (result.agent, result.output) == ("a", "a answers itself")
    assert "handoff limit (max_handoffs=1)" in str(model_of(polite).calls[1].messages[-1].content)
    assert model_of(bottom).call_count == 0


async def test_s2_an_agent_that_insists_after_the_refusal_ends_the_run_with_an_error() -> None:
    """Claim: an agent that asks for the handoff again after the refusal ends the run with
    HandoffError (a ping-pong cannot continue); the target never runs."""

    bottom = scripted("b", "must not run")
    stubborn = scripted("a", transfer("b"), transfer("b"))
    team = Team(
        "desk",
        orchestrator=scripted("lead", transfer("a")),
        members=[stubborn, bottom],
        handoffs={"lead": ["a"], "a": ["b"]},
        limits=TeamLimits(max_handoffs=1),
    )

    with pytest.raises(HandoffError, match="again after the handoff limit"):
        await team.run("go")

    assert model_of(bottom).call_count == 0


# ------------------------------------------------------------------ scenario 3: peer consult


def peer(name: str, other: str) -> Agent:
    def respond(call: ModelCall) -> Any:
        last = call.last_message
        if isinstance(last, ToolMessage):
            return f"{name} finished with the answer"
        if str(last.content).startswith("Question from"):
            return f"{name} answers: 42"
        return tool_call("consult_peer", {"peer": other, "question": f"Help {name}?"})

    return Agent(name, description=f"The {name} agent.", model=FunctionChatModel(respond))


async def test_s3_peer_consult_returns_the_peers_answer_to_the_asker() -> None:
    """Claim: a member consults a declared peer; the peer answers one question without taking
    over, the asker sees the answer in its next turn and finishes, and the lead is not involved."""

    asker, helper = peer("asker", "helper"), peer("helper", "asker")
    lead = scripted("lead", delegate("asker"), "done")
    events: list[RunEvent] = []
    team = Team(
        "desk",
        orchestrator=lead,
        members=[asker, helper],
        peers={"asker": ["helper"]},
        hooks=[events.append],
    )

    result = await team.run("go")

    [report] = tool_results(result)
    assert "asker finished with the answer" in report
    helper_question = str(model_of(helper).calls[0].messages[-1].content)
    assert helper_question.startswith("Question from asker:\nHelp asker?")
    asker_second_turn = model_of(asker).calls[1].messages[-1]
    assert isinstance(asker_second_turn, ToolMessage)
    assert "helper answers: 42" in str(asker_second_turn.content)
    assert model_of(lead).call_count == 2
    assert [(e.agent, e.target, e.mode) for e in of_type(events, DelegationEvent)] == [
        ("lead", "asker", "delegate"),
        ("asker", "helper", "consult"),
    ]
    assert result.usage.by_agent["helper"].requests == 1


async def test_s3_consult_of_an_undeclared_peer_is_refused() -> None:
    """Claim: a member cannot consult an agent that is not its declared peer; that agent makes
    no call."""

    asker = peer("asker", "helper")
    helper = peer("helper", "asker")
    team = Team(
        "desk",
        orchestrator=scripted("lead", delegate("asker"), "done"),
        members=[asker, helper],
        peers={},
    )

    await team.run("go")

    assert model_of(helper).call_count == 0
    refused = model_of(asker).calls[1].messages[-1]
    assert isinstance(refused, ToolMessage) and "<result" not in str(refused.content)


# ------------------------------------------------------------------ scenario 9: nesting and fan-out


def chain_team(depth: int, **limits: Any) -> tuple[Team, list[Agent], list[RunEvent]]:
    tiers = [
        scripted(f"tier{i}", delegate(f"tier{i + 1}"), f"tier{i} done")
        if i < depth
        else scripted(f"tier{i}", "bottom reached")
        for i in range(1, depth + 1)
    ]
    delegates = {"lead": ["tier1"], **{f"tier{i}": [f"tier{i + 1}"] for i in range(1, depth)}}
    events: list[RunEvent] = []
    team = Team(
        "desk",
        orchestrator=scripted("lead", delegate("tier1"), "done"),
        members=tiers,
        delegates=delegates,
        limits=TeamLimits(**limits) if limits else None,
        hooks=[events.append],
    )
    return team, tiers, events


async def test_s9_nested_delegation_reaches_depth_four_with_depths_recorded() -> None:
    """Claim: with the default limit a chain lead -> tier1 -> tier2 -> tier3 -> tier4 runs to the
    bottom; AgentStartedEvent depths are 0 (team), 1 (lead), then 2 to 5 for the tiers."""

    team, tiers, events = chain_team(4)

    await team.run("go")

    assert model_of(tiers[-1]).call_count == 1
    depths = [(e.agent, e.depth) for e in of_type(events, AgentStartedEvent)]
    assert depths == [
        ("desk", 0),
        ("lead", 1),
        ("tier1", 2),
        ("tier2", 3),
        ("tier3", 4),
        ("tier4", 5),
    ]


async def test_s9_depth_limit_refuses_the_next_level_as_tool_text() -> None:
    """Claim: with max_delegation_depth=2 the delegation from tier2 is refused with the limit's
    name and tier3 never runs; the refusal reaches tier2 as text so it can still finish."""

    team, tiers, _ = chain_team(3, max_delegation_depth=2)

    result = await team.run("go")

    refusal = model_of(tiers[1]).calls[1].messages[-1]
    assert isinstance(refusal, ToolMessage)
    assert "max_delegation_depth=2" in str(refusal.content)
    assert model_of(tiers[2]).call_count == 0
    assert result.output == "done"


async def test_s9_per_member_call_cap_refuses_the_extra_call() -> None:
    """Claim: a member receives at most max_calls_per_member briefs per run; the second is
    refused."""

    worker = scripted("worker", "first")
    team = Team(
        "desk",
        orchestrator=scripted("lead", [delegate("worker"), delegate("worker")], "done"),
        members=[worker],
        limits=TeamLimits(max_calls_per_member=1),
    )

    result = await team.run("go")

    ok, refused = tool_results(result)
    assert 'status="ok"' in ok
    assert 'status="error"' in refused and "max_calls_per_member=1" in refused
    assert model_of(worker).call_count == 1


async def test_s9_fan_out_limit_refuses_the_extra_parallel_delegation() -> None:
    """Claim: with max_parallel_delegations=1 two delegations in one turn run one and refuse
    the other, which never runs."""

    a, b = scripted("a", "from a"), scripted("b", "from b")
    team = Team(
        "desk",
        orchestrator=scripted("lead", [delegate("a"), delegate("b")], "done"),
        members=[a, b],
        limits=TeamLimits(max_parallel_delegations=1),
    )

    result = await team.run("go")

    ran, refused = tool_results(result)
    assert 'status="ok"' in ran and "max_parallel_delegations=1" in refused
    assert (model_of(a).call_count, model_of(b).call_count) == (1, 0)


async def test_s9_delegation_results_are_paired_with_their_requests() -> None:
    """Claim: every executed delegation has exactly one DelegationEvent and one
    DelegationResultEvent with the same correlation id; a refused delegation starts no agent and
    emits neither (it is only tool text)."""

    events: list[RunEvent] = []
    worker = scripted("a", "x")
    team = Team(
        "desk",
        orchestrator=scripted("lead", [delegate("a"), delegate("a")], "done"),
        members=[worker],
        limits=TeamLimits(max_calls_per_member=1),
        hooks=[events.append],
    )

    result = await team.run("go")

    [request] = of_type(events, DelegationEvent)
    [reply] = of_type(events, DelegationResultEvent)
    assert reply.correlation_id == request.correlation_id and reply.status == "ok"
    assert len(tool_results(result)) == 2 and model_of(worker).call_count == 1
