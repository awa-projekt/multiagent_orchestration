"""Loop protection between agents and one step counter per turn."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from awa.agents import Agent
from awa.contracts import LimitExceededEvent, RunEvent, UsageLimits
from awa.errors import MaxStepsExceededError, UsageLimitExceededError
from awa.team import Team, TeamLimits
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper


def tool_texts(model: FunctionChatModel | FakeChatModel) -> list[str]:
    """The tool results the model saw in its last call."""

    return [str(m.content) for m in model.calls[-1].messages if isinstance(m, ToolMessage)]


def consult(peer: str, question: str) -> Any:
    return tool_call("consult_peer", {"peer": peer, "question": question})


def limits_hit(events: list[RunEvent]) -> list[str]:
    return [e.limit for e in events if isinstance(e, LimitExceededEvent)]


def _consulting(name: str, plan: dict[int, Any]) -> Agent:
    """An agent that makes the call `plan[n]` in its n-th run (0-based), else answers."""

    runs: list[int] = []

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"{name} answer: {call.last_message.content[-60:]}"
        runs.append(1)
        step = plan.get(len(runs) - 1)
        return step if step is not None else f"{name} direct answer"

    return Agent(name, description=f"The {name}.", model=FunctionChatModel(respond))


def _lead(target: str) -> Agent:
    return Agent(
        "lead",
        model=FakeChatModel(
            [tool_call("delegate_task", {"agent": target, "task": "review"}), "ok"]
        ),
    )


async def test_consult_back_allowed_by_default() -> None:
    # writer asks the reviewer, who asks the writer back with another question: allowed.
    writer = _consulting("writer", {0: consult("reviewer", "Is this clear?")})
    reviewer = _consulting("reviewer", {0: consult("writer", "Which audience?")})
    events: list[RunEvent] = []
    team = Team(
        "desk",
        orchestrator=_lead("writer"),
        members=[writer, reviewer],
        peers={"writer": ["reviewer"]},
        hooks=[events.append],
    )

    await team.run("go")

    assert "call_cycle" not in limits_hit(events)
    reviewer_model = reviewer._model
    assert isinstance(reviewer_model, FunctionChatModel)
    assert any("writer direct answer" in text for text in tool_texts(reviewer_model))


async def test_consult_cycle_same_brief_rejected() -> None:
    # writer -> reviewer("Is this clear?") -> writer("Which audience?") -> reviewer("Is this
    # clear?") again: the last call repeats a call further up the chain and is refused.
    writer = _consulting(
        "writer",
        {0: consult("reviewer", "Is this clear?"), 1: consult("reviewer", "Is this clear?")},
    )
    reviewer = _consulting("reviewer", {0: consult("writer", "Which audience?")})
    events: list[RunEvent] = []
    team = Team(
        "desk",
        orchestrator=_lead("writer"),
        members=[writer, reviewer],
        peers={"writer": ["reviewer"]},
        hooks=[events.append],
    )

    await team.run("go")

    assert "call_cycle" in limits_hit(events)
    writer_model = writer._model
    assert isinstance(writer_model, FunctionChatModel)
    refusals = [t for call in writer_model.calls for t in _tools_of(call) if "would loop" in t]
    assert refusals and "desk.reviewer" in refusals[0]


def _tools_of(call: ModelCall) -> list[str]:
    return [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]


async def test_strict_cycles_refuse_any_member_on_the_chain() -> None:
    writer = _consulting("writer", {0: consult("reviewer", "Is this clear?")})
    reviewer = _consulting("reviewer", {0: consult("writer", "Which audience?")})
    events: list[RunEvent] = []
    team = Team(
        "desk",
        orchestrator=_lead("writer"),
        members=[writer, reviewer],
        peers={"writer": ["reviewer"]},
        limits=TeamLimits(allow_cycles=False),
        hooks=[events.append],
    )

    await team.run("go")

    assert "call_cycle" in limits_hit(events)


async def test_repeated_identical_delegate_call_steered() -> None:
    same = tool_call("delegate_task", {"agent": "worker", "task": "count the rows"})
    lead_model = FakeChatModel([same, same, same, "done"])
    worker_model = FakeChatModel(["42 rows"], repeat_last=True)
    events: list[RunEvent] = []
    team = Team(
        "desk",
        orchestrator=Agent("lead", model=lead_model),
        members=[Agent("worker", description="Counts.", model=worker_model)],
        hooks=[events.append],
    )

    await team.run("go")

    assert len(worker_model.calls) == 1  # the repeats never ran again
    results = tool_texts(lead_model)
    assert "42 rows" in results[0]
    assert "already made this exact delegate_task call" in results[1]
    assert "will not run again" in results[2]
    assert limits_hit(events).count("repeated_call") == 2


async def test_repeat_polling_tool_not_blocked() -> None:
    polls: list[int] = []

    @tool
    def job_status(job_id: str) -> str:
        """Report a job's status."""

        polls.append(1)
        return "running" if len(polls) < 5 else "done"

    check = tool_call("job_status", {"job_id": "j1"})
    agent = Agent(
        "poller",
        model=FakeChatModel([check, check, check, check, check, "finished"]),
        tools=[job_status],
    )

    result = await agent.run("wait for j1")

    assert len(polls) == 5 and result.output == "finished"


async def test_per_tool_limit_consult_peer() -> None:
    writer = _consulting(
        "writer",
        {0: [consult("reviewer", "q1"), consult("reviewer", "q2"), consult("reviewer", "q3")]},
    )
    reviewer = _consulting("reviewer", {})
    events: list[RunEvent] = []
    team = Team(
        "desk",
        orchestrator=_lead("writer"),
        members=[writer, reviewer],
        peers={"writer": ["reviewer"]},
        hooks=[events.append],
    )

    await team.run("go", usage_limits=UsageLimits(per_tool_limits={"consult_peer": 2}))

    writer_model = writer._model
    assert isinstance(writer_model, FunctionChatModel)
    texts = tool_texts(writer_model)
    assert sum("reviewer direct answer" in t for t in texts) == 2
    assert sum("per_tool_limits" in t for t in texts) == 1
    assert "per_tool_limits" in limits_hit(events)


async def test_as_tool_recursion_bounded_with_max_depth() -> None:
    # An agent that keeps calling itself through a tool, each time with a new task.
    holder: dict[str, Agent] = {}
    calls: list[int] = []

    async def deeper(level: int) -> str:
        """Go one level deeper."""

        calls.append(level)
        return (await holder["agent"].run(f"level {level + 1}")).text

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "back up"
        level = int(str(call.last_message.content).split()[-1])
        return tool_call("deeper", {"level": level})

    holder["agent"] = Agent("diver", model=FunctionChatModel(respond), tools=[deeper])

    with pytest.raises(UsageLimitExceededError, match="max_depth=3"):
        await holder["agent"].run("level 0", usage_limits=UsageLimits(max_depth=3))
    assert len(calls) == 4  # depths 1 to 3 run; the run at depth 4 is refused


async def test_as_tool_cycle_with_same_task_is_refused() -> None:
    holder: dict[str, Any] = {}
    runs: list[int] = []

    async def ask_self(task: str) -> str:
        """Ask yourself (as a tool) about a task."""

        return str(await holder["tool"].ainvoke({"task": task}))

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"saw: {call.last_message.content}"
        runs.append(1)
        return tool_call("ask_self", {"task": "same task"})

    agent = Agent("helper", model=FunctionChatModel(respond), tools=[ask_self])
    holder["tool"] = agent.as_tool()

    result = await agent.run("start")

    # start -> helper("same task") -> helper("same task") again: the second is refused.
    assert len(runs) == 2
    assert "would loop" in result.text and "helper -> helper" in result.text


async def test_delegation_limits_end_only_that_delegation() -> None:
    @tool
    def step(n: int) -> str:
        """Do one step."""

        return f"step {n} done"

    def busy(call: ModelCall) -> Any:
        return tool_call("step", {"n": len(call.messages)})

    worker = Agent("worker", description="Works.", model=FunctionChatModel(busy), tools=[step])
    lead_model = FakeChatModel(
        [tool_call("delegate_task", {"agent": "worker", "task": "work hard"}), "gave up"]
    )
    events: list[RunEvent] = []
    team = Team(
        "desk",
        orchestrator=Agent("lead", model=lead_model),
        members=[worker],
        limits=TeamLimits(delegation_limits=UsageLimits(max_requests=3)),
        hooks=[events.append],
    )

    result = await team.run("go")

    assert result.output == "gave up"
    worker_model = worker._model
    assert isinstance(worker_model, FunctionChatModel) and worker_model.call_count == 3
    assert "delegation_limits" in tool_texts(lead_model)[0]
    assert "delegation_max_requests" in limits_hit(events)


@tool
def refund(amount: int) -> str:
    """Refund an amount."""

    return f"refunded {amount}"


@tool
def lookup(query: str) -> str:
    """Look something up."""

    return f"found {query}"


async def test_budget_not_reset_after_resume() -> None:
    model = FakeChatModel([tool_call("refund", {"amount": 5}), tool_call("lookup", {"query": "a"})])
    agent = Agent(
        "clerk",
        model=model,
        tools=[create_tool_approval_wrapper(refund), lookup],
        checkpointer=InMemorySaver(),
    )

    paused = await agent.run("refund", usage_limits=UsageLimits(max_requests=2))
    assert paused.is_interrupted and paused.thread_id is not None
    assert paused.usage.requests == 1

    # The resumed run continues the paused run's budget: one request left, not two.
    with pytest.raises(UsageLimitExceededError, match="max_requests=2"):
        await agent.resume(paused.thread_id, {"action": "approve"})
    assert len(model.calls) == 2


async def test_resume_continues_the_per_tool_counts() -> None:
    model = FakeChatModel(
        [
            [tool_call("lookup", {"query": "a"}), tool_call("refund", {"amount": 5})],
            tool_call("lookup", {"query": "b"}),
            "done",
        ]
    )
    agent = Agent(
        "clerk",
        model=model,
        tools=[create_tool_approval_wrapper(refund), lookup],
        checkpointer=InMemorySaver(),
    )

    paused = await agent.run("go", usage_limits=UsageLimits(per_tool_limits={"lookup": 1}))
    assert paused.thread_id is not None
    done = await agent.resume(paused.thread_id, {"action": "approve"})

    assert done.output == "done"
    last = [str(m.content) for m in model.calls[-1].messages if isinstance(m, ToolMessage)]
    assert "per_tool_limits" in last[-1]


async def test_max_steps_survives_approval_resume() -> None:
    model = FakeChatModel([tool_call("refund", {"amount": 5}), tool_call("lookup", {"query": "x"})])
    agent = Agent(
        "clerk",
        model=model,
        tools=[create_tool_approval_wrapper(refund), lookup],
        checkpointer=InMemorySaver(),
        max_steps=2,
    )

    paused = await agent.run("refund 5")
    assert paused.is_interrupted and paused.thread_id is not None

    # One step was used before the pause; the resumed turn has one left, not two.
    with pytest.raises(MaxStepsExceededError):
        await agent.resume(paused.thread_id, {"action": "approve"})


async def test_max_steps_resets_on_new_turn() -> None:
    agent = Agent(
        "clerk",
        model=FunctionChatModel(_tool_then_answer),
        tools=[lookup],
        checkpointer=InMemorySaver(),
        max_steps=2,
    )

    first = await agent.run("one")
    assert first.thread_id is not None
    second = await agent.run("two", thread_id=first.thread_id)
    third = await agent.run("three", thread_id=first.thread_id)

    assert (first.output, second.output, third.output) == ("answer", "answer", "answer")


def _tool_then_answer(call: ModelCall) -> Any:
    if isinstance(call.last_message, ToolMessage):
        return "answer"
    return tool_call("lookup", {"query": "x"})


class Verdict(BaseModel):
    ok: bool


async def test_structured_repair_does_not_preempt_team_final_step() -> None:
    """A structured-output repair counts as a step for the team too: the final tool-free call
    is the last one `max_steps` allows, never one more."""

    seen_tools: list[int] = []

    def respond(call: ModelCall) -> Any:
        seen_tools.append(len(call.tool_names))
        turn = sum(isinstance(m, AIMessage) for m in call.messages)
        if not call.tool_names:
            return '{"ok": true}'  # the final, tool-free step answers in the schema
        if turn == 0:
            return '{"ok": "not a bool"}'  # forces one structured-output repair
        return tool_call("delegate_task", {"agent": "worker", "task": f"step {turn}"})

    lead = Agent(
        "lead",
        model=FunctionChatModel(respond, profile={"structured_output": True}),
        output_type=Verdict,
        max_steps=3,
    )
    worker = Agent(
        "worker", description="Works.", model=FakeChatModel(["did it"], repeat_last=True)
    )
    team = Team("desk", orchestrator=lead, members=[worker])

    result = await team.run("go")

    assert result.output == Verdict(ok=True)
    assert len(seen_tools) == 3 and seen_tools[-1] == 0


async def test_team_final_step_counts_the_steps_before_a_pause() -> None:
    """The team's final tool-free step comes at `max_steps` of the whole turn, also when the
    turn paused for an approval in between (the counter is never reset by a resume)."""

    offered: list[bool] = []

    def respond(call: ModelCall) -> Any:
        offered.append(bool(call.tool_names))
        if not call.tool_names:
            return "final answer"
        if not any(isinstance(m, AIMessage) for m in call.messages):
            return tool_call("refund", {"amount": 5})
        return tool_call("lookup", {"query": "more"})

    lead = Agent(
        "lead",
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(refund), lookup],
        max_steps=3,
    )
    worker = Agent("worker", description="Works.", model=FakeChatModel(["did it"]))
    team = Team("desk", orchestrator=lead, members=[worker], checkpointer=InMemorySaver())

    paused = await team.run("go")
    assert paused.is_interrupted and paused.thread_id is not None
    done = await team.resume(paused.thread_id, {"action": "approve"})

    assert done.output == "final answer"
    assert offered == [True, True, False]
