"""Captured content in spans and event logs masks personal data by default."""

from __future__ import annotations

import inspect
import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from _helpers.fake_secrets import fake_secret
from awa.contracts.events import (
    AgentFinishedEvent,
    AgentStartedEvent,
    DelegationEvent,
    ErrorEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from awa.observability import (
    SECRETS_REDACTOR,
    EventLogger,
    OpenTelemetrySubscriber,
    connect_langfuse,
)
from awa.observability.otel import PII_REDACTOR, resolve_redactor

EMAIL = "jane.doe@example.com"
IBAN = "DE89 3704 0044 0532 0130 00"
CARD = "4111 1111 1111 1111"
KEY = fake_secret("sk-", "proj-Zx9Qw3Lm8Vb2Nc7Rt5Yu")
PERSONAL = (EMAIL, IBAN, CARD)
TEXT = f"mail {EMAIL} iban {IBAN} card {CARD} key {KEY}"

IDS: dict[str, Any] = {
    "run_id": "run_1",
    "root_run_id": "run_1",
    "agent": "lead",
    "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
}


def _play(subscriber: Any) -> None:
    subscriber(AgentStartedEvent(input_preview=TEXT, **IDS))
    subscriber(ToolCallEvent(tool_name="lookup", call_id="c1", arguments={"q": TEXT}, **IDS))
    subscriber(ToolResultEvent(tool_name="lookup", call_id="c1", content=TEXT, **IDS))
    subscriber(DelegationEvent(target="ana", task=TEXT, correlation_id="d1", **IDS))
    subscriber(ErrorEvent(error_type="ValueError", message=TEXT, **IDS))
    subscriber(AgentFinishedEvent(**IDS))


def _export(**options: Any) -> str:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    subscriber = OpenTelemetrySubscriber(provider.get_tracer("t"), capture_content=True, **options)
    _play(subscriber)
    subscriber.close()
    return json.dumps(
        [
            [
                s.name,
                dict(s.attributes or {}),
                [(e.name, dict(e.attributes or {})) for e in s.events],
            ]
            for s in exporter.get_finished_spans()
        ],
        default=str,
    )


def test_captured_content_is_masked_by_default() -> None:
    text = _export()

    for value in (*PERSONAL, KEY):
        assert value not in text
    assert "[REDACTED:email]" in text


def test_redactor_none_sends_content_unchanged() -> None:
    text = _export(redactor=None)

    assert all(value in text for value in (*PERSONAL, KEY))


def test_guard_redaction_off_still_masks_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("awa.plugins.switches.is_off", lambda key, **_: key == "guard.redaction")

    assert resolve_redactor(PII_REDACTOR) is SECRETS_REDACTOR
    text = _export()

    assert KEY not in text
    assert all(value in text for value in PERSONAL)


def test_the_default_redactor_is_shared() -> None:
    defaults = [
        inspect.signature(OpenTelemetrySubscriber.__init__).parameters["redactor"].default,
        inspect.signature(connect_langfuse).parameters["redactor"].default,
        inspect.signature(EventLogger.__init__).parameters["redactor"].default,
    ]

    assert all(default is PII_REDACTOR for default in defaults)
    other = SECRETS_REDACTOR
    assert resolve_redactor(other) is other
    assert resolve_redactor(None) is None


def test_event_logger_masks_personal_data_by_default(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("awa.test.masking")
    event = ToolCallEvent(tool_name="lookup", call_id="c1", arguments={"q": TEXT}, **IDS)

    with caplog.at_level(logging.INFO, logger=logger.name):
        EventLogger(logger=logger)(event)
        EventLogger(logger=logger)(AgentFinishedEvent(**IDS, duration_ms=1234))
    lines = [record.getMessage() for record in caplog.records]

    assert lines[0] == "tool_call agent=lead depth=0 run=run_1 tool=lookup call_id=c1"
    assert lines[1].startswith("agent_finished agent=lead depth=0 run=run_1 duration_ms=1234")
    error = ErrorEvent(error_type="ValueError", message=TEXT, **IDS)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=logger.name):
        EventLogger(logger=logger)(error)
    line = caplog.records[0].getMessage()
    assert "run_1" in line and EMAIL not in line and CARD not in line and KEY not in line


def test_event_observation_input_is_masked() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    subscriber = OpenTelemetrySubscriber(
        provider.get_tracer("t"), capture_content=True, langfuse=True
    )
    _play(subscriber)
    subscriber.close()

    inputs = [
        str(span.attributes.get("langfuse.observation.input", ""))
        for span in exporter.get_finished_spans()
        if span.attributes
    ]
    assert any("[REDACTED:email]" in value for value in inputs)
    assert all(EMAIL not in value and CARD not in value for value in inputs)
