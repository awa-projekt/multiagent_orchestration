"""Offline tests for `examples/05_approvals_and_limits.py`."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from _helpers.examples import load_example, tool_results
from awa.contracts import UsageLimits
from awa.testing import FakeChatModel, tool_call


def refund_script(amount: float, reply: str) -> FakeChatModel:
    return FakeChatModel(
        [
            tool_call(
                "issue_refund",
                {"order_id": "A-1002", "amount_eur": amount, "reason": "never received"},
            ),
            reply,
        ]
    )


async def test_large_refund_pauses_until_approved() -> None:
    example = load_example("05_approvals_and_limits")
    gateway = example.PaymentsGateway()
    agent = example.build_refunds_agent(gateway)

    with agent.override(model=refund_script(480, "Your EUR 480 refund is on its way.")):
        paused = await agent.run("Refund the full EUR 480 for order A-1002.")
        assert paused.is_interrupted and paused.output is None
        request = paused.interrupts[0]["value"]
        assert (request["tool"], request["args"]["amount_eur"]) == ("issue_refund", 480)
        assert gateway.refunds == []

        result = await agent.resume(paused.thread_id, {"action": "approve"})

    assert result.output == "Your EUR 480 refund is on its way."
    assert gateway.refunds == [("A-1002", 480.0)]


async def test_rejected_refund_is_reported_to_the_agent() -> None:
    example = load_example("05_approvals_and_limits")
    gateway = example.PaymentsGateway()
    agent = example.build_refunds_agent(gateway)
    model = refund_script(480, "Sorry, this refund could not be approved.")

    with agent.override(model=model):
        paused = await agent.run("Refund the full EUR 480 for order A-1002.")
        result = await agent.resume(
            paused.thread_id, {"action": "reject", "note": "needs a manager's sign-off"}
        )

    assert result.output == "Sorry, this refund could not be approved."
    assert "Reason: needs a manager's sign-off." in str(model.calls[1].last_message.content)
    assert gateway.refunds == []


async def test_small_refund_runs_without_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    example = load_example("05_approvals_and_limits")
    gateway = example.PaymentsGateway()
    agent = example.build_refunds_agent(gateway)

    async def never_asked(pending: dict[str, Any]) -> dict[str, str]:
        raise AssertionError(f"unexpected approval request: {pending}")

    monkeypatch.setattr(example, "ask_reviewer", never_asked)
    with agent.override(model=refund_script(25, "Refunded EUR 25.")):
        result = await example.handle(agent, "Please refund EUR 25 for order A-1002.")

    assert result.output == "Refunded EUR 25."
    assert gateway.refunds == [("A-1002", 25.0)]


async def test_hardened_tools_report_errors_and_truncate_output() -> None:
    example = load_example("05_approvals_and_limits")
    agent = example.build_support_agent()
    model = FakeChatModel(
        [
            tool_call("account_lookup", {"customer_id": "C-404"}),
            tool_call("account_lookup", {"customer_id": "C-17"}),
            tool_call("search_help_center", {"query": "refund"}),
            "Annual plans can be refunded within 30 days.",
        ]
    )

    with agent.override(model=model):
        reply = await example.answer(agent, "I'm customer C-17. Can I get a refund?")

    assert reply == "Annual plans can be refunded within 30 days."
    failed, found, articles = tool_results(model.calls[3].messages)
    assert failed.startswith("Error: KeyError:")
    assert json.loads(found)["plan"] == "business"
    assert re.search(r"… \[truncated \d+ characters\]$", articles)
    assert len(articles) < 450


async def test_input_guardrail_blocks_before_any_model_call() -> None:
    example = load_example("05_approvals_and_limits")
    agent = example.build_support_agent()
    model = FakeChatModel(["never used"])

    with agent.override(model=model):
        reply = await example.answer(agent, "My password is hunter2, please reset it.")

    assert reply == "Blocked by secrets: contains blocked term: 'password'"
    assert model.call_count == 0


async def test_output_guardrail_blocks_email_addresses() -> None:
    example = load_example("05_approvals_and_limits")
    agent = example.build_support_agent()

    with agent.override(model=FakeChatModel(["Write to billing@example.com for refunds."])):
        reply = await example.answer(agent, "Who handles refunds?")

    assert reply == "Blocked by no_email_addresses: the answer contains an email address"


async def test_usage_limits_and_max_steps_stop_runaway_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The scripted model repeats one search; let max_steps end it, not the no-progress stop.
    monkeypatch.setenv("AWA_MAX_IDENTICAL_CALLS", "100")
    example = load_example("05_approvals_and_limits")
    agent = example.build_support_agent()
    search = tool_call("search_help_center", {"query": "refund"})

    with agent.override(model=FakeChatModel([search])):
        limited = await example.answer(agent, "How do refunds work?", UsageLimits(max_requests=1))
    assert limited == "Stopped: Usage limit exceeded: requests=2 > max_requests=1"

    looping = FakeChatModel([search], repeat_last=True)
    with agent.override(model=looping):
        stopped = await example.answer(agent, "How do refunds work?")
    assert stopped.startswith(
        f"Stopped: Agent 'support_agent' exceeded max_steps={example.MAX_STEPS}"
    )
    assert looping.call_count == example.MAX_STEPS
