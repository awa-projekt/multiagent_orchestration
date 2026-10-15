"""The OpenTelemetry bridge on a real benchmark run, validated at a real collector.

The offline core benchmark (scripted models, fake network: the team with delegation and a peer
consult of scenario 1, the agent network of scenario 5, and the served approval flow of
scenario 6) runs with `OpenTelemetrySubscriber` as a global subscriber and exports over HTTP to a
local OTLP receiver on a free port (`tests/_helpers/otlp_collector.py`). The test then decodes the spans
the receiver got, as a backend would, and validates them against the GenAI semantic conventions
(`tests/_helpers/genai_conventions.py`: one named rule per convention, each checked and each shown to fail
on a seeded violation).

Two wires run: OTLP/JSON with an exporter that needs only the SDK, and OTLP protobuf with the real
`OTLPSpanExporter`, which is skipped when `opentelemetry-exporter-otlp-proto-http` and
`opentelemetry-proto` are not installed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402
from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: E402

import awa  # noqa: E402
from _helpers.genai_conventions import (  # noqa: E402
    METRIC_RULES,
    RULES,
    Report,
    known_values,
    validate,
    validate_metrics,
)
from _helpers.otlp_collector import WIRES, Collector, WireEvent, WireSpan, exporter  # noqa: E402
from awa.observability import OpenTelemetrySubscriber  # noqa: E402
from awa.runtime import use_global_subscriber  # noqa: E402

ROOT = Path(__file__).resolve().parents[2] / "examples" / "benchmarks" / "core"
for folder in (ROOT, ROOT.parent.parent):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import harness  # noqa: E402

CONVERSATION = "bench-thread-1"
RUNS = (  # (scenario, case): a team with delegation and consult, a network, a served approval
    ("01_market_brief", "linux-distributions"),
    ("05_agent_network", "q3-supplier-risk-memo"),
    ("06_automation", "large-invoice-approved"),
)


@dataclass
class Traced:
    """What one benchmark run exported: what the collector received, the metrics, the subscriber."""

    wire: str
    spans: list[WireSpan]
    raw: bytes
    content_types: set[str]
    paths: set[str]
    metrics: Any
    subscriber: OpenTelemetrySubscriber
    records: list[harness.RunRecord]


def run_traced(
    wire: str, runs: tuple[tuple[str, str], ...] = RUNS, **subscriber_options: Any
) -> Traced:
    """Run the offline benchmark `runs` and export its spans to a local collector over `wire`."""

    scenarios = {s.slug: s for s in harness.SCENARIOS}
    reader = InMemoryMetricReader()
    with Collector() as collector:
        provider = TracerProvider(resource=Resource({"service.name": "awa-benchmark"}))
        provider.add_span_processor(BatchSpanProcessor(exporter(wire, collector.endpoint)))
        meter = MeterProvider(metric_readers=[reader]).get_meter("awa")
        subscriber = OpenTelemetrySubscriber(
            provider.get_tracer("awa", awa.__version__),
            meter=meter,
            attributes={"gen_ai.conversation.id": CONVERSATION},
            **subscriber_options,
        )

        async def play() -> list[harness.RunRecord]:
            records: list[harness.RunRecord] = []
            with use_global_subscriber(subscriber):
                for slug, case in runs:
                    found, _ = await harness.run_scenario(
                        scenarios[slug],
                        1,
                        harness.Setup(offline=True),
                        harness.Ledger(10_000),
                        cases=[case],
                    )
                    records.extend(found)
            return records

        records = asyncio.run(play())
        subscriber.close()
        assert provider.force_flush(30_000)
        provider.shutdown()
        return Traced(
            wire=wire,
            spans=collector.spans,
            raw=collector.raw,
            content_types={r.content_type for r in collector.requests},
            paths={r.path for r in collector.requests},
            metrics=reader.get_metrics_data(),
            subscriber=subscriber,
            records=records,
        )


@pytest.fixture(scope="module", params=WIRES)
def traced(request: pytest.FixtureRequest) -> Traced:
    return run_traced(request.param)


def named(spans: list[WireSpan], name: str) -> list[WireSpan]:
    return [s for s in spans if s.name == name]


def children(spans: list[WireSpan], parent: WireSpan) -> list[WireSpan]:
    return [s for s in spans if s.parent_id == parent.span_id]


# ------------------------------------------------------------------------ the run and the wire


def test_the_benchmark_ran_and_the_collector_received_it_over_http(traced: Traced) -> None:
    assert [r.passed for r in traced.records] == [True, True, True], [
        (r.case, r.error, r.notes) for r in traced.records
    ]
    assert traced.paths == {"/v1/traces"}
    assert traced.content_types == {
        "application/json" if traced.wire == "json" else "application/x-protobuf"
    }
    assert traced.subscriber.open_spans == 0
    operations = {s.operation for s in traced.spans}
    assert operations == {"invoke_workflow", "invoke_agent", "chat", "execute_tool"}
    assert len(traced.spans) > 60
    assert len({s.trace_id for s in traced.spans}) >= 5  # one trace per top-level run


# ------------------------------------------------------------------------ the conventions


def test_spans_follow_the_genai_semantic_conventions(traced: Traced) -> None:
    report = validate(traced.spans)
    assert report.ok, str(report)
    unchecked = [r.id for r in RULES if not report.checks[r.id] and r.id not in ("G10",)]
    assert unchecked == []  # G10 (message content) needs content capture: see the next test


def test_content_spans_follow_the_conventions_too() -> None:
    traced = run_traced("json", RUNS[:1], capture_content=True)
    report = validate(traced.spans)
    assert report.ok, str(report)
    assert report.checks["G10"] > 20
    assert any("gen_ai.input.messages" in s.attributes for s in traced.spans)
    assert any("gen_ai.tool.call.arguments" in s.attributes for s in traced.spans)


def test_attributes_and_values_are_in_the_pinned_conventions(traced: Traced) -> None:
    known = known_values()
    used = {key for span in traced.spans for key in span.attributes if key.startswith("gen_ai.")}
    assert used - set(known["attributes"]) == set()
    assert {s.operation for s in traced.spans} <= set(known["operations"])
    metric_tokens = {
        dict(point.attributes)["gen_ai.token.type"]
        for resource in traced.metrics.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == "gen_ai.client.token.usage"
        for point in metric.data.data_points
    }
    assert metric_tokens == set(known["token_types"])


def test_the_metrics_follow_the_conventions(traced: Traced) -> None:
    report = validate_metrics(traced.metrics)
    assert report.ok, str(report)
    assert all(report.checks[r.id] for r in METRIC_RULES)


# ------------------------------------------------------------------------ what the trace shows


def test_the_team_run_shows_delegation_as_nested_agent_spans(traced: Traced) -> None:
    spans = traced.spans
    [workflow] = [s for s in named(spans, "invoke_workflow market-brief") if s.parent_id is None][
        :1
    ]
    [lead] = [s for s in children(spans, workflow) if s.name == "invoke_agent brief_lead"]
    nested = [
        s.attributes["gen_ai.agent.name"]
        for s in children(spans, lead)
        if s.operation == "invoke_agent"
    ]
    assert nested == ["researcher", "analyst", "writer"]
    tools = [s for s in children(spans, lead) if s.operation == "execute_tool"]
    assert [t.attributes["gen_ai.tool.name"] for t in tools] == ["delegate_task"] * 3
    assert all(t.attributes["gen_ai.agent.name"] == "brief_lead" for t in tools)
    delegations = [e for e in lead.events if e.name == "awa.delegation"]
    assert [e.attributes["awa.target"] for e in delegations] == nested
    # the analyst consults the researcher: a nested agent span inside a nested agent span
    [analyst] = [s for s in children(spans, lead) if s.name == "invoke_agent analyst"]
    assert [s.name for s in children(spans, analyst) if s.operation == "invoke_agent"] == [
        "invoke_agent researcher"
    ]
    [researcher] = [s for s in children(spans, lead) if s.name == "invoke_agent researcher"]
    assert {
        t.attributes["gen_ai.tool.name"]
        for t in children(spans, researcher)
        if t.operation == "execute_tool"
    } == {"web_search", "fetch_url"}


def test_the_usage_of_the_top_level_run_is_the_sum_of_its_chat_spans(traced: Traced) -> None:
    spans = traced.spans
    by_trace: dict[str, list[WireSpan]] = {}
    for span in spans:
        by_trace.setdefault(span.trace_id, []).append(span)
    for trace_spans in by_trace.values():
        [root] = [s for s in trace_spans if s.parent_id is None]
        for key in ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"):
            chats = sum(s.attributes[key] for s in trace_spans if s.operation == "chat")
            assert root.attributes[key] == chats, root.name
        if "awa.usage.input_tokens" in root.attributes:
            assert (
                root.attributes["awa.usage.input_tokens"]
                == root.attributes["gen_ai.usage.input_tokens"]
            )


def test_the_agent_network_is_one_workflow_with_its_agents_nested(traced: Traced) -> None:
    [workflow] = named(traced.spans, "invoke_workflow supplier-risk-network")
    agents = {s.attributes["gen_ai.agent.name"] for s in children(traced.spans, workflow)}
    assert {"risk_lead", "spend_analyst", "logistics_analyst", "risk_writer"} <= agents
    names = {e.name for e in workflow.events}
    assert {"awa.delegation", "awa.delegation_result", "awa.agent_message"} <= names
    tool_names = {
        s.attributes["gen_ai.tool.name"] for s in traced.spans if s.operation == "execute_tool"
    }
    assert {"create_tasks", "write_shared", "read_shared", "query_csv", "json_query"} <= tool_names


def test_the_served_path_shows_the_approval_and_the_resume(traced: Traced) -> None:
    events = [(s, e) for s in traced.spans for e in s.events if e.name.startswith("awa.approval")]
    requested = [(s, e) for s, e in events if e.name == "awa.approval_requested"]
    decided = [(s, e) for s, e in events if e.name == "awa.approval_decided"]
    assert requested and decided
    assert {e.attributes["awa.tool"] for _, e in requested + decided} == {"book_invoice"}
    assert {e.attributes["awa.decision"] for _, e in decided} == {"approve"}
    assert all(
        s.operation == "invoke_agent" and s.attributes["gen_ai.agent.name"] == "clerk"
        for s, _ in events
    )
    # the request and its decision are two runs (the process restarted in between): two traces
    assert {s.trace_id for s, _ in requested}.isdisjoint({s.trace_id for s, _ in decided})
    # the call that paused the run ends marked incomplete; nothing else does
    assert {s.name for s in traced.spans if s.attributes.get("awa.incomplete")} == {
        "execute_tool book_invoice"
    }
    booked = [s for s in traced.spans if s.name == "execute_tool book_invoice"]
    assert len(booked) >= 3


def test_content_is_not_in_the_benchmark_export_by_default(traced: Traced) -> None:
    assert not any(
        key in span.attributes
        for span in traced.spans
        for key in (
            "gen_ai.input.messages",
            "gen_ai.output.messages",
            "gen_ai.tool.call.arguments",
            "gen_ai.tool.call.result",
        )
    )
    # prompts, pages, payloads
    for text in (b"Debian", b"Wikipedia", b"RE-17", b"Beta GmbH", b"Acme"):
        assert text not in traced.raw


# ------------------------------------------------------------------------ the validator itself

Mutation = Callable[[list[WireSpan]], list[WireSpan]]


def edit(spans: list[WireSpan], pick: Callable[[WireSpan], bool], **changes: Any) -> list[WireSpan]:
    """A copy of `spans` where the first span matching `pick` has `changes` applied."""

    done = False
    result = []
    for span in spans:
        if not done and pick(span):
            attributes = changes.pop("attributes", None)
            drop = changes.pop("drop", ())
            if attributes is not None or drop:
                merged = {k: v for k, v in span.attributes.items() if k not in drop}
                merged.update(attributes or {})
                changes["attributes"] = merged
            span = dataclasses.replace(span, **changes)
            done = True
        result.append(span)
    assert done
    return result


def op(name: str) -> Callable[[WireSpan], bool]:
    return lambda s: s.operation == name


MUTATIONS: dict[str, Mutation] = {
    "G01": lambda s: edit(s, op("chat"), attributes={"gen_ai.operation.name": "frobnicate"}),
    "G02": lambda s: edit(s, op("execute_tool"), name="run the tool"),
    "G03": lambda s: edit(s, op("chat"), kind="SERVER"),
    "G04": lambda s: edit(s, op("chat"), drop=("gen_ai.provider.name",)),
    "G05": lambda s: edit(s, op("execute_tool"), drop=("gen_ai.agent.name",)),
    "G06": lambda s: edit(s, op("chat"), attributes={"gen_ai.usage.input_tokens": "many"}),
    "G07": lambda s: edit(s, op("execute_tool"), attributes={"gen_ai.usage.input_tokens": 5}),
    "G08": lambda s: edit(s, op("execute_tool"), status_code="ERROR"),
    "G09": lambda s: edit(s, op("chat"), attributes={"gen_ai.conversation.id": s[0].trace_id}),
    "G10": lambda s: edit(s, op("chat"), attributes={"gen_ai.output.messages": "not json"}),
    "T01": lambda s: edit(s, op("chat"), span_id="zz"),
    "T02": lambda s: edit(s, op("chat"), parent_id="0123456789abcdef"),
    "T03": lambda s: edit(
        s, op("chat"), parent_id=next(x.span_id for x in s if x.operation == "invoke_workflow")
    ),
    "T04": lambda s: edit(s, op("chat"), end_ns=1),
    "T05": lambda s: [
        x
        for x in s
        if x.parent_id != next(w.span_id for w in s if w.operation == "invoke_workflow")
    ],
    "T06": lambda s: [
        x
        for x in s
        if x.name != "invoke_agent researcher"
        or x.parent_id is None
        or x.parent_id not in {lead.span_id for lead in s if lead.name == "invoke_agent brief_lead"}
    ],
    "T07": lambda s: edit(
        s,
        lambda x: x.name == "invoke_agent brief_lead",
        attributes={"gen_ai.usage.output_tokens": 1},
    ),
    "T08": lambda s: edit(s, op("invoke_agent"), attributes={"awa.incomplete": True}),
    "T09": lambda s: edit(s, lambda x: bool(x.events), events=(WireEvent("weird", 0, {}),)),
    "T10": lambda s: edit(s, op("chat"), scope_name="somebody-else"),
}


@pytest.fixture(scope="module")
def sample() -> Iterator[list[WireSpan]]:
    """The first benchmark scenario's spans: small, with delegation, consult and tools."""

    yield run_traced("json", RUNS[:1]).spans


def test_the_validator_passes_the_real_run_and_every_rule_has_a_mutation(
    sample: list[WireSpan],
) -> None:
    assert validate(sample).ok
    assert set(MUTATIONS) == {r.id for r in RULES}


@pytest.mark.parametrize("rule", sorted(MUTATIONS))
def test_the_validator_fails_a_seeded_violation_of_each_rule(
    rule: str, sample: list[WireSpan]
) -> None:
    mutated = MUTATIONS[rule](list(sample))
    report: Report = validate(mutated)
    assert report.failed(rule), f"{rule} did not fail: {report}"


def metrics_of(
    *,
    unit: str = "{token}",
    attributes: dict[str, Any] | None = None,
    count: int = 3,
    drop: str = "",
) -> Any:
    """SDK-shaped metrics data with the two GenAI client metrics, for the validator's own tests."""

    point = SimpleNamespace(
        attributes=attributes
        or {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "openai",
            "gen_ai.token.type": "input",
        },
        count=count,
        sum=5.0,
    )
    metrics = [
        SimpleNamespace(
            name=name,
            unit=unit if name.endswith("usage") else "s",
            data=SimpleNamespace(data_points=[point]),
        )
        for name in ("gen_ai.client.token.usage", "gen_ai.client.operation.duration")
        if name != drop
    ]
    return SimpleNamespace(
        resource_metrics=[SimpleNamespace(scope_metrics=[SimpleNamespace(metrics=metrics)])]
    )


def test_the_metric_validator_passes_good_metrics_and_fails_each_seeded_violation() -> None:
    assert validate_metrics(metrics_of()).ok
    bad_attributes = {"gen_ai.operation.name": "chat", "gen_ai.token.type": "total"}
    assert validate_metrics(metrics_of(drop="gen_ai.client.operation.duration")).failed("M01")
    assert validate_metrics(metrics_of(unit="By")).failed("M01")
    assert validate_metrics(metrics_of(attributes=bad_attributes)).failed("M02")
    assert validate_metrics(metrics_of(count=0)).failed("M03")
    assert {r.id for r in METRIC_RULES} == {"M01", "M02", "M03"}
