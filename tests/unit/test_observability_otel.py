from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from awa._internal.install import install_hint
from awa.agents import Agent
from awa.contracts.events import (
    AgentFinishedEvent,
    AgentStartedEvent,
    ModelResponseEvent,
    RunCompletedEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from awa.contracts.results import RunResult
from awa.contracts.usage import ModelPrice, PriceTable, Usage
from awa.errors import OptionalDependencyError
from awa.observability import OpenTelemetrySubscriber
from awa.team import Team
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def otel() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    provider.shutdown()


def subscriber(provider: TracerProvider, **kwargs: Any) -> OpenTelemetrySubscriber:
    return OpenTelemetrySubscriber(provider.get_tracer("test"), **kwargs)


def by_name(spans: tuple[ReadableSpan, ...], name: str) -> list[ReadableSpan]:
    return [span for span in spans if span.name == name]


def one(spans: tuple[ReadableSpan, ...], name: str) -> ReadableSpan:
    [span] = by_name(spans, name)
    return span


def parent_id(span: ReadableSpan) -> int | None:
    return span.parent.span_id if span.parent is not None else None


def span_id(span: ReadableSpan) -> int:
    context = span.get_span_context()
    assert context is not None
    return context.span_id


def trace_id(span: ReadableSpan) -> int:
    context = span.get_span_context()
    assert context is not None
    return context.trace_id


@tool
def lookup(query: str) -> str:
    """Look something up."""
    return f"result for {query}"


def ids(run_id: str, *, agent: str = "lead", depth: int = 0, root: str = "run_1") -> dict[str, Any]:
    return {"run_id": run_id, "root_run_id": root, "agent": agent, "depth": depth}


async def test_single_agent_run_becomes_agent_chat_and_tool_spans(otel: Any) -> None:
    provider, exporter = otel
    sub = subscriber(provider)
    agent = Agent(
        "helper",
        model=FakeChatModel([tool_call("lookup", {"query": "otel"}), "All done."]),
        tools=[lookup],
        hooks=[sub],
    )

    result = await agent.run("Find otel docs")

    spans = exporter.get_finished_spans()
    assert sorted(span.name for span in spans) == [
        "chat",
        "chat",
        "execute_tool lookup",
        "invoke_agent helper",
    ]
    root = one(spans, "invoke_agent helper")
    assert root.parent is None
    assert root.kind is SpanKind.INTERNAL
    assert root.status.status_code is StatusCode.UNSET
    attrs = dict(root.attributes or {})
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.agent.name"] == "helper"
    assert attrs["awa.run_id"] == result.run_id
    assert attrs["gen_ai.usage.input_tokens"] == result.usage.input_tokens
    assert attrs["gen_ai.usage.output_tokens"] == result.usage.output_tokens
    assert attrs["awa.usage.requests"] == 2
    assert attrs["awa.usage.tool_calls"] == 1
    assert "gen_ai.input.messages" not in attrs
    assert "gen_ai.output.messages" not in attrs

    chats = sorted(by_name(spans, "chat"), key=lambda s: s.start_time or 0)
    tool_span = one(spans, "execute_tool lookup")
    for child in (*chats, tool_span):
        assert parent_id(child) == span_id(root)
        assert trace_id(child) == trace_id(root)
    assert all(chat.kind is SpanKind.CLIENT for chat in chats)
    first = dict(chats[0].attributes or {})
    assert first["gen_ai.operation.name"] == "chat"
    assert first["gen_ai.response.finish_reasons"] == ("tool_call",)
    assert first["awa.tool_call_names"] == ("lookup",)
    assert dict(chats[1].attributes or {})["gen_ai.response.finish_reasons"] == ("stop",)
    tool_attrs = dict(tool_span.attributes or {})
    assert tool_attrs["gen_ai.tool.name"] == "lookup"
    assert tool_attrs["gen_ai.operation.name"] == "execute_tool"
    assert tool_attrs["gen_ai.tool.call.id"]
    assert tool_attrs["gen_ai.agent.name"] == "helper"  # conditionally required: who ran the tool
    # `gen_ai.provider.name` is required on a chat span; a model that reports none is "unknown"
    assert first["gen_ai.provider.name"] == "unknown"
    assert "gen_ai.tool.call.arguments" not in tool_attrs
    assert "gen_ai.tool.call.result" not in tool_attrs
    # Timing follows the events: chat, then the tool, then the final chat, all inside the run.
    assert root.start_time is not None and root.end_time is not None
    assert chats[0].end_time is not None and tool_span.start_time is not None
    assert chats[0].end_time <= tool_span.start_time
    assert tool_span.end_time is not None and chats[1].start_time is not None
    assert tool_span.end_time <= chats[1].start_time
    assert root.start_time <= chats[0].start_time <= root.end_time  # type: ignore[operator]
    assert sub.open_spans == 0


async def test_chat_span_names_the_model_when_the_event_reports_it(otel: Any) -> None:
    class NamedModelResponse(ModelResponseEvent):
        model: str | None = None

    provider, exporter = otel
    sub = subscriber(provider)
    sub(AgentStartedEvent(**ids("run_1"), timestamp=T0))
    sub(
        NamedModelResponse(
            **ids("run_1"),
            timestamp=T0 + timedelta(seconds=1),
            model="gpt-test",
            input_tokens=7,
            output_tokens=3,
        )
    )
    sub(AgentFinishedEvent(**ids("run_1"), timestamp=T0 + timedelta(seconds=2)))

    chat = one(exporter.get_finished_spans(), "chat gpt-test")
    attrs = dict(chat.attributes or {})
    assert attrs["gen_ai.request.model"] == attrs["gen_ai.response.model"] == "gpt-test"
    assert (attrs["gen_ai.usage.input_tokens"], attrs["gen_ai.usage.output_tokens"]) == (7, 3)
    assert chat.start_time == int(T0.timestamp()) * 10**9
    assert chat.end_time == int(T0.timestamp() + 1) * 10**9


async def test_prices_add_the_run_cost_to_the_root_span(otel: Any) -> None:
    provider, exporter = otel
    prices = PriceTable(prices={"m1": ModelPrice(input_per_mtok=1.0, output_per_mtok=2.0)})
    agent = Agent(
        "helper",
        model=FakeChatModel(["Hi."], model_name="m1"),
        hooks=[subscriber(provider, prices=prices)],
    )

    result = await agent.run("Hello")

    root = one(exporter.get_finished_spans(), "invoke_agent helper")
    assert dict(root.attributes or {})["awa.usage.cost"] == pytest.approx(result.usage.cost(prices))


def delegate(agent: str, task: str) -> Any:
    return tool_call("delegate_task", {"agent": agent, "task": task})


async def test_team_with_parallel_delegation_nests_member_runs(otel: Any) -> None:
    provider, exporter = otel
    sub = subscriber(provider)
    lead = Agent(
        "lead",
        description="Coordinates.",
        model=FakeChatModel([[delegate("a", "x"), delegate("b", "y")], "Merged."]),
    )
    a = Agent("a", description="A.", model=FakeChatModel(["from a"]))
    b = Agent("b", description="B.", model=FakeChatModel(["from b"]))
    team = Team("desk", orchestrator=lead, members=[a, b], hooks=[sub])

    result = await team.run("Go")

    spans = exporter.get_finished_spans()
    desk = one(spans, "invoke_workflow desk")
    lead_span = one(spans, "invoke_agent lead")
    a_span = one(spans, "invoke_agent a")
    b_span = one(spans, "invoke_agent b")
    assert desk.parent is None
    desk_operation = dict(desk.attributes or {})
    assert desk_operation["gen_ai.operation.name"] == "invoke_workflow"
    assert desk_operation["gen_ai.workflow.name"] == "desk"
    assert parent_id(lead_span) == span_id(desk)
    assert parent_id(a_span) == parent_id(b_span) == span_id(lead_span)
    assert len({trace_id(span) for span in spans}) == 1
    tools = by_name(spans, "execute_tool delegate_task")
    assert len(tools) == 2
    assert all(parent_id(span) == span_id(lead_span) for span in tools)
    member_chats = [
        s for s in by_name(spans, "chat") if parent_id(s) in {span_id(a_span), span_id(b_span)}
    ]
    assert len(member_chats) == 2
    delegations = [event for event in lead_span.events if event.name == "awa.delegation"]
    assert sorted(str((e.attributes or {})["awa.target"]) for e in delegations) == ["a", "b"]
    assert all("awa.task" not in (e.attributes or {}) for e in delegations)
    results = [event for event in lead_span.events if event.name == "awa.delegation_result"]
    assert len(results) == 2
    desk_attrs = dict(desk.attributes or {})
    assert desk_attrs["gen_ai.usage.input_tokens"] == result.usage.input_tokens
    assert desk_attrs["awa.usage.total_tokens"] == result.usage.total_tokens
    assert (
        dict(lead_span.attributes or {})["gen_ai.usage.input_tokens"] == result.usage.input_tokens
    )
    assert sub.open_spans == 0


async def test_handoff_links_the_receiving_run_to_the_source(otel: Any) -> None:
    provider, exporter = otel
    lead = Agent(
        "lead",
        description="Leads.",
        model=FakeChatModel(
            [
                AIMessage(
                    content="", tool_calls=[tool_call("transfer_to_writer", {"reason": "prose"})]
                )
            ]
        ),
    )
    writer = Agent("writer", description="Writes.", model=FakeChatModel(["A poem."]))
    team = Team(
        "desk",
        orchestrator=lead,
        members=[writer],
        handoffs={"lead": ["writer"]},
        hooks=[subscriber(provider)],
    )

    await team.run("Write a poem.")

    spans = exporter.get_finished_spans()
    desk = one(spans, "invoke_workflow desk")
    lead_span = one(spans, "invoke_agent lead")
    writer_span = one(spans, "invoke_agent writer")
    assert parent_id(lead_span) == parent_id(writer_span) == span_id(desk)
    [handoff] = [event for event in desk.events if event.name == "awa.handoff"]
    attrs = dict(handoff.attributes or {})
    assert (attrs["awa.source"], attrs["awa.target"]) == ("lead", "writer")
    assert "awa.reason" not in attrs
    [link] = writer_span.links
    assert link.context.span_id == span_id(lead_span)


async def test_hook_on_team_and_member_does_not_duplicate_spans(otel: Any) -> None:
    provider, exporter = otel
    sub = subscriber(provider)
    lead = Agent("lead", description="L.", model=FakeChatModel([delegate("a", "x"), "Done."]))
    a = Agent("a", description="A.", model=FakeChatModel(["from a"]), hooks=[sub])
    await Team("desk", orchestrator=lead, members=[a], hooks=[sub]).run("Go")

    spans = exporter.get_finished_spans()
    assert len(by_name(spans, "invoke_agent a")) == 1
    assert len(by_name(spans, "chat")) == 3


async def test_failed_run_has_error_status_and_exception_event(otel: Any) -> None:
    def explode(call: ModelCall) -> Any:
        raise RuntimeError("model down")

    provider, exporter = otel
    for capture in (False, True):
        exporter.clear()
        sub = subscriber(provider, capture_content=capture)
        agent = Agent("helper", model=FunctionChatModel(explode), hooks=[sub])

        with pytest.raises(RuntimeError):
            await agent.run("Hi")

        root = one(exporter.get_finished_spans(), "invoke_agent helper")
        assert root.status.status_code is StatusCode.ERROR
        assert dict(root.attributes or {})["error.type"] == "RuntimeError"
        [exception] = [event for event in root.events if event.name == "exception"]
        # the text of an error can quote the prompt: it is content, exported only on request
        expected = {"exception.type": "RuntimeError"}
        if capture:
            expected["exception.message"] = "model down"
        assert dict(exception.attributes or {}) == expected
        assert sub.open_spans == 0


async def test_tool_error_marks_the_tool_span(otel: Any) -> None:
    provider, exporter = otel
    for capture in (False, True):
        exporter.clear()
        sub = subscriber(provider, capture_content=capture)
        sub(AgentStartedEvent(**ids("run_1"), timestamp=T0))
        sub(ToolCallEvent(**ids("run_1"), tool_name="pay", call_id="c1", timestamp=T0))
        sub(
            ToolResultEvent(
                **ids("run_1"),
                tool_name="pay",
                call_id="c1",
                content="card 4242 declined",
                is_error=True,
                timestamp=T0 + timedelta(seconds=1),
            )
        )
        sub(AgentFinishedEvent(**ids("run_1"), timestamp=T0 + timedelta(seconds=2)))
        span = one(exporter.get_finished_spans(), "execute_tool pay")
        assert span.status.status_code is StatusCode.ERROR
        assert dict(span.attributes or {})["error.type"] == "tool_error"
        assert ("4242" in (span.status.description or "")) is capture


async def test_a_raising_tracer_never_fails_the_run() -> None:
    class Broken:
        def start_span(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("exporter misconfigured")

    agent = Agent(
        "helper",
        model=FakeChatModel(["Hi."]),
        hooks=[OpenTelemetrySubscriber(Broken())],  # type: ignore[arg-type]
    )
    assert (await agent.run("Hello")).output == "Hi."


async def test_capture_content_records_truncated_inputs_outputs_and_tool_io(otel: Any) -> None:
    provider, exporter = otel
    agent = Agent(
        "helper",
        model=FakeChatModel([tool_call("lookup", {"query": "otel"}), "x" * 100]),
        tools=[lookup],
        hooks=[subscriber(provider, capture_content=True, max_content_chars=40)],
    )

    await agent.run("Find otel docs")

    spans = exporter.get_finished_spans()
    root = dict(one(spans, "invoke_agent helper").attributes or {})
    [message] = json.loads(str(root["gen_ai.input.messages"]))
    assert message == {"role": "user", "parts": [{"type": "text", "content": "Find otel docs"}]}
    [output] = json.loads(str(root["gen_ai.output.messages"]))
    assert output["role"] == "assistant" and output["finish_reason"] == "stop"
    assert len(output["parts"][0]["content"]) == 40
    assert output["parts"][0]["content"].endswith("...")
    tool_attrs = dict(one(spans, "execute_tool lookup").attributes or {})
    assert json.loads(str(tool_attrs["gen_ai.tool.call.arguments"])) == {"query": "otel"}
    assert tool_attrs["gen_ai.tool.call.result"] == "result for otel"
    chats = sorted(by_name(spans, "chat"), key=lambda s: s.start_time or 0)
    [first] = json.loads(str(dict(chats[0].attributes or {})["gen_ai.output.messages"]))
    assert first["parts"] == [{"type": "tool_call", "name": "lookup"}]


async def test_capture_content_includes_delegated_tasks(otel: Any) -> None:
    provider, exporter = otel
    lead = Agent("lead", description="L.", model=FakeChatModel([delegate("a", "secret"), "Ok."]))
    a = Agent("a", description="A.", model=FakeChatModel(["done"]))
    team = Team(
        "desk", orchestrator=lead, members=[a], hooks=[subscriber(provider, capture_content=True)]
    )
    await team.run("Go")

    lead_span = one(exporter.get_finished_spans(), "invoke_agent lead")
    [delegation] = [event for event in lead_span.events if event.name == "awa.delegation"]
    assert "secret" in str((delegation.attributes or {})["awa.task"])


def test_max_content_chars_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_content_chars"):
        OpenTelemetrySubscriber(max_content_chars=0)


async def test_concurrent_runs_get_separate_traces(otel: Any) -> None:
    provider, exporter = otel
    sub = subscriber(provider)

    def slow(name: str) -> Agent:
        async_model = FakeChatModel([tool_call("lookup", {"query": name}), f"{name} done"])
        return Agent(name, model=async_model, tools=[lookup], hooks=[sub])

    await asyncio.gather(slow("one").run("a"), slow("two").run("b"), slow("one").run("c"))

    spans = exporter.get_finished_spans()
    roots = [span for span in spans if span.name.startswith("invoke_agent")]
    assert len(roots) == 3
    assert len({trace_id(span) for span in roots}) == 3
    for root in roots:
        children = [span for span in spans if parent_id(span) == span_id(root)]
        assert sorted(span.name for span in children) == ["chat", "chat", "execute_tool lookup"]
        assert {trace_id(span) for span in children} == {trace_id(root)}
    assert sub.open_spans == 0


async def test_missing_end_events_are_closed_with_the_run(otel: Any) -> None:
    provider, exporter = otel
    sub = subscriber(provider)
    sub(AgentStartedEvent(**ids("run_1"), timestamp=T0))
    sub(
        AgentStartedEvent(
            **ids("run_2", agent="child", depth=1), parent_run_id="run_1", timestamp=T0
        )
    )
    sub(ToolCallEvent(**ids("run_2", agent="child", depth=1), tool_name="t", call_id="c1"))
    assert sub.open_spans == 3

    sub(AgentFinishedEvent(**ids("run_1"), timestamp=T0 + timedelta(seconds=5)))

    assert sub.open_spans == 0
    spans = exporter.get_finished_spans()
    child = one(spans, "invoke_agent child")
    tool_span = one(spans, "execute_tool t")
    assert dict(child.attributes or {})["awa.incomplete"] is True
    assert dict(tool_span.attributes or {})["awa.incomplete"] is True
    assert child.end_time == int((T0 + timedelta(seconds=5)).timestamp()) * 10**9
    assert parent_id(tool_span) == span_id(child)


async def test_out_of_order_and_orphan_events_do_not_break(otel: Any) -> None:
    provider, exporter = otel
    sub = subscriber(provider)
    # Events of a run that was never started, with no open root: ignored or self-contained.
    sub(ModelResponseEvent(**ids("run_x", root="run_x")))
    sub(ToolResultEvent(**ids("run_x", root="run_x"), tool_name="t", call_id="c"))
    sub(AgentFinishedEvent(**ids("run_x", root="run_x"), timestamp=T0))
    # A result without its call becomes a zero-length span under the run.
    sub(AgentStartedEvent(**ids("run_1"), timestamp=T0))
    sub(ToolResultEvent(**ids("run_1"), tool_name="late", call_id="c9", timestamp=T0))
    sub(ToolResultEvent(**ids("run_1"), tool_name="late", call_id="c9", timestamp=T0))
    sub(AgentFinishedEvent(**ids("run_1"), timestamp=T0))
    # Events after the run finished are ignored.
    sub(ModelResponseEvent(**ids("run_1")))

    names = sorted(span.name for span in exporter.get_finished_spans())
    assert names == [
        "execute_tool late",
        "execute_tool late",
        "invoke_agent lead",
        "invoke_agent lead",
    ]
    assert sub.open_spans == 0


async def test_run_completed_and_close_end_open_spans(otel: Any) -> None:
    provider, exporter = otel
    sub = subscriber(provider)
    sub(AgentStartedEvent(**ids("run_1"), timestamp=T0))
    result: RunResult[Any] = RunResult(run_id="run_1", agent="lead", output="ok", usage=Usage())
    sub(RunCompletedEvent(**ids("run_1"), result=result))
    assert sub.open_spans == 0

    sub(AgentStartedEvent(**ids("run_2", root="run_2"), timestamp=T0))
    sub(ToolCallEvent(**ids("run_2", root="run_2"), tool_name="t", call_id="c"))
    sub.close()
    assert sub.open_spans == 0
    assert len(exporter.get_finished_spans()) == 3


async def test_root_span_joins_the_callers_active_span(otel: Any) -> None:
    provider, exporter = otel
    agent = Agent("helper", model=FakeChatModel(["Hi."]), hooks=[subscriber(provider)])

    with provider.get_tracer("app").start_as_current_span("handle_request") as outer:
        await agent.run("Hello")

    spans = exporter.get_finished_spans()
    root = one(spans, "invoke_agent helper")
    request = one(spans, "handle_request")
    assert parent_id(root) == span_id(request)
    assert trace_id(root) == outer.get_span_context().trace_id


async def test_attributes_are_set_on_every_span(otel: Any) -> None:
    provider, exporter = otel
    agent = Agent(
        "helper",
        model=FakeChatModel([tool_call("lookup", {"query": "q"}), "Done."]),
        tools=[lookup],
        hooks=[subscriber(provider, attributes={"gen_ai.conversation.id": "thread-7"})],
    )
    await agent.run("Hi")

    spans = exporter.get_finished_spans()
    assert len(spans) == 4
    assert all(dict(s.attributes or {})["gen_ai.conversation.id"] == "thread-7" for s in spans)


def test_missing_dependency_raises_with_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", None)
    with pytest.raises(OptionalDependencyError, match=re.escape(install_hint("otel"))):
        OpenTelemetrySubscriber()
