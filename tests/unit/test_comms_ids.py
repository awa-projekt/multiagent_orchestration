"""Run ids across agent boundaries: live state kept out of
the metadata, and one session id, call id and call chain per run tree."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

from langchain_core.messages import ToolMessage
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from awa.agents import Agent
from awa.contracts import AgentStartedEvent, DelegationEvent, RunEvent
from awa.observability import OpenTelemetrySubscriber
from awa.runtime import current_run_context
from awa.runtime.context import TEAM_NAME_KEY
from awa.team import Team
from awa.team._run import TeamRun
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call


@pytest.fixture
def otel() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    provider.shutdown()


def named(spans: tuple[ReadableSpan, ...], name: str) -> ReadableSpan:
    [span] = [s for s in spans if s.name == name]
    return span


def started(events: list[RunEvent], agent: str) -> AgentStartedEvent:
    [event] = [e for e in events if isinstance(e, AgentStartedEvent) and e.agent == agent]
    return event


# ------------------------------------------------------------------ metadata


async def test_internal_state_is_not_in_metadata() -> None:
    found: list[Any] = []
    team: Team | None = None

    def peek() -> str:
        """Look at the team run."""

        ctx = current_run_context()
        assert ctx is not None and team is not None
        found.append(team._key in ctx.metadata)
        found.append(ctx.internal[team._key])
        found.append(ctx.internal[TEAM_NAME_KEY])
        return "ok"

    team = Team(
        "desk",
        orchestrator=Agent(
            "lead", model=FakeChatModel([tool_call("peek", {}), "done"]), tools=[peek]
        ),
        members=[Agent("member", model=FakeChatModel(["x"]))],
    )

    await team.run("go")

    in_metadata, in_internal, team_name = found
    assert in_metadata is False
    assert isinstance(in_internal, TeamRun)
    assert team_name == "desk"


# ------------------------------------------------------------------ session and call ids


async def test_session_id_without_checkpointer() -> None:
    events: list[RunEvent] = []
    seen: list[str] = []

    def where() -> str:
        """Report the session."""

        ctx = current_run_context()
        assert ctx is not None
        seen.append(ctx.session_id)
        return "ok"

    agent = Agent(
        "solo",
        model=FakeChatModel([tool_call("where", {}), "done", "again"]),
        tools=[where],
        hooks=[events.append],
    )

    await agent.run("go")
    await agent.run("go", metadata={"awa.session_id": "ses_given"})

    first, second = (e for e in events if isinstance(e, AgentStartedEvent))
    assert first.session_id is not None and first.session_id.startswith("ses_")
    assert seen == [first.session_id]
    assert second.session_id == "ses_given"


async def test_child_run_inherits_session_id() -> None:
    events: list[RunEvent] = []
    helper = Agent("helper", model=FakeChatModel(["helped"]))
    lead = Agent(
        "lead",
        model=FakeChatModel([tool_call("helper", {"task": "help"}), "done"]),
        tools=[helper.as_tool()],
        hooks=[events.append],
    )

    await lead.run("go")

    assert started(events, "helper").session_id == started(events, "lead").session_id
    assert started(events, "helper").branch == "helper"


async def test_network_teammate_shares_session_id() -> None:
    from test_team_network_approvals import hiring

    events: list[RunEvent] = []
    network, _, _ = hiring(hooks=[events.append])

    paused = await network.run("Hire Ada.", metadata={"awa.session_id": "ses_hiring"})

    sessions = {e.agent: e.session_id for e in events if isinstance(e, AgentStartedEvent)}
    assert {"hiring", "lead", "recruiter", "checker"} <= set(sessions)
    assert set(sessions.values()) == {"ses_hiring"}
    assert paused.thread_id is not None and paused.thread_id != "ses_hiring"


def _team(events: list[RunEvent], hooks: list[Any] | None = None) -> Team:
    member = Agent("writer", description="Writes.", model=FakeChatModel(["written"]))
    lead = Agent(
        "lead",
        model=FakeChatModel(
            [tool_call("delegate_task", {"agent": "writer", "task": "write it"}, id="tc_9"), "done"]
        ),
    )
    return Team("desk", orchestrator=lead, members=[member], hooks=[events.append, *(hooks or [])])


async def test_agent_started_event_has_agent_call_id() -> None:
    events: list[RunEvent] = []

    await _team(events).run("go")

    [delegation] = [e for e in events if isinstance(e, DelegationEvent)]
    writer = started(events, "writer")
    assert writer.agent_call_id == delegation.correlation_id
    assert writer.tool_call_id == "tc_9"
    assert writer.branch == "lead/desk.writer"
    assert started(events, "desk").agent_call_id is None

    helper = Agent("helper", model=FakeChatModel(["helped"]))
    lead = Agent(
        "solo",
        model=FakeChatModel([tool_call("helper", {"task": "help"}), "done"]),
        tools=[helper.as_tool()],
        hooks=[events.append],
    )
    await lead.run("go")
    call_id = started(events, "helper").agent_call_id
    assert call_id is not None and call_id.startswith("call_")


async def test_child_span_parent_is_delegate_tool_span(
    otel: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = otel
    events: list[RunEvent] = []

    await _team(events, [OpenTelemetrySubscriber(provider.get_tracer("t"))]).run("go")

    spans = exporter.get_finished_spans()
    tool_span = named(spans, "execute_tool delegate_task")
    writer = named(spans, "invoke_agent writer")
    # Agent spans nest under the calling agent; the delegate_task call is a span link.
    assert writer.parent is not None
    assert writer.parent.span_id == named(spans, "invoke_agent lead").context.span_id
    assert [link.context.span_id for link in writer.links] == [tool_span.context.span_id]
    assert dict(tool_span.attributes or {})["gen_ai.tool.call.id"] == "tc_9"
    attributes = dict(writer.attributes or {})
    assert attributes["awa.agent_call_id"] == started(events, "writer").agent_call_id
    assert attributes["awa.branch"] == "lead/desk.writer"


async def test_bus_message_does_not_override_conversation_id(
    otel: tuple[TracerProvider, InMemorySpanExporter],
) -> None:
    provider, exporter = otel

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "sent"
        return tool_call("send_message", {"to": "b", "subject": "hi", "content": "note"})

    a = Agent("a", model=FunctionChatModel(respond))
    b = Agent("b", model=FakeChatModel(["fine"]))
    lead = Agent(
        "lead",
        model=FakeChatModel([tool_call("delegate_task", {"agent": "a", "task": "tell b"}), "ok"]),
    )
    team = Team(
        "desk",
        orchestrator=lead,
        members=[a, b],
        peers={"a": ["b"]},
        hooks=[OpenTelemetrySubscriber(provider.get_tracer("t"), langfuse=True)],
    )

    await team.run("go", metadata={"awa.session_id": "ses_team"})

    spans = exporter.get_finished_spans()
    message = named(spans, "awa.agent_message")
    attributes = dict(message.attributes or {})
    assert attributes["gen_ai.conversation.id"] == "ses_team"
    assert attributes["session.id"] == "ses_team"
    assert attributes["messaging.message.conversation_id"].startswith("msg")
