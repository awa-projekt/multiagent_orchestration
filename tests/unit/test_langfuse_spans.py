"""What a run puts on its spans for Langfuse, and `connect_langfuse` with its masking, metadata and transport rules."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("opentelemetry.exporter.otlp.proto.http")
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from _helpers import traced_team
from _helpers.fake_secrets import fake_secret
from _helpers.langfuse_receiver import Receiver
from awa import Agent, Team, tool
from awa.errors import ConfigurationError
from awa.kernel.redaction import RegexRedactor
from awa.observability import OpenTelemetrySubscriber, connect_langfuse
from awa.testing import FakeChatModel, tool_call

LANGFUSE_VARIABLES = (
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
    "LANGFUSE_HOST",
    "AWA_LANGFUSE_TRACING",
    "AWA_LANGFUSE_CAPTURE_CONTENT",
)


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in LANGFUSE_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env of the developer's checkout


def tracer() -> tuple[Any, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def demo_spans(**options: Any) -> list[ReadableSpan]:
    """The spans of the scripted demo (pause for an approval, approve, finish)."""

    test_tracer, exporter = tracer()
    tracing = OpenTelemetrySubscriber(test_tracer, **options)
    asyncio.run(traced_team.run_scenario(traced_team.build_team(hooks=[tracing])))
    tracing.close()
    return list(exporter.get_finished_spans())


def attr(span: ReadableSpan, name: str) -> Any:
    return (span.attributes or {}).get(name)


def named(spans: list[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name == name]


def test_the_request_metadata_of_a_run_is_on_every_span_of_both_traces() -> None:
    spans = demo_spans()

    assert len({s.context.trace_id for s in spans}) == 2  # paused run and resumed run
    for span in spans:
        assert attr(span, "awa.tenant") == "demo-shop", span.name
        assert attr(span, "awa.request_id") == "req-demo-1", span.name
        assert attr(span, "awa.actor") == "demo-user", span.name


def test_the_thread_becomes_the_session_and_the_actor_the_user() -> None:
    spans = demo_spans()

    threads = {attr(s, "awa.thread_id") for s in spans}
    assert len(threads) == 1 and str(next(iter(threads))).startswith("thread_")
    thread = next(iter(threads))
    assert all(attr(s, "session.id") == thread for s in spans)
    assert all(attr(s, "user.id") == "demo-user" for s in spans)


def test_an_explicit_session_and_user_win_over_the_run_metadata() -> None:
    spans = demo_spans(session_id="chat-42", user_id="u-7")

    assert all(attr(s, "session.id") == "chat-42" for s in spans)
    assert all(attr(s, "user.id") == "u-7" for s in spans)
    assert all(str(attr(s, "awa.thread_id")).startswith("thread_") for s in spans)


def test_only_scalar_awa_values_of_the_metadata_are_sent() -> None:
    test_tracer, exporter = tracer()
    tracing = OpenTelemetrySubscriber(test_tracer)
    agent = Agent("solo", model=FakeChatModel(["done"]), hooks=[tracing])
    metadata = {
        "awa.tenant": "acme",
        "awa.attempt": 2,
        "awa.blob": {"nested": "value"},
        "awa.actor": {"id": "alice", "email": "alice@example.com"},
        "customer_email": "bob@example.com",
    }

    asyncio.run(agent.run("hi", metadata=metadata))

    (span,) = named(list(exporter.get_finished_spans()), "invoke_agent solo")
    assert attr(span, "awa.tenant") == "acme"
    assert attr(span, "awa.attempt") == 2
    assert attr(span, "awa.actor") == "alice"
    assert attr(span, "awa.blob") is None
    assert not any("bob@example.com" in str(v) for v in (span.attributes or {}).values())
    assert not any("alice@example.com" in str(v) for v in (span.attributes or {}).values())


def test_metadata_values_are_redacted_and_cut() -> None:
    test_tracer, exporter = tracer()
    tracing = OpenTelemetrySubscriber(test_tracer, redactor=RegexRedactor.pii())
    agent = Agent("solo", model=FakeChatModel(["done"]), hooks=[tracing])
    metadata = {"awa.actor": "jane.doe@example.com", "awa.note": "x" * 1000}

    asyncio.run(agent.run("hi", metadata=metadata))

    (span,) = named(list(exporter.get_finished_spans()), "invoke_agent solo")
    assert "jane.doe@example.com" not in json.dumps(dict(span.attributes or {}))
    assert attr(span, "user.id") == "[REDACTED:email]"
    assert len(attr(span, "awa.note")) == 256


def test_the_declared_role_is_on_the_agent_span_and_not_inherited() -> None:
    spans = demo_spans(langfuse=True)

    (lead,) = named(spans, "invoke_agent lead")
    assert attr(lead, "awa.agent_role") == "orchestrator"
    assert attr(lead, "langfuse.observation.metadata.agent_role") == "orchestrator"
    (researcher,) = named(spans, "invoke_agent researcher")
    assert attr(researcher, "awa.agent_role") == "specialist"
    team = named(spans, "invoke_workflow desk")
    assert team and all(attr(s, "awa.agent_role") is None for s in team)
    assert all(attr(s, "awa.agent_role") is None for s in spans if s.name.startswith("chat"))


def test_a_nested_agent_without_a_role_does_not_show_the_role_of_its_caller() -> None:
    test_tracer, exporter = tracer()
    tracing = OpenTelemetrySubscriber(test_tracer)
    helper = Agent("helper", description="Helps.", model=FakeChatModel(["helped"]))
    lead = Agent(
        "lead",
        role="orchestrator",
        model=FakeChatModel(
            [tool_call("delegate_task", {"agent": "helper", "task": "help"}), "done"]
        ),
    )
    team = Team("crew", orchestrator=lead, members=[helper], hooks=[tracing])

    asyncio.run(team.run("go"))

    spans = list(exporter.get_finished_spans())
    (lead_span,) = named(spans, "invoke_agent lead")
    (helper_span,) = named(spans, "invoke_agent helper")
    assert attr(lead_span, "awa.agent_role") == "orchestrator"
    assert attr(helper_span, "awa.agent_role") is None


def test_langfuse_mode_sends_the_metadata_and_the_run_id_as_trace_metadata() -> None:
    spans = demo_spans(langfuse=True)

    roots = [s for s in spans if s.name == "invoke_workflow desk"]
    assert len(roots) == 2
    for span in spans:
        assert attr(span, "langfuse.trace.metadata.tenant") == "demo-shop", span.name
        assert attr(span, "langfuse.trace.metadata.request_id") == "req-demo-1"
        assert str(attr(span, "langfuse.trace.metadata.thread_id")).startswith("thread_")
        root = next(r for r in roots if r.context.trace_id == span.context.trace_id)
        assert attr(span, "langfuse.trace.metadata.run_id") == attr(root, "awa.run_id")
        assert attr(span, "langfuse.trace.metadata.agent_role") is None


def test_langfuse_mode_names_the_trace_after_the_top_level_run() -> None:
    spans = demo_spans(langfuse=True)

    assert {attr(s, "langfuse.trace.name") for s in spans} == {"desk"}
    named_spans = demo_spans(langfuse=True, trace_name="refund desk")
    assert {attr(s, "langfuse.trace.name") for s in named_spans} == {"refund desk"}


def test_without_langfuse_mode_no_trace_metadata_or_level_is_sent() -> None:
    spans = demo_spans()

    for span in spans:
        names = set(span.attributes or {})
        assert not any(name.startswith("langfuse.") for name in names), span.name


def test_a_failed_tool_call_has_the_level_error() -> None:
    spans = demo_spans(langfuse=True)

    lookups = named(spans, "execute_tool lookup_order")
    failed = [s for s in lookups if attr(s, "error.type") == "tool_error"]
    assert len(failed) == 1
    assert attr(failed[0], "langfuse.observation.level") == "ERROR"
    assert attr(failed[0], "langfuse.observation.status_message") == "tool_error"
    ok = [s for s in lookups if s is not failed[0]]
    assert ok and attr(ok[0], "langfuse.observation.level") is None


def test_the_error_text_is_only_sent_with_content_capture() -> None:
    hidden = demo_spans(langfuse=True)
    shown = demo_spans(langfuse=True, capture_content=True)

    def status(spans: list[ReadableSpan]) -> str:
        (failed,) = [
            s
            for s in named(spans, "execute_tool lookup_order")
            if attr(s, "langfuse.observation.level") == "ERROR"
        ]
        return str(attr(failed, "langfuse.observation.status_message"))

    assert "A-1" not in status(hidden)
    assert "no order A-1" in status(shown)


def test_a_failed_run_has_the_level_error() -> None:
    def explode(order_id: str) -> str:
        """Fail."""

        raise RuntimeError(f"backend down for {order_id}")

    test_tracer, exporter = tracer()
    tracing = OpenTelemetrySubscriber(test_tracer, langfuse=True)
    agent = Agent(
        "fragile",
        tools=[explode],
        model=FakeChatModel([tool_call("explode", {"order_id": "A-7"})]),
        hooks=[tracing],
    )

    with pytest.raises(RuntimeError):
        asyncio.run(agent.run("go"))

    (span,) = named(list(exporter.get_finished_spans()), "invoke_agent fragile")
    assert attr(span, "langfuse.observation.level") == "ERROR"
    assert attr(span, "langfuse.observation.status_message") == "RuntimeError"


def test_the_tool_call_that_waits_for_an_approval_has_the_level_warning() -> None:
    spans = demo_spans(langfuse=True)

    refunds = named(spans, "execute_tool issue_refund")
    assert len(refunds) == 2  # the paused call and the approved call of the resumed run
    waiting = [s for s in refunds if attr(s, "awa.waiting_for_approval")]
    assert len(waiting) == 1
    assert attr(waiting[0], "langfuse.observation.level") == "WARNING"
    assert attr(waiting[0], "langfuse.observation.status_message") == "waiting for approval"
    assert attr(waiting[0], "awa.incomplete") is True
    done = next(s for s in refunds if s is not waiting[0])
    assert attr(done, "langfuse.observation.level") is None


def test_the_approved_tool_call_of_the_resumed_run_shows_its_arguments() -> None:
    refunds = named(demo_spans(capture_content=True), "execute_tool issue_refund")
    done = next(s for s in refunds if not attr(s, "awa.waiting_for_approval"))

    assert json.loads(attr(done, "gen_ai.tool.call.arguments")) == {
        "order_id": "A-7",
        "amount": 80.0,
    }
    assert "Refunded 80.00 EUR" in attr(done, "gen_ai.tool.call.result")

    plain = named(demo_spans(), "execute_tool issue_refund")
    assert all(attr(s, "gen_ai.tool.call.arguments") is None for s in plain)


def test_an_eval_case_that_pauses_keeps_both_traces_in_the_thread_session() -> None:
    from awa.evals.runner import isolated_run

    test_tracer, exporter = tracer()
    tracing = OpenTelemetrySubscriber(test_tracer, langfuse=True)
    team = traced_team.build_team(hooks=[tracing])

    run = asyncio.run(isolated_run(team, traced_team.TASK, decisions=[{"action": "approve"}]))
    tracing.close()

    assert run.error is None and run.result is not None and len(run.segments) == 2
    spans = list(exporter.get_finished_spans())
    assert len({s.context.trace_id for s in spans}) == 2
    assert {attr(s, "session.id") for s in spans if s.parent is None} == {run.result.thread_id}


def test_a_response_that_fails_the_output_check_is_a_generation_with_its_tokens() -> None:
    from langchain_core.messages import AIMessage
    from pydantic import BaseModel

    class Total(BaseModel):
        total: int

    usage = {"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200}
    test_tracer, exporter = tracer()
    tracing = OpenTelemetrySubscriber(test_tracer, langfuse=True)
    agent = Agent(
        "typed",
        output_type=Total,
        model=FakeChatModel(
            [
                AIMessage(content='{"reason": "no total"}', usage_metadata=usage),
                AIMessage(content='{"total": 3}', usage_metadata=usage),
            ],
            profile={"structured_output": True},
        ),
        hooks=[tracing],
    )

    result = asyncio.run(agent.run("Add the numbers."))
    tracing.close()

    assert result.output == Total(total=3) and result.usage.requests == 2
    chats = [s for s in exporter.get_finished_spans() if s.name.split()[0] == "chat"]
    assert [attr(s, "gen_ai.usage.input_tokens") for s in chats] == [1000, 1000]
    assert sum(attr(s, "gen_ai.usage.output_tokens") for s in chats) == result.usage.output_tokens


def test_a_generation_lasts_as_long_as_the_model_call_not_the_preparation_before_it() -> None:
    from datetime import UTC, datetime, timedelta

    from awa.contracts import AgentStartedEvent, ModelResponseEvent

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    ids = {"run_id": "r1", "root_run_id": "r1", "agent": "clerk"}
    test_tracer, exporter = tracer()
    tracing = OpenTelemetrySubscriber(test_tracer, langfuse=True)
    tracing(AgentStartedEvent(**ids, timestamp=t0))
    tracing(
        ModelResponseEvent(**ids, timestamp=t0 + timedelta(seconds=5), model="m", duration_ms=2000)
    )
    tracing(ModelResponseEvent(**ids, timestamp=t0 + timedelta(seconds=6), model="m"))
    tracing.close()

    first, second = sorted(
        named(list(exporter.get_finished_spans()), "chat m"), key=lambda s: s.end_time or 0
    )
    assert ((first.end_time or 0) - (first.start_time or 0)) / 1e9 == 2.0
    assert (
        (second.end_time or 0) - (second.start_time or 0)
    ) / 1e9 == 1.0  # no duration: since the last step


SEARCH_TEXT = "overdue invoices of Hans Beispiel in Regensburg"


def test_without_content_capture_no_mail_address_or_search_text_is_in_any_span() -> None:
    @tool
    def search_customers(query: str, notify: str) -> str:
        """Search the customer records."""

        return f"3 matches for {query}; notified {notify}"

    test_tracer, exporter = tracer()
    tracing = OpenTelemetrySubscriber(test_tracer, langfuse=True)  # default: no content
    agent = Agent(
        "clerk",
        tools=[search_customers],
        model=FakeChatModel(
            [
                tool_call(
                    "search_customers", {"query": SEARCH_TEXT, "notify": "anna.muster@example.com"}
                ),
                f"Found 3 for {SEARCH_TEXT}, told anna.muster@example.com.",
            ]
        ),
        hooks=[tracing],
    )
    metadata = {"awa.actor": "anna.muster@example.com", "awa.request_id": "req-1"}

    asyncio.run(
        agent.run(f"Find {SEARCH_TEXT} and mail anna.muster@example.com", metadata=metadata)
    )

    spans = list(exporter.get_finished_spans())
    assert spans
    for span in spans:
        values = json.dumps(dict(span.attributes or {}), default=str)
        values += json.dumps([dict(e.attributes or {}) for e in span.events], default=str)
        values += str(span.status.description or "")
        assert "@example.com" not in values, span.name
        assert "Regensburg" not in values and "Hans Beispiel" not in values, span.name


def test_an_event_observation_shows_its_details_as_input() -> None:
    spans = demo_spans(langfuse=True)

    (decided,) = named(spans, "awa.approval_decided")
    shown = json.loads(attr(decided, "langfuse.observation.input"))
    assert shown["decision"] == "approve" and shown["reviewer"] == "ops-lead"
    assert shown["tool"] == "issue_refund" and shown["agent"] == "payments"
    assert "note" not in shown  # content: only with capture_content


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    rec = Receiver()
    rec.thread.start()
    yield rec
    rec.server.shutdown()
    rec.server.server_close()


PERSONAL_TASK = (
    "Customer jane.doe@example.com (IBAN DE89 3704 0044 0532 0130 00) asks for a refund; "
    f"the shop token is {fake_secret('sk-', 'ant-api03-abcdefghijklmnopqrstuvwxyz0123456789')}."
)


def play_personal(connection: Any) -> None:
    team = traced_team.build_team(hooks=[connection.subscriber])

    async def scenario() -> None:
        paused = await team.run(PERSONAL_TASK, metadata={"awa.actor": "jane.doe@example.com"})
        assert paused.thread_id is not None
        await team.resume(paused.thread_id, {"action": "approve", "reviewer": "ops-lead"})

    asyncio.run(scenario())
    connection.shutdown()


def test_connect_langfuse_masks_credentials_and_personal_data_by_default(
    receiver: Receiver,
) -> None:
    connection = connect_langfuse(
        public_key="pk-lf-test",
        secret_key="sk-lf-secret",
        base_url=receiver.url,
        capture_content=True,
    )

    play_personal(connection)

    body = b"".join(receiver.bodies)
    assert receiver.spans(), "nothing reached the server"
    for leaked in (
        b"jane.doe@example.com",
        b"DE89 3704 0044 0532 0130 00",
        fake_secret("sk-", "ant-api03-abcdefghijklmnopqrstuvwxyz0123456789").encode(),
        b"sk-lf-secret",
    ):
        assert leaked not in body, leaked
    assert b"[REDACTED:email]" in body
    assert b"Customer" in body  # the content itself was captured


def test_connect_langfuse_without_a_redactor_sends_content_unchanged(receiver: Receiver) -> None:
    connection = connect_langfuse(
        public_key="pk", secret_key="sk", base_url=receiver.url, capture_content=True, redactor=None
    )

    play_personal(connection)

    assert b"jane.doe@example.com" in b"".join(receiver.bodies)


def test_connect_langfuse_sends_its_metadata_as_trace_metadata_redacted(receiver: Receiver) -> None:
    connection = connect_langfuse(
        public_key="pk",
        secret_key="sk",
        base_url=receiver.url,
        metadata={"blueprint": "refund-desk", "owner": "ops@example.com", "version": 3},
    )

    play_personal(connection)

    spans = receiver.spans()
    assert spans
    for span in spans:
        assert span.attributes.get("langfuse.trace.metadata.blueprint") == "refund-desk"
        assert span.attributes.get("langfuse.trace.metadata.owner") == "[REDACTED:email]"
        assert span.attributes.get("langfuse.trace.metadata.version") == 3


@pytest.mark.parametrize(
    "base_url", ["http://langfuse.example.com", "http://10.0.0.5:3000", "http://langfuse-web:3000"]
)
def test_plain_http_to_another_computer_is_refused(base_url: str) -> None:
    with pytest.raises(ConfigurationError, match="https"):
        connect_langfuse(public_key="pk", secret_key="sk", base_url=base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://[::1]:3000",
        "https://lf.example.com",
    ],
)
def test_https_and_this_computer_are_accepted(base_url: str) -> None:
    connect_langfuse(public_key="pk", secret_key="sk", base_url=base_url).shutdown()


def test_plain_http_inside_a_private_network_needs_an_explicit_choice() -> None:
    connection = connect_langfuse(
        public_key="pk", secret_key="sk", base_url="http://langfuse-web:3000", allow_http=True
    )

    assert connection.endpoint == "http://langfuse-web:3000/api/public/otel/v1/traces"
    connection.shutdown()
