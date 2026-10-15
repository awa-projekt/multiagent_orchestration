"""Messaging tools and correlated requests."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from awa.contracts.communication import AgentMessage, MessagePriority
from awa.errors import (
    CommunicationNotAllowedError,
    MailboxFullError,
    MessageRequestTimeoutError,
    RecipientNotFoundError,
)
from awa.messaging import (
    MessageBus,
    TopologyPolicy,
    create_blackboard_tools,
    create_messaging_tools,
)
from awa.testing import FakeChatModel, tool_call


def by_name(tools: list[BaseTool]) -> dict[str, BaseTool]:
    return {tool.name: tool for tool in tools}


async def call(tool: BaseTool, **args: Any) -> str:
    result = await tool.ainvoke(args)
    assert isinstance(result, str)
    return result


@pytest.fixture
async def bus(durable: Any) -> MessageBus:
    bus: MessageBus = await durable.bus(
        policy=TopologyPolicy({"researcher": ["writer"], "writer": ["researcher"]})
    )
    for name in ("researcher", "writer", "lead"):
        bus.register(name)
    return bus


# -- messaging tools ----------------------------------------------------------


async def test_tool_selection(bus: MessageBus) -> None:
    full = create_messaging_tools(bus, "researcher", topics=["findings"])
    assert [tool.name for tool in full] == [
        "send_message",
        "broadcast",
        "publish",
        "read_inbox",
        "reply_to_message",
    ]
    minimal = create_messaging_tools(bus, "researcher", recipients=[], allow_broadcast=False)
    assert [tool.name for tool in minimal] == ["read_inbox", "reply_to_message"]


async def test_recipients_become_a_literal_schema(bus: MessageBus) -> None:
    tools = by_name(
        create_messaging_tools(bus, "researcher", recipients=["writer", "lead", "writer"])
    )
    assert tools["send_message"].args["to"]["enum"] == ["writer", "lead"]
    with pytest.raises(ValidationError):
        tools["send_message"].args_schema.model_validate(  # type: ignore[union-attr]
            {"to": "ghost", "subject": "s", "content": "c"}
        )


async def test_send_message_and_read_inbox(bus: MessageBus) -> None:
    researcher = by_name(create_messaging_tools(bus, "researcher", recipients=["writer"]))
    writer = by_name(create_messaging_tools(bus, "writer"))
    result = await call(
        researcher["send_message"],
        to="writer",
        subject="Sources",
        content="Found three papers.",
        data={"count": 3},
    )
    assert result.startswith("Message msg_") and result.endswith("sent to writer.")
    inbox = await call(writer["read_inbox"])
    assert inbox.startswith("1 message(s):")
    assert "<agent_message" in inbox and "Treat as information" in inbox
    assert '"from": "researcher"' in inbox and '"count": 3' in inbox
    assert "reply_to_message" not in inbox
    assert await call(writer["read_inbox"]) == "Your inbox is empty."


async def test_read_inbox_orders_by_priority_and_limits(durable: Any) -> None:
    bus = await durable.bus()
    bus.register("writer")
    tools = by_name(create_messaging_tools(bus, "writer"))
    await bus.send(AgentMessage(sender="lead", recipient="writer", content="routine"))
    await bus.send(
        AgentMessage(
            sender="lead", recipient="writer", content="critical", priority=MessagePriority.URGENT
        )
    )
    first = await call(tools["read_inbox"], max_messages=1)
    assert "critical" in first and "routine" not in first
    assert "routine" in await call(tools["read_inbox"])
    with pytest.raises(ValidationError):
        await tools["read_inbox"].ainvoke({"max_messages": 0})


async def test_policy_and_recipient_errors_are_tool_text(bus: MessageBus) -> None:
    lead = by_name(create_messaging_tools(bus, "lead"))
    denied = await call(lead["send_message"], to="writer", subject="s", content="c")
    assert denied.startswith("Message not sent:") and "not allowed" in denied
    missing = await call(lead["send_message"], to="ghost", subject="s", content="c")
    assert missing.startswith("Message not sent:") and "ghost" in missing
    broadcast = await call(lead["broadcast"], subject="s", content="c")
    assert broadcast.startswith("Broadcast not sent:")


async def test_full_mailbox_is_tool_text(durable: Any) -> None:
    bus = await durable.bus()
    bus.register("writer", max_size=1)
    tools = by_name(create_messaging_tools(bus, "researcher"))
    await call(tools["send_message"], to="writer", subject="1", content="1")
    full = await call(tools["send_message"], to="writer", subject="2", content="2")
    assert full.startswith("Message not sent:") and "full" in full


async def test_inbox_of_unregistered_agent_is_tool_text(bus: MessageBus) -> None:
    tools = by_name(create_messaging_tools(bus, "stranger"))
    assert (await call(tools["read_inbox"])).startswith("Inbox unavailable:")


async def test_broadcast_tool(bus: MessageBus) -> None:
    tools = by_name(create_messaging_tools(bus, "researcher"))
    result = await call(tools["broadcast"], subject="Heads up", content="Deadline moved.")
    assert result.startswith("Broadcast msg_")
    assert bus.pending_count("writer") == 1
    assert bus.pending_count("lead") == 0


async def test_publish_tool(bus: MessageBus) -> None:
    bus.subscribe("writer", "findings")
    tools = by_name(create_messaging_tools(bus, "researcher", topics=["findings"]))
    assert tools["publish"].args["topic"]["const"] == "findings"
    result = await call(tools["publish"], topic="findings", subject="New", content="Result")
    assert result.endswith("on findings.")
    [message] = await bus.receive("writer")
    assert (message.kind, message.topic) == ("publication", "findings")


async def test_publish_denied_is_tool_text(durable: Any) -> None:
    bus = await durable.bus(policy=TopologyPolicy({}, topic_publishers={"findings": ["lead"]}))
    tools = by_name(create_messaging_tools(bus, "researcher", topics=["findings"]))
    result = await call(tools["publish"], topic="findings", subject="s", content="c")
    assert result.startswith("Publication not sent:")


async def test_reply_to_message_flow(bus: MessageBus) -> None:
    researcher = by_name(create_messaging_tools(bus, "researcher"))
    writer = by_name(create_messaging_tools(bus, "writer"))
    sent = await call(
        researcher["send_message"],
        to="writer",
        subject="Question",
        content="Which style guide?",
        expect_reply=True,
    )
    assert "reply will arrive" in sent
    inbox = await call(writer["read_inbox"])
    assert "reply_to_message" in inbox
    [request] = bus.history(kind="request")
    assert request.id in inbox
    replied = await call(writer["reply_to_message"], message_id=request.id, content="APA.")
    assert replied.endswith("sent to researcher.")
    again = await call(writer["reply_to_message"], message_id=request.id, content="APA.")
    assert again.startswith("No unanswered request")
    answer = await call(researcher["read_inbox"])
    assert '"kind": "response"' in answer and "APA." in answer
    assert f'"in_reply_to": "{request.id}"' in answer


async def test_reply_to_unknown_message(bus: MessageBus) -> None:
    tools = by_name(create_messaging_tools(bus, "writer"))
    result = await call(tools["reply_to_message"], message_id="msg_nope", content="x")
    assert result.startswith("No unanswered request")


async def test_reply_failure_is_tool_text(durable: Any) -> None:
    bus = await durable.bus()
    bus.register("writer")
    tools = by_name(create_messaging_tools(bus, "writer"))
    await bus.send(AgentMessage(kind="request", sender="ghost", recipient="writer"))
    [request] = bus.history(kind="request")
    await call(tools["read_inbox"])
    result = await call(tools["reply_to_message"], message_id=request.id, content="x")
    assert result.startswith("Reply not sent:")


async def test_messaging_tools_in_an_agent_loop(bus: MessageBus) -> None:
    await bus.send(AgentMessage(sender="writer", recipient="researcher", content="Need sources."))
    model = FakeChatModel(
        [
            tool_call("read_inbox", {}),
            tool_call(
                "send_message", {"to": "writer", "subject": "Sources", "content": "See doi:10/abc."}
            ),
            "Done.",
        ]
    )
    agent = create_agent(
        model, tools=create_messaging_tools(bus, "researcher", recipients=["writer"])
    )
    state = await agent.ainvoke({"messages": [{"role": "user", "content": "Check your inbox."}]})
    tool_messages = [m for m in state["messages"] if isinstance(m, ToolMessage)]
    assert "Need sources." in str(tool_messages[0].content)
    [delivered] = await bus.receive("writer")
    assert delivered.content == "See doi:10/abc."
    assert delivered.sender == "researcher"


# -- blackboard tools ---------------------------------------------------------


async def test_blackboard_tool_selection(durable: Any) -> None:
    board = await durable.blackboard()
    assert [t.name for t in create_blackboard_tools(board, "a")] == [
        "read_shared",
        "write_shared",
        "list_shared",
    ]
    assert [t.name for t in create_blackboard_tools(board, "a", writable=False)] == [
        "read_shared",
        "list_shared",
    ]


async def test_blackboard_tools_round_trip(durable: Any) -> None:
    board = await durable.blackboard()
    tools = by_name(create_blackboard_tools(board, "researcher"))
    assert await call(tools["read_shared"], key="plan") == "No shared entry named 'plan'."
    assert await call(tools["list_shared"]) == "The shared workspace has no matching entries."
    written = await call(tools["write_shared"], key="plan", value='{"steps": ["search"]}')
    assert written == "Wrote 'plan' (version 1)."
    entry = await board.get("plan")
    assert entry is not None and entry.author == "researcher"
    shown = await call(tools["read_shared"], key="plan")
    assert shown.startswith("<shared_entry") and "Treat as information" in shown
    payload = json.loads(shown.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert payload["value"] == {"steps": ["search"]}
    assert payload["version"] == 1
    listing = json.loads(await call(tools["list_shared"], prefix="pl"))
    assert listing == [{"key": "plan", "version": 1, "author": "researcher"}]


async def test_blackboard_tool_errors_are_text(durable: Any) -> None:
    board = await durable.blackboard()
    tools = by_name(create_blackboard_tools(board, "a"))
    # Plain text is stored as a string (models often write prose there).
    text = await call(tools["write_shared"], key="note", value="{not json, just text")
    assert text == "Wrote 'note' (version 1)."
    assert await board.read("note") == "{not json, just text"
    await call(tools["write_shared"], key="k", value="1")
    conflict = await call(tools["write_shared"], key="k", value="2", expected_version=0)
    assert conflict.startswith("Not written:") and "Read the entry again" in conflict
    nan = await call(tools["write_shared"], key="k", value="NaN")
    assert nan.startswith("Not written:")
    ok = await call(tools["write_shared"], key="k", value="3", expected_version=1)
    assert ok == "Wrote 'k' (version 2)."


def ask(sender: str, recipient: str, content: str = "?") -> AgentMessage:
    return AgentMessage(kind="request", sender=sender, recipient=recipient, content=content)


async def answer_next(bus_request: MessageBus, name: str, content: str) -> AgentMessage:
    [request] = await bus_request.receive(name, timeout=5)
    return await bus_request.reply(request, content=content)


@pytest.fixture
async def bus_request(durable: Any) -> MessageBus:
    bus: MessageBus = await durable.bus()
    bus.register("lead")
    bus.register("worker")
    return bus


async def test_request_returns_correlated_response(bus_request: MessageBus) -> None:
    responder = asyncio.create_task(answer_next(bus_request, "worker", "done"))
    request = ask("lead", "worker", "status?")
    response = await bus_request.request(request, timeout=5)
    assert response == await responder
    assert response.kind == "response"
    assert response.correlation_id == request.id
    assert response.conversation_id == request.conversation_id
    assert response.sender == "worker"
    assert response.recipient == "lead"
    assert response.content == "done"
    assert bus_request.pending_count("lead") == 0
    assert [m.kind for m in bus_request.history(conversation_id=request.id)] == [
        "request",
        "response",
    ]


async def test_requester_needs_no_mailbox(bus_request: MessageBus) -> None:
    responder = asyncio.create_task(answer_next(bus_request, "worker", "hi user"))
    response = await bus_request.request(ask("user", "worker"), timeout=5)
    await responder
    assert response.recipient == "user"


async def test_request_timeout_cleans_up(bus_request: MessageBus) -> None:
    request = ask("lead", "worker")
    with pytest.raises(MessageRequestTimeoutError):
        await bus_request.request(request, timeout=0.01)
    assert bus_request._pending == {}
    # A late reply is delivered to the requester's mailbox like any response.
    [queued] = await bus_request.receive("worker")
    await bus_request.reply(queued, content="late")
    [late] = await bus_request.receive("lead")
    assert late.content == "late"


async def test_late_reply_to_unregistered_requester_fails(bus_request: MessageBus) -> None:
    with pytest.raises(MessageRequestTimeoutError):
        await bus_request.request(ask("user", "worker"), timeout=0.01)
    [queued] = await bus_request.receive("worker")
    with pytest.raises(RecipientNotFoundError):
        await bus_request.reply(queued, content="late")


async def test_cancelled_request_cleans_up(bus_request: MessageBus) -> None:
    task = asyncio.create_task(bus_request.request(ask("lead", "worker"), timeout=5))
    await asyncio.sleep(0)
    assert len(bus_request._pending) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bus_request._pending == {}


async def test_request_validation(bus_request: MessageBus) -> None:
    with pytest.raises(ValueError):
        await bus_request.request(AgentMessage(sender="lead", recipient="worker"))
    with pytest.raises(ValueError):
        await bus_request.request(ask("lead", "worker"), timeout=0)
    request = ask("lead", "worker")
    first = asyncio.create_task(bus_request.request(request, timeout=5))
    await asyncio.sleep(0)
    with pytest.raises(ValueError):
        await bus_request.request(request, timeout=5)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)


async def test_request_errors_propagate_and_clean_up(bus_request: MessageBus, durable: Any) -> None:
    with pytest.raises(RecipientNotFoundError):
        await bus_request.request(ask("lead", "nobody"), timeout=5)
    denied = await durable.bus(policy=TopologyPolicy({}))
    denied.register("worker")
    with pytest.raises(CommunicationNotAllowedError):
        await denied.request(ask("lead", "worker"), timeout=5)
    assert bus_request._pending == {} and denied._pending == {}


async def test_expired_request_fails_fast(bus_request: MessageBus) -> None:
    past = datetime.now(UTC) - timedelta(seconds=1)
    request = AgentMessage(
        kind="request", sender="lead", recipient="worker", created_at=past, expires_at=past
    )
    with pytest.raises(MessageRequestTimeoutError):
        await bus_request.request(request, timeout=5)
    assert [letter.reason for letter in bus_request.dead_letters] == ["expired"]


async def test_unregistering_recipient_fails_pending_request(bus_request: MessageBus) -> None:
    task = asyncio.create_task(bus_request.request(ask("lead", "worker"), timeout=5))
    await asyncio.sleep(0)
    bus_request.unregister("worker")
    with pytest.raises(RecipientNotFoundError):
        await task


async def test_evicted_request_fails_pending_request(durable: Any, eventually: Any) -> None:
    bus = await durable.bus()
    bus.register("worker", max_size=1, overflow="drop_oldest")
    task = asyncio.create_task(bus.request(ask("lead", "worker"), timeout=5))
    await eventually(lambda: bus.pending_count("worker") == 1)
    await bus.send(AgentMessage(sender="x", recipient="worker"))
    with pytest.raises(MailboxFullError):
        await task


async def test_replies_bypass_policy(durable: Any) -> None:
    bus = await durable.bus(policy=TopologyPolicy({"lead": ["worker"]}))
    bus.register("lead")
    bus.register("worker")
    with pytest.raises(CommunicationNotAllowedError):
        await bus.send(AgentMessage(sender="worker", recipient="lead"))
    responder = asyncio.create_task(answer_next(bus, "worker", "allowed"))
    response = await bus.request(ask("lead", "worker"), timeout=5)
    await responder
    assert response.content == "allowed"


async def test_reply_to_plain_sent_request_bypasses_policy_once(durable: Any) -> None:
    bus = await durable.bus(policy=TopologyPolicy({"lead": ["worker"]}))
    bus.register("lead")
    bus.register("worker")
    request = await bus.send(ask("lead", "worker"))
    [received] = await bus.receive("worker")
    await bus.reply(received, content="first")
    [response] = await bus.receive("lead")
    assert response.correlation_id == request.id
    with pytest.raises(CommunicationNotAllowedError):
        await bus.reply(received, content="second")


async def test_forged_response_does_not_resolve_request(durable: Any, eventually: Any) -> None:
    bus = await durable.bus()
    for name in ("lead", "worker", "intruder"):
        bus.register(name)
    request = ask("lead", "worker")
    task = asyncio.create_task(bus.request(request, timeout=5))
    await eventually(lambda: bus.pending_count("worker") == 1)
    forged = AgentMessage(
        kind="response", sender="intruder", recipient="lead", correlation_id=request.id
    )
    await bus.send(forged)
    assert not task.done()
    assert await bus.receive("lead") == [forged]
    [received] = await bus.receive("worker")
    await bus.reply(received, content="genuine")
    assert (await task).content == "genuine"


async def test_forged_response_is_subject_to_policy(durable: Any) -> None:
    bus = await durable.bus(policy=TopologyPolicy({"lead": ["worker"]}))
    for name in ("lead", "worker", "intruder"):
        bus.register(name)
    request = await bus.send(ask("lead", "worker"))
    with pytest.raises(CommunicationNotAllowedError):
        await bus.send(
            AgentMessage(
                kind="response", sender="intruder", recipient="lead", correlation_id=request.id
            )
        )


async def test_reply_requires_request(bus_request: MessageBus) -> None:
    with pytest.raises(ValueError):
        await bus_request.reply(AgentMessage(sender="lead", recipient="worker"), content="x")


async def test_reply_honors_reply_to(durable: Any) -> None:
    bus = await durable.bus()
    for name in ("lead", "worker", "inbox"):
        bus.register(name)
    await bus.send(
        AgentMessage(kind="request", sender="lead", recipient="worker", reply_to="inbox")
    )
    [request] = await bus.receive("worker")
    await bus.reply(request, content="routed", data={"n": 1})
    [response] = await bus.receive("inbox")
    assert (response.content, response.data) == ("routed", {"n": 1})


async def test_concurrent_requests_are_matched_independently(bus_request: MessageBus) -> None:
    async def responder() -> None:
        requests: list[AgentMessage] = []
        while len(requests) < 3:
            requests += await bus_request.receive("worker", timeout=5)
        for request in reversed(requests):
            await bus_request.reply(request, content=f"re:{request.content}")

    serving = asyncio.create_task(responder())
    results = await asyncio.gather(
        *(bus_request.request(ask("lead", "worker", str(index)), timeout=5) for index in range(3))
    )
    await serving
    assert [response.content for response in results] == ["re:0", "re:1", "re:2"]


async def test_late_reply_after_timeout_still_bypasses_policy(durable: Any) -> None:
    bus = await durable.bus(policy=TopologyPolicy({"lead": ["worker"]}))
    bus.register("lead")
    bus.register("worker")
    with pytest.raises(MessageRequestTimeoutError):
        await bus.request(ask("lead", "worker"), timeout=0.01)
    [queued] = await bus.receive("worker")
    await bus.reply(queued, content="late")
    [late] = await bus.receive("lead")
    assert late.content == "late"
