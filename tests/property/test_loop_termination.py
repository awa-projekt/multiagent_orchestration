"""Every agent loop ends within its written cap, whatever the models answer.

The model answers are drawn by Hypothesis, and once a script runs out the model repeats
its most adversarial move forever. The caps are the defaults the code documents:

- one agent: at most `max_steps` model calls per turn (structured-output repairs
  included), so at most `max_steps * PARALLEL` tool executions;
- a team: the agent holding control makes at most `max_steps` calls in each of its at most
  `1 + max_handback_rounds` turns; each member receives at most `max_calls_per_member`
  delegations and consultations, each at most `max_steps` calls. So

      model calls <= (1 + max_handback_rounds) * lead_steps
                     + members * max_calls_per_member * member_steps.

A run may also end with a limit error, never with anything else, and never hang.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from awa.agents import Agent
from awa.errors import MaxStepsExceededError, NoProgressError, StructuredOutputError
from awa.team import Team, TeamLimits
from awa.testing import FunctionChatModel, ModelCall, tool_call

PARALLEL = 2
"""Tool calls per model answer in the scripts below (the most any move makes)."""

EXECUTED: list[str] = []


@tool
def probe(n: int) -> str:
    """Look something up."""

    EXECUTED.append("probe")
    return f"probe {n}"


AGENT_MOVES = st.sampled_from(
    ["same_tool", "new_tool", "two_tools", "empty", "bad_output", "final"]
)


class Verdict(BaseModel):
    ok: bool


def _agent_answer(move: str, call: ModelCall, structured: bool) -> Any:
    turn = sum(isinstance(m, AIMessage) for m in call.messages)
    if move == "same_tool":
        return tool_call("probe", {"n": 1})
    if move == "new_tool":
        return tool_call("probe", {"n": turn})
    if move == "two_tools":
        return [tool_call("probe", {"n": turn}), tool_call("probe", {"n": turn + 100})]
    if move == "empty":
        return ""
    if move == "bad_output":
        return '{"ok": "not a bool"}' if structured else "not json"
    return '{"ok": true}' if structured else "final"


@given(
    moves=st.lists(AGENT_MOVES, max_size=12),
    forever=st.sampled_from(["same_tool", "two_tools", "bad_output"]),
    max_steps=st.integers(min_value=1, max_value=6),
    structured=st.booleans(),
)
def test_an_agent_loop_ends_within_max_steps(
    moves: list[str], forever: str, max_steps: int, structured: bool
) -> None:
    EXECUTED.clear()

    def respond(call: ModelCall) -> Any:
        move = moves[call.index] if call.index < len(moves) else forever
        return _agent_answer(move, call, structured)

    model = FunctionChatModel(respond, profile={"structured_output": True} if structured else None)
    agent = Agent(
        "solo",
        model=model,
        tools=[probe],
        max_steps=max_steps,
        output_type=Verdict if structured else None,
    )

    # A repeat without progress (same call, same result) may also stop it first.
    with contextlib.suppress(MaxStepsExceededError, StructuredOutputError, NoProgressError):
        asyncio.run(agent.run("go"))

    assert model.call_count <= max_steps
    assert len(EXECUTED) <= max_steps * PARALLEL


TEAM_MOVES = st.sampled_from(
    [
        "delegate_same",
        "delegate_new",
        "delegate_both",
        "consult_same",
        "consult_new",
        "empty",
        "final",
    ]
)


def _team_answer(name: str, move: str, call: ModelCall) -> Any:
    if isinstance(call.last_message, ToolMessage) and move == "final":
        return f"{name} done"
    turn = sum(isinstance(m, AIMessage) for m in call.messages)
    tools = set(call.tool_names)
    if "delegate_task" in tools and move.startswith("delegate"):
        if move == "delegate_same":
            return tool_call("delegate_task", {"agent": "a", "task": "same"})
        if move == "delegate_new":
            return tool_call("delegate_task", {"agent": "b", "task": f"task {turn}"})
        return [
            tool_call("delegate_task", {"agent": "a", "task": f"part {turn}"}),
            tool_call("delegate_task", {"agent": "b", "task": f"part {turn}"}),
        ]
    if "consult_peer" in tools and move.startswith("consult"):
        peer = "b" if name == "a" else "a"
        question = "same?" if move == "consult_same" else f"question {turn}?"
        return tool_call("consult_peer", {"peer": peer, "question": question})
    if move == "empty":
        return ""
    return f"{name} done"


@pytest.mark.slow
@given(
    scripts=st.fixed_dictionaries(
        {name: st.lists(TEAM_MOVES, max_size=8) for name in ("lead", "a", "b")}
    ),
    forever=st.sampled_from(["delegate_same", "delegate_both", "consult_same", "consult_new"]),
)
def test_a_team_ends_within_its_written_cap(scripts: dict[str, list[str]], forever: str) -> None:
    lead_steps, member_steps = 4, 3
    limits = TeamLimits(max_calls_per_member=3, max_delegation_depth=3, max_handback_rounds=1)
    models: dict[str, FunctionChatModel] = {}

    def model_for(name: str) -> FunctionChatModel:
        def respond(call: ModelCall) -> Any:
            script = scripts[name]
            move = script[call.index] if call.index < len(script) else forever
            return _team_answer(name, move, call)

        models[name] = FunctionChatModel(respond)
        return models[name]

    team = Team(
        "desk",
        orchestrator=Agent("lead", model=model_for("lead"), max_steps=lead_steps),
        members=[
            Agent("a", description="A.", model=model_for("a"), max_steps=member_steps),
            Agent("b", description="B.", model=model_for("b"), max_steps=member_steps),
        ],
        peers={"a": ["b"]},
        limits=limits,
    )

    with contextlib.suppress(MaxStepsExceededError, NoProgressError):
        asyncio.run(team.run("go"))

    cap = (1 + limits.max_handback_rounds) * lead_steps + 2 * (
        (limits.max_calls_per_member or 0) * member_steps
    )
    calls = sum(model.call_count for model in models.values())
    assert calls <= cap, f"{calls} model calls > cap {cap}"
    assert models["a"].call_count <= (limits.max_calls_per_member or 0) * member_steps
    assert models["b"].call_count <= (limits.max_calls_per_member or 0) * member_steps
