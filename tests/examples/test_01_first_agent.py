"""Offline tests for `examples/01_first_agent.py`."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import ToolMessage

from _helpers.examples import load_example, system_prompt
from awa import Blueprint, Team, quick
from awa.contracts import RunCompletedEvent, RunEvent, ToolCallEvent
from awa.testing import FunctionChatModel, ModelCall, structured_output, tool_call


async def collect(stream: Any) -> list[RunEvent]:
    return [event async for event in stream]


def order_model() -> FunctionChatModel:
    """Looks the order up, then answers from the tool result."""

    answer_type = load_example("01_first_agent").OrderAnswer

    def respond(call: ModelCall) -> Any:
        if not isinstance(call.last_message, ToolMessage):
            order_id = re.search(r"[Aa]-\d{4}", str(call.last_message.content))
            assert order_id is not None
            return tool_call("lookup_order", {"order_id": order_id.group()})
        order = json.loads(str(call.last_message.content))
        return structured_output(
            answer_type,
            {
                "order_id": order["order_id"],
                "status": order["status"],
                "estimated_delivery": order["eta"],
                "reply": f"Your order is {order['status']}.",
                "needs_human": order["status"] == "delayed",
            },
        )

    return FunctionChatModel(respond)


async def test_agent_answers_with_typed_output_from_its_tool() -> None:
    example = load_example("01_first_agent")
    assistant = example.build_order_assistant()

    with assistant.override(model=order_model()):
        result = await assistant.run("Where is my order a-1003?")

    answer = result.output
    assert isinstance(answer, example.OrderAnswer)
    assert (answer.order_id, answer.status, answer.estimated_delivery) == (
        "A-1003",
        "delayed",
        "2026-10-09",
    )
    assert answer.needs_human is True
    assert result.usage.requests == 2 and result.usage.tool_calls >= 1
    assert assistant.card.tools == ("lookup_order",)


async def test_stream_yields_tool_events_and_the_final_result() -> None:
    example = load_example("01_first_agent")
    assistant = example.build_order_assistant()

    with assistant.override(model=order_model()):
        events = await collect(assistant.stream("Has order A-1001 shipped yet?"))

    calls = [e for e in events if isinstance(e, ToolCallEvent)]
    assert [c.tool_name for c in calls][0] == "lookup_order"
    completed = events[-1]
    assert isinstance(completed, RunCompletedEvent)
    assert completed.result.output.status == "shipped"


def test_quick_team_delegates_to_a_checker_that_uses_its_own_and_matched_tools() -> None:
    def respond(call: ModelCall) -> Any:
        results = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]
        if "Validates the totals" in system_prompt(call).split("\n")[1]:
            if not results:
                return tool_call("lookup_purchase_order", {"po_number": "PO-7"})
            if len(results) == 1:
                return tool_call("calculator", {"expression": "1000 * 1.19"})
            return f"PO total 1190.00 EUR; invoice total {results[-1]} EUR: they match."
        if not results:
            return tool_call("delegate_task", {"agent": "checker", "task": "Check INV-1042."})
        return f"INV-1042 is correct. {results[-1]}"

    desk = load_example("01_first_agent").build_invoice_desk(model=FunctionChatModel(respond))
    result = desk.run_sync("Invoice INV-1042, PO-7: 1000.00 EUR net + 19% VAT. Correct?")

    assert isinstance(desk, Team)
    assert "they match" in result.output and "1190" in result.output
    report = quick.explain(desk)
    checker = report.for_agent("checker")
    assert checker.explicit_tools == ("lookup_purchase_order",)
    assert checker.tools == ("builtin:calculator",)
    assert "data-validation" in checker.skills
    assert report.for_agent("coordinator").skills[0] == "orchestration-planning"
    assert report.blueprint is not None
    again = Blueprint.from_yaml_text(report.blueprint.to_yaml())
    assert again.agent("checker").tools == ["lookup_purchase_order"] and again.auto
