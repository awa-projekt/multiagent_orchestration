"""Trace tags, `record_span`, and middleware hook nodes that record state keys instead of the conversation."""

from __future__ import annotations

import sys
from typing import Any

import pytest
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
from langchain_core.tracers.run_collector import RunCollectorCallbackHandler

from awa.agents import Agent
from awa.agents.middleware import node_input_summary
from awa.core.settings import Settings
from awa.observability import build_trace_tags, spans
from awa.testing import FakeChatModel


def test_build_trace_tags_omits_langsmith_tag_when_tracing_disabled() -> None:
    settings = Settings(langsmith_tracing=False)

    tags = build_trace_tags(settings, workflow="main")

    assert tags == ["workflow:main", f"env:{settings.env}"]
    assert "langsmith" not in tags


def test_build_trace_tags_adds_langsmith_tag_when_tracing_enabled() -> None:
    settings = Settings(langsmith_tracing=True)

    tags = build_trace_tags(settings, workflow="main")

    assert "langsmith" in tags


def test_record_span_is_a_noop_without_opentelemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "opentelemetry", None)  # an import now raises ImportError

    assert spans._tracer() is None
    spans.record_span("awa.test", {"mode": "x"})


def test_record_span_sets_attributes_and_leaves_none_out(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter, provider = InMemorySpanExporter(), TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(spans, "_tracer", lambda: provider.get_tracer("t"))

    spans.record_span("awa.test", {"mode": "strict", "count": 3, "missing": None})

    [span] = exporter.get_finished_spans()
    assert span.name == "awa.test"
    assert dict(span.attributes or {}) == {"mode": "strict", "count": 3}


SECRET = "SECRET-CONVERSATION-TEXT"


def _all_runs(collector: RunCollectorCallbackHandler) -> list[Any]:
    found: list[Any] = []
    pending = list(collector.traced_runs)
    while pending:
        run = pending.pop()
        found.append(run)
        pending.extend(run.child_runs)
    return found


def _hook_inputs(collector: RunCollectorCallbackHandler) -> dict[str, dict[str, Any]]:
    return {
        run.name: run.inputs
        for run in _all_runs(collector)
        if run.run_type == "chain" and ("before_" in run.name or "after_" in run.name)
    }


async def _run(agent: Agent) -> RunCollectorCallbackHandler:
    collector = RunCollectorCallbackHandler()
    graph = agent._compiled_graph(None)
    await graph.ainvoke({"messages": [("user", SECRET)]}, config={"callbacks": [collector]})
    return collector


async def test_builtin_middleware_hook_runs_record_keys_not_messages(monkeypatch: Any) -> None:
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    agent = Agent(
        "traced", model=FakeChatModel(["hi"]), middleware=[ModelCallLimitMiddleware(run_limit=3)]
    )

    collector = await _run(agent)

    inputs = _hook_inputs(collector)
    assert "ModelCallLimitMiddleware.before_model" in inputs  # a built-in middleware
    assert "AgentControlMiddleware.before_agent" in inputs  # an awa one
    for name, recorded in inputs.items():
        assert set(recorded) <= {"keys", "messages"}, name
        assert SECRET not in str(recorded), name
    model_runs = [run for run in _all_runs(collector) if run.run_type == "llm"]
    assert model_runs and SECRET in str(model_runs[0].inputs)


def test_a_middleware_with_its_own_policy_is_kept() -> None:
    from awa._internal.upstream.graph import TracePolicy

    own = TracePolicy(process_inputs=lambda value: {"mine": True})

    class Custom(AgentMiddleware):  # type: ignore[type-arg]
        trace_policy = own

        def before_model(self, state: Any, runtime: Any) -> None:
            return None

    custom = Custom()
    agent = Agent("traced", model=FakeChatModel(["hi"]), middleware=[custom])
    agent._compiled_graph(None)

    assert custom.trace_policy is own


def test_node_input_summary_of_a_non_mapping() -> None:
    assert node_input_summary("text") == {"type": "str"}
    assert node_input_summary({"messages": [1, 2], "a": 1}) == {
        "keys": ["a", "messages"],
        "messages": 2,
    }
