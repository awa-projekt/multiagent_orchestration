"""Human-in-the-loop pauses of an `AgentNetwork`: parked tasks, durable pause records, resume."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from _helpers.polling import until
from awa.agents import Agent
from awa.contracts import DelegationResultEvent
from awa.errors import AgentRunError, ConfigurationError, NoPendingInterruptError
from awa.tasks import TaskBoard, TaskStatus
from awa.team import AgentNetwork
from awa.testing import FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper


def results_in(text: str) -> int:
    """Results a closing note gives the lead: new handbacks plus those listed as received."""

    received = text.split("Results you already received: ", 1)
    listed = received[1].split(". ", 1)[0].count(" (") if len(received) == 2 else 0
    return text.count("<handback") + listed


AWAITING = "awa.awaiting_approval"
SENT: list[str] = []


@tool
def send_offer(candidate: str) -> str:
    """Send the offer letter to a candidate."""

    SENT.append(candidate)
    return f"offer sent to {candidate}"


@tool
def check_references(candidate: str) -> str:
    """Check the references of a candidate."""

    return f"references of {candidate} are fine"


@pytest.fixture(autouse=True)
def _clear() -> None:
    SENT.clear()


def lead(*plan: Any, tools: list[Any] | None = None) -> Agent:
    """Plans with one scripted response per model call, then answers from the handbacks."""

    def respond(call: ModelCall) -> Any:
        # Counted from the conversation, so a lead resumed in a new process continues.
        turn = sum(isinstance(m, AIMessage) for m in call.messages)
        if turn < len(plan):
            return plan[turn]
        last = call.last_message
        if isinstance(last, ToolMessage):
            return "Planned."
        text = str(last.content)
        if "Write the final answer" in text or "Time is up" in text:
            return f"FINAL: {results_in(text)} results"
        return "Noted."

    return Agent(
        "lead",
        description="Plans the hiring steps.",
        model=FunctionChatModel(respond),
        tools=tools or [],
    )


def worker(name: str, capability: str, tool_name: str, *, gated: bool) -> Agent:
    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"{name}: {call.last_message.content}"
        return tool_call(tool_name, {"candidate": "Ada"})

    tool_object = send_offer if tool_name == "send_offer" else check_references
    return Agent(
        name,
        description=f"The {name}.",
        capabilities=[capability],
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(tool_object) if gated else tool_object],
    )


def create(title: str, capability: str) -> Any:
    return tool_call("create_task", {"title": title, "required_capabilities": [capability]})


def calls(agent: Agent) -> int:
    model = agent._model
    assert isinstance(model, FunctionChatModel)
    return model.call_count


def hiring(**kwargs: Any) -> tuple[AgentNetwork, Agent, Agent]:
    recruiter = worker("recruiter", "offers", "send_offer", gated=True)
    checker = worker("checker", "references", "check_references", gated=False)
    network = AgentNetwork(
        "hiring",
        lead=lead(create("Check references", "references"), create("Send offer", "offers")),
        teammates=[recruiter, checker],
        checkpointer=InMemorySaver(),
        **kwargs,
    )
    return network, recruiter, checker


async def test_a_teammate_approval_parks_its_task_while_others_finish() -> None:
    network, recruiter, checker = hiring()

    paused = await network.run("Hire Ada.")

    assert paused.is_interrupted and paused.output is None
    assert paused.metadata["stop_reason"] == "interrupted"
    [pending] = paused.interrupts
    assert (pending["agent"], pending["task_id"]) == ("recruiter", "task_2")
    assert pending["value"]["tool"] == "send_offer" and SENT == []
    board = {task.title: task for task in network.task_board.list()}
    assert board["Check references"].status is TaskStatus.COMPLETED
    offer = board["Send offer"]
    assert offer.status is TaskStatus.IN_PROGRESS and offer.data["awa.awaiting_approval"]
    checker_calls = calls(checker)

    assert paused.thread_id is not None
    done = await network.resume(paused.thread_id, {"action": "approve"})

    assert SENT == ["Ada"] and not done.is_interrupted
    assert done.output == "FINAL: 2 results"
    assert done.metadata["stop_reason"] == "completed"
    assert done.metadata["tasks"]["completed"] == 2
    assert network.task_board.get("task_2").data["awa.awaiting_approval"] is False
    assert calls(checker) == checker_calls  # finished work is not repeated
    assert calls(recruiter) == 2
    with pytest.raises(NoPendingInterruptError):
        await network.resume(paused.thread_id, {"action": "approve"})


async def test_a_rejection_completes_the_task_with_the_rejection() -> None:
    network, _, _ = hiring()

    paused = await network.run("Hire Ada.")
    assert paused.thread_id is not None
    done = await network.resume(paused.thread_id, {"action": "reject", "note": "budget"})

    assert SENT == [] and done.output == "FINAL: 2 results"
    assert "budget" in (network.task_board.get("task_2").result or "")


async def test_several_parked_tasks_are_resumed_one_decision_at_a_time() -> None:
    first = worker("first", "offers", "send_offer", gated=True)
    second = worker("second", "offers2", "send_offer", gated=True)
    network = AgentNetwork(
        "hiring",
        lead=lead(create("Offer one", "offers"), create("Offer two", "offers2")),
        teammates=[first, second],
        checkpointer=InMemorySaver(),
    )

    paused = await network.run("Hire two.")

    assert [(p["agent"], p["task_id"]) for p in paused.interrupts] == [
        ("first", "task_1"),
        ("second", "task_2"),
    ]
    assert paused.thread_id is not None
    with pytest.raises(ConfigurationError, match="mapping of interrupt id"):
        await network.resume(paused.thread_id, {"action": "approve"})

    again = await network.resume(
        paused.thread_id, {paused.interrupts[1]["id"]: {"action": "approve"}}
    )

    assert again.is_interrupted and [p["agent"] for p in again.interrupts] == ["first"]
    assert SENT == ["Ada"]
    done = await network.resume(paused.thread_id, {"action": "approve"})
    assert done.output == "FINAL: 2 results" and SENT == ["Ada", "Ada"]


async def test_a_lead_approval_pauses_the_network_and_resumes_the_lead() -> None:
    @tool
    def publish_plan(summary: str) -> str:
        """Publish the hiring plan."""

        SENT.append(summary)
        return "plan published"

    boss = lead(
        tool_call("publish_plan", {"summary": "plan"}),
        create("Check references", "references"),
        tools=[create_tool_approval_wrapper(publish_plan)],
    )
    checker = worker("checker", "references", "check_references", gated=False)
    network = AgentNetwork("hiring", lead=boss, teammates=[checker], checkpointer=InMemorySaver())

    paused = await network.run("Hire Ada.")

    [pending] = paused.interrupts
    assert (pending["agent"], pending["task_id"]) == ("lead", None)
    assert calls(checker) == 0 and network.task_board.list() == []
    assert paused.thread_id is not None
    done = await network.resume(paused.thread_id, {"action": "approve"})

    assert SENT == ["plan"] and done.output == "FINAL: 1 results"
    assert done.metadata["lead_turns"] >= 2


async def test_the_pause_record_survives_a_new_network_object() -> None:
    saver = InMemorySaver()
    board = TaskBoard()

    def build() -> AgentNetwork:
        return AgentNetwork(
            "hiring",
            lead=lead(create("Check references", "references"), create("Send offer", "offers")),
            teammates=[
                worker("recruiter", "offers", "send_offer", gated=True),
                worker("checker", "references", "check_references", gated=False),
            ],
            task_board=board,
            checkpointer=saver,
        )

    paused = await build().run("Hire Ada.", thread_id="hire-ada")
    assert paused.thread_id == "hire-ada"

    done = await build().resume("hire-ada", {"action": "approve"})

    assert SENT == ["Ada"] and done.output == "FINAL: 2 results"


async def test_the_paused_thread_shows_the_pending_interrupts_with_their_ids() -> None:
    network, _, _ = hiring()

    paused = await network.run("Hire Ada.")

    assert network.checkpointer is not None and paused.thread_id is not None
    saved = await network.checkpointer.aget_tuple(
        {"configurable": {"thread_id": paused.thread_id, "checkpoint_ns": ""}}
    )
    assert saved is not None
    ids = [
        interrupt.id
        for write in saved.pending_writes or ()
        if write[1] == "__interrupt__"
        for interrupt in write[2]
    ]
    assert ids == [paused.interrupts[0]["id"]]


async def test_without_a_checkpointer_a_teammate_approval_fails_its_task() -> None:
    recruiter = worker("recruiter", "offers", "send_offer", gated=True)
    network = AgentNetwork(
        "hiring", lead=lead(create("Send offer", "offers")), teammates=[recruiter]
    )

    result = await network.run("Hire Ada.")

    assert not result.is_interrupted and result.metadata["tasks"]["failed"] == 1
    assert "checkpointer" in (network.task_board.get("task_1").result or "")
    with pytest.raises(ConfigurationError, match="checkpointer"):
        await network.resume("t", None)


# --------------------------------------------------------------------------- failure modes


def gated_send_offer_agent(name: str, **kwargs: Any) -> Agent:
    """A member whose only tool needs approval and that keeps its own checkpointer."""

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"{name}: {call.last_message.content}"
        return tool_call("send_offer", {"candidate": "Ada"})

    return Agent(
        name,
        description=f"The {name}.",
        capabilities=["offers"],
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(send_offer)],
        checkpointer=InMemorySaver(),
        **kwargs,
    )


async def test_a_teammate_that_pauses_on_its_own_thread_fails_its_task() -> None:
    network = AgentNetwork(
        "hiring",
        lead=lead(create("Send offer", "offers")),
        teammates=[gated_send_offer_agent("recruiter")],
    )

    result = await network.run("Hire Ada.")

    assert not result.is_interrupted and SENT == []
    assert "cannot resume" in (network.task_board.get("task_1").result or "")


async def test_a_lead_that_pauses_on_its_own_thread_fails_the_run() -> None:
    @tool
    def publish_plan(summary: str) -> str:
        """Publish the hiring plan."""

        return "published"

    boss = Agent(
        "lead",
        description="Plans.",
        model=FunctionChatModel(lambda call: tool_call("publish_plan", {"summary": "plan"})),
        tools=[create_tool_approval_wrapper(publish_plan)],
        checkpointer=InMemorySaver(),
    )
    network = AgentNetwork(
        "hiring",
        lead=boss,
        teammates=[worker("checker", "references", "check_references", gated=False)],
    )

    with pytest.raises(AgentRunError, match="cannot resume"):
        await network.run("Hire Ada.")


async def test_resume_without_a_pause_record_is_refused() -> None:
    network, _, _ = hiring()

    with pytest.raises(NoPendingInterruptError, match="no pending interrupt"):
        await network.resume("never-ran", {"action": "approve"})
    done = await network.run("Hire Ada.", thread_id="t1")
    assert done.is_interrupted
    await network.resume("t1", {"action": "approve"})
    with pytest.raises(NoPendingInterruptError):
        await network.resume("t1", {"action": "approve"})


async def test_a_task_the_lead_cancels_while_parked_is_withdrawn_not_dropped() -> None:
    events: list[Any] = []
    holder: dict[str, AgentNetwork] = {}

    @tool
    async def check_references(candidate: str) -> str:
        """Check the references of a candidate."""

        # The recruiter's task is parked once the board says it waits for a decision.
        await until(lambda: holder["net"].task_board.get("task_1").data.get(AWAITING) is True)
        return "fine"

    def on_update(call: ModelCall) -> Any:
        if "Updates from your network" in str(call.last_message.content):
            return tool_call("cancel_task", {"task_id": "task_1", "reason": "not needed"})
        return "Noted."

    def respond(call: ModelCall) -> Any:
        turn = sum(isinstance(m, AIMessage) for m in call.messages)
        plan = [create("Send offer", "offers"), create("Check references", "references")]
        if turn < len(plan):
            return plan[turn]
        if isinstance(call.last_message, ToolMessage):
            return "Planned."
        return on_update(call)

    checker = Agent(
        "checker",
        description="The checker.",
        capabilities=["references"],
        model=FunctionChatModel(
            lambda call: (
                f"done: {call.last_message.content}"
                if isinstance(call.last_message, ToolMessage)
                else tool_call("check_references", {"candidate": "Ada"})
            )
        ),
        tools=[check_references],
    )
    network = AgentNetwork(
        "hiring",
        lead=Agent("lead", description="Plans.", model=FunctionChatModel(respond)),
        teammates=[worker("recruiter", "offers", "send_offer", gated=True), checker],
        checkpointer=InMemorySaver(),
        hooks=[events.append],
    )
    holder["net"] = network

    result = await network.run("Hire Ada.")

    assert not result.is_interrupted and SENT == []
    assert network.task_board.get("task_1").status is TaskStatus.CANCELLED
    withdrawn = [
        e
        for e in events
        if isinstance(e, DelegationResultEvent) and "withdrawn" in e.content_preview
    ]
    assert [(e.target, e.status) for e in withdrawn] == [("recruiter", "error")]
