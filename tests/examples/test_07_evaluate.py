"""Offline test of `examples/07_evaluate.py` and `examples/evals/support_triage_cases.yaml`.

Every agent of the support desk (and the model judge) runs on one
`FunctionChatModel` registered as the `default` profile, which plays each
role from the tools it is offered. A "regressed" variant of the desk stops
delegating billing questions, and the comparison with the baseline catches it.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

from _helpers.examples import EXAMPLES_DIR, load_example
from awa.evals import EvalReport, JudgeVerdict
from awa.models import ModelRegistry
from awa.testing import FunctionChatModel, ModelCall, structured_output, tool_call


def request(messages: tuple[BaseMessage, ...]) -> str:
    return next(str(m.content) for m in messages if isinstance(m, HumanMessage))


def results_of(messages: tuple[BaseMessage, ...], tool: str) -> list[str]:
    return [str(m.content) for m in messages if isinstance(m, ToolMessage) and m.name == tool]


class SupportDesk:
    """Plays triage, the specialists, refunds and the judge, chosen by the bound tools."""

    def __init__(self, *, route_billing: bool = True) -> None:
        self.route_billing = route_billing

    def __call__(self, call: ModelCall) -> Any:
        tools = set(call.tool_names)
        if "JudgeVerdict" in tools:
            return self.judge(call)
        if "transfer_to_refunds" in tools:
            return self.triage(call)
        if "issue_refund" in tools:
            return self.refunds(call)
        if "track_shipment" in tools:
            return self.orders(call)
        if "lookup_invoice" in tools:
            return self.billing(call)
        raise AssertionError(f"unexpected agent with tools {sorted(tools)}")

    def triage(self, call: ModelCall) -> Any:
        text = request(call.messages)
        answers = results_of(call.messages, "delegate_task")
        if answers:
            bodies = [re.sub(r"</?result[^>]*>", "", answer).strip() for answer in answers]
            return "Thanks for reaching out! " + " ".join(bodies)
        if re.search(r"broken|money back|refund", text, re.IGNORECASE):
            return tool_call("transfer_to_refunds", {"reason": "refund request"})
        calls = [
            tool_call(
                "delegate_task", {"agent": "order_specialist", "task": f"Status of order {n}"}
            )
            for n in re.findall(r"order (\d{4})", text, re.IGNORECASE)
        ]
        invoices = re.findall(r"INV-\d{4}", text)
        if invoices and not self.route_billing:
            return "Your invoice looks fine to me."
        calls += [
            tool_call("delegate_task", {"agent": "billing_specialist", "task": f"Check {i}"})
            for i in invoices
        ]
        return calls

    def orders(self, call: ModelCall) -> Any:
        order_id = re.findall(r"\d{4}", request(call.messages))[-1]
        looked_up = results_of(call.messages, "lookup_order")
        if not looked_up:
            return tool_call("lookup_order", {"order_id": order_id})
        if looked_up[0].startswith("No order"):
            return f"Order {order_id} cannot be found."
        order = json.loads(looked_up[0])
        tracked = results_of(call.messages, "track_shipment")
        if not tracked:
            return tool_call("track_shipment", {"shipment_id": order["shipment"]})
        shipment = json.loads(tracked[0])
        when = shipment.get("expected_on") or shipment.get("delivered_on")
        return f"Order {order_id} is {order['status']} ({shipment['carrier']}, {when})."

    def billing(self, call: ModelCall) -> Any:
        invoice_id = re.findall(r"INV-\d{4}", request(call.messages))[-1]
        looked_up = results_of(call.messages, "lookup_invoice")
        if not looked_up:
            return tool_call("lookup_invoice", {"invoice_id": invoice_id})
        invoice = json.loads(looked_up[0])
        return f"Invoice {invoice_id} is {'paid' if invoice['paid'] else 'unpaid (open)'}."

    def refunds(self, call: ModelCall) -> Any:
        looked_up = results_of(call.messages, "lookup_order")
        if not looked_up:
            return tool_call("lookup_order", {"order_id": "1001"})
        order = json.loads(looked_up[0])
        return tool_call(
            "issue_refund", {"order_id": "1001", "amount": order["total"], "reason": "broken"}
        )

    def judge(self, call: ModelCall) -> Any:
        prompt = str(call.last_message.content)
        response = prompt.split("<response>")[-1]
        good = "1002" in response
        return structured_output(
            JudgeVerdict, {"score": 0.9 if good else 0.3, "reasoning": "answers the order question"}
        )


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch) -> SupportDesk:
    role = SupportDesk()
    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(role))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    load_example("07_evaluate")
    sys.modules["blueprints.support_tools"].REFUNDS.clear()
    return role


async def test_the_support_desk_passes_every_case(desk: SupportDesk) -> None:
    example = load_example("07_evaluate")
    dataset = example.load_cases()
    assert [case.id for case in dataset] == [
        "order-in-transit",
        "invoice-paid",
        "order-and-invoice",
        "unknown-order",
        "refund-needs-approval",
    ]

    report = await example.run_evaluation()

    assert report.pass_rate == 1.0, report.to_markdown()
    assert example.gate(report) == []
    by_id = {result.case_id: result for result in report.cases}
    assert by_id["order-in-transit"].tool_calls == [
        "delegate_task",
        "lookup_order",
        "track_shipment",
    ]
    refund = by_id["refund-needs-approval"]
    assert refund.interrupted and refund.handoff_path == ("triage", "refunds")
    assert sys.modules["blueprints.support_tools"].REFUNDS == []  # nothing paid out
    judge = report.scorer("judge")
    assert judge is not None and judge.count == 2
    assert report.judge_usage.requests == 2
    assert "eval_judge" not in report.usage.by_agent
    assert report.usage.by_agent.keys() >= {"triage", "order_specialist", "billing_specialist"}


async def test_a_change_that_stops_routing_billing_is_caught(
    desk: SupportDesk, tmp_path: Path
) -> None:
    example = load_example("07_evaluate")
    baseline_path = (await example.run_evaluation()).save(tmp_path / "baseline.json")
    baseline = EvalReport.from_json(baseline_path)

    desk.route_billing = False  # the "change": triage now answers billing questions itself
    report = await example.run_evaluation()

    problems = example.gate(report, baseline)
    assert "regression: invoice-paid / delegated:billing_specialist" in problems
    assert "regression: invoice-paid / tool:lookup_invoice" in problems
    assert any(problem.startswith("pass rate 60%") for problem in problems)
    comparison = report.compare(baseline)
    assert {change.case_id for change in comparison.regressions} == {
        "invoice-paid",
        "order-and-invoice",
    }
    assert not comparison.improvements


def test_the_cli_runs_the_same_evaluation(
    desk: SupportDesk, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from awa.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(
        [
            "eval",
            str(EXAMPLES_DIR / "blueprints" / "support_triage.yaml"),
            str(EXAMPLES_DIR / "evals" / "support_triage_cases.yaml"),
            "--format",
            "markdown",
            "--tools",
            str(EXAMPLES_DIR / "blueprints" / "support_tools.py"),
            "--scorer",
            "max_tokens:60000",
            "--json",
            str(tmp_path / "report.json"),
        ]
    )
    assert args.handler(args) == 0, capsys.readouterr().out
    assert "- Pass rate: **100.0%** (5/5 runs)" in capsys.readouterr().out
