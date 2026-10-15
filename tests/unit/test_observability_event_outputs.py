"""Every sink's exact output for one representative run: log lines, trace lines, spans, audit records.

The expected outputs live in `tests/fixtures/observability_event_outputs.json`; any change to a
log line, a trace line, a span attribute or an audit record shows up here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from awa.contracts.communication import AgentMessage
from awa.contracts.events import (
    AgentFinishedEvent,
    AgentMessageEvent,
    AgentStartedEvent,
    ApprovalDecidedEvent,
    ApprovalRequestedEvent,
    DelegationEvent,
    DelegationResultEvent,
    ErrorEvent,
    GuardrailBlockedEvent,
    HandoffEvent,
    LimitExceededEvent,
    MessageUndeliveredEvent,
    ModelFallbackEvent,
    ModelResponseEvent,
    ModelRetryEvent,
    OutputRetryEvent,
    RunCancelledEvent,
    RunCompletedEvent,
    RunEvent,
    StepEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from awa.contracts.results import RunResult
from awa.contracts.usage import ModelPrice, PriceTable, Usage
from awa.kernel.redaction import RegexRedactor
from awa.observability import (
    AuditLog,
    InMemoryAuditSink,
    OpenTelemetrySubscriber,
    describe_event,
    verify_chain,
)
from awa.observability import audit as audit_module
from awa.observability._trace_encode import TraceEncoder
from awa.observability.trace_store import TraceRecorder, read_trace
from awa.observability.trace_view import build_tree

EXPECTED = Path(__file__).parents[1] / "fixtures" / "observability_event_outputs.json"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
SECRET = "env-secret-value-1234"


def _ids(run: str, depth: int = 0, agent: str = "lead", step: int = 0) -> dict[str, Any]:
    return {
        "run_id": run,
        "root_run_id": "root",
        "agent": agent,
        "depth": depth,
        "timestamp": T0 + timedelta(seconds=step),
    }


def _message(**extra: Any) -> AgentMessage:
    return AgentMessage(
        id="msg_1",
        kind="request",
        sender="lead",
        recipient="writer",
        topic="drafts",
        subject="Draft for jane@example.com",
        content="Please write the summary.",
        conversation_id="conv_1",
        created_at=T0,
        **extra,
    )


def events() -> list[RunEvent]:
    """One run tree that raises every event type with every optional field set."""

    usage = Usage()
    usage.record_model_call(
        "lead", model="gpt-test", input_tokens=120, output_tokens=30, cache_read_tokens=40
    )
    child = {"agent": "writer", "depth": 1}
    return [
        AgentStartedEvent(
            **_ids("root", step=0),
            kind="team",
            input_preview="Summarise the report for jane@example.com",
            session_id="session_1",
        ),
        AgentStartedEvent(
            **_ids("lead_run", 1, step=1),
            parent_run_id="root",
            input_preview="Summarise the report",
            agent_call_id="call_lead",
            branch="b1",
        ),
        TextDeltaEvent(**_ids("lead_run", 1, step=2), delta="Sum"),
        ModelResponseEvent(
            **_ids("lead_run", 1, step=3),
            content="Calling the search tool",
            tool_call_names=("search", "delegate_task"),
            input_tokens=120,
            output_tokens=30,
            cache_read_tokens=40,
            cache_write_tokens=10,
            model="gpt-test",
            provider="openai",
            finish_reason="tool_calls",
            prefix_hash="abc123",
            duration_ms=250,
            cleared_tool_results=2,
        ),
        ToolCallEvent(
            **_ids("lead_run", 1, step=4),
            tool_name="search",
            call_id="call_1",
            arguments={"query": "report for jane@example.com", "token": SECRET},
        ),
        ToolResultEvent(
            **_ids("lead_run", 1, step=5),
            tool_name="search",
            call_id="call_1",
            content='<tool_output note="data">{"content": "found 3 hits"}</tool_output>',
            cache="miss",
        ),
        ToolCallEvent(
            **_ids("lead_run", 1, step=6),
            tool_name="delegate_task",
            call_id="call_2",
            arguments={"agent": "writer", "task": "Write it up"},
        ),
        DelegationEvent(
            **_ids("lead_run", 1, step=7),
            target="writer",
            task="Write it up for jane@example.com",
            correlation_id="corr_1",
        ),
        AgentStartedEvent(
            **{**_ids("writer_run", step=8), **child},
            parent_run_id="lead_run",
            input_preview="Write it up",
            tool_call_id="call_2",
        ),
        ToolCallEvent(
            **{**_ids("writer_run", step=9), **child},
            tool_name="transfer_to_editor",
            call_id="call_3",
            arguments={"note": "hand over"},
        ),
        ToolResultEvent(
            **{**_ids("writer_run", step=10), **child},
            tool_name="fetch",
            call_id="call_4",
            content="HTTP 500 from server",
            is_error=True,
        ),
        AgentFinishedEvent(
            **{**_ids("writer_run", step=11), **child},
            output_preview="The summary is done",
            duration_ms=3000,
            usage=usage,
        ),
        DelegationResultEvent(
            **_ids("lead_run", 1, step=12),
            target="writer",
            correlation_id="corr_1",
            content_preview="The summary is done",
        ),
        DelegationEvent(
            **_ids("lead_run", 1, step=13),
            target="critic",
            task="Is it right?",
            mode="consult",
            correlation_id="corr_2",
        ),
        HandoffEvent(
            **_ids("lead_run", 1, step=14), source="lead", target="editor", reason="needs polish"
        ),
        AgentMessageEvent(**_ids("lead_run", 1, step=15), message=_message()),
        MessageUndeliveredEvent(
            **_ids("lead_run", 1, step=16),
            message=_message(),
            recipient="writer",
            reason="mailbox full",
        ),
        ApprovalRequestedEvent(
            **_ids("lead_run", 1, step=17),
            tool="send_mail",
            interrupt_id="int_1",
            args_preview='{"to": "jane@example.com"}',
            reason="needs a person",
            thread_id="thread_1",
            expires_at=T0 + timedelta(hours=1),
        ),
        ApprovalDecidedEvent(
            **_ids("lead_run", 1, step=18),
            tool="send_mail",
            decision="approve",
            interrupt_id="int_1",
            reviewer="boss@example.com",
            note="fine",
            latency_ms=900,
            remembered=True,
            expired=True,
        ),
        GuardrailBlockedEvent(
            **_ids("lead_run", 1, step=19),
            guardrail="pii",
            stage="tool",
            reason="mail address jane@example.com",
            tool="send_mail",
        ),
        LimitExceededEvent(
            **_ids("lead_run", 1, step=20),
            limit="tool_calls",
            value=11,
            maximum=10,
            message="too many tool calls",
        ),
        StepEvent(
            **_ids("lead_run", 1, step=21),
            step="check",
            index=1,
            total=3,
            status="failed",
            title="Check the totals",
            checks=(
                {"id": "sum", "kind": "rule", "passed": False, "message": "off by 2"},
                {"id": "names", "kind": "rule", "passed": True},
            ),
            message="totals differ",
            attempt=2,
            case="case_1",
        ),
        ModelRetryEvent(
            **_ids("lead_run", 1, step=22),
            model="gpt-test",
            attempt=2,
            delay=1.5,
            error_type="RateLimitError",
            kind="rate_limit",
            status=429,
        ),
        OutputRetryEvent(
            **_ids("lead_run", 1, step=23), check="schema", attempt=1, max_retries=3, kind="schema"
        ),
        ModelFallbackEvent(
            **_ids("lead_run", 1, step=24),
            from_model="gpt-test",
            to_model="gpt-small",
            reason="overloaded",
            error_type="APIError",
        ),
        ErrorEvent(
            **_ids("lead_run", 1, step=25),
            error_type="UsageLimitExceeded",
            message="tool call limit reached for jane@example.com",
            stop={
                "stop_reason": "usage_limit",
                "category": "limit",
                "bound": "tool_calls",
                "value": 11,
                "maximum": 10,
                "last_steps": ["search", "send_mail"],
            },
        ),
        RunCancelledEvent(
            **_ids("root", step=26), reason="user", cancelled_by="ops@example.com", elapsed_ms=26000
        ),
        AgentFinishedEvent(
            **_ids("root", step=27), output_preview="Done", duration_ms=27000, usage=usage
        ),
        RunCompletedEvent(
            **_ids("root", step=28),
            result=RunResult(run_id="root", agent="lead", output="Done", usage=usage),
        ),
    ]


def _spans(**options: Any) -> list[dict[str, Any]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    subscriber = OpenTelemetrySubscriber(provider.get_tracer("test"), **options)
    for event in events():
        subscriber(event)
    subscriber.close()
    spans = [
        {
            "name": span.name,
            "kind": span.kind.name,
            "status": span.status.status_code.name,
            "attributes": dict(span.attributes or {}),
            "events": [
                {"name": event.name, "attributes": dict(event.attributes or {})}
                for event in span.events
            ],
            "links": [dict(link.attributes or {}) for link in span.links],
        }
        for span in exporter.get_finished_spans()
    ]
    provider.shutdown()
    result: list[dict[str, Any]] = json.loads(json.dumps(spans, default=list))
    return result


class _FixedClock(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return T0


def _audit(**options: Any) -> list[dict[str, Any]]:
    sink = InMemoryAuditSink()
    log = AuditLog(sink, event_types={event.type for event in events()}, **options)
    for event in events():
        log(event)
    assert verify_chain(sink.read())
    return [record.model_dump(mode="json") for record in sink.read()]


def _trees(folder: Path) -> dict[str, Any]:
    views: dict[str, Any] = {}
    for level in ("summary", "full"):
        path = folder / f"{level}.jsonl"
        recorder = TraceRecorder(path, level=level, redactor=RegexRedactor.pii())
        for event in events():
            recorder(event)
        recorder.close("ok")
        tree = build_tree(read_trace(path))
        views[level] = {
            "text": tree.render().replace(str(path), "<file>"),
            "tree": {**tree.to_dict(), "file": "<file>"},
        }
    return views


def outputs(monkeypatch: pytest.MonkeyPatch, folder: Path) -> dict[str, Any]:
    """Every output of every sink for `events()`."""

    monkeypatch.setattr(audit_module, "datetime", _FixedClock)
    run = events()
    prices = PriceTable(prices={"gpt-test": ModelPrice(input_per_mtok=1.0, output_per_mtok=2.0)})
    return {
        "log": [describe_event(event) for event in run],
        "chat": [describe_event(event, style="chat") for event in run],
        "trace": {
            level: [
                TraceEncoder(level, RegexRedactor.pii(), [SECRET], with_root=True).encode(event)
                for event in run
            ]
            for level in ("summary", "full")
        },
        "otel": _spans(),
        "otel_content_langfuse": _spans(
            capture_content=True,
            langfuse=True,
            prices=prices,
            session_id="s1",
            user_id="u1",
            tags=["t"],
            trace_name="n",
        ),
        "audit": _audit(),
        "audit_content": _audit(include_content=True, redactor=RegexRedactor.pii()),
        "trace_view": _trees(folder),
    }


def test_every_sink_output_is_stable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    actual = json.loads(json.dumps(outputs(monkeypatch, tmp_path)))
    for key in expected:
        assert actual[key] == expected[key], key
    assert actual.keys() == expected.keys()
