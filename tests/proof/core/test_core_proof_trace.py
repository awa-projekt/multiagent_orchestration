"""Core proof, scenario 8: the event trace of a run is complete, ordered, not duplicated, with usage.

The trace is what a reader of the logs relies on, so the tests compare it with what really
happened (model calls made, tools executed, approvals asked) and not with a copy of the code.
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.tools import tool

from _helpers.proof_helpers import delegate, model_of, of_type, only, scripted, transfer
from awa.agents import Agent
from awa.contracts import (
    AgentFinishedEvent,
    ApprovalDecidedEvent,
    ApprovalRequestedEvent,
    DelegationEvent,
    DelegationResultEvent,
    HandoffEvent,
    ModelResponseEvent,
    RunEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from awa.persistence import SqliteCheckpointer
from awa.team import Team
from awa.testing import FakeChatModel, tool_call
from awa.tools import create_tool_approval_wrapper

EXECUTED: list[str] = []


@tool
def lookup(order: str) -> str:
    """Look an order up."""

    EXECUTED.append(order)
    return f"{order}: shipped"


def build(events: list[RunEvent]) -> tuple[Team, Agent]:
    EXECUTED.clear()
    worker = Agent(
        "worker",
        description="Looks orders up.",
        model=FakeChatModel([tool_call("lookup", {"order": "A-7"}), "found it"]),
        tools=[lookup],
    )
    team = Team(
        "desk",
        orchestrator=scripted("lead", delegate("worker"), transfer("writer", "prose")),
        members=[worker, scripted("writer", "final text")],
        handoffs={"lead": ["writer"]},
        hooks=[events.append],
    )
    return team, worker


def labels(events: list[RunEvent]) -> list[str]:
    """One short label per event of the kinds the trace must contain."""

    out: list[str] = []
    for event in events:
        if isinstance(event, DelegationEvent):
            out.append(f"delegate:{event.agent}>{event.target}")
        elif isinstance(event, DelegationResultEvent):
            out.append(f"delegated:{event.target}:{event.status}")
        elif isinstance(event, HandoffEvent):
            out.append(f"handoff:{event.source}>{event.target}")
        elif isinstance(event, ToolCallEvent):
            out.append(f"call:{event.agent}:{event.tool_name}")
        elif isinstance(event, ToolResultEvent):
            out.append(f"result:{event.agent}:{event.tool_name}")
    return out


async def test_s8_delegation_tool_call_and_handoff_appear_once_and_in_order() -> None:
    """Claim: one run with a delegation, a tool call inside it and a handoff yields exactly this
    sequence of trace events, each once, in causal order (request before result)."""

    events: list[RunEvent] = []
    team, _ = build(events)

    await team.run("go")

    assert labels(events) == [
        "call:lead:delegate_task",
        "delegate:lead>worker",
        "call:worker:lookup",
        "result:worker:lookup",
        "delegated:worker:ok",
        "result:lead:delegate_task",
        "call:lead:transfer_to_writer",
        "result:lead:transfer_to_writer",
        "handoff:lead>writer",
    ]
    assert EXECUTED == ["A-7"]  # the tool ran once, and the trace shows exactly that one call


async def test_s8_every_model_call_is_in_the_trace_and_the_usage_adds_up() -> None:
    """Claim: the ModelResponseEvents equal the model calls made (5), each carries tokens, their sum
    equals result.usage, and the usage by agent equals the events by agent."""

    events: list[RunEvent] = []
    team, worker = build(events)

    result = await team.run("go")

    responses = of_type(events, ModelResponseEvent)
    assert len(responses) == 5 == result.usage.requests
    assert all(r.input_tokens > 0 and r.output_tokens > 0 for r in responses)
    assert sum(r.input_tokens for r in responses) == result.usage.input_tokens
    assert sum(r.output_tokens for r in responses) == result.usage.output_tokens
    by_agent: dict[str, int] = {}
    for r in responses:
        by_agent[r.agent] = by_agent.get(r.agent, 0) + 1
    assert by_agent == {name: u.requests for name, u in result.usage.by_agent.items()}
    assert model_of(worker).call_count == by_agent["worker"] == 2


async def test_s8_events_share_one_root_run_and_know_their_parent_and_depth() -> None:
    """Claim: all events of a run tree carry the same root run id; the member's events are one
    level deeper than the lead's and name the lead's run as their parent."""

    events: list[RunEvent] = []
    team, _ = build(events)

    result = await team.run("go")

    assert {e.root_run_id for e in events} == {result.run_id}
    lead_run = next(e for e in events if e.agent == "lead").run_id
    worker_events = [e for e in events if e.agent == "worker"]
    assert {e.depth for e in worker_events} == {2}
    assert {e.parent_run_id for e in worker_events if e.type == "agent_started"} == {lead_run}
    assert [e.timestamp for e in events] == sorted(e.timestamp for e in events)


async def test_s8_approval_pause_and_resume_trace_each_step_once(tmp_path: Path) -> None:
    """Claim: across a pause and a resume of a gated call the trace has one tool call, one approval
    request (before the pause ended), one decision (in the resume) and one tool result; the call
    is not repeated by the resume."""

    EXECUTED.clear()
    db = tmp_path / "t.db"

    def clerk(events: list[RunEvent], *script: object) -> Agent:
        return Agent(
            "clerk",
            model=FakeChatModel(list(script)),
            tools=[create_tool_approval_wrapper(lookup)],
            checkpointer=SqliteCheckpointer(db),
            hooks=[events.append],
        )

    before: list[RunEvent] = []
    paused = await clerk(before, tool_call("lookup", {"order": "A-9"})).run("go")
    assert paused.thread_id and paused.is_interrupted
    after: list[RunEvent] = []
    await clerk(after, "done").resume(paused.thread_id, {"action": "approve", "reviewer": "ann"})

    assert [type(e).__name__ for e in only(before, ToolCallEvent, ApprovalRequestedEvent)] == [
        "ToolCallEvent",
        "ApprovalRequestedEvent",
    ]
    assert only(after, ToolCallEvent) == []
    [decided] = of_type(after, ApprovalDecidedEvent)
    assert (decided.decision, decided.reviewer, decided.tool) == ("approve", "ann", "lookup")
    [result_event] = of_type(after, ToolResultEvent)
    assert not result_event.is_error and EXECUTED == ["A-9"]
    assert len(of_type(before, AgentFinishedEvent)) == len(of_type(after, AgentFinishedEvent)) == 1
