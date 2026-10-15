"""OpenTelemetry bridge: what leaves the process. Content capture off and on, hostile content.

Every test exports to a real local OTLP receiver (`tests/otlp_collector.py`) and looks at what
the receiver got: the decoded spans and the raw request bytes. With content capture off (the
default) no prompt, reply, tool argument, tool result, delegated task, error text or message
body may appear anywhere in them. With it on, secrets are redacted (personal data too with
`RegexRedactor.pii()`), and a control run with `redactor=None` shows that the checks would see a
leak.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

from langchain_core.tools import tool
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

import awa
from _helpers.fake_secrets import fake_secret
from _helpers.genai_conventions import validate
from _helpers.otlp_collector import WIRES, Collector, WireSpan, exporter
from awa.agents import Agent
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
    RunCancelledEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from awa.observability import SECRETS_REDACTOR, OpenTelemetrySubscriber, RegexRedactor
from awa.team import Team
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call

# Text that must never leave the process unless content capture is on.
PROMPT = "CANARY-PROMPT-91f2"
ARGUMENT = "CANARY-ARGUMENT-77c1"
RESULT = "CANARY-RESULT-3b9d"
REPLY = "CANARY-REPLY-5e0a"
TASK = "CANARY-TASK-12ab"
FAILURE = "CANARY-FAILURE-c4d8"
REASON = "CANARY-REASON-0e6f"
NOTE = "CANARY-NOTE-a83c"
BODY = "CANARY-BODY-d5b1"
CANARIES = (PROMPT, ARGUMENT, RESULT, REPLY, TASK, FAILURE, REASON, NOTE, BODY)

# Credentials of the formats the redactor knows.
API_KEY = fake_secret("sk-", "proj-Zx9Qw3Lm8Vb2Nc7Rt5Yu")
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1rAbCdEf"
BEARER = "abcDEF1234567890xyzQ"
DSN_PASSWORD = "S3cr3tPassw0rd"
DSN = f"postgresql://admin:{DSN_PASSWORD}@db.internal:5432/prod"
PASSWORD = "hunter2hunter2"
AWS = fake_secret("AKIA", "IOSFODNN7EXAMPLE")
GITHUB = fake_secret("ghp_", "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8")
PRIVATE_KEY_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"
PRIVATE_KEY = fake_secret(
    "-----BEGIN ", f"PRIVATE KEY-----\n{PRIVATE_KEY_BODY}\n-----END PRIVATE KEY-----"
)
SECRETS = (API_KEY, JWT, BEARER, DSN_PASSWORD, PASSWORD, AWS, GITHUB, PRIVATE_KEY_BODY)

# Personal data of the formats the opt-in redactor knows.
EMAIL = "jane.doe@example.com"
IBAN = "DE89 3704 0044 0532 0130 00"
CARD = "4111 1111 1111 1111"
PHONE = "+49 30 1234567"
ADDRESS = "192.168.12.34"
PII = (EMAIL, IBAN, CARD, PHONE, ADDRESS)

HOSTILE = (
    f"{PROMPT} mail {EMAIL}, iban {IBAN}, card {CARD}, call {PHONE}, host {ADDRESS}; "
    f"key {API_KEY} jwt {JWT} Authorization: Bearer {BEARER} dsn {DSN} password={PASSWORD} "
    f"aws {AWS} github {GITHUB}\n{PRIVATE_KEY}"
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
IDS: dict[str, Any] = {"run_id": "run_1", "root_run_id": "run_1", "agent": "lead", "timestamp": T0}


@tool
def lookup(query: str, password: str = "", note: str = "") -> str:
    """Look something up."""
    return f"{RESULT} {DSN} {API_KEY}"


@tool
def explode(query: str) -> str:
    """Always fails."""
    raise ValueError(f"{FAILURE} {API_KEY} for {query}")


@dataclass
class Exported:
    spans: list[WireSpan]
    raw: bytes

    @property
    def text(self) -> str:
        """Everything the receiver got, decoded: names, attributes, events, status messages."""

        return json.dumps(
            [
                [s.name, s.attributes, [(e.name, e.attributes) for e in s.events], s.status_message]
                for s in self.spans
            ],
            default=str,
        )

    def leaks(self, values: tuple[str, ...]) -> list[str]:
        """Which of `values` appear in the raw request bytes or in the decoded spans."""

        return [v for v in values if v.encode() in self.raw or v in self.text]


async def export_run(
    wire: str, play: Callable[[OpenTelemetrySubscriber], Awaitable[None]], **options: Any
) -> Exported:
    """Run `play` with a subscriber that exports to a local collector over `wire`."""

    with Collector() as collector:
        provider = TracerProvider(resource=Resource({"service.name": "awa-content-test"}))
        provider.add_span_processor(BatchSpanProcessor(exporter(wire, collector.endpoint)))
        subscriber = OpenTelemetrySubscriber(provider.get_tracer("awa", awa.__version__), **options)
        await play(subscriber)
        subscriber.close()
        assert provider.force_flush(30_000)
        provider.shutdown()
        return Exported(collector.spans, collector.raw)


def delegate(agent: str, task: str) -> Any:
    return tool_call("delegate_task", {"agent": agent, "task": task})


async def hostile_team_run(subscriber: OpenTelemetrySubscriber) -> None:
    """A team run where every kind of content carries canaries, secrets and personal data."""

    worker = Agent(
        "worker",
        description="Looks things up.",
        model=FakeChatModel(
            [
                tool_call(
                    "lookup", {"query": f"{ARGUMENT} {HOSTILE}", "password": PASSWORD, "note": JWT}
                ),
                tool_call("explode", {"query": ARGUMENT}),  # fails the worker's run
            ],
            model_name="gpt-4o-mini",
        ),
        tools=[lookup, explode],
    )
    lead = Agent(
        "lead",
        description="Coordinates.",
        model=FakeChatModel(
            [delegate("worker", f"{TASK} {HOSTILE}"), f"{REPLY} {HOSTILE}"], model_name="gpt-4o"
        ),
    )
    team = Team("desk", orchestrator=lead, members=[worker], hooks=[subscriber])
    await team.run(HOSTILE)


async def hostile_failures_and_governance(subscriber: OpenTelemetrySubscriber) -> None:
    """A run that fails with a hostile message, and the governance events with hostile text."""

    def explode_model(call: ModelCall) -> Any:
        raise RuntimeError(f"{FAILURE} {HOSTILE}")

    broken = Agent("broken", model=FunctionChatModel(explode_model), hooks=[subscriber])
    with pytest.raises(RuntimeError):
        await broken.run(HOSTILE)

    subscriber(AgentStartedEvent(input_preview=HOSTILE, **IDS))
    subscriber(
        DelegationEvent(target="worker", task=f"{TASK} {HOSTILE}", correlation_id="c1", **IDS)
    )
    subscriber(
        DelegationResultEvent(
            target="worker",
            status="ok",
            content_preview=f"{REPLY} {HOSTILE}",
            correlation_id="c1",
            **IDS,
        )
    )
    subscriber(
        ApprovalRequestedEvent(
            tool="pay",
            interrupt_id="i1",
            thread_id="t1",
            args_preview=json.dumps({"to": ARGUMENT, "note": HOSTILE}),
            **IDS,
        )
    )
    subscriber(
        ApprovalDecidedEvent(
            tool="pay", decision="reject", reviewer="bob", note=f"{NOTE} {HOSTILE}", **IDS
        )
    )
    subscriber(
        GuardrailBlockedEvent(guardrail="terms", stage="input", reason=f"{REASON} {HOSTILE}", **IDS)
    )
    subscriber(HandoffEvent(source="lead", target="worker", reason=f"{REASON} {HOSTILE}", **IDS))
    subscriber(
        AgentMessageEvent(
            message=AgentMessage(
                sender="lead",
                recipient="worker",
                subject=f"{BODY} {HOSTILE}",
                content=f"{BODY} {HOSTILE}",
            ),
            **IDS,
        )
    )
    subscriber(ToolCallEvent(tool_name="pay", call_id="c9", arguments={"to": HOSTILE}, **IDS))
    subscriber(
        ToolResultEvent(
            tool_name="pay", call_id="c9", content=f"{RESULT} {HOSTILE}", is_error=True, **IDS
        )
    )
    subscriber(RunCancelledEvent(reason="stopped by the operator", elapsed_ms=5, **IDS))
    subscriber(ErrorEvent(error_type="ValueError", message=f"{FAILURE} {HOSTILE}", **IDS))
    subscriber(AgentFinishedEvent(output_preview=HOSTILE, **IDS))


async def hostile_everything(subscriber: OpenTelemetrySubscriber) -> None:
    await hostile_team_run(subscriber)
    await hostile_failures_and_governance(subscriber)


@pytest.fixture(params=WIRES)
def wire(request: pytest.FixtureRequest) -> str:
    return str(request.param)


async def test_with_content_capture_off_nothing_of_the_content_leaves(wire: str) -> None:
    exported = await export_run(wire, hostile_everything)
    assert exported.leaks(CANARIES) == []
    assert exported.leaks(SECRETS) == []
    assert exported.leaks(PII) == []
    assert exported.leaks(("CANARY", "hunter2", "REDACTED", "BEGIN PRIVATE")) == []

    # the trace itself is complete: structure, names, tokens, errors and governance
    names = {s.name for s in exported.spans}
    assert {
        "invoke_workflow desk",
        "invoke_agent lead",
        "invoke_agent worker",
        "execute_tool lookup",
        "execute_tool explode",
        "execute_tool delegate_task",
        "chat gpt-4o",
        "chat gpt-4o-mini",
        "invoke_agent broken",
    } <= names
    failed_tool = next(s for s in exported.spans if s.name == "execute_tool explode")
    assert failed_tool.status_code == "ERROR"
    assert failed_tool.status_message == "tool_error"
    assert failed_tool.attributes["error.type"] == "tool_error"
    broken = next(s for s in exported.spans if s.name == "invoke_agent broken")
    assert broken.attributes["error.type"] == "RuntimeError"
    [exception] = [e for e in broken.events if e.name == "exception"]
    assert dict(exception.attributes) == {"exception.type": "RuntimeError"}
    events = {e.name for s in exported.spans for e in s.events}
    assert {
        "awa.delegation",
        "awa.delegation_result",
        "awa.approval_requested",
        "awa.approval_decided",
        "awa.guardrail_blocked",
        "awa.handoff",
        "awa.agent_message",
        "awa.run_cancelled",
    } <= events


async def test_the_default_is_off_and_no_content_attribute_exists(wire: str) -> None:
    exported = await export_run(wire, hostile_everything)
    content_keys = {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "gen_ai.system_instructions",
        "gen_ai.tool.definitions",
    }
    assert not any(key in s.attributes for s in exported.spans for key in content_keys)
    captured = {
        "awa.task",
        "awa.content",
        "awa.args",
        "awa.note",
        "awa.reason",
        "exception.message",
    }
    for span in exported.spans:
        for event in span.events:
            if event.name != "awa.run_cancelled":  # its reason is the operator's, not content
                assert not captured & set(event.attributes), event.name


async def test_with_capture_on_the_content_is_there_and_secrets_are_redacted(wire: str) -> None:
    exported = await export_run(
        wire, hostile_everything, capture_content=True, redactor=SECRETS_REDACTOR
    )
    for canary in CANARIES:
        assert canary in exported.text, f"{canary} was not captured"
    assert exported.leaks(SECRETS) == []
    assert "[REDACTED:" in exported.text
    # SECRETS_REDACTOR is passed explicitly: it masks credentials only, personal data stays
    assert EMAIL in exported.text


async def test_with_capture_on_the_pii_redactor_also_masks_personal_data(wire: str) -> None:
    exported = await export_run(
        wire, hostile_everything, capture_content=True, redactor=RegexRedactor.pii()
    )
    for canary in CANARIES:
        assert canary in exported.text, f"{canary} was not captured"
    assert exported.leaks(SECRETS) == []
    assert exported.leaks(PII) == []
    for kind in ("email", "iban", "credit_card", "phone", "ipv4", "api_key", "jwt"):
        assert f"[REDACTED:{kind}]" in exported.text


async def test_the_checks_would_see_a_leak_without_a_redactor(wire: str) -> None:
    """Control: with `redactor=None` the same run does leak, so the other tests can fail."""

    exported = await export_run(wire, hostile_everything, capture_content=True, redactor=None)
    assert {API_KEY, JWT, DSN_PASSWORD, PASSWORD, GITHUB} <= set(exported.leaks(SECRETS))
    assert set(exported.leaks(PII)) == set(PII)


@pytest.mark.slow
async def test_captured_values_are_cut_after_redaction_so_no_secret_is_half_exported(
    wire: str,
) -> None:
    secret = "sk-proj-" + "Q" * 40

    async def play(subscriber: OpenTelemetrySubscriber) -> None:
        subscriber(AgentStartedEvent(input_preview=f"{'a' * 50} {secret} {'b' * 50}", **IDS))
        subscriber(
            ToolCallEvent(
                tool_name="t", call_id="c", arguments={"text": f"{'a' * 50} {secret}"}, **IDS
            )
        )
        subscriber(AgentFinishedEvent(output_preview="done", **IDS))

    for limit in range(40, 130, 7):  # every cut position around the secret
        exported = await export_run(wire, play, capture_content=True, max_content_chars=limit)
        assert "sk-proj" not in exported.text, limit
        assert "QQQQ" not in exported.text, limit


async def test_hostile_tool_arguments_of_every_shape_are_redacted(wire: str) -> None:
    class Holder:
        def __str__(self) -> str:
            return f"Holder(token={API_KEY})"

    arguments = {
        "nested": {
            "deep": [{"authorization": f"Bearer {BEARER}"}, [DSN, ({"password": PASSWORD},)]]
        },
        API_KEY: "a secret used as a key",
        "object": Holder(),
        "bytes": f"{GITHUB}".encode(),
        "unicode": f"schlüssel {AWS} ✓",
        "number": 12345,
    }

    async def play(subscriber: OpenTelemetrySubscriber) -> None:
        subscriber(AgentStartedEvent(**IDS))
        subscriber(ToolCallEvent(tool_name="t", call_id="c", arguments=arguments, **IDS))
        subscriber(AgentFinishedEvent(**IDS))

    exported = await export_run(wire, play, capture_content=True)
    assert exported.leaks((API_KEY, BEARER, DSN_PASSWORD, PASSWORD, GITHUB, AWS)) == []
    [call] = [s for s in exported.spans if s.name == "execute_tool t"]
    shown = json.loads(call.attributes["gen_ai.tool.call.arguments"])
    assert shown["number"] == 12345 and "✓" in shown["unicode"]


async def test_exception_text_of_a_failed_run_is_exported_only_with_capture_and_redacted(
    wire: str,
) -> None:
    async def play(subscriber: OpenTelemetrySubscriber) -> None:
        subscriber(AgentStartedEvent(**IDS))
        subscriber(
            ErrorEvent(error_type="ProviderError", message=f"{FAILURE} key {API_KEY}", **IDS)
        )

    off = await export_run(wire, play)
    assert off.leaks((FAILURE, API_KEY)) == []
    on = await export_run(wire, play, capture_content=True)
    assert FAILURE in on.text and API_KEY not in on.text


@pytest.mark.parametrize("capture", [False, True])
async def test_failed_tools_and_failed_runs_follow_the_conventions_too(
    wire: str, capture: bool
) -> None:
    async def play(subscriber: OpenTelemetrySubscriber) -> None:
        await hostile_team_run(subscriber)

        def explode_model(call: ModelCall) -> Any:
            raise RuntimeError(FAILURE)

        broken = Agent("broken", model=FunctionChatModel(explode_model), hooks=[subscriber])
        with pytest.raises(RuntimeError):
            await broken.run("Hi")

    exported = await export_run(wire, play, capture_content=capture)
    report = validate(exported.spans)
    assert report.ok, str(report)
    assert report.checks["G08"] == len(exported.spans)
    assert {s.status_code for s in exported.spans} == {"UNSET", "ERROR"}
    assert (report.checks["G10"] > 0) is capture


async def test_the_reviewer_and_who_cancelled_are_masked_personal_data_without_capture(
    wire: str,
) -> None:
    async def play(subscriber: OpenTelemetrySubscriber) -> None:
        subscriber(AgentStartedEvent(**IDS))
        subscriber(ApprovalDecidedEvent(tool="pay", decision="approve", reviewer=EMAIL, **IDS))
        subscriber(RunCancelledEvent(reason="stop", elapsed_ms=5, cancelled_by=EMAIL, **IDS))

    exported = await export_run(wire, play)

    assert exported.leaks((EMAIL,)) == []
    events = {e.name for s in exported.spans for e in s.events}
    assert {"awa.approval_decided", "awa.run_cancelled"} <= events
