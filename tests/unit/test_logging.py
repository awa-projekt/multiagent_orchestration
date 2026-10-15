"""`get_logger` and `audit_node`: namespacing, redaction of credentials and audit events."""

from __future__ import annotations

import logging

import pytest

from _helpers.fake_secrets import fake_secret
from awa._internal.logging import get_logger
from awa.contracts.events import AuditEvent
from awa.observability import audit_node


def test_get_logger_is_namespaced_under_awa() -> None:
    logger = get_logger("my_module")

    assert logger.name == "awa.my_module"


def test_get_logger_accepts_an_already_prefixed_name() -> None:
    assert get_logger("awa.audit").name == "awa.audit"


def test_a_bad_log_format_reaches_the_handler_instead_of_raising_at_the_call_site() -> None:
    [redact] = get_logger("bad_format").filters
    record = logging.LogRecord(
        "awa.bad_format", logging.WARNING, "x", 1, "%s and %s", ("one",), None
    )

    assert redact.filter(record) is True


def test_awa_loggers_redact_credentials_in_messages_and_tracebacks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dsn = "postgresql://app:hunter2hunter2@db.internal/prod"
    key = fake_secret("sk-", "proj-abcdefghijklmnopqrstuvwx")
    logger = get_logger("tools_of_a_user")

    with caplog.at_level(logging.INFO, logger="awa"):
        try:
            raise RuntimeError(f"could not pay with {key} on {dsn}")
        except RuntimeError:
            logger.exception("tool failed for %s", dsn)

    [record] = caplog.records
    shown = caplog.text
    assert "hunter2hunter2" not in shown and key not in shown
    assert "tool failed for" in shown and "RuntimeError" in shown and "[REDACTED:" in shown
    assert record.exc_info is not None  # the exception object itself stays for handlers


def test_every_get_logger_call_installs_the_redaction_once() -> None:
    first, second = get_logger("again"), get_logger("again")

    assert first is second
    assert len([f for f in first.filters if type(f).__name__ == "_RedactSecrets"]) == 1


def test_get_logger_does_not_double_the_awa_prefix() -> None:
    assert get_logger("tracing").name == "awa.tracing"
    assert get_logger("awa.tracing").name == "awa.tracing"
    assert get_logger("awa").name == "awa"


def test_audit_node_reports_success_and_failure_to_the_sink() -> None:
    events: list[AuditEvent] = []

    with audit_node(
        "fetch", agent_name="researcher", output_artifact_refs=["doc:1"], sink=events.append
    ):
        pass
    with pytest.raises(ValueError), audit_node("parse", sink=events.append):
        raise ValueError("bad")

    assert [(e.node_name, e.event_type) for e in events] == [
        ("fetch", "node_completed"),
        ("parse", "node_failed"),
    ]
    assert events[0].agent_name == "researcher"
    assert events[0].output_artifact_refs == ["doc:1"]


def test_audit_node_survives_a_failing_sink() -> None:
    def broken_sink(event: AuditEvent) -> None:
        raise RuntimeError("sink down")

    with audit_node("step", sink=broken_sink):
        pass


def test_audit_node_logs_the_event_as_json(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="awa.audit"), audit_node("step"):
        pass

    assert AuditEvent.model_validate_json(caplog.records[0].getMessage()).node_name == "step"
