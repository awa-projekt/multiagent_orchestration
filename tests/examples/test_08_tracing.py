"""Offline tests for `examples/08_tracing.py`."""

from __future__ import annotations

import pytest

from _helpers.examples import load_example
from awa.testing import FakeChatModel, tool_call


async def test_subscribers_see_every_event_and_presence_is_tracked(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    pytest.importorskip("opentelemetry.sdk")
    example = load_example("08_tracing")
    observed = example.build_observed_team()
    team = observed.team
    [monitor] = team.members
    lead_model = FakeChatModel(
        [
            tool_call("delegate_task", {"agent": "monitor", "task": "Health of checkout?"}),
            "Checkout is degraded: 4.2% errors and 1.85 s p95 latency.",
        ]
    )
    monitor_model = FakeChatModel(
        [tool_call("service_health", {"service": "checkout"}), "checkout: degraded, 4.2% errors."]
    )

    with (
        caplog.at_level("INFO", logger="awa.events"),
        team.orchestrator.override(model=lead_model),
        monitor.override(model=monitor_model),
    ):
        result = await team.run("Is checkout healthy right now?")

    assert result.output.startswith("Checkout is degraded")
    printed = capsys.readouterr().out
    assert "[on_call_lead] delegates to monitor: Health of checkout?" in printed
    assert "[monitor] calls service_health({'service': 'checkout'})" in printed

    types = observed.recorder.types()
    assert types[0] == "agent_started" and types[-1] == "agent_finished"
    assert {"delegation", "delegation_result", "tool_call", "tool_result"} <= set(types)
    assert any("delegation agent=on_call_lead" in r.getMessage() for r in caplog.records)

    spans = observed.spans.get_finished_spans()
    names = [span.name for span in spans]
    assert names.count("execute_tool service_health") == 1
    assert {"invoke_workflow on_call", "invoke_agent on_call_lead", "invoke_agent monitor"} <= set(
        names
    )
    assert all(span.status.status_code.name != "ERROR" for span in spans)

    presence = team.directory.presence("monitor")
    assert (presence.total_runs, presence.failed_runs, presence.status.value) == (1, 0, "available")
