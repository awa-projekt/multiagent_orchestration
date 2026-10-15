from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, tool

from awa.agents import Agent
from awa.contracts import (
    AgentLike,
    AgentMessageEvent,
    AgentStartedEvent,
    AgentStatus,
    DelegationResultEvent,
    ErrorEvent,
    LimitExceededEvent,
    RunCompletedEvent,
    RunEvent,
    UsageLimits,
)
from awa.errors import (
    AgentNotFoundError,
    ConfigurationError,
    SystemBusyError,
    TopologyError,
    UsageLimitExceededError,
)
from awa.tasks import TaskBoard, TaskStatus
from awa.team import AgentNetwork, NetworkLimits, Team, is_team_note
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call


def results_in(text: str) -> int:
    """Results a closing note gives the lead: new handbacks plus those listed as received."""

    received = text.split("Results you already received: ", 1)
    listed = received[1].split(". ", 1)[0].count(" (") if len(received) == 2 else 0
    return text.count("<handback") + listed


Spans = list[tuple[str, float, float]]


def create(title: str, **args: Any) -> Any:
    return tool_call("create_task", {"title": title, **args})


def planning_lead(
    *plan: Any, final: Callable[[str], str] | None = None, on_update: Any = None
) -> Agent:
    """A lead that plans with one scripted response per model call, then reacts to its prompt.

    Tool calls in one response run concurrently, so each planning step is its own
    response to keep task ids deterministic.
    """

    def respond(call: ModelCall) -> Any:
        if not plan and call.index == 0:
            return "Direct answer."
        if call.index < len(plan):
            return plan[call.index]
        last = call.last_message
        if isinstance(last, ToolMessage):
            return "Planned."
        text = str(last.content)
        if "Write the final answer" in text or "Time is up" in text:
            return final(text) if final else f"FINAL with {results_in(text)} results"
        if on_update is not None:
            return on_update(call)
        return "Noted."

    return Agent("lead", description="Plans and synthesises.", model=FunctionChatModel(respond))


def slow_tool(
    name: str, delay: float, spans: Spans | None, barrier: asyncio.Barrier | None = None
) -> BaseTool:
    @tool
    async def work() -> str:
        """Do the work."""
        if barrier is not None:
            async with asyncio.timeout(10):
                await barrier.wait()
        start = time.perf_counter()
        await asyncio.sleep(delay)
        if spans is not None:
            spans.append((name, start, time.perf_counter()))
        return f"{name} worked"

    return work


def teammate(
    name: str,
    capability: str,
    *,
    delay: float = 0.0,
    spans: Spans | None = None,
    respond: Callable[[ModelCall], Any] | None = None,
    barrier: asyncio.Barrier | None = None,
) -> Agent:
    def default(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"{name} result"
        return tool_call("work")

    return Agent(
        name,
        description=f"The {name}.",
        capabilities=[capability],
        model=FunctionChatModel(respond or default),
        tools=[slow_tool(name, delay, spans, barrier)],
    )


def network(lead: Agent, *mates: Agent, **kwargs: Any) -> AgentNetwork:
    return AgentNetwork("studio", lead=lead, teammates=list(mates), **kwargs)


def calls_of(agent: Agent) -> list[ModelCall]:
    model = agent._model
    assert isinstance(model, FunctionChatModel | FakeChatModel)
    return model.calls


_ASYNCIO = Path(asyncio.__file__).parent.as_posix() + "/"


def leaked() -> list[asyncio.Task[Any]]:
    """Tasks still running, except the connection pools and listeners of durable backends.

    On Python 3.11, `asyncio.wait_for` runs its awaitable in an inner task whose
    coroutine is asyncio's own (the pool scheduler's `Event.wait()`). Such a task
    lives only while the task awaiting it does, and that task is checked itself.
    """

    def infrastructure(task: asyncio.Task[Any]) -> bool:
        code = getattr(task.get_coro(), "cr_code", None)
        filename = code.co_filename.replace("\\", "/") if code is not None else ""
        return "psycopg_pool" in filename or filename.startswith(_ASYNCIO)

    return [
        t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not infrastructure(t)
    ]


# --------------------------------------------------------------------------- core flow


async def test_plan_parallel_work_handbacks_and_final_synthesis() -> None:
    # Both teammates must be working at once to pass the barrier (else it times out).
    barrier = asyncio.Barrier(2)
    lead = planning_lead(
        create("Research", required_capabilities=["research"]),
        create("Outline", required_capabilities=["writing"]),
    )
    net = network(
        lead,
        teammate("researcher", "research", barrier=barrier),
        teammate("writer", "writing", barrier=barrier),
    )

    result = await net.run("Write an explainer.")

    assert result.output == "FINAL with 2 results"
    assert result.metadata["tasks"]["completed"] == 2
    assert result.metadata["handbacks"] == 2
    assert result.metadata["stop_reason"] == "completed"
    assert net.task_board.get("task_1").owner == "researcher"
    assert net.task_board.get("task_1").result == "researcher result"
    assert net.task_board.get("task_2").owner == "writer"
    assert {"lead", "researcher", "writer"} <= set(result.usage.by_agent)
    assert result.usage.requests == sum(u.requests for u in result.usage.by_agent.values())
    assert result.agent == "lead" and result.handoff_path == ("lead",)
    assert leaked() == []


async def test_trivial_goal_is_answered_in_one_lead_turn() -> None:
    net = network(planning_lead(), teammate("writer", "writing"))

    result = await net.run("Hi!")

    assert result.output == "Direct answer."
    assert result.metadata["lead_turns"] == 1 and result.metadata["handbacks"] == 0


async def test_final_turn_receives_every_new_result_and_the_board_summary() -> None:
    seen: list[str] = []

    def final(text: str) -> str:
        seen.append(text)
        return "done"

    lead = planning_lead(create("A"), create("B"), final=final)
    await network(lead, teammate("writer", "writing")).run("go")

    [text] = seen
    assert "Tasks of this run: 2 completed." in text
    # A result the lead already got in an update is listed by id, not repeated.
    new = text.count('<handback task_id="task_')
    listed = text.split("Results you already received: ")[1].count("(completed)") if new < 2 else 0
    assert new + listed == 2
    assert "writer result" in text


async def test_dependencies_are_respected_and_results_reach_the_brief() -> None:
    order: list[str] = []

    def respond(call: ModelCall) -> Any:
        brief = str(call.messages[1].content)
        if isinstance(call.last_message, ToolMessage):
            return f"result of {brief.split(':')[0]}"
        order.append(brief.split(":")[0])
        return tool_call("work")

    writer = teammate("writer", "writing", delay=0.05, respond=respond)
    lead = planning_lead(create("Draft"), create("Polish", depends_on=["task_1"]))
    net = network(lead, writer, teammate("editor", "writing", delay=0.05, respond=respond))

    await net.run("go")

    assert order == ["Task task_1", "Task task_2"]
    polish_briefs = [
        str(c.messages[1].content)
        for mate in (writer, net.teammates[1])
        for c in calls_of(mate)
        if str(c.messages[1].content).startswith("Task task_2")
    ]
    assert (
        "Results of the tasks this builds on:\n- task_1 (Draft): result of Task task_1"
        in (polish_briefs[0])
    )


async def test_capability_matching_routes_tasks() -> None:
    lead = planning_lead(
        create("Find sources", required_capabilities=["research"]),
        create("Write prose", required_capabilities=["writing"]),
        create("Anything"),
    )
    net = network(lead, teammate("researcher", "research"), teammate("writer", "writing"))

    await net.run("go")

    assert net.task_board.get("task_1").owner == "researcher"
    assert net.task_board.get("task_2").owner == "writer"
    assert net.task_board.get("task_3").status is TaskStatus.COMPLETED


# --------------------------------------------------------------------------- failures


async def test_failed_task_is_retried_then_succeeds() -> None:
    attempts: list[int] = []

    def flaky(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "second attempt worked"
        attempts.append(call.index)
        if len(attempts) == 1:
            return tool_call("fail_task", {"task_id": "task_1", "reason": "source down"})
        return tool_call("work")

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage) and "failed" in str(
            call.last_message.content
        ):
            return "gave up"
        return flaky(call)

    net = network(planning_lead(create("Fetch")), teammate("writer", "writing", respond=respond))

    result = await net.run("go")

    task = net.task_board.get("task_1")
    assert task.status is TaskStatus.COMPLETED and task.result == "second attempt worked"
    assert result.metadata["handbacks"] == 1


async def test_permanent_failure_is_reported_to_the_lead() -> None:
    def broken(call: ModelCall) -> Any:
        raise RuntimeError("model unavailable")

    seen: list[str] = []
    lead = planning_lead(create("Fetch"), final=lambda text: seen.append(text) or "reported")
    events: list[RunEvent] = []
    net = network(
        lead,
        teammate("writer", "writing", respond=broken),
        limits=NetworkLimits(max_task_attempts=2),
        hooks=[events.append],
    )

    result = await net.run("go")

    assert result.output == "reported"
    task = net.task_board.get("task_1")
    assert task.status is TaskStatus.FAILED
    assert "RuntimeError: model unavailable" in (task.result or "")
    assert 'agent="writer" status="failed"' in seen[0]
    [returned] = [e for e in events if isinstance(e, DelegationResultEvent)]
    assert returned.status == "error" and "Failed after 2 attempt(s)" in returned.content_preview
    assert sum(isinstance(e, AgentStartedEvent) and e.agent == "writer" for e in events) == 2


async def test_a_teammate_failure_reaches_the_board_without_its_secrets() -> None:
    key = "sk-ant-api03-" + "A1b2C3d4" * 6

    def broken(call: ModelCall) -> Any:
        raise RuntimeError(f"rejected key {key}")

    net = network(
        planning_lead(create("Fetch"), final=lambda text: "reported"),
        teammate("writer", "writing", respond=broken),
        limits=NetworkLimits(max_task_attempts=1),
    )

    await net.run("go")

    reason = net.task_board.get("task_1").result or ""
    assert "RuntimeError: rejected key" in reason and "A1b2C3d4" * 6 not in reason


async def test_usage_limit_ends_the_run_and_cleans_up() -> None:
    net = network(
        planning_lead(create("A"), create("B")), teammate("writer", "writing", delay=0.01)
    )

    with pytest.raises(UsageLimitExceededError):
        await net.run("go", usage_limits=UsageLimits(max_requests=3))

    assert leaked() == []


# --------------------------------------------------------------------------- messaging


async def test_teammates_message_each_other_while_working() -> None:
    def researcher_respond(call: ModelCall) -> Any:
        last = call.last_message
        if not isinstance(last, ToolMessage):
            return tool_call("work")
        if "worked" in str(last.content):  # the writer is busy with its task by now
            return tool_call(
                "send_message", {"to": "writer", "subject": "tip", "content": "Cite RFC 42."}
            )
        return "research done"

    def writer_respond(call: ModelCall) -> Any:
        notes = [m for m in call.messages if isinstance(m, HumanMessage) and is_team_note(m)]
        if isinstance(call.last_message, ToolMessage):
            return "draft citing RFC 42" if notes else "draft"
        return tool_call("work")

    lead = planning_lead(
        create("Research", required_capabilities=["research"]),
        create("Write", required_capabilities=["writing"]),
    )
    writer = teammate("writer", "writing", delay=0.4, respond=writer_respond)
    events: list[RunEvent] = []
    net = network(
        lead,
        teammate("researcher", "research", delay=0.2, respond=researcher_respond),
        writer,
        hooks=[events.append],
    )

    await net.run("go")

    assert net.task_board.get("task_2").result == "draft citing RFC 42"
    delivered = [m for c in calls_of(writer) for m in c.messages if is_team_note(m)]
    assert "Cite RFC 42." in str(delivered[0].content)
    [message] = [
        e for e in events if isinstance(e, AgentMessageEvent) and "handback" not in e.message.data
    ]
    assert (message.agent, message.message.recipient) == ("researcher", "writer")


async def test_idle_teammate_gets_a_message_turn_and_can_ask_the_lead() -> None:
    def lead_respond(call: ModelCall) -> Any:
        last = call.last_message
        if call.index == 0:
            return tool_call("send_message", {"to": "writer", "subject": "q", "content": "Ready?"})
        if isinstance(last, ToolMessage):
            return "asked"
        return "final"

    def writer_respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "replied"
        return tool_call("send_message", {"to": "lead", "subject": "a", "content": "Yes."})

    lead = Agent("lead", model=FunctionChatModel(lead_respond))
    writer = teammate("writer", "writing", respond=writer_respond)
    net = network(lead, writer, limits=NetworkLimits(max_message_turns=1))

    result = await net.run("go")

    assert "Ready?" in str(calls_of(writer)[0].messages)
    assert "Yes." in str(calls_of(lead)[-1].messages)
    assert result.metadata["lead_turns"] == 2


async def test_non_edges_are_denied_between_teammates() -> None:
    net = network(
        planning_lead(),
        teammate("a", "x"),
        teammate("b", "x"),
        teammate("c", "x"),
        peers={"a": ["b"]},
    )

    assert net.topology()["peers"] == {"a": ["b"], "b": ["a"]}
    # Explicit edges opt into messaging: `c` has none, so it reports through the board only.
    assert "send_message" not in {t.name for t in net.agent("c").tools}
    [send] = [t for t in net.agent("a").tools if t.name == "send_message"]
    assert set(send.tool_call_schema.model_json_schema()["properties"]["to"]["enum"]) == {
        "lead",
        "b",
    }


# --------------------------------------------------------------------------- coordination


async def test_lead_adds_a_follow_up_task_after_a_handback() -> None:
    created: list[str] = []

    def on_update(call: ModelCall) -> Any:
        if 'task_id="task_1"' in str(call.last_message.content) and not created:
            created.append("task_3")
            return create("Follow-up", depends_on=["task_1"], required_capabilities=["writing"])
        return "Noted."

    lead = planning_lead(
        create("First", required_capabilities=["writing"]),
        create("Slow side task", required_capabilities=["editing"]),
        on_update=on_update,
    )
    net = network(
        lead, teammate("writer", "writing", delay=0.05), teammate("editor", "editing", delay=0.3)
    )

    result = await net.run("go")

    follow_up = net.task_board.get("task_3")
    assert follow_up.title == "Follow-up" and follow_up.status is TaskStatus.COMPLETED
    assert follow_up.owner == "writer"
    assert result.metadata["tasks"]["completed"] == 3
    assert result.output == "FINAL with 3 results"


async def test_teammates_create_follow_up_tasks_for_each_other() -> None:
    def researcher_respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "facts gathered"
        return create("Write it up", depends_on=["task_1"], required_capabilities=["writing"])

    lead = planning_lead(create("Gather facts", required_capabilities=["research"]))
    net = network(
        lead,
        teammate("researcher", "research", respond=researcher_respond),
        teammate("writer", "writing"),
        teammates_create_tasks=True,
    )

    result = await net.run("go")

    follow_up = net.task_board.get("task_2")
    assert (follow_up.created_by, follow_up.owner) == ("researcher", "writer")
    assert follow_up.status is TaskStatus.COMPLETED
    assert result.metadata["tasks"]["completed"] == 2


async def test_lead_can_cancel_a_task_while_it_is_worked_on() -> None:
    def on_update(call: ModelCall) -> Any:
        if 'task_id="task_1"' in str(call.last_message.content):
            return tool_call("cancel_task", {"task_id": "task_2", "reason": "not needed"})
        return "Noted."

    lead = planning_lead(
        create("Quick", required_capabilities=["writing"]),
        create("Slow", required_capabilities=["editing"]),
        on_update=on_update,
    )
    net = network(
        lead, teammate("writer", "writing", delay=0.05), teammate("editor", "editing", delay=0.3)
    )

    result = await net.run("go")

    assert net.task_board.get("task_2").status is TaskStatus.CANCELLED
    assert result.metadata["tasks"] == {**result.metadata["tasks"], "completed": 1, "cancelled": 1}
    assert result.metadata["handbacks"] == 1


async def test_stuck_tasks_are_reported_in_the_final_turn() -> None:
    seen: list[str] = []
    lead = planning_lead(
        create("Sign off", owner="lead"), final=lambda text: seen.append(text) or "partial answer"
    )

    result = await network(lead, teammate("writer", "writing")).run("go")

    assert result.output == "partial answer"
    assert "task_1 (pending)" in seen[0]
    assert result.metadata["tasks"]["pending"] == 1


async def test_idle_timeout_when_open_tasks_are_owned_outside_the_network() -> None:
    board = TaskBoard()
    await board.create("External", created_by="user")
    await board.claim("task_1", "outsider")
    lead = planning_lead(final=lambda text: "gave up waiting")
    net = network(
        lead,
        teammate("writer", "writing"),
        task_board=board,
        limits=NetworkLimits(idle_timeout=0.1),
    )

    result = await net.run("go", metadata={"awa.tasks": ["task_1"]})

    assert result.metadata["stop_reason"] == "idle_timeout"
    assert result.output == "gave up waiting"


# --------------------------------------------------------------------------- limits


async def test_max_tasks_refuses_further_tasks() -> None:
    lead = planning_lead(create("A"), create("B"), create("C"))
    net = network(lead, teammate("writer", "writing"), limits=NetworkLimits(max_tasks=2))

    result = await net.run("go")

    tool_results = [str(m.content) for m in result.messages if isinstance(m, ToolMessage)]
    assert tool_results[2] == (
        "Not created: this run is limited to max_tasks=2 and only 0 more can be created."
    )
    assert len(net.task_board) == 2 and result.metadata["tasks"]["completed"] == 2


async def test_max_lead_turns_reserves_the_final_turn() -> None:
    lead = planning_lead(create("A"), create("B"))
    net = network(
        lead, teammate("writer", "writing", delay=0.05), limits=NetworkLimits(max_lead_turns=2)
    )

    result = await net.run("go")

    assert result.metadata["lead_turns"] == 2
    assert result.output == "FINAL with 2 results"


async def test_run_timeout_stops_teammates_and_asks_for_a_final_answer() -> None:
    lead = planning_lead(create("Endless"), final=lambda text: "best effort")
    net = network(
        lead, teammate("writer", "writing", delay=10), limits=NetworkLimits(run_timeout=1.0)
    )

    result = await net.run("go")

    assert result.output == "best effort"
    assert result.metadata["stop_reason"] == "run_timeout"
    task = net.task_board.get("task_1")
    assert task.status is TaskStatus.PENDING and task.owner is None
    assert leaked() == []


async def test_a_run_timeout_after_the_last_lead_turn_adds_no_closing_turn() -> None:
    lead = planning_lead(create("Endless"), final=lambda text: "best effort")
    net = network(
        lead,
        teammate("writer", "writing", delay=10),
        limits=NetworkLimits(run_timeout=1.0, max_lead_turns=1),
    )

    result = await net.run("go")

    assert result.metadata["lead_turns"] == 1
    assert result.metadata["stop_reason"] == "max_lead_turns"
    assert leaked() == []


async def test_max_concurrent_teammates() -> None:
    spans: Spans = []
    lead = planning_lead(
        create("A", required_capabilities=["x"]), create("B", required_capabilities=["y"])
    )
    net = network(
        lead,
        teammate("a", "x", delay=0.1, spans=spans),
        teammate("b", "y", delay=0.1, spans=spans),
        limits=NetworkLimits(max_concurrent_teammates=1),
    )

    await net.run("go")

    (_, _, first_end), (_, second_start, _) = sorted(spans, key=lambda s: s[1])
    assert second_start >= first_end


async def test_cancelling_a_run_cleans_up_workers_and_releases_tasks() -> None:
    asyncio.get_running_loop().set_debug(True)
    net = network(planning_lead(create("Endless")), teammate("writer", "writing", delay=10))

    run = asyncio.create_task(net.run("go"))
    async with asyncio.timeout(10):
        while (
            "task_1" not in net.task_board
            or net.task_board.get("task_1").status is TaskStatus.PENDING
        ):
            await asyncio.sleep(0.01)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert leaked() == []
    assert net.task_board.get("task_1").status is TaskStatus.PENDING
    assert net.directory.presence("writer").status is AgentStatus.AVAILABLE


# --------------------------------------------------------------------------- observability


async def test_stream_interleaves_concurrent_teammates_and_ends_with_completion() -> None:
    lead = planning_lead(
        create("A", required_capabilities=["x"]), create("B", required_capabilities=["y"])
    )
    net = network(lead, teammate("a", "x", delay=0.1), teammate("b", "y", delay=0.1))

    events = [event async for event in net.stream("go")]

    lifecycle = [(e.type, e.agent) for e in events if e.type in ("agent_started", "agent_finished")]
    a_start = lifecycle.index(("agent_started", "a"))
    b_start = lifecycle.index(("agent_started", "b"))
    assert max(a_start, b_start) < min(
        lifecycle.index(("agent_finished", "a")), lifecycle.index(("agent_finished", "b"))
    )
    assert lifecycle[0] == ("agent_started", "studio")
    completed = events[-1]
    assert isinstance(completed, RunCompletedEvent)
    assert completed.result.output == "FINAL with 2 results"
    teammate_starts = [e for e in events if isinstance(e, AgentStartedEvent) and e.agent in "ab"]
    assert {e.parent_run_id for e in teammate_starts} == {completed.result.run_id}
    assert {e.depth for e in teammate_starts} == {1}


async def test_directory_presence_reflects_busy_teammates() -> None:
    seen: list[AgentStatus] = []
    net_ref: list[AgentNetwork] = []

    @tool
    def peek() -> str:
        """Look at the directory."""
        seen.append(net_ref[0].directory.presence("writer").status)
        return "peeked"

    def respond(call: ModelCall) -> Any:
        return "done" if isinstance(call.last_message, ToolMessage) else tool_call("peek")

    writer = Agent("writer", model=FunctionChatModel(respond), tools=[peek])
    net = network(planning_lead(create("A")), writer)
    net_ref.append(net)

    await net.run("go")

    assert seen == [AgentStatus.BUSY]
    assert net.directory.presence("writer").status is AgentStatus.AVAILABLE


# --------------------------------------------------------------------------- construction


def test_validation_and_structure() -> None:
    lead = planning_lead()
    with pytest.raises(TopologyError, match="must not also be a teammate"):
        AgentNetwork("n", lead=lead, teammates=[lead])
    with pytest.raises(TopologyError, match="needs at least one member"):
        AgentNetwork("n", lead=lead, teammates=[])
    with pytest.raises(TopologyError, match="Network 'n': the name 'a' is used more than once"):
        AgentNetwork("n", lead=lead, teammates=[teammate("a", "x"), teammate("a", "y")])
    with pytest.raises(TopologyError, match="unknown agent 'ghost'"):
        AgentNetwork("n", lead=lead, teammates=[teammate("a", "x")], peers={"a": ["ghost"]})
    with pytest.raises(TypeError, match="must be Agents"):
        AgentNetwork("n", lead=lead, teammates=[object()])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="peers must be a mapping"):
        AgentNetwork("n", lead=lead, teammates=[teammate("a", "x")], peers="some")  # type: ignore[arg-type]

    net = network(lead, teammate("a", "x"), teammate("b", "y"))
    assert isinstance(net, AgentLike)
    assert net.card.role == "network" and net.card.capabilities == ("x", "y")
    assert net.topology() == {
        "lead": "lead",
        "teammates": ["a", "b"],
        "peers": {"a": ["b"], "b": ["a"]},
    }
    assert net.to_mermaid() == "\n".join(
        [
            "flowchart TD",
            '    n0(["lead"])',
            '    n1["a"]',
            '    n2["b"]',
            "    n0 --> n1",
            "    n0 --> n2",
            "    n1 <-.-> n2",
        ]
    )
    lead_tools = {t.name for t in net.agent("lead").tools}
    assert {"create_task", "cancel_task", "send_message", "broadcast", "find_agents"} <= lead_tools
    assert not {"claim_task", "complete_task"} & lead_tools
    mate_tools = {t.name for t in net.agent("a").tools}
    assert {"fail_task", "send_message"} <= mate_tools
    # Planning stays with the lead unless `teammates_create_tasks`; no directory or board views.
    assert not {"create_task", "list_tasks", "get_task", "find_agents"} & mate_tools
    # A teammate's final answer completes its task; completing it by tool too duplicated work.
    assert "complete_task" not in mate_tools
    assert not {"claim_task", "release_task", "broadcast"} & mate_tools
    assert net.directory.names() == ["a", "b", "lead"]
    assert repr(net) == "AgentNetwork('studio', lead='lead', teammates=[a, b])"


async def test_one_run_at_a_time_only_with_a_shared_board_and_no_threads() -> None:
    net = network(
        planning_lead(create("Endless")),
        teammate("writer", "writing", delay=10),
        task_board=TaskBoard(),
    )

    with pytest.raises(ValueError, match="thread_id"):
        await net.run("go", thread_id="t")
    with pytest.raises(ValueError, match="task_9 .* are not on its task board"):
        await net.run("go", metadata={"awa.tasks": ["task_9"]})
    first = asyncio.create_task(net.run("go"))
    await asyncio.sleep(0)
    with pytest.raises(SystemBusyError, match="already running"):
        await net.run("again")
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_network_as_a_team_member() -> None:
    net = AgentNetwork(
        "studio", lead=planning_lead(create("A")), teammates=[teammate("writer", "writing")]
    )
    lead = Agent(
        "boss",
        model=FakeChatModel(
            [tool_call("delegate_task", {"agent": "studio", "task": "make it"}), "shipped"]
        ),
    )
    team = Team("company", orchestrator=lead, members=[net])

    result = await team.run("go")

    assert result.output == "shipped"
    assert "FINAL with 1 results" in str(result.messages)
    assert {"boss", "lead", "writer"} <= set(result.usage.by_agent)


def test_run_sync() -> None:
    net = network(planning_lead(), teammate("writer", "writing"))

    assert net.run_sync("Hi").output == "Direct answer."


async def test_lead_plans_a_dependent_graph_in_one_create_tasks_call() -> None:
    order: list[str] = []

    def respond(call: ModelCall) -> Any:
        brief = str(call.messages[1].content)
        if isinstance(call.last_message, ToolMessage):
            return f"done: {brief.split(':')[1].strip().splitlines()[0]}"
        order.append(brief.split(":")[0])
        return tool_call("work")

    graph = tool_call(
        "create_tasks",
        {
            "tasks": [
                {"key": "review", "title": "Review", "depends_on": ["draft"]},
                {"key": "research", "title": "Research"},
                {"key": "draft", "title": "Draft", "depends_on": ["research"]},
            ]
        },
    )
    lead = planning_lead(graph)
    net = network(
        lead,
        teammate("a", "writing", delay=0.02, respond=respond),
        teammate("b", "writing", delay=0.02, respond=respond),
    )

    result = await net.run("go")

    assert order == ["Task task_1", "Task task_2", "Task task_3"]
    assert [net.task_board.get(f"task_{i}").title for i in (1, 2, 3)] == [
        "Research",
        "Draft",
        "Review",
    ]
    assert net.task_board.get("task_3").depends_on == ("task_2",)
    assert result.metadata["tasks"]["completed"] == 3
    assert result.output == "FINAL with 3 results"
    created = next(m for m in result.messages if isinstance(m, ToolMessage))
    assert str(created.content).startswith('Created 3 tasks {"review": "task_3"')


async def test_create_tasks_batch_counts_against_max_tasks() -> None:
    batch = tool_call(
        "create_tasks", {"tasks": [{"key": "a", "title": "A"}, {"key": "b", "title": "B"}]}
    )
    net = network(planning_lead(batch), teammate("w", "x"), limits=NetworkLimits(max_tasks=1))

    result = await net.run("go")

    refusal = next(m for m in result.messages if isinstance(m, ToolMessage))
    assert (
        refusal.content
        == "Not created: this run is limited to max_tasks=1 and only 1 more can be created."
    )
    assert len(net.task_board) == 0


# --------------------------------------------------------------------------- failure modes


async def test_unknown_member_and_history_are_refused() -> None:
    events: list[RunEvent] = []
    net = network(planning_lead(), teammate("a", "x"), hooks=[events.append])

    with pytest.raises(AgentNotFoundError, match="nobody"):
        net.agent("nobody")
    with pytest.raises(ConfigurationError, match="history is not supported"):
        await net.run("go", history=[HumanMessage(content="earlier")])
    assert [e.error_type for e in events if isinstance(e, ErrorEvent)] == ["ConfigurationError"]


async def test_max_lead_turns_of_one_ends_the_run_without_a_closing_turn() -> None:
    events: list[RunEvent] = []
    net = network(
        planning_lead(create("A")),
        teammate("writer", "writing"),
        limits=NetworkLimits(max_lead_turns=1),
        hooks=[events.append],
    )

    result = await net.run("go")

    assert result.metadata["lead_turns"] == 1 and result.metadata["stop_reason"] == "max_lead_turns"
    assert result.output == "Planned."
    [limit] = [e for e in events if isinstance(e, LimitExceededEvent)]
    assert (limit.limit, limit.maximum) == ("max_lead_turns", 1)


async def test_a_failing_message_turn_does_not_fail_the_run() -> None:
    def lead_respond(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call("send_message", {"to": "writer", "subject": "q", "content": "Ready?"})
        return "asked"

    def broken(call: ModelCall) -> Any:
        raise RuntimeError("model unavailable")

    net = network(
        Agent("lead", model=FunctionChatModel(lead_respond)),
        teammate("writer", "writing", respond=broken),
        limits=NetworkLimits(max_message_turns=1),
    )

    result = await net.run("go")

    assert result.output == "asked"
    assert len(calls_of(net.agent("writer"))) == 1
