"""Hand-offs between agents: filtered history, small shared values and blocking waits."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt

from awa.agents import Agent
from awa.contracts import HandoffEvent, RunEvent
from awa.errors import AgentRunError, HandoffError
from awa.messaging.tools import _render_entry
from awa.team import HandoffInput, Team, TeamLimits, is_team_note
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call


def transfer(target: str, reason: str = "") -> Any:
    return tool_call(f"transfer_to_{target}", {"reason": reason})


def scripted(name: str, *responses: Any, **kwargs: Any) -> Agent:
    return Agent(
        name, description=f"The {name} agent.", model=FakeChatModel(list(responses)), **kwargs
    )


def model_of(agent: Agent) -> FakeChatModel | FunctionChatModel:
    model = agent._model
    assert isinstance(model, FakeChatModel | FunctionChatModel)
    return model


def contents(messages: tuple[BaseMessage, ...] | list[BaseMessage]) -> list[tuple[str, str]]:
    return [(m.type, str(m.content)) for m in messages if m.type != "system"]


def desk(lead: Agent, writer: Agent, **kwargs: Any) -> Team:
    kwargs.setdefault("handoffs", {"lead": ["writer"], "writer": ["lead"]})
    return Team("desk", orchestrator=lead, members=[writer], **kwargs)


# --------------------------------------------------------------------------- basic handoff


async def test_handoff_transfers_control_with_filtered_history() -> None:
    lead = scripted(
        "lead",
        AIMessage(content="Let me get our writer.", tool_calls=[transfer("writer", "needs prose")]),
    )
    writer = scripted("writer", "Here is your poem.")
    events: list[RunEvent] = []
    team = desk(lead, writer, hooks=[events.append])

    result = await team.run("Write a poem.")

    assert (result.agent, result.output) == ("writer", "Here is your poem.")
    assert result.handoff_path == ("lead", "writer")
    assert model_of(lead).call_count == 1
    assert contents(model_of(writer).calls[0].messages) == [
        ("human", "Write a poem."),
        ("human", "[lead]: Let me get our writer."),
        (
            "human",
            "lead transferred the conversation to you (writer). Their reason: needs prose\n"
            "Continue from here and respond to the user directly.",
        ),
    ]
    [handoff] = [e for e in events if isinstance(e, HandoffEvent)]
    assert (handoff.source, handoff.target, handoff.reason) == ("lead", "writer", "needs prose")
    names = {m.name for m in result.new_messages() if isinstance(m, AIMessage)}
    assert names == {"lead", "writer"}


async def test_full_history_is_the_raw_transcript() -> None:
    lead = scripted("lead", AIMessage(content="On it.", tool_calls=[transfer("writer")]))
    writer = scripted("writer", "Poem.")
    team = desk(lead, writer, handoff_history="full")

    await team.run("Write a poem.")

    seen = [
        f"{m.type}:{'note' if is_team_note(m) else m.content}"
        for m in model_of(writer).calls[0].messages
        if m.type != "system"
    ]
    assert seen == ["human:Write a poem.", "ai:On it.", "tool:Transferred to writer.", "human:note"]


async def test_custom_history_function() -> None:
    seen: list[HandoffInput] = []

    def only_reason(handoff: HandoffInput) -> list[BaseMessage]:
        seen.append(handoff)
        return [HumanMessage(content=f"context from {handoff.source}")]

    lead = scripted("lead", transfer("writer", "style"))
    writer = scripted("writer", "Poem.")
    team = desk(lead, writer, handoff_history=only_reason)

    await team.run("Write a poem.")

    first, handoff = seen
    assert (first.source, first.target) == (None, "lead")
    assert (handoff.source, handoff.target, handoff.reason) == ("lead", "writer", "style")
    assert model_of(writer).calls[0].messages[1].content == "context from lead"


# --------------------------------------------------------------------------- loop guards


async def test_ping_pong_transfer_is_rejected() -> None:
    lead = scripted("lead", transfer("writer"))
    writer = scripted("writer", transfer("lead"), "I will handle it.")
    team = desk(lead, writer)

    result = await team.run("go")

    rejection = model_of(writer).calls[1].messages[-1]
    assert isinstance(rejection, ToolMessage)
    assert "lead just transferred this conversation to you" in str(rejection.content)
    assert (result.agent, result.output) == ("writer", "I will handle it.")


async def test_only_the_first_transfer_per_turn_executes() -> None:
    lead = scripted("lead", [transfer("a"), transfer("b")])
    team = Team(
        "desk",
        orchestrator=lead,
        members=[scripted("a", "a answers"), scripted("b", "b answers")],
        handoffs={"lead": ["a", "b"]},
    )

    result = await team.run("go")

    assert result.handoff_path == ("lead", "a")
    tool_results = [str(m.content) for m in result.messages if isinstance(m, ToolMessage)]
    assert tool_results == ["Transferred to a.", "Ignored: only one transfer per turn."]


class HoldFirstTransfer(AgentMiddleware):
    """Runs `transfer_to_a` only after the turn's other transfer finished."""

    def __init__(self) -> None:
        super().__init__()
        self._other_done = asyncio.Event()

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        if request.tool_call["name"] != "transfer_to_a":
            try:
                return await handler(request)
            finally:
                self._other_done.set()
        async with asyncio.timeout(10):
            await self._other_done.wait()
        return await handler(request)


async def test_first_transfer_wins_even_when_a_later_one_starts_first() -> None:
    lead = scripted("lead", [transfer("a"), transfer("b")], middleware=[HoldFirstTransfer()])
    team = Team(
        "desk",
        orchestrator=lead,
        members=[scripted("a", "a answers"), scripted("b", "b answers")],
        handoffs={"lead": ["a", "b"]},
    )

    result = await team.run("go")

    assert result.handoff_path == ("lead", "a")
    assert result.output == "a answers"


async def test_max_handoffs_rejects_then_raises() -> None:
    def team_for(a: Agent) -> Team:
        return Team(
            "desk",
            orchestrator=scripted("lead", transfer("a")),
            members=[a, scripted("b", "b answers")],
            handoffs={"lead": ["a"], "a": ["b"]},
            limits=TeamLimits(max_handoffs=1),
        )

    polite = scripted("a", transfer("b"), "a answers itself")
    result = await team_for(polite).run("go")
    assert (result.agent, result.output) == ("a", "a answers itself")
    assert "handoff limit (max_handoffs=1)" in str(model_of(polite).calls[1].messages[-1].content)

    stubborn = scripted("a", transfer("b"), transfer("b"))
    with pytest.raises(HandoffError, match="again after the handoff limit"):
        await team_for(stubborn).run("go")


async def test_delegated_agents_cannot_transfer_control() -> None:
    writer = scripted("writer", transfer("lead"), "draft")
    lead = scripted("lead", tool_call("delegate_task", {"agent": "writer", "task": "t"}), "done")
    team = Team("desk", orchestrator=lead, members=[writer], handoffs={"writer": ["lead"]})

    result = await team.run("go")

    rejection = model_of(writer).calls[1].messages[-1]
    assert "you are working on a delegated task" in str(rejection.content)
    assert (result.agent, result.handoff_path) == ("lead", ("lead",))


async def test_rejected_transfer_does_not_end_the_agent_loop() -> None:
    lead = scripted("lead", [transfer("a"), transfer("a")])
    team = Team(
        "desk", orchestrator=lead, members=[scripted("a", "a answers")], handoffs={"lead": ["a"]}
    )

    result = await team.run("go")

    assert result.output == "a answers"


# --------------------------------------------------------------------------- multi-turn


async def test_threads_resume_with_the_last_active_agent() -> None:
    def respond(call: ModelCall) -> Any:
        return transfer("writer") if call.index == 0 else "lead again"

    lead = Agent("lead", model=FunctionChatModel(respond))
    writer = scripted("writer", "first poem", "second poem")
    team = desk(lead, writer, checkpointer=InMemorySaver())

    first = await team.run("Write a poem.")
    assert first.thread_id is not None
    second = await team.run("Another one.", thread_id=first.thread_id)

    assert (second.agent, second.output, second.handoff_path) == (
        "writer",
        "second poem",
        ("writer",),
    )
    assert contents(model_of(writer).calls[1].messages) == [
        ("human", "Write a poem."),
        ("ai", "first poem"),
        ("human", "Another one."),
    ]
    assert contents(second.new_messages()) == [("human", "Another one."), ("ai", "second poem")]
    assert contents(second.messages)[0] == ("human", "Write a poem.")


async def test_threads_can_always_return_to_the_orchestrator() -> None:
    def respond(call: ModelCall) -> Any:
        return transfer("writer") if call.index == 0 else "lead again"

    lead = Agent("lead", model=FunctionChatModel(respond))
    team = desk(
        lead, scripted("writer", "poem"), checkpointer=InMemorySaver(), resume_with="orchestrator"
    )

    first = await team.run("Write a poem.")
    second = await team.run("Thanks!", thread_id=first.thread_id)

    assert (second.agent, second.output) == ("lead", "lead again")
    assert contents(model_of(lead).calls[1].messages) == [
        ("human", "Write a poem."),
        ("human", "[writer]: poem"),
        ("human", "Thanks!"),
    ]


async def test_thread_id_requires_a_checkpointer() -> None:
    team = desk(scripted("lead", "x"), scripted("writer", "y"))

    with pytest.raises(ValueError, match="no checkpointer"):
        await team.run("go", thread_id="t1")
    with pytest.raises(ValueError, match="no checkpointer"):
        await team.resume("t1", "yes")


async def test_history_argument_seeds_the_conversation() -> None:
    lead = scripted("lead", "answer")
    team = desk(lead, scripted("writer", "y"))

    result = await team.run("And now?", history=[{"role": "user", "content": "Earlier question"}])

    assert contents(model_of(lead).calls[0].messages) == [
        ("human", "Earlier question"),
        ("human", "And now?"),
    ]
    assert result.new_messages_start == 1


# --------------------------------------------------------------------------- human in the loop


def ask(question: str) -> str:
    """Ask a human."""
    return f"human said {interrupt(question)}"


async def test_interrupt_of_the_active_agent_pauses_and_resumes() -> None:
    lead = scripted("lead", transfer("writer"))
    writer = scripted(
        "writer", tool_call("ask", {"question": "Publish?"}), "published", tools=[ask]
    )
    team = desk(lead, writer, checkpointer=InMemorySaver())

    paused = await team.run("Write and publish.")

    assert paused.is_interrupted and paused.output is None
    assert paused.agent == "writer" and paused.interrupts[0]["value"] == "Publish?"
    assert paused.handoff_path == ("lead", "writer")
    assert paused.thread_id is not None

    resumed = await team.resume(paused.thread_id, "yes")

    assert (resumed.agent, resumed.output) == ("writer", "published")
    assert model_of(lead).call_count == 1 and model_of(writer).call_count == 2
    assert str(model_of(writer).calls[1].messages[-1].content) == "human said yes"
    with pytest.raises(ValueError, match="no pending interrupt"):
        await team.resume(paused.thread_id, "again")


async def test_interrupt_inside_a_delegated_member_pauses_the_team() -> None:
    checker = scripted(
        "checker", tool_call("ask", {"question": "Approve?"}), "approved", tools=[ask]
    )
    lead = scripted(
        "lead", tool_call("delegate_task", {"agent": "checker", "task": "check"}), "done"
    )
    team = Team("desk", orchestrator=lead, members=[checker], checkpointer=InMemorySaver())

    paused = await team.run("go")
    assert paused.is_interrupted and paused.interrupts[0]["value"] == "Approve?"
    assert paused.thread_id is not None

    resumed = await team.resume(paused.thread_id, "ok")

    assert resumed.output == "done"
    assert model_of(checker).call_count == 2 and model_of(lead).call_count == 2
    delegated = [str(m.content) for m in resumed.messages if isinstance(m, ToolMessage)]
    assert any("approved" in content for content in delegated)


async def test_interrupt_without_a_checkpointer_is_a_clear_error() -> None:
    lead = scripted("lead", tool_call("ask", {"question": "?"}), tools=[ask])
    team = Team("desk", orchestrator=lead, members=[scripted("writer", "x")])

    with pytest.raises(AgentRunError, match="checkpointer"):
        await team.run("go")


async def test_interrupt_inside_a_nested_team_pauses_the_outer_team() -> None:
    checker = scripted(
        "checker", tool_call("ask", {"question": "Approve?"}), "approved", tools=[ask]
    )
    inner = Team(
        "review",
        orchestrator=scripted(
            "reviewer",
            tool_call("delegate_task", {"agent": "checker", "task": "check"}),
            "reviewed",
        ),
        members=[checker],
    )
    lead = scripted("lead", tool_call("delegate_task", {"agent": "review", "task": "go"}), "done")
    team = Team("desk", orchestrator=lead, members=[inner], checkpointer=InMemorySaver())

    paused = await team.run("go")
    assert paused.is_interrupted and paused.thread_id is not None

    resumed = await team.resume(paused.thread_id, "ok")

    assert resumed.output == "done"
    assert model_of(checker).call_count == 2
    assert str(model_of(checker).calls[1].messages[-1].content) == "human said ok"


class _Entry:
    def __init__(self, value: object) -> None:
        self.value = value
        self.version = 1
        self.author = "collector"
        self.updated_at = datetime(2026, 10, 8, tzinfo=UTC)


def _body(text: str) -> dict[str, object]:
    return json.loads(text[text.index(">") + 1 : text.rindex("<")])


def test_a_long_shared_value_comes_in_parts_that_add_up_to_the_value() -> None:
    value = {"items": [{"id": i, "subject": "x" * 50} for i in range(400)]}
    whole = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    parts, offset = [], 0
    while True:
        body = _body(_render_entry(_Entry(value), "results/a", offset=offset, max_chars=8_000))  # type: ignore[arg-type]
        assert len(str(body["value"])) <= 8_000 and body["value_chars"] == len(whole)
        parts.append(str(body["value"]))
        if "next_offset" not in body:
            break
        offset = int(body["next_offset"])  # type: ignore[call-overload]
    assert "".join(parts) == whole


def test_a_short_shared_value_is_returned_whole_and_compact() -> None:
    text = _render_entry(_Entry({"a": 1}), "k", max_chars=8_000)  # type: ignore[arg-type]
    assert _body(text)["value"] == {"a": 1} and "value_chars" not in _body(text)
    assert '\n  "' not in text  # no indentation


def test_waiting_for_background_tasks_has_no_short_default_timeout() -> None:
    # A 60-second default made models call wait_for_delegations again and again.
    from awa.team._delegation import _WaitArgs

    assert _WaitArgs().timeout is None
