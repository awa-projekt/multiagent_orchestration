"""Reliable agent communication: run isolation, team semantics, context by reference, graceful budgets and how a run scope ends."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from awa.agents import Agent
from awa.agents.middleware import ROOT_RESERVE, budget_stage, remaining_requests
from awa.contracts import (
    DelegationEvent,
    DelegationResultEvent,
    LimitExceededEvent,
    MessageUndeliveredEvent,
    RunEvent,
    UsageLimits,
)
from awa.errors import UsageLimitExceededError
from awa.messaging import Blackboard, InMemoryMessageBus
from awa.runtime.context import RunContext
from awa.tasks import TaskBoard, TaskStatus
from awa.team import AgentNetwork, NetworkLimits, Team, TeamLimits, is_team_note
from awa.team._network_coordinator import resolve_task_refs
from awa.team._scoped import MAX_PAUSED_RUNS, RunScope
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper


def results_in(text: str) -> int:
    """Results a closing note gives the lead: new handbacks plus those listed as received."""

    received = text.split("Results you already received: ", 1)
    listed = received[1].split(". ", 1)[0].count(" (") if len(received) == 2 else 0
    return text.count("<handback") + listed


SENT: list[str] = []


@tool
def send_offer(candidate: str) -> str:
    """Send the offer letter to a candidate."""

    SENT.append(candidate)
    return f"offer sent to {candidate}"


@pytest.fixture(autouse=True)
def _clear() -> None:
    SENT.clear()


def goal_of(call: ModelCall) -> str:
    """The run's goal letter: the last word of the first user message."""

    first = next(m for m in call.messages if isinstance(m, HumanMessage))
    return str(first.content).split()[-1]


def tool_texts(call: ModelCall) -> list[str]:
    return [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]


# --------------------------------------------------------------------------- Team isolation


async def test_two_concurrent_runs_of_one_team_share_no_messages_keys_or_tasks() -> None:
    both_working = asyncio.Barrier(2)

    @tool
    async def sync() -> str:
        """Wait for the other run."""
        async with asyncio.timeout(10):
            await both_working.wait()
        return "synced"

    def lead_respond(call: ModelCall) -> Any:
        goal = goal_of(call)
        turn = sum(isinstance(m, AIMessage) for m in call.messages)
        script = [
            tool_call("create_task", {"title": f"Task {goal}"}),
            tool_call("delegate_task", {"agent": "researcher", "task": f"Research {goal}"}),
            tool_call("delegate_task", {"agent": "writer", "task": f"Write {goal}"}),
            tool_call("list_tasks"),
        ]
        if turn < len(script):
            return script[turn]
        return f"done {goal}: {tool_texts(call)[-1]}"

    def researcher_respond(call: ModelCall) -> Any:
        goal = goal_of(call)
        turn = sum(isinstance(m, AIMessage) for m in call.messages)
        if turn == 0:
            return tool_call("sync")
        if turn == 1:
            return [
                tool_call("write_shared", {"key": "fact", "value": f"fact {goal}"}),
                tool_call(
                    "send_message", {"to": "writer", "subject": "note", "content": f"note {goal}"}
                ),
            ]
        return f"researched {goal}"

    def writer_respond(call: ModelCall) -> Any:
        if not isinstance(call.last_message, ToolMessage):
            return tool_call("read_shared", {"key": "fact"})
        notes = [str(m.content) for m in call.messages if is_team_note(m)]
        return f"wrote | {tool_texts(call)[-1]} | {' '.join(notes)}"

    team = Team(
        "desk",
        orchestrator=Agent("lead", model=FunctionChatModel(lead_respond)),
        members=[
            Agent("researcher", model=FunctionChatModel(researcher_respond), tools=[sync]),
            Agent("writer", model=FunctionChatModel(writer_respond)),
        ],
        peers={"researcher": ["writer"]},
        blackboard=True,
        task_board=True,
    )

    first, second = await asyncio.gather(team.run("Goal A"), team.run("Goal B"))

    for result, own, other in ((first, "A", "B"), (second, "B", "A")):
        written = next(
            str(m.content)
            for m in result.messages
            if isinstance(m, ToolMessage) and "wrote |" in str(m.content)
        )
        assert f"fact {own}" in written and f"fact {other}" not in written
        assert f"note {own}" in written and f"note {other}" not in written
        assert f"Task {own}" in str(result.output) and f"Task {other}" not in str(result.output)


async def test_a_network_run_works_only_on_its_own_tasks_of_a_shared_board() -> None:
    board = TaskBoard()
    await board.create("Left over from another run", created_by="lead")
    briefs: list[str] = []

    def lead_respond(call: ModelCall) -> Any:
        last = call.last_message
        if call.index == 0:
            return tool_call("create_task", {"title": "Mine"})
        if isinstance(last, ToolMessage):
            return "Planned."
        return f"FINAL {results_in(str(last.content))}"

    def mate_respond(call: ModelCall) -> Any:
        briefs.append(str(call.messages[1].content))
        return "did it"

    network = AgentNetwork(
        "studio",
        lead=Agent("lead", model=FunctionChatModel(lead_respond)),
        teammates=[Agent("writer", model=FunctionChatModel(mate_respond))],
        task_board=board,
    )

    result = await network.run("go")

    assert result.output == "FINAL 1"
    assert result.metadata["tasks"]["completed"] == 1
    assert board.get("task_1").status is TaskStatus.PENDING  # not this run's task
    assert board.get("task_2").data["awa.run"] == result.run_id
    assert "Left over" not in briefs[0] and "Mine" in briefs[0]


async def test_two_concurrent_runs_of_one_network_are_isolated() -> None:
    both_working = asyncio.Barrier(2)

    @tool
    async def work() -> str:
        """Do the work."""
        async with asyncio.timeout(10):
            await both_working.wait()
        return "worked"

    def lead_respond(call: ModelCall) -> Any:
        goal = goal_of(call)
        last = call.last_message
        if isinstance(last, HumanMessage) and last.content == f"Goal {goal}":  # plan
            return tool_call("create_task", {"title": f"Task {goal}"})
        if isinstance(last, ToolMessage):
            return "Planned."
        return f"FINAL {goal}: {last.content}"

    briefs: list[str] = []

    def mate_respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "result of " + str(call.messages[1].content).splitlines()[0]
        briefs.append(str(call.messages[1].content))
        return tool_call("work")

    network = AgentNetwork(
        "studio",
        lead=Agent("lead", model=FunctionChatModel(lead_respond)),
        teammates=[Agent("writer", model=FunctionChatModel(mate_respond), tools=[work])],
    )

    first, second = await asyncio.gather(network.run("Goal A"), network.run("Goal B"))

    for result, own, other in ((first, "A", "B"), (second, "B", "A")):
        assert result.metadata["tasks"]["completed"] == 1
        assert f"Task {own}" in str(result.output) and f"Task {other}" not in str(result.output)
    assert len(briefs) == 2
    assert all(("Task A" in b) != ("Task B" in b) for b in briefs)


# --------------------------------------------------------------------------- pauses


def hiring_lead(*plan: Any) -> Agent:
    def respond(call: ModelCall) -> Any:
        if "Hire" not in goal_of(call) and "Hire" not in str(call.messages[1].content):
            return "Direct answer."
        turn = sum(isinstance(m, AIMessage) for m in call.messages)
        if turn < len(plan):
            return plan[turn]
        last = call.last_message
        if isinstance(last, ToolMessage):
            return "Planned."
        return f"FINAL: {str(last.content)}"

    return Agent("lead", model=FunctionChatModel(respond))


def recruiter() -> Agent:
    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"recruiter: {call.last_message.content}"
        return tool_call("send_offer", {"candidate": "Ada"})

    return Agent(
        "recruiter",
        capabilities=["offers"],
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(send_offer)],
    )


async def test_a_paused_network_run_is_invisible_to_a_second_run_and_still_resumes() -> None:
    network = AgentNetwork(
        "hiring",
        lead=hiring_lead(tool_call("create_task", {"title": "Send offer"})),
        teammates=[recruiter()],
        checkpointer=InMemorySaver(),
    )

    paused = await network.run("Hire Ada.")
    assert paused.is_interrupted and paused.thread_id is not None

    events: list[RunEvent] = []
    other = await AgentNetwork(
        "hiring2",
        lead=hiring_lead(),
        teammates=[recruiter()],
        checkpointer=InMemorySaver(),
        hooks=[events.append],
    ).run("Just answer.")
    second = await network.run("Answer directly please.")

    # The second run never sees (or offers to cancel) the first run's parked task.
    assert second.metadata["tasks"] == {**second.metadata["tasks"], "in_progress": 0}
    assert "task_1" not in str(second.messages) and not second.is_interrupted
    assert not other.is_interrupted

    done = await network.resume(paused.thread_id, {"action": "approve"})
    assert done.metadata["tasks"]["completed"] == 1 and SENT == ["Ada"]


async def test_cancelling_a_task_that_waits_for_approval_reports_the_withdrawn_approval() -> None:
    events: list[RunEvent] = []
    network = AgentNetwork(
        "hiring",
        lead=hiring_lead(tool_call("create_task", {"title": "Send offer"})),
        teammates=[recruiter()],
        checkpointer=InMemorySaver(),
        hooks=[events.append],
    )

    paused = await network.run("Hire Ada.")
    assert paused.is_interrupted and paused.thread_id is not None
    # The lead's creator rights let it cancel the parked task (here from outside the run).
    await network.task_board.cancel("task_1", author="lead", reason="role closed")
    events.clear()
    done = await network.resume(paused.thread_id, {"action": "approve"})

    assert not done.is_interrupted and SENT == []
    [withdrawn] = [e for e in events if isinstance(e, DelegationResultEvent)]
    assert withdrawn.status == "error" and withdrawn.correlation_id == "task_1"
    assert "pending approval" in withdrawn.content_preview


async def test_run_timeout_pauses_with_pending_approvals_instead_of_dropping_them() -> None:
    events: list[RunEvent] = []

    @tool
    async def slow() -> str:
        """Takes long."""
        await asyncio.sleep(10)
        return "late"

    slowpoke = Agent(
        "slowpoke", capabilities=["slow"], model=FakeChatModel([tool_call("slow")]), tools=[slow]
    )
    network = AgentNetwork(
        "hiring",
        lead=hiring_lead(
            tool_call(
                "create_tasks",
                {
                    "tasks": [
                        {"key": "o", "title": "Send offer", "required_capabilities": ["offers"]},
                        {"key": "s", "title": "Slow", "required_capabilities": ["slow"]},
                    ]
                },
            )
        ),
        teammates=[recruiter(), slowpoke],
        checkpointer=InMemorySaver(),
        limits=NetworkLimits(run_timeout=1.0),
        hooks=[events.append],
    )

    paused = await network.run("Hire Ada.")

    assert paused.is_interrupted and paused.metadata["stop_reason"] == "run_timeout"
    assert [i["task_id"] for i in paused.interrupts] == ["task_1"]
    assert any(isinstance(e, LimitExceededEvent) and e.limit == "run_timeout" for e in events)


def offer_team(*lead_script: Any, hooks: list[Any] | None = None) -> Team:
    publisher = Agent(
        "publisher",
        model=FakeChatModel([tool_call("send_offer", {"candidate": "Ada"}), "sent"]),
        tools=[create_tool_approval_wrapper(send_offer)],
    )
    return Team(
        "desk",
        orchestrator=Agent("lead", model=FakeChatModel(list(lead_script))),
        members=[publisher],
        checkpointer=InMemorySaver(),
        hooks=hooks or [],
        limits=TeamLimits(max_calls_per_member=1),
    )


async def test_team_resume_keeps_counters_and_announces_delegations_once() -> None:
    events: list[RunEvent] = []
    team = offer_team(
        tool_call("delegate_task", {"agent": "publisher", "task": "offer"}),
        tool_call("delegate_task", {"agent": "publisher", "task": "again"}),
        "done",
        hooks=[events.append],
    )

    paused = await team.run("go")
    assert paused.is_interrupted and paused.thread_id is not None
    requests_before = paused.usage.requests
    done = await team.resume(paused.thread_id, {"action": "approve"})

    delegations = [e for e in events if isinstance(e, DelegationEvent)]
    assert [e.task for e in delegations] == ["offer"]  # announced once; "again" refused
    second = [str(m.content) for m in done.messages if isinstance(m, ToolMessage)][-1]
    assert "max_calls_per_member=1" in second  # the count survived the pause
    assert done.usage.requests > requests_before  # usage continues the paused run's
    assert done.output == "done" and SENT == ["Ada"]


async def test_team_usage_limits_cover_the_resumed_part_of_a_run() -> None:
    team = offer_team(tool_call("delegate_task", {"agent": "publisher", "task": "offer"}))

    paused = await team.run("go", usage_limits=UsageLimits(max_requests=3))
    assert paused.is_interrupted and paused.thread_id is not None
    with pytest.raises(UsageLimitExceededError):
        await team.resume(paused.thread_id, {"action": "approve"})


# --------------------------------------------------------------------------- team semantics


async def test_background_delegation_to_a_member_with_approval_gated_tools_is_refused() -> None:
    lead = Agent(
        "lead",
        model=FakeChatModel(
            [
                tool_call(
                    "delegate_task", {"agent": "recruiter", "task": "offer", "background": True}
                ),
                "ok",
            ]
        ),
    )
    team = Team("desk", orchestrator=lead, members=[recruiter()], checkpointer=InMemorySaver())

    result = await team.run("go")

    refusal = next(str(m.content) for m in result.messages if isinstance(m, ToolMessage))
    assert "a background delegation cannot pause" in refusal and "background=false" in refusal
    assert SENT == [] and result.output == "ok"


async def test_an_ungated_interrupt_in_a_background_delegation_gets_a_clear_message() -> None:
    from langgraph.types import interrupt

    def ask(question: str) -> str:
        """Ask a human."""
        return str(interrupt(question))

    asker = Agent("asker", model=FakeChatModel([tool_call("ask", {"question": "?"})]), tools=[ask])
    lead = Agent(
        "lead",
        model=FakeChatModel(
            [
                tool_call("delegate_task", {"agent": "asker", "task": "ask", "background": True}),
                tool_call("wait_for_delegations"),
                "ok",
            ]
        ),
    )
    team = Team("desk", orchestrator=lead, members=[asker])

    result = await team.run("go")

    waited = [str(m.content) for m in result.messages if isinstance(m, ToolMessage)][-1]
    assert "background delegation, which cannot pause" in waited
    assert "checkpointer" not in waited


async def test_a_lead_at_max_steps_gives_a_final_answer_without_tools() -> None:
    seen: list[ModelCall] = []

    def respond(call: ModelCall) -> Any:
        seen.append(call)
        if "step limit" in str(call.last_message.content):
            return "best answer so far"
        return tool_call("delegate_task", {"agent": "writer", "task": "more"})

    team = Team(
        "desk",
        orchestrator=Agent("lead", model=FunctionChatModel(respond), max_steps=3),
        members=[Agent("writer", model=FakeChatModel(["draft"], repeat_last=True))],
    )

    result = await team.run("go")

    assert result.output == "best answer so far"
    assert len(seen) == 3 and seen[-1].tool_names == () and seen[0].tool_names


async def test_read_inbox_says_which_messages_come_first() -> None:
    from awa.messaging import InMemoryMessageBus, create_messaging_tools

    bus = InMemoryMessageBus()
    bus.register("a")
    [read] = [t for t in create_messaging_tools(bus, "a") if t.name == "read_inbox"]
    assert "newest" not in read.description and "oldest first" in read.description


async def test_a_paused_team_run_keeps_its_workspace_and_a_second_run_gets_its_own() -> None:
    def lead_respond(call: ModelCall) -> Any:
        goal = goal_of(call)
        turn = sum(isinstance(m, AIMessage) for m in call.messages)
        if goal == "first":
            script = [
                tool_call("write_shared", {"key": "k", "value": "from first"}),
                tool_call("delegate_task", {"agent": "recruiter", "task": "offer"}),
                tool_call("read_shared", {"key": "k"}),
            ]
        else:
            script = [tool_call("read_shared", {"key": "k"})]
        return script[turn] if turn < len(script) else f"{goal}: {tool_texts(call)[-1]}"

    team = Team(
        "desk",
        orchestrator=Agent("lead", model=FunctionChatModel(lead_respond)),
        members=[recruiter()],
        checkpointer=InMemorySaver(),
        blackboard=True,
    )

    paused = await team.run("first")
    second = await team.run("second")
    assert paused.thread_id is not None
    done = await team.resume(paused.thread_id, {"action": "approve"})

    assert second.output == "second: No shared entry named 'k'."
    assert "from first" in str(done.output) and SENT == ["Ada"]


def _ctx(max_requests: int | None, used: int, *, depth: int = 0) -> RunContext[Any]:
    limits = UsageLimits(max_requests=max_requests)
    ctx: RunContext[Any] = RunContext(run_id="r", agent="a", deps=None, limits=limits)
    ctx.depth = depth
    for _ in range(used):
        ctx.usage.request_started()
    return ctx


def test_budget_stages_follow_the_share_of_requests_used() -> None:
    assert budget_stage(_ctx(None, 5)) is None
    assert budget_stage(_ctx(20, 10)) is None
    assert budget_stage(_ctx(20, 14)) == ("note", 6)
    assert budget_stage(_ctx(20, 17)) == ("final", 3)
    assert budget_stage(_ctx(20, 19)) == ("final", 1)


def test_a_delegated_run_leaves_a_reserve_for_the_agents_above_it() -> None:
    assert remaining_requests(_ctx(20, 5)) == (15, 20)
    assert remaining_requests(_ctx(20, 5, depth=2)) == (15 - ROOT_RESERVE, 20 - ROOT_RESERVE)


def test_task_references_are_replaced_and_unknown_ones_kept() -> None:
    results = {"T-1": "the full report"}
    text = "Intro\n{{task:T-1}}\nand {{ task: T-9 }}"
    assert resolve_task_refs(text, results.get) == "Intro\nthe full report\nand {{ task: T-9 }}"


async def test_low_budget_notes_and_a_final_call_without_tools() -> None:
    seen: list[tuple[int, str]] = []

    @tool
    def lookup(n: int) -> str:
        """Look up n."""

        return f"value {n}"

    def respond(call: ModelCall) -> Any:
        last = call.messages[-1]
        note = str(last.content) if isinstance(last, HumanMessage) else ""
        seen.append((len(call.tool_names), note))
        return "answer" if not call.tool_names else tool_call("lookup", {"n": call.index})

    agent = Agent("solo", model=FunctionChatModel(respond), tools=[lookup], max_steps=50)
    result = await agent.run("go", usage_limits=UsageLimits(max_requests=10))

    assert result.output == "answer"
    assert any(tools and note.startswith("Budget note") for tools, note in seen)
    assert seen[-1][0] == 0 and "Do not call tools" in seen[-1][1]
    assert len(seen) <= 10  # the last allowed call answers instead of hitting the ceiling


async def test_delegation_result_inlines_the_shared_values_the_member_wrote() -> None:
    board = Blackboard()
    researcher = Agent(
        "researcher",
        description="Researches.",
        model=FakeChatModel(
            [tool_call("write_shared", {"key": "evidence/a1", "value": '[{"claim": "x"}]'}), "done"]
        ),
    )
    lead = Agent(
        "lead",
        description="Leads.",
        model=FakeChatModel(
            [tool_call("delegate_task", {"agent": "researcher", "task": "find x"}), "final"]
        ),
    )
    team = Team("t", orchestrator=lead, members=[researcher], blackboard=board)
    result = await team.run("go")

    [delegated] = [
        str(m.content)
        for m in result.messages
        if isinstance(m, ToolMessage) and "researcher" in str(m.content)
    ]
    assert "evidence/a1" in delegated and '"claim"' in delegated


async def test_a_member_that_repeats_itself_ends_only_its_delegation() -> None:
    @tool
    def search(query: str) -> str:
        """Search."""

        return "nothing found"

    member = Agent(
        "worker",
        description="Works.",
        model=FunctionChatModel(lambda call: tool_call("search", {"query": "x"})),
        tools=[search],
        max_steps=50,
    )
    lead = Agent(
        "lead",
        description="Leads.",
        model=FakeChatModel(
            [tool_call("delegate_task", {"agent": "worker", "task": "try"}), "gave up"]
        ),
    )
    result = await Team("t", orchestrator=lead, members=[member]).run("go")

    assert result.output == "gave up"
    [delegated] = [str(m.content) for m in result.messages if isinstance(m, ToolMessage)]
    assert "without progress" in delegated


async def test_a_long_network_result_reaches_the_lead_by_reference() -> None:
    import re

    from awa.team import AgentNetwork

    report = "finding. " * 400  # far above NetworkLimits.handback_chars
    seen: list[str] = []

    def lead_respond(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call("create_task", {"title": "Write the report", "owner": "writer"})
        last = call.last_message
        if isinstance(last, ToolMessage):
            return "Planned."
        text = str(last.content)
        seen.append(text)
        if "Write the final answer" in text:
            [task_id] = re.findall(r'<handback task_id="([^"]+)"', text)
            return f"Report:\n{{{{task:{task_id}}}}}"
        return "Noted."

    def writer_respond(call: ModelCall) -> Any:
        return report

    lead = Agent("lead", description="Leads.", model=FunctionChatModel(lead_respond))
    writer = Agent("writer", description="Writes.", model=FunctionChatModel(writer_respond))
    result = await AgentNetwork("n", lead=lead, teammates=[writer]).run("go")

    closing = seen[-1]
    assert report not in closing and "get_task" in closing
    assert result.text == f"Report:\n{report.strip()}"


def scope(**options: Any) -> RunScope:
    return RunScope(
        "k", **{"bus": InMemoryMessageBus, "blackboard": True, "task_board": True, **options}
    )


def test_resuming_without_saved_state_warns(caplog: pytest.LogCaptureFixture) -> None:
    runs = scope()
    with caplog.at_level("WARNING", logger="awa.team.scope"):
        runs.start(None)  # a new run is no resume
        assert caplog.text == ""
        runs.start("t-lost")
    assert "resuming thread 't-lost' found no saved state" in caplog.text


async def test_resuming_a_remembered_run_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    runs = scope()
    resources = runs.start(None)
    await runs.finish(None, resources, paused_on="t1")  # type: ignore[arg-type]
    with caplog.at_level("WARNING", logger="awa.team.scope"):
        assert runs.start("t1") is resources
    assert caplog.text == ""


def test_runs_dropped_after_the_limit_warn_when_resumed(caplog: pytest.LogCaptureFixture) -> None:
    runs = scope()
    for number in range(MAX_PAUSED_RUNS + 1):
        runs._paused[f"t{number}"] = runs.start(None)
        while len(runs._paused) > MAX_PAUSED_RUNS:
            runs._paused.popitem(last=False)
    with caplog.at_level("WARNING", logger="awa.team.scope"):
        runs.start("t0")
    assert "found no saved state" in caplog.text


def test_shared_objects_survive_a_restart_so_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    runs = scope(bus=InMemoryMessageBus(), blackboard=None, task_board=None)
    with caplog.at_level("WARNING", logger="awa.team.scope"):
        runs.start("t-lost")
    assert caplog.text == ""


async def test_a_failing_report_does_not_replace_the_runs_outcome(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runs = scope()
    resources = runs.start(None)
    assert isinstance(resources.bus, InMemoryMessageBus)
    resources.bus.register("b")
    await resources.bus.send(_note())

    class Broken:
        async def emit(self, event: Any) -> None:
            raise RuntimeError("subscriber failed")

        def event_fields(self) -> dict[str, Any]:
            return {"run_id": "r", "agent": "a", "depth": 0}

    with caplog.at_level("WARNING", logger="awa.team.scope"):
        await runs.finish(Broken(), resources, paused_on=None)  # type: ignore[arg-type]
    assert "reporting undelivered messages failed" in caplog.text


def _note() -> Any:
    from awa.contracts import AgentMessage

    return AgentMessage(sender="a", recipient="b", subject="s", content="c")


async def test_a_cancelled_team_run_still_reports_unread_mail() -> None:
    reached = asyncio.Event()
    forever = asyncio.Event()

    @tool
    async def stall() -> str:
        """Wait until the run is cancelled."""
        reached.set()
        await forever.wait()
        return "never"

    def member(name: str, *script: Any) -> Agent:
        return Agent(name, description=f"The {name}.", model=FakeChatModel(list(script)))

    events: list[RunEvent] = []
    team = Team(
        "desk",
        orchestrator=Agent(
            "lead",
            model=FakeChatModel(
                [
                    tool_call("delegate_task", {"agent": "researcher", "task": "look"}),
                    tool_call("stall"),
                ]
            ),
            tools=[stall],
        ),
        members=[
            member(
                "researcher",
                tool_call("send_message", {"to": "writer", "subject": "n", "content": "FYI"}),
                "done",
            ),
            member("writer", "ok"),
        ],
        peers={"researcher": ["writer"]},
        hooks=[events.append],
    )

    task = asyncio.create_task(team.run("go"))
    await asyncio.wait_for(reached.wait(), timeout=30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [undelivered] = [e for e in events if isinstance(e, MessageUndeliveredEvent)]
    assert undelivered.recipient == "writer" and undelivered.message.content == "FYI"
    assert team.bus is not None and team.bus.pending_count("writer") == 0


async def test_a_small_handback_limit_keeps_the_summary_within_it() -> None:
    from awa.team import AgentNetwork, NetworkLimits

    report = "finding. " * 40
    seen: list[str] = []

    def lead_respond(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call("create_task", {"title": "Write the report", "owner": "writer"})
        if isinstance(call.last_message, ToolMessage):
            return "Planned."
        seen.append(str(call.last_message.content))
        return "Done."

    lead = Agent("lead", description="Leads.", model=FunctionChatModel(lead_respond))
    writer = Agent("writer", description="Writes.", model=FunctionChatModel(lambda call: report))
    limits = NetworkLimits(handback_chars=50)
    await AgentNetwork("n", lead=lead, teammates=[writer], limits=limits).run("go")

    handback = next(text for text in seen if "[Summary of a" in text)
    head = handback.split(" ...\n[Summary of a")[0].rsplit(">", 1)[-1].strip()
    assert 0 < len(head) <= 50
