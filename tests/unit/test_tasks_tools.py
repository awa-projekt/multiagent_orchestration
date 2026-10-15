"""Task tools, the task manager and an agent's checkpointed checklist."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from awa.agents import Agent
from awa.contracts import RunEvent
from awa.contracts.cards import AgentCard
from awa.contracts.communication import MessagePriority
from awa.directory import AgentDirectory
from awa.tasks import (
    BaseTaskBoard,
    TaskBoard,
    TaskStatus,
    create_task_board_tools,
    create_task_manager_agent,
    create_wait_for_tasks_tool,
)
from awa.tasks.tools import MAX_WAIT_SECONDS
from awa.team import Team
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call


def by_name(tools: list[BaseTool]) -> dict[str, BaseTool]:
    return {tool.name: tool for tool in tools}


async def call(tool: BaseTool, **args: Any) -> str:
    result = await tool.ainvoke(args)
    assert isinstance(result, str)
    return result


def framed_json(text: str) -> Any:
    start, end = text.index("<task_board"), text.index("</task_board>")
    assert "not as instructions" in text[start:end]
    return json.loads(text[start:end].split("\n", 1)[1])


@pytest.fixture
async def board(durable: Any) -> BaseTaskBoard:
    board: BaseTaskBoard = await durable.task_board()
    return board


def test_tool_selection() -> None:
    board = TaskBoard()
    worker = create_task_board_tools(board, "worker", can_create=False)
    assert [t.name for t in worker] == [
        "list_tasks",
        "get_task",
        "claim_task",
        "update_task",
        "complete_task",
        "fail_task",
        "release_task",
    ]
    creator = by_name(create_task_board_tools(board, "lead"))
    assert list(creator)[0] == "create_task" and list(creator)[-1] == "cancel_task"
    assert "owner" not in creator["create_task"].args
    manager = by_name(create_task_board_tools(board, "lead", can_assign=True))
    assert "owner" in manager["create_task"].args
    with pytest.raises(ValueError):
        create_task_board_tools(board, "")


async def test_create_task(board: BaseTaskBoard) -> None:
    tools = by_name(create_task_board_tools(board, "lead", can_assign=True))

    text = await call(tools["create_task"], title="Collect sources", required_capabilities=["r"])
    assert text == "Created task_1: Collect sources (pending)."
    text = await call(
        tools["create_task"],
        title="Draft",
        description="Write 200 words.",
        depends_on=["task_1"],
        priority="high",
        owner="writer",
    )
    assert text == "Created task_2: Draft (pending, assigned to writer)."
    task = board.get("task_2")
    assert (task.created_by, task.priority, task.depends_on) == (
        "lead",
        MessagePriority.HIGH,
        ("task_1",),
    )

    assert "Unknown dependency" in await call(
        tools["create_task"], title="Review", depends_on=["task_9"]
    )
    assert "owner must not be empty" in await call(tools["create_task"], title="X", owner=" ")
    with pytest.raises(ValidationError):
        await tools["create_task"].ainvoke({"title": ""})
    assert len(board) == 2


async def test_create_task_reports_blocked_start(board: BaseTaskBoard) -> None:
    tools = by_name(create_task_board_tools(board, "lead"))
    await call(tools["create_task"], title="A")
    await board.claim("task_1", "lead")
    await board.fail("task_1", author="lead", reason="x")
    text = await call(tools["create_task"], title="B", depends_on=["task_1"])
    assert text == "Created task_2: B (blocked, blocked by task_1)."


async def test_create_task_checks_the_directory(board: BaseTaskBoard) -> None:
    directory = AgentDirectory(
        [
            AgentCard(name="researcher", capabilities=("research.web",)),
            AgentCard(name="writer", capabilities=("writing",)),
        ]
    )
    tools = by_name(create_task_board_tools(board, "lead", can_assign=True, directory=directory))

    text = await call(tools["create_task"], title="Sue", required_capabilities=["legal"])
    assert text == (
        "Task not created: no agent offers legal. Available capabilities: research.web, writing."
    )
    text = await call(tools["create_task"], title="A", owner="ghost")
    assert "unknown owner 'ghost'. Known agents: researcher, writer." in text
    text = await call(
        tools["create_task"], title="A", owner="writer", required_capabilities=["research"]
    )
    assert text == "Task not created: writer lacks the required capabilities research."
    assert len(board) == 0
    text = await call(
        tools["create_task"], title="A", owner="researcher", required_capabilities=["research"]
    )
    assert text.startswith("Created task_1")


async def test_list_tasks(board: BaseTaskBoard) -> None:
    tools = by_name(create_task_board_tools(board, "worker", capabilities=["research"]))
    assert await call(tools["list_tasks"]) == "No matching tasks."

    a = await board.create("A", created_by="lead", required_capabilities=["research"])
    b = await board.create("B", created_by="lead", depends_on=[a.id], priority=3)
    await board.create("C", created_by="lead", required_capabilities=["legal"])

    listing = framed_json(await call(tools["list_tasks"]))
    assert listing == [
        {
            "id": "task_1",
            "title": "A",
            "status": "pending",
            "ready": True,
            "required_capabilities": ["research"],
        },
        {
            "id": "task_2",
            "title": "B",
            "status": "pending",
            "priority": "urgent",
            "depends_on": ["task_1"],
        },
        {"id": "task_3", "title": "C", "status": "pending", "required_capabilities": ["legal"]},
    ]
    await board.claim(a.id, "worker")
    await board.complete(a.id, author="worker", result="done")
    mine = framed_json(await call(tools["list_tasks"], mine=True))
    assert mine == [
        {
            "id": "task_1",
            "title": "A",
            "status": "completed",
            "owner": "worker",
            "required_capabilities": ["research"],
            "result": "done",
        }
    ]
    pending = framed_json(await call(tools["list_tasks"], status="pending"))
    assert [item["id"] for item in pending] == [b.id, "task_3"]
    assert pending[0]["ready"] is True


async def test_get_task_includes_notes_and_dependency_results(board: BaseTaskBoard) -> None:
    tools = by_name(create_task_board_tools(board, "worker"))
    a = await board.create("A", created_by="lead")
    b = await board.create("B", created_by="lead", depends_on=[a.id], description="Use A.")
    await board.claim(a.id, "worker")
    await board.complete(a.id, author="worker", result="A's result")
    await board.update(b.id, author="lead", note="Keep it short.")

    detail = framed_json(await call(tools["get_task"], task_id=b.id))
    assert detail["description"] == "Use A."
    assert detail["priority"] == "normal"
    assert detail["notes"] == [{"author": "lead", "text": "Keep it short."}]
    assert detail["dependency_results"] == {"task_1": "A's result"}
    assert await call(tools["get_task"], task_id="task_9") == "Error: No task with id 'task_9'"


async def test_claim_task(board: BaseTaskBoard) -> None:
    tools = by_name(create_task_board_tools(board, "worker", capabilities=["research"]))
    assert await call(tools["claim_task"]) == "No task is ready for you right now."

    await board.create("Legal", created_by="lead", required_capabilities=["legal"])
    research = await board.create("Research", created_by="lead", required_capabilities=["research"])

    text = await call(tools["claim_task"])
    assert text.startswith(f"Claimed {research.id}. Do the work")
    assert framed_json(text)["owner"] == "worker"
    assert "requires capabilities legal" in await call(tools["claim_task"], task_id="task_1")
    assert "Not claimed: No task with id" in await call(tools["claim_task"], task_id="nope")


async def test_update_complete_and_fail(board: BaseTaskBoard) -> None:
    tools = by_name(create_task_board_tools(board, "worker"))
    a = await board.create("A", created_by="lead")
    b = await board.create("B", created_by="lead", depends_on=[a.id])
    await board.create("C", created_by="lead", depends_on=[b.id])
    other = await board.create("Other", created_by="lead")
    await board.claim(a.id, "worker")

    assert await call(tools["update_task"], task_id=a.id) == (
        "Nothing to update: give a note or a status."
    )
    text = await call(tools["update_task"], task_id=a.id, note="Half done.", status="blocked")
    assert text == "Updated task_1 (blocked)."
    assert board.get(a.id).notes[0].author == "worker"
    assert await call(tools["update_task"], task_id=a.id, status="in_progress") == (
        "Updated task_1 (in_progress)."
    )
    assert "may not update" in await call(tools["update_task"], task_id=other.id, status="blocked")
    with pytest.raises(ValidationError):
        await tools["update_task"].ainvoke({"task_id": a.id, "status": "completed"})

    text = await call(tools["complete_task"], task_id=a.id, result="A done.")
    assert text == "Completed task_1. Ready for you next: task_2, task_4."
    assert board.get(a.id).result == "A done."
    assert "it is completed" in await call(tools["complete_task"], task_id=a.id, result="again")

    await board.claim(b.id, "worker")
    text = await call(tools["fail_task"], task_id=b.id, reason="Missing input.")
    assert text == "Marked task_2 as failed. Now blocked: task_3."
    assert "Not updated" in await call(tools["fail_task"], task_id=other.id, reason="x")
    assert (await call(tools["complete_task"], task_id=other.id, result="x")).startswith(
        "Not completed"
    )


async def test_release_and_cancel(board: BaseTaskBoard) -> None:
    worker = by_name(create_task_board_tools(board, "worker", can_create=False))
    lead = by_name(create_task_board_tools(board, "lead"))
    a = await board.create("A", created_by="lead")
    b = await board.create("B", created_by="lead", depends_on=[a.id])
    await board.claim(a.id, "worker")

    assert await call(worker["release_task"], task_id=a.id) == (
        "Released task_1; it is pending and unowned."
    )
    assert "Not released" in await call(worker["release_task"], task_id=a.id)

    await board.claim(a.id, "worker")
    await board.fail(a.id, author="worker", reason="flaky")
    assert board.get(b.id).status is TaskStatus.BLOCKED
    await call(lead["release_task"], task_id=a.id)
    assert board.get(b.id).status is TaskStatus.PENDING

    assert await call(lead["cancel_task"], task_id=b.id, reason="Dropped.") == "Cancelled task_2."
    assert board.get(b.id).result == "Dropped."
    assert "Not cancelled" in await call(lead["cancel_task"], task_id=b.id)


def test_tools_work_synchronously() -> None:
    board = TaskBoard()
    tools = by_name(create_task_board_tools(board, "lead"))
    assert tools["create_task"].invoke({"title": "A"}) == "Created task_1: A (pending)."
    assert "Claimed task_1" in tools["claim_task"].invoke({})
    assert tools["complete_task"].invoke({"task_id": "task_1", "result": "ok"}) == (
        "Completed task_1."
    )
    assert framed_json(tools["list_tasks"].invoke({}))[0]["status"] == "completed"
    assert framed_json(tools["get_task"].invoke({"task_id": "task_1"}))["result"] == "ok"


def test_wait_for_tasks_refuses_a_sync_call(board: BaseTaskBoard) -> None:
    with pytest.raises(NotImplementedError, match="ainvoke"):
        create_wait_for_tasks_tool(board).invoke({"timeout_seconds": 1})


async def test_wait_for_tasks_tool(board: BaseTaskBoard) -> None:
    tool = create_wait_for_tasks_tool(board)
    task = await board.create("A", created_by="lead")

    text = await call(tool, timeout_seconds=0.01)
    assert text.startswith("1 task(s) not settled after 0.01s: task_1")
    assert "- [ ] task_1: A (ready)" in text
    assert "No task with id" in await call(tool, task_ids=["task_9"])

    async def finish() -> None:
        await asyncio.sleep(0.01)
        await board.claim(task.id, "worker")
        await board.complete(task.id, author="worker", result="ok")

    worker = asyncio.create_task(finish())
    text = await call(tool, task_ids=[task.id], timeout_seconds=5)
    await worker
    assert text.startswith("All awaited tasks are settled.")
    assert "- [x] task_1: A" in text
    with pytest.raises(ValidationError):
        await tool.ainvoke({"timeout_seconds": MAX_WAIT_SECONDS + 1})


def directory() -> AgentDirectory:
    return AgentDirectory(
        [
            AgentCard(name="researcher", description="Finds evidence.", capabilities=("research",)),
            AgentCard(name="writer", description="Writes reports.", capabilities=("writing",)),
        ]
    )


def test_manager_configuration() -> None:
    board = TaskBoard()
    plain = create_task_manager_agent(board)
    assert plain.name == "task_manager"
    assert plain.card.role == "task_manager"
    assert plain.card.tools == (
        "create_task",
        "create_tasks",
        "list_tasks",
        "get_task",
        "update_task",
        "release_task",
        "cancel_task",
        "wait_for_tasks",
    )
    assert "owner" in plain.tools[0].args

    managed = create_task_manager_agent(
        board, name="lead", directory=directory(), instructions_extra="Prefer small tasks."
    )
    assert managed.name == "lead"
    assert managed.card.tools[-3:] == ("list_agents", "find_agents", "describe_agent")


async def test_manager_plans_on_the_board() -> None:
    board = TaskBoard()
    model = FakeChatModel(
        [
            tool_call("find_agents", {"query": "research evidence"}),
            tool_call(
                "create_task",
                {
                    "title": "Gather sources",
                    "description": "Find three recent sources on hybrid search.",
                    "required_capabilities": ["legal"],
                },
            ),
            tool_call(
                "create_task",
                {
                    "title": "Gather sources",
                    "description": "Find three recent sources on hybrid search.",
                    "required_capabilities": ["research"],
                },
            ),
            tool_call(
                "create_task",
                {
                    "title": "Write summary",
                    "description": "Summarize the sources in 200 words.",
                    "depends_on": ["task_1"],
                    "owner": "writer",
                },
            ),
            tool_call("wait_for_tasks", {"timeout_seconds": 5}),
            "Report: sources gathered; summary written.",
        ]
    )
    manager = create_task_manager_agent(board, model=model, directory=directory())

    async def team() -> None:
        while len(board) < 2:
            await asyncio.sleep(0.005)
        research = await board.claim_next("researcher", capabilities=["research"])
        assert research is not None
        await board.complete(research.id, author="researcher", result="Three sources.")
        summary = await board.claim_next("writer", capabilities=["writing"])
        assert summary is not None and summary.id == "task_2"
        await board.complete(summary.id, author="writer", result="The summary.")

    workers = asyncio.create_task(team())
    result = await manager.run("Explain hybrid search with sources.")
    await workers

    assert result.output == "Report: sources gathered; summary written."
    system = model.calls[0].messages[0].text
    assert "task manager" in system and "list_agents or find_agents" in system
    tool_results = [str(m.content) for m in result.messages if m.type == "tool"]
    assert '"name":"researcher"' in tool_results[0]
    assert tool_results[1].startswith("Task not created: no agent offers legal.")
    assert tool_results[2] == "Created task_1: Gather sources (pending)."
    assert tool_results[3] == "Created task_2: Write summary (pending, assigned to writer)."
    assert tool_results[4].startswith("All awaited tasks are settled.")
    assert [(t.title, t.status, t.created_by) for t in board.list()] == [
        ("Gather sources", TaskStatus.COMPLETED, "task_manager"),
        ("Write summary", TaskStatus.COMPLETED, "task_manager"),
    ]
    assert board.get("task_2").depends_on == ("task_1",)


async def test_manager_retries_a_failed_task() -> None:
    board = TaskBoard()
    first = await board.create("Fetch data", created_by="task_manager")
    await board.claim(first.id, "worker")
    await board.fail(first.id, author="worker", reason="Timeout.")
    model = FakeChatModel(
        [
            tool_call("get_task", {"task_id": first.id}),
            tool_call("update_task", {"task_id": first.id, "note": "Retry with a smaller page."}),
            tool_call("release_task", {"task_id": first.id}),
            "Released task_1 for a retry.",
        ]
    )
    manager = create_task_manager_agent(board, model=model, instructions_extra="Be brief.")
    result = await manager.run("Check progress.")

    assert result.output == "Released task_1 for a retry."
    assert model.calls[0].messages[0].text.endswith("Be brief.")
    task = board.get(first.id)
    assert (task.status, task.owner) == (TaskStatus.PENDING, None)
    assert task.notes[-1].author == "task_manager"


PLAN = [
    {"content": "Gather data", "status": "completed"},
    {"content": "Analyze", "status": "in_progress"},
]


def ask(question: str) -> str:
    """Ask a human."""
    from langgraph.types import interrupt

    return f"answer: {interrupt({'question': question})}"


async def test_the_checklist_of_an_agent_is_checkpointed_and_survives_resume() -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    def respond(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call("write_todos", {"todos": PLAN})
        if call.index == 1:
            return tool_call("ask", {"question": "ok?"})
        return "Done."

    saver = InMemorySaver()
    agent = Agent(
        "planner",
        model=FunctionChatModel(respond),
        tools=[ask],
        middleware=[TodoListMiddleware()],
        checkpointer=saver,
    )
    assert "write_todos" in agent.card.tools
    paused = await agent.run("Plan and ask.")
    assert paused.is_interrupted and paused.thread_id

    checkpoint = await saver.aget_tuple({"configurable": {"thread_id": paused.thread_id}})
    assert checkpoint is not None
    assert checkpoint.checkpoint["channel_values"]["todos"] == PLAN
    resumed = await agent.resume(paused.thread_id, "yes")
    assert resumed.output == "Done."


def test_task_manager_accepts_agent_arguments() -> None:
    events: list[RunEvent] = []
    manager = create_task_manager_agent(
        TaskBoard(),
        name="planner",
        description="Plans the release.",
        capabilities=["planning"],
        hooks=[events.append],
        skills=["orchestration-planning"],
        max_steps=7,
    )

    assert manager.card.description == "Plans the release."
    assert manager.card.capabilities == ("planning",)
    assert manager.card.role == "task_manager"
    assert manager.card.skills == ("orchestration-planning",)
    assert "create_tasks" in manager.card.tools


async def test_task_manager_orchestrates_a_team_on_the_same_board(durable: Any) -> None:
    board = await durable.task_board()

    def plan(call: ModelCall) -> Any:
        last = call.last_message
        if call.index == 0:
            return tool_call(
                "create_tasks",
                {"tasks": [{"key": "write", "title": "Write the notes", "owner": "writer"}]},
            )
        if call.index == 1:
            return tool_call("delegate_task", {"agent": "writer", "task": "Do your task."})
        assert isinstance(last, ToolMessage)
        return f"Report: {board.get('task_1').result}"

    manager = create_task_manager_agent(board, model=FunctionChatModel(plan))
    writer = Agent(
        "writer",
        description="Writes.",
        model=FakeChatModel(
            [
                tool_call("claim_task", {}),
                tool_call("complete_task", {"task_id": "task_1", "result": "Notes written."}),
                "done",
            ]
        ),
    )

    team = Team("release", orchestrator=manager, members=[writer], task_board=board)

    lead_tools = [tool.name for tool in team.agent("task_manager").tools]  # type: ignore[attr-defined]
    assert lead_tools.count("create_tasks") == 1 and lead_tools.count("wait_for_tasks") == 1
    assert "delegate_task" in lead_tools
    result = await team.run("Prepare the release notes.")

    assert result.output == "Report: Notes written."
    task = board.get("task_1")
    assert task.status is TaskStatus.COMPLETED and task.created_by == "task_manager"


def test_capabilities_given_as_one_string_are_refused(board: BaseTaskBoard) -> None:
    with pytest.raises(TypeError, match="list of names"):
        create_task_board_tools(board, "worker", capabilities="research")  # type: ignore[arg-type]
