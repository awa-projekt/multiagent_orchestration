"""Langfuse attributes of the OpenTelemetry bridge and `connect_langfuse` (offline, local receiver)."""

from __future__ import annotations

import asyncio
import base64
import http.server
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")
pytest.importorskip("opentelemetry.exporter.otlp.proto.http")

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from _helpers import traced_team
from _helpers.otlp_collector import decode_protobuf  # noqa: E402
from awa.contracts.usage import ModelPrice, PriceTable  # noqa: E402
from awa.errors import ConfigurationError  # noqa: E402
from awa.observability import OpenTelemetrySubscriber, connect_langfuse  # noqa: E402


def run_demo(**options: Any) -> list[ReadableSpan]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing = OpenTelemetrySubscriber(provider.get_tracer("test"), **options)
    asyncio.run(traced_team.run_scenario(traced_team.build_team(hooks=[tracing])))
    tracing.close()
    return list(exporter.get_finished_spans())


def attr(span: ReadableSpan, name: str) -> Any:
    return (span.attributes or {}).get(name)


def test_langfuse_mode_types_every_span_so_the_agent_graph_can_be_drawn() -> None:
    spans = run_demo(langfuse=True)

    kinds = {(s.name.split(" ")[0], attr(s, "langfuse.observation.type")) for s in spans}
    assert ("invoke_workflow", "chain") in kinds
    assert ("invoke_agent", "agent") in kinds
    assert ("chat", "generation") in kinds
    assert ("execute_tool", "tool") in kinds
    assert all(attr(s, "langfuse.observation.type") for s in spans)
    agents = {s.name for s in spans if attr(s, "langfuse.observation.type") == "agent"}
    assert {"invoke_agent lead", "invoke_agent researcher", "invoke_agent payments"} <= agents


def test_langfuse_mode_counts_tokens_only_on_model_calls() -> None:
    spans = run_demo(langfuse=True)

    run_spans = [s for s in spans if s.name.startswith("invoke_")]
    assert run_spans
    assert not any(attr(s, "gen_ai.usage.input_tokens") is not None for s in run_spans)
    chats = [s for s in spans if s.name.startswith("chat")]
    roots = [
        s for s in run_spans if attr(s, "awa.depth") == 0 and s.name.startswith("invoke_workflow")
    ]
    first_trace_chats = [c for c in chats if c.context.trace_id == roots[0].context.trace_id]
    assert sum(attr(c, "gen_ai.usage.input_tokens") for c in first_trace_chats) == attr(
        roots[0], "awa.subtree.input_tokens"
    )


def test_langfuse_mode_sends_approvals_delegations_and_handoffs_as_event_observations() -> None:
    spans = run_demo(langfuse=True)

    events = [s for s in spans if attr(s, "langfuse.observation.type") == "event"]
    names = {s.name for s in events}
    assert {
        "awa.delegation",
        "awa.delegation_result",
        "awa.handoff",
        "awa.approval_requested",
        "awa.approval_decided",
    } <= names
    by_id = {s.context.span_id: s for s in spans}
    for event in events:
        assert event.start_time == event.end_time
        parent = by_id[event.parent.span_id]  # type: ignore[union-attr]
        assert parent.name.startswith("invoke_")
    requested = next(s for s in events if s.name == "awa.approval_requested")
    assert attr(requested, "awa.tool") == "issue_refund"
    decided = next(s for s in events if s.name == "awa.approval_decided")
    assert attr(decided, "awa.decision") == "approve"
    assert attr(decided, "awa.reviewer") == "ops-lead"


def test_without_langfuse_mode_no_langfuse_observation_attribute_is_set() -> None:
    spans = run_demo()

    assert not any(attr(s, "langfuse.observation.type") for s in spans)
    assert not any(s.name.startswith("awa.") for s in spans)
    assert not any(attr(s, "gen_ai.usage.cost") is not None for s in spans)


def test_session_user_tags_and_trace_name_are_on_every_span() -> None:
    spans = run_demo(session_id="s-1", user_id="u-1", tags=["a", "b"], trace_name="refund-desk")

    assert spans
    for span in spans:
        assert attr(span, "session.id") == "s-1"
        assert attr(span, "gen_ai.conversation.id") == "s-1"
        assert attr(span, "user.id") == "u-1"
        assert list(attr(span, "langfuse.trace.tags")) == ["a", "b"]
        assert attr(span, "langfuse.trace.name") == "refund-desk"


def test_every_model_call_carries_its_cost_when_prices_are_given() -> None:
    prices = PriceTable(
        prices={traced_team.MODEL_NAME: ModelPrice(input_per_mtok=1000.0, output_per_mtok=2000.0)}
    )

    spans = run_demo(langfuse=True, prices=prices)

    chats = [s for s in spans if s.name.startswith("chat")]
    assert chats
    for span in chats:
        expected = (
            attr(span, "gen_ai.usage.input_tokens") * 1000.0
            + attr(span, "gen_ai.usage.output_tokens") * 2000.0
        ) / 1_000_000
        assert attr(span, "gen_ai.usage.cost") == pytest.approx(expected)
        assert attr(span, "awa.usage.cost") == pytest.approx(expected)


def test_a_model_without_a_price_does_not_lose_the_span() -> None:
    prices = PriceTable(prices={"other-model": ModelPrice(input_per_mtok=1.0, output_per_mtok=1.0)})

    spans = run_demo(langfuse=True, prices=prices)

    chats = [s for s in spans if s.name.startswith("chat")]
    assert chats and all(attr(s, "gen_ai.usage.cost") is None for s in chats)


class Receiver:
    """A local HTTP server that records the requests an exporter sends to it."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        self.attempts: list[str] = []
        # (path, attempt number for that path) -> (seconds to wait, status); a status other
        # than 200 is not recorded as received.
        self.respond: Any = None
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                headers = {k.lower(): v for k, v in self.headers.items()}
                outer.attempts.append(self.path)
                if outer.respond is not None:
                    delay, status = outer.respond(self.path, outer.attempts.count(self.path))
                    time.sleep(delay)
                    if status != 200:
                        self.send_response(status)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                outer.requests.append((self.path, headers, body))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def spans(self) -> list[Any]:
        return [s for _, _, body in self.requests for s in decode_protobuf(body)]


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    rec = Receiver()
    rec.thread.start()
    yield rec
    rec.server.shutdown()
    rec.server.server_close()


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_BASE_URL",
        "LANGFUSE_HOST",
        "LANGFUSE_RELEASE",
        "LANGFUSE_TRACING_ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)


def play(connection: Any) -> None:
    asyncio.run(traced_team.run_scenario(traced_team.build_team(hooks=[connection.subscriber])))
    connection.shutdown()


def test_connect_reads_the_standard_variables_and_sends_to_langfuses_otlp_path(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", receiver.url + "/")

    connection = connect_langfuse(session_id="s-9", user_id="u-9", tags=["t"])
    play(connection)

    assert connection.endpoint == receiver.url + "/api/public/otel/v1/traces"
    assert receiver.requests, "nothing reached the server"
    path, headers, body = receiver.requests[0]
    assert path == "/api/public/otel/v1/traces"
    expected = base64.b64encode(b"pk-lf-test:sk-lf-test").decode()
    assert headers["authorization"] == f"Basic {expected}"
    assert headers["x-langfuse-ingestion-version"] == "4"
    spans = receiver.spans()
    kinds = {s.attributes.get("langfuse.observation.type") for s in spans}
    assert kinds >= {"chain", "agent", "generation", "tool"}
    assert all(s.attributes.get("session.id") == "s-9" for s in spans)
    assert all(s.attributes.get("user.id") == "u-9" for s in spans)
    assert b"sk-lf-test" not in body
    assert "sk-lf-test" not in repr(connection)


def test_langfuse_host_is_the_fallback_of_base_url(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGFUSE_HOST", receiver.url)

    connection = connect_langfuse(public_key="pk", secret_key="sk")

    assert connection.endpoint == receiver.url + "/api/public/otel/v1/traces"
    connection.shutdown()


def test_without_a_server_setting_langfuse_cloud_is_used() -> None:
    connection = connect_langfuse(public_key="pk", secret_key="sk")

    assert connection.endpoint == "https://cloud.langfuse.com/api/public/otel/v1/traces"
    connection.shutdown()


def test_missing_keys_are_a_clear_error() -> None:
    with pytest.raises(ConfigurationError, match="LANGFUSE_PUBLIC_KEY"):
        connect_langfuse()
    with pytest.raises(ConfigurationError, match="LANGFUSE_SECRET_KEY"):
        connect_langfuse(public_key="pk")


@pytest.mark.parametrize(
    "base_url", ["localhost:3000", "ftp://host", "file:///etc/passwd", "http://"]
)
def test_a_server_address_that_is_not_http_is_refused(base_url: str) -> None:
    with pytest.raises(ConfigurationError, match="http"):
        connect_langfuse(public_key="pk", secret_key="sk", base_url=base_url)


def test_environment_is_set_on_resource_and_spans(receiver: Receiver) -> None:
    connection = connect_langfuse(
        public_key="pk", secret_key="sk", base_url=receiver.url, environment="staging"
    )
    play(connection)

    spans = receiver.spans()
    assert spans
    assert all(s.attributes.get("langfuse.environment") == "staging" for s in spans)
    assert all(s.resource.get("langfuse.environment") == "staging" for s in spans)


def test_release_version_and_environment_reach_every_span(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LANGFUSE_RELEASE", "v2026.10")
    monkeypatch.setenv("LANGFUSE_TRACING_ENVIRONMENT", "staging")

    connection = connect_langfuse(
        public_key="pk", secret_key="sk", base_url=receiver.url, version="1.4.0"
    )
    play(connection)

    spans = receiver.spans()
    assert spans
    for span in spans:
        assert span.attributes.get("langfuse.release") == "v2026.10"
        assert span.attributes.get("langfuse.environment") == "staging"
        assert span.attributes.get("langfuse.version") == "1.4.0"
        assert span.attributes.get("langfuse.trace.metadata.awa_version")


@pytest.mark.parametrize(
    ("capture_content", "comment"),
    [(False, "case c1"), (True, "clear answer for [REDACTED:email]")],
)
def test_eval_results_reach_the_trace_of_their_run_as_scores(
    receiver: Receiver, capture_content: bool, comment: str
) -> None:
    import json

    from awa.contracts.events import AgentStartedEvent
    from awa.evals.report import CaseResult, EvalReport
    from awa.evals.scorers import Score

    connection = connect_langfuse(
        public_key="pk", secret_key="sk", base_url=receiver.url, capture_content=capture_content
    )
    events: list[Any] = []
    team = traced_team.build_team(hooks=[connection.subscriber, events.append])
    asyncio.run(traced_team.run_scenario(team))
    root = next(e for e in events if isinstance(e, AgentStartedEvent) and e.parent_run_id is None)
    trace_id = connection.subscriber.trace_id(root.run_id)
    assert trace_id is not None and len(trace_id) == 32

    assert connection.score("unknown-run", "x", 1.0) is False
    case = CaseResult(
        case_id="c1",
        status="passed",
        scores=[
            Score(name="judge", value=0.8, passed=True, reason="clear answer for jane@example.com")
        ],
    ).attach(trace=None, events=events)
    report = EvalReport(
        name="demo", system="team", dataset="demo", started_at="2026-10-08T00:00:00Z", cases=[case]
    )
    assert connection.score_eval(report) == 2
    connection.shutdown()

    scores = [
        (headers, json.loads(body))
        for path, headers, body in receiver.requests
        if path == "/api/public/scores"
    ]
    assert [body["name"] for _, body in scores] == ["eval.passed", "eval.judge"]
    passed, judge = (body for _, body in scores)
    assert passed == {
        "traceId": trace_id,
        "name": "eval.passed",
        "value": 1,
        "dataType": "BOOLEAN",
        "comment": "case c1",
    }
    assert judge["value"] == 0.8 and judge["dataType"] == "NUMERIC"
    assert judge["comment"] == comment
    expected = base64.b64encode(b"pk:sk").decode()
    assert all(headers["authorization"] == f"Basic {expected}" for headers, _ in scores)


TRACES = "/api/public/otel/v1/traces"


@pytest.fixture
def quick_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("awa.observability.langfuse._RETRY_INITIAL_DELAY", 0.05)
    monkeypatch.setattr("awa.observability.langfuse._RETRY_MAX_DELAY", 0.1)


def test_a_slow_span_export_is_tried_again_and_reaches_langfuse(
    receiver: Receiver, quick_retries: None
) -> None:
    # The first export answers only after the timeout (a busy server), the next one at once.
    receiver.respond = lambda path, n: (1.0 if path == TRACES and n == 1 else 0.0, 200)
    connection = connect_langfuse(
        public_key="pk", secret_key="sk", base_url=receiver.url, export_timeout_seconds=0.3
    )
    play(connection)

    assert receiver.attempts.count(TRACES) >= 2
    assert receiver.spans(), "the retried batch did not arrive"
    assert connection.dropped_spans == 0


def test_spans_still_failing_are_dropped_with_one_counted_warning(
    receiver: Receiver, quick_retries: None, caplog: pytest.LogCaptureFixture
) -> None:
    receiver.respond = lambda path, n: (0.0, 503 if path == TRACES else 200)
    connection = connect_langfuse(
        public_key="pk", secret_key="sk", base_url=receiver.url, export_timeout_seconds=0.3
    )
    with caplog.at_level("WARNING", logger="awa.tracing"):
        play(connection)

    assert connection.dropped_spans > 0
    warnings = [r.getMessage() for r in caplog.records if r.name == "awa.tracing"]
    assert warnings and all("they are dropped" in w for w in warnings)
    assert f"({connection.dropped_spans} dropped so far)" in warnings[-1]
    assert "after 3 tries" in warnings[-1]


def test_the_export_timeout_comes_from_the_setting(
    receiver: Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.core.settings import get_settings

    monkeypatch.setenv("AWA_TRACE_EXPORT_TIMEOUT", "12.5")
    get_settings.cache_clear()
    try:
        connection = connect_langfuse(public_key="pk", secret_key="sk", base_url=receiver.url)
    finally:
        get_settings.cache_clear()
    assert connection._timeout == 12.5
    connection.shutdown()
    with pytest.raises(ConfigurationError, match="positive"):
        connect_langfuse(
            public_key="pk", secret_key="sk", base_url=receiver.url, export_timeout_seconds=0
        )


def _traced_run(connection: Any) -> str:
    from awa.contracts.events import AgentStartedEvent

    events: list[Any] = []
    team = traced_team.build_team(hooks=[connection.subscriber, events.append])
    asyncio.run(traced_team.run_scenario(team))
    return next(
        e.run_id for e in events if isinstance(e, AgentStartedEvent) and e.parent_run_id is None
    )


def test_a_score_is_sent_again_after_a_timeout_or_server_error(
    receiver: Receiver, quick_retries: None
) -> None:
    scores = "/api/public/scores"
    replies = {1: (1.0, 200), 2: (0.0, 503)}
    receiver.respond = lambda path, n: replies.get(n, (0.0, 200)) if path == scores else (0, 200)
    connection = connect_langfuse(
        public_key="pk", secret_key="sk", base_url=receiver.url, export_timeout_seconds=0.3
    )
    run_id = _traced_run(connection)

    assert connection.score(run_id, "helpful", 0.9) is True
    assert receiver.attempts.count(scores) == 3
    connection.shutdown()


def test_a_refused_score_is_not_sent_again(
    receiver: Receiver, quick_retries: None, caplog: pytest.LogCaptureFixture
) -> None:
    scores = "/api/public/scores"
    receiver.respond = lambda path, n: (0.0, 400 if path == scores else 200)
    connection = connect_langfuse(
        public_key="pk", secret_key="sk", base_url=receiver.url, export_timeout_seconds=0.3
    )
    run_id = _traced_run(connection)

    with caplog.at_level("WARNING", logger="awa.langfuse"):
        assert connection.score(run_id, "helpful", 0.9) is False
    assert receiver.attempts.count(scores) == 1
    assert "refused the score 'helpful' (HTTP 400)" in caplog.text
    connection.shutdown()
