"""Offline tests for `examples/03_team.py`."""

from __future__ import annotations

import json
from typing import Any

from _helpers.examples import load_example, tool_results
from awa.contracts import DelegationEvent, DelegationResultEvent, RunCompletedEvent, RunEvent
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call


async def test_lead_delegates_in_parallel_and_reports_usage() -> None:
    team = load_example("03_team").build_market_desk()
    market, competitors = team.members

    def lead(call: ModelCall) -> Any:
        if call.index == 0:
            return [
                tool_call(
                    "delegate_task",
                    {"agent": "market_analyst", "task": "Size the home batteries market."},
                ),
                tool_call(
                    "delegate_task",
                    {"agent": "competitor_analyst", "task": "Profile home battery competitors."},
                ),
            ]
        results = "\n".join(tool_results(call.messages))
        assert "4.2" in results and "VoltHaus" in results
        return "Enter the market: EUR 4.2bn growing 18.5% a year; VoltHaus leads the premium end."

    recorded: list[RunEvent] = []
    with (
        team.orchestrator.override(model=FunctionChatModel(lead)),
        market.override(
            model=FakeChatModel(
                [
                    tool_call("market_figures", {"segment": "home batteries"}),
                    "Home batteries: EUR 4.2bn, 18.5% annual growth.",
                ]
            )
        ),
        competitors.override(
            model=FakeChatModel(
                [
                    tool_call("competitor_profiles", {"segment": "home batteries"}),
                    "VoltHaus: premium. Cellar Energy: low price.",
                ]
            )
        ),
    ):
        async for event in team.stream("Should we enter the home battery market in 2027?"):
            recorded.append(event)

    completed = recorded[-1]
    assert isinstance(completed, RunCompletedEvent)
    result = completed.result
    assert result.output.startswith("Enter the market")
    kinds = [type(e) for e in recorded if isinstance(e, DelegationEvent | DelegationResultEvent)]
    # Both delegations start before either returns: they ran concurrently.
    assert kinds == [DelegationEvent, DelegationEvent, DelegationResultEvent, DelegationResultEvent]
    assert set(result.usage.by_agent) == {"lead_analyst", "market_analyst", "competitor_analyst"}
    assert result.usage.requests == 6
    assert team.limits.max_parallel_delegations == 4


async def test_triage_hands_off_and_the_specialist_keeps_the_thread() -> None:
    example = load_example("03_team")
    billing_system = example.sample_billing_system()
    team = example.build_support_desk(billing_system)
    billing = team.members[0]

    triage_model = FakeChatModel(
        [tool_call("transfer_to_billing", {"reason": "Duplicate charge on an invoice."})]
    )

    def bill(call: ModelCall) -> Any:
        last = call.last_message
        if call.index == 0:
            assert "transferred the conversation to you" in str(last.content)
            return tool_call("lookup_invoice", {"invoice_id": "INV-2201"})
        if call.index == 1:
            assert json.loads(str(last.content))["times_charged"] == 2
            return "You were charged twice for INV-2201. Shall I refund the duplicate?"
        if call.index == 2:
            earlier = " ".join(str(m.content) for m in call.messages)
            assert "charged twice for invoice INV-2201" in earlier
            return tool_call("refund_duplicate_charge", {"invoice_id": "INV-2201"})
        return "Done: EUR 49.00 is on its way back to you."

    with (
        team.orchestrator.override(model=triage_model),
        billing.override(model=FunctionChatModel(bill)),
    ):
        first = await team.run("Hi, I was charged twice for invoice INV-2201.")
        follow_up = await team.run(
            "Yes please, refund the second charge.", thread_id=first.thread_id
        )

    assert first.handoff_path == ("triage", "billing")
    assert first.agent == "billing" and first.output.startswith("You were charged twice")
    assert follow_up.handoff_path == ("billing",)
    assert follow_up.output == "Done: EUR 49.00 is on its way back to you."
    assert billing_system.refunds == ["INV-2201"]
    assert triage_model.call_count == 1
    assert team.topology()["delegates"] == {}
