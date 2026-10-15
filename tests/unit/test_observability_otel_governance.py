"""OpenTelemetry bridge: provider names, workflow spans, governance span events, metrics."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

from langchain_core.messages import AIMessage
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from _helpers.fake_secrets import fake_secret
from awa.agents import Agent
from awa.contracts.events import (
    AgentFinishedEvent,
    AgentStartedEvent,
    ApprovalDecidedEvent,
    ApprovalRequestedEvent,
    ErrorEvent,
    GuardrailBlockedEvent,
    LimitExceededEvent,
    ModelResponseEvent,
    RunCancelledEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from awa.observability import SECRETS_REDACTOR, OpenTelemetrySubscriber, RegexRedactor
from awa.observability.otel import provider_name
from awa.testing import FakeChatModel
from awa.workflows import Sequential

IDS: dict[str, Any] = {"run_id": "run_1", "root_run_id": "run_1", "agent": "lead"}


@pytest.fixture
def otel() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    provider.shutdown()


def one(spans: tuple[ReadableSpan, ...], name: str) -> ReadableSpan:
    [span] = [s for s in spans if s.name == name]
    return span


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openai", None, "openai"),
        ("google_genai", "gemini-2.5-pro", "gcp.gemini"),
        ("bedrock_converse", None, "aws.bedrock"),
        ("Azure", None, "azure.ai.openai"),
        ("acme-llm", None, "acme-llm"),
        (None, "gpt-4o-mini-2024-07-18", "openai"),
        (None, "claude-sonnet-4-5", "anthropic"),
        (None, "anthropic:claude-haiku", "anthropic"),
        (None, "models/gemini-2.0-flash", "gcp.gemini"),
        (None, "mistral-large", "mistral_ai"),
        (None, "my-local-model", None),
        (None, None, None),
    ],
)
def test_provider_name(provider: str | None, model: str | None, expected: str | None) -> None:
    assert provider_name(provider, model) == expected


async def test_chat_and_agent_spans_carry_the_provider(otel: Any) -> None:
    provider, exporter = otel
    reply = AIMessage(
        content="Hi",
        response_metadata={
            "model_name": "claude-sonnet-4-5",
            "model_provider": "anthropic",
            "stop_reason": "end_turn",
        },
    )
    agent = Agent(
        "helper",
        model=FakeChatModel([reply]),
        hooks=[OpenTelemetrySubscriber(provider.get_tracer("t"))],
    )
    await agent.run("Hello")

    spans = exporter.get_finished_spans()
    chat = dict(one(spans, "chat claude-sonnet-4-5").attributes or {})
    assert chat["gen_ai.provider.name"] == "anthropic"
    assert chat["gen_ai.response.finish_reasons"] == ("end_turn",)
    root = dict(one(spans, "invoke_agent helper").attributes or {})
    assert root["gen_ai.provider.name"] == "anthropic"
    assert root["gen_ai.request.model"] == "claude-sonnet-4-5"
    assert root["gen_ai.operation.name"] == "invoke_agent"


async def test_workflows_become_invoke_workflow_spans(otel: Any) -> None:
    provider, exporter = otel
    first = Agent("first", model=FakeChatModel(["one"]))
    second = Agent("second", model=FakeChatModel(["two"]))
    pipeline = Sequential("pipeline", [first, second])
    sub = OpenTelemetrySubscriber(provider.get_tracer("t"))
    async for event in pipeline.stream("Go"):
        sub(event)

    spans = exporter.get_finished_spans()
    root = one(spans, "invoke_workflow pipeline")
    attrs = dict(root.attributes or {})
    assert attrs["gen_ai.operation.name"] == "invoke_workflow"
    assert attrs["gen_ai.workflow.name"] == "pipeline"
    steps = [s for s in spans if s.name.startswith("invoke_agent ")]
    assert sorted(s.name for s in steps) == ["invoke_agent first", "invoke_agent second"]
    assert all(s.parent is not None and s.parent.span_id == root.context.span_id for s in steps)


def test_governance_events_become_span_events(otel: Any) -> None:
    provider, exporter = otel
    sub = OpenTelemetrySubscriber(provider.get_tracer("t"))
    sub(AgentStartedEvent(**IDS))
    sub(
        ApprovalRequestedEvent(
            tool="pay", interrupt_id="i1", thread_id="t1", args_preview='{"a": 1}', **IDS
        )
    )
    sub(ApprovalDecidedEvent(tool="pay", decision="reject", reviewer="bob", latency_ms=5, **IDS))
    sub(GuardrailBlockedEvent(guardrail="pii", stage="input", reason="email (1)", **IDS))
    sub(LimitExceededEvent(limit="max_requests", value=3, maximum=2, **IDS))
    sub(RunCancelledEvent(reason="timeout", elapsed_ms=10, **IDS))
    sub(ErrorEvent(error_type="RunTimeoutError", message="timed out", **IDS))

    span = one(exporter.get_finished_spans(), "invoke_agent lead")
    events = {event.name: dict(event.attributes or {}) for event in span.events}
    assert events["awa.approval_requested"]["awa.interrupt_id"] == "i1"
    assert events["awa.approval_requested"]["gen_ai.conversation.id"] == "t1"
    assert "awa.args" not in events["awa.approval_requested"]
    assert events["awa.approval_decided"]["awa.decision"] == "reject"
    assert events["awa.approval_decided"]["awa.reviewer"] == "bob"
    assert events["awa.guardrail_blocked"]["awa.stage"] == "input"
    assert "awa.reason" not in events["awa.guardrail_blocked"]  # a reason can quote the content
    assert events["awa.limit_exceeded"]["awa.maximum"] == 2
    assert events["awa.run_cancelled"]["awa.reason"] == "timeout"


def test_captured_content_and_errors_are_redacted(otel: Any) -> None:
    provider, exporter = otel
    sub = OpenTelemetrySubscriber(
        provider.get_tracer("t"), capture_content=True, redactor=RegexRedactor.pii()
    )
    sub(AgentStartedEvent(input_preview="I am jane@example.com", **IDS))
    sub(
        ToolCallEvent(
            tool_name="login",
            call_id="c",
            arguments={"password": "pw", "user": "a@b.example"},
            **IDS,
        )
    )
    sub(
        ToolResultEvent(
            tool_name="login",
            call_id="c",
            content=f"token {fake_secret('sk-', 'abcdefghijklmnopqrst')}",
            **IDS,
        )
    )
    sub(ErrorEvent(error_type="E", message="failed for Bearer abcdefghijkl", **IDS))

    spans = exporter.get_finished_spans()
    text = " ".join(
        str(dict(s.attributes or {})) + str([e.attributes for e in s.events]) for s in spans
    )
    for secret in (
        "jane@example.com",
        "a@b.example",
        fake_secret("sk-", "abcdefghijklmnopqrst"),
        "abcdefghijkl",
        "'pw'",
    ):
        assert secret not in text
    assert "[REDACTED:email]" in text


def test_the_secrets_redactor_covers_secrets_only(otel: Any) -> None:
    provider, exporter = otel
    sub = OpenTelemetrySubscriber(
        provider.get_tracer("t"), capture_content=True, redactor=SECRETS_REDACTOR
    )
    sub(
        AgentStartedEvent(
            input_preview=f"jane@example.com key {fake_secret('sk-', 'abcdefghijklmnopqrst')}",
            **IDS,
        )
    )
    sub(AgentFinishedEvent(**IDS))

    attrs = dict(one(exporter.get_finished_spans(), "invoke_agent lead").attributes or {})
    assert "jane@example.com" in str(attrs["gen_ai.input.messages"])
    assert fake_secret("sk-", "abcdefghijklmnopqrst") not in str(attrs["gen_ai.input.messages"])


def test_metrics_record_token_usage_and_duration(otel: Any) -> None:
    provider, _ = otel
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader])
    sub = OpenTelemetrySubscriber(provider.get_tracer("t"), meter=meters.get_meter("awa"))
    sub(AgentStartedEvent(**IDS))
    sub(
        ModelResponseEvent(
            input_tokens=12, output_tokens=5, model="gpt-4o", provider="openai", **IDS
        )
    )
    sub(AgentFinishedEvent(**IDS))

    data = reader.get_metrics_data()
    metrics = {
        metric.name: metric
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }
    usage = metrics["gen_ai.client.token.usage"]
    assert usage.unit == "{token}"
    by_type = {point.attributes["gen_ai.token.type"]: point.sum for point in usage.data.data_points}
    assert by_type == {"input": 12, "output": 5}
    point = next(iter(usage.data.data_points))
    assert point.attributes["gen_ai.provider.name"] == "openai"
    assert point.attributes["gen_ai.request.model"] == "gpt-4o"
    duration = metrics["gen_ai.client.operation.duration"]
    assert duration.unit == "s"
    assert next(iter(duration.data.data_points)).count == 1
    meters.shutdown()


def test_a_guardrail_reason_is_exported_with_content_capture_only(otel: Any) -> None:
    provider, exporter = otel
    sub = OpenTelemetrySubscriber(provider.get_tracer("t"), capture_content=True)
    sub(AgentStartedEvent(**IDS))
    sub(
        GuardrailBlockedEvent(
            guardrail="terms",
            stage="input",
            reason=f"term {fake_secret('sk-', 'abcdefghijklmnopqrst')}",
            **IDS,
        )
    )

    sub(AgentFinishedEvent(**IDS))

    [event] = one(exporter.get_finished_spans(), "invoke_agent lead").events
    assert dict(event.attributes or {})["awa.reason"] == "term [REDACTED:api_key]"


def test_metrics_of_a_model_without_a_reported_provider_carry_the_unknown_provider(
    otel: Any,
) -> None:
    provider, _ = otel
    reader = InMemoryMetricReader()
    sub = OpenTelemetrySubscriber(
        provider.get_tracer("t"), meter=MeterProvider(metric_readers=[reader]).get_meter("awa")
    )
    sub(AgentStartedEvent(**IDS))
    sub(ModelResponseEvent(input_tokens=1, output_tokens=1, model="my-local-model", **IDS))
    sub(AgentFinishedEvent(**IDS))

    points = [
        dict(point.attributes)
        for resource in reader.get_metrics_data().resource_metrics  # type: ignore[union-attr]
        for scope in resource.scope_metrics
        for metric in scope.metrics
        for point in metric.data.data_points
    ]
    assert points and all(p["gen_ai.provider.name"] == "unknown" for p in points)
