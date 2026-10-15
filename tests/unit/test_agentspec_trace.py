"""The agent spec in effect is visible per run: `awa.agent.*` attributes on the agent's span."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from awa.agents import Agent
from awa.blueprints import Blueprint
from awa.models import ModelRegistry
from awa.observability import OpenTelemetrySubscriber
from awa.runtime import use_global_subscriber
from awa.team import Team
from awa.testing import FakeChatModel, structured_output, tool_call

EXAMPLES = Path(__file__).resolve().parents[1] / "fixtures" / "agentspec" / "examples"


@pytest.fixture
def otel() -> Iterator[tuple[TracerProvider, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    provider.shutdown()


def _spans(exporter: InMemorySpanExporter, name: str) -> list[Any]:
    return [s for s in exporter.get_finished_spans() if s.name == name]


async def test_a_structured_agent_run_carries_its_spec(otel: Any) -> None:
    provider, exporter = otel
    bp = Blueprint.from_yaml_file(EXAMPLES / "worker.yaml")
    models = ModelRegistry()
    answer = {"lines": [], "status": "escalate", "reason": "no lines"}
    models.register("default", FakeChatModel([[structured_output("CheckInvoiceOutput", answer)]]))
    agent = bp.build_agent("check_invoice", model_registry=models)
    assert isinstance(agent, Agent)

    with use_global_subscriber(OpenTelemetrySubscriber(provider.get_tracer("t"))):
        await agent.run("Invoice INV-1 has no lines.")

    [span] = _spans(exporter, "invoke_agent check_invoice")
    attrs = dict(span.attributes or {})
    assert attrs["awa.agent.role"] == "worker"
    assert attrs["awa.agent.template"] == "awa.system/1"
    assert attrs["awa.agent.tools"] == "builtin:read_csv"
    assert attrs["awa.agent.skills"] == "invoice-reconciliation"
    assert "awa.agent.selection_ids" not in attrs
    prompt = agent._static_prompt  # type: ignore[attr-defined]
    assert attrs["awa.agent.prompt_hash"] == hashlib.sha256(prompt.encode()).hexdigest()[:16]
    values = " ".join(str(v) for v in attrs.values())
    assert "sk-" not in values and "@" not in values
    # The spec belongs to the agent's span, not to every span of the trace.
    assert all("awa.agent.tools" not in (s.attributes or {}) for s in _spans(exporter, "chat"))


async def test_a_nested_agent_does_not_inherit_the_spec_of_its_parent(otel: Any) -> None:
    provider, exporter = otel
    member = Agent("member", model=FakeChatModel(["done"]), description="Does it.")
    lead = Agent(
        "lead",
        model=FakeChatModel(
            [[tool_call("delegate_task", {"agent": "member", "task": "do it"})], "finished"]
        ),
        description="Leads.",
        metadata={"awa.agent.template": "awa.system/1", "awa.agent.tools": "x"},
    )
    team = Team("t", orchestrator=lead, members=[member])

    with use_global_subscriber(OpenTelemetrySubscriber(provider.get_tracer("t"))):
        await team.run("go")

    lead_span = _spans(exporter, "invoke_agent lead")
    member_span = _spans(exporter, "invoke_agent member")
    assert lead_span and dict(lead_span[0].attributes or {})["awa.agent.tools"] == "x"
    assert member_span and "awa.agent.tools" not in (member_span[0].attributes or {})
