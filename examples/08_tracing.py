"""Observability: hooks, event logs, OpenTelemetry spans and live presence in the directory.

Every run of an agent or team is one stream of typed events. This example
attaches four subscribers to a small team: a custom `AgentHooks` subclass
that prints delegations and tool calls, an `EventLogger` writing one log line
per event, an `EventRecorder` keeping events for inspection, and an
`OpenTelemetrySubscriber` turning runs, model calls and tool calls into
OpenTelemetry spans (here collected by the SDK's in-memory exporter;
`connect_langfuse()` sends the same spans to Langfuse, see docs/guide/langfuse.md).
The team's directory tracks each agent's presence (runs, failures, average
duration) from the same events.

Run:
    pip install -e ".[anthropic,otel]" opentelemetry-sdk
    export ANTHROPIC_API_KEY=...
    python examples/08_tracing.py
    # No model key? Run its offline tests instead:
    #   python -m pytest tests/examples/test_08_tracing.py
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from _support import require_model_credentials
from awa import Agent, AgentHooks, Team
from awa.contracts import DelegationEvent, ErrorEvent, ToolCallEvent
from awa.observability import EventLogger, EventRecorder, OpenTelemetrySubscriber

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

SERVICE_STATUS = {
    "checkout": {"status": "degraded", "error_rate_pct": 4.2, "p95_latency_ms": 1850},
    "search": {"status": "healthy", "error_rate_pct": 0.1, "p95_latency_ms": 210},
}


def service_health(service: str) -> str:
    """Return the current status, error rate and p95 latency of a service."""

    return json.dumps({"service": service, **SERVICE_STATUS.get(service, {"status": "unknown"})})


class ConsoleHooks(AgentHooks):
    """Prints what the team is doing, as it happens."""

    async def on_delegation(self, event: DelegationEvent) -> None:
        print(f"[{event.agent}] delegates to {event.target}: {event.task}")

    async def on_tool_call(self, event: ToolCallEvent) -> None:
        print(f"[{event.agent}] calls {event.tool_name}({event.arguments})")

    async def on_error(self, event: ErrorEvent) -> None:
        print(f"[{event.agent}] failed: {event.error_type}: {event.message}")


@dataclass(frozen=True)
class ObservedTeam:
    """The team, plus the recorder and the exporter collecting its spans."""

    team: Team
    recorder: EventRecorder
    spans: InMemorySpanExporter


def build_observed_team() -> ObservedTeam:
    """Build an on-call team with every observability subscriber attached."""

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    recorder = EventRecorder()
    spans = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    tracing = OpenTelemetrySubscriber(provider.get_tracer("on_call"))
    monitor = Agent(
        "monitor",
        description="Reads live service health metrics.",
        instructions="Report the health of the services you are asked about, with figures.",
        tools=[service_health],
        capabilities=["monitoring"],
    )
    on_call = Agent(
        "on_call_lead",
        description="Answers questions about the state of production.",
        instructions="Delegate metric lookups to the monitor, then answer in two sentences.",
    )
    team = Team(
        "on_call",
        orchestrator=on_call,
        members=[monitor],
        hooks=[ConsoleHooks(), EventLogger(), recorder, tracing],
    )
    return ObservedTeam(team=team, recorder=recorder, spans=spans)


async def main() -> None:
    require_model_credentials()
    logging.basicConfig(level=logging.INFO, format="%(name)s %(message)s")
    observed = build_observed_team()

    result = await observed.team.run("Is checkout healthy right now?")
    print(f"\n{result.output}\n")

    print(f"{len(observed.recorder)} events: {sorted(set(observed.recorder.types()))}")
    for span in observed.spans.get_finished_spans():
        attributes = span.attributes or {}
        tokens = attributes.get("gen_ai.usage.output_tokens", "-")
        duration_ms = ((span.end_time or 0) - (span.start_time or 0)) / 1e6
        status = span.status.status_code.name
        print(f"span {span.name:<32} {status:<5} {duration_ms:>8.1f} ms  output_tokens={tokens}")
    for name in observed.team.directory.names():
        presence = observed.team.directory.presence(name)
        print(
            f"{name:<14} {presence.status.value:<9} runs={presence.total_runs} "
            f"failed={presence.failed_runs} avg_ms={presence.average_duration_ms}"
        )


if __name__ == "__main__":
    asyncio.run(main())
