"""Your first agent and your first team.

The order-status assistant looks orders up with a plain Python function
(awa turns it into a tool; the docstring becomes its description) and must
answer with an `OrderAnswer`, so callers get validated fields instead of prose.
The same agent is then streamed to show tool calls and text as they happen.

`quick.team` builds a team from the name of a process, the orchestrator
(`lead`) and the members with their tasks. Auto-matching gives every member the
builtin skills and tools its task calls for (the checker gets the
`data-validation` skill and the `calculator` tool) and only suggests tools that
would act outside the agent, such as sending email; the checker also brings a
tool of its own. `quick.explain` says what each agent got and why, and its
`blueprint` is the equivalent YAML.

The same team from the command line:
    awa new process "Invoice processing" --structure team --lead coordinator \\
        --agent "extractor=Reads incoming invoices and extracts the fields" \\
        --agent "checker=Validates the totals against the purchase order"
    awa show invoice_processing.yaml

Run:
    pip install -e ".[anthropic]"
    export ANTHROPIC_API_KEY=...
    python examples/01_first_agent.py
    # No model key? Run its offline tests instead:
    #   python -m pytest tests/examples/test_01_first_agent.py
"""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from _support import require_model_credentials
from awa import Agent, Team, quick, tool
from awa.contracts import RunCompletedEvent, TextDeltaEvent, ToolCallEvent, ToolResultEvent

ORDERS: dict[str, dict[str, str]] = {
    "A-1001": {"status": "shipped", "carrier": "ParcelNet", "eta": "2026-10-02"},
    "A-1002": {"status": "processing", "carrier": "", "eta": "2026-10-06"},
    "A-1003": {"status": "delayed", "carrier": "SwiftFreight", "eta": "2026-10-09"},
}
PURCHASE_ORDERS = {"PO-7": {"supplier": "Nordwind GmbH", "total": "1190.00 EUR"}}


def lookup_order(order_id: str) -> str:
    """Look up an order by id and return its status, carrier and estimated delivery date."""

    order = ORDERS.get(order_id.strip().upper())
    if order is None:
        return json.dumps({"order_id": order_id, "error": "unknown order id"})
    return json.dumps({"order_id": order_id.strip().upper(), **order})


class OrderAnswer(BaseModel):
    """The assistant's answer about one order."""

    order_id: str = Field(description="The order the customer asked about.")
    status: Literal["shipped", "processing", "delayed", "unknown"]
    estimated_delivery: str | None = Field(description="ISO date, if known.")
    reply: str = Field(description="A short, friendly reply to the customer.")
    needs_human: bool = Field(description="True if a support person should follow up.")


def build_order_assistant() -> Agent[None, OrderAnswer]:
    """Build the order-status assistant."""

    return Agent(
        "order_assistant",
        description="Answers questions about the status of customer orders.",
        instructions=(
            "You answer customers' questions about their orders. Always look the order up "
            "with lookup_order before answering; never guess a status or date. Set "
            "needs_human when an order is delayed or unknown."
        ),
        tools=[lookup_order],
        output_type=OrderAnswer,
    )


@tool
def lookup_purchase_order(po_number: str) -> str:
    """Look up a purchase order by its number (e.g. PO-7): supplier and total."""

    order = PURCHASE_ORDERS.get(po_number.strip().upper())
    if order is None:
        return f"No purchase order {po_number}."
    return f"{po_number}: supplier {order['supplier']}, total {order['total']}"


def build_invoice_desk(model: str | BaseChatModel | None = None) -> Team:
    """The invoice desk: a coordinator, an extractor and a checker with its own tool."""

    return quick.team(
        "Invoice processing",
        lead="coordinator",
        members={
            "extractor": "Reads incoming invoices and extracts the fields.",
            "checker": {
                "task": "Validates the totals against the purchase order.",
                "tools": [lookup_purchase_order],
            },
        },
        model=model,
    )


async def main() -> None:
    require_model_credentials()
    assistant = build_order_assistant()

    result = await assistant.run("Hi, where is my order a-1003?")
    answer = result.output
    print(f"{answer.order_id}: {answer.status} (ETA {answer.estimated_delivery})")
    print(f"Reply: {answer.reply}")
    print(f"Needs a human: {answer.needs_human}")
    print(f"Usage: {result.usage.requests} model calls, {result.usage.total_tokens} tokens\n")

    print("Streaming a second question:")
    async for event in assistant.stream("Has order A-1001 shipped yet?"):
        if isinstance(event, ToolCallEvent):
            print(f"  -> {event.tool_name}({event.arguments})")
        elif isinstance(event, ToolResultEvent):
            print(f"  <- {event.content}")
        elif isinstance(event, TextDeltaEvent):
            print(event.delta, end="", flush=True)
        elif isinstance(event, RunCompletedEvent):
            print(f"\n  result: {event.result.output!r}")

    desk = build_invoice_desk()
    explanation = quick.explain(desk)
    print(f"\n{explanation}\n")
    team_result = await desk.run(
        "Invoice INV-1042 from Nordwind GmbH, PO-7: 1000.00 EUR net + 19% VAT. Is it correct?"
    )
    print(team_result.output, end="\n\n")
    if explanation.blueprint is not None:
        print(f"== The same team as a blueprint\n{explanation.blueprint.to_yaml()}")


if __name__ == "__main__":
    asyncio.run(main())
