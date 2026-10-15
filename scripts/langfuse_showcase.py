"""Send the invoice desk of the guided tour to Langfuse, for the tracing pictures of the docs.

    python scripts/langfuse_showcase.py

The run uses scripted models and costs nothing: an invoice checker reads invoice INV-2042 from the
ledger, a payment clerk releases it; the release from 5,000 EUR waits for the head of finance, who
approves, and the run resumes on the same thread. It has a hard model-call limit (`--cap`).

Needs an installed awa with the extra `langfuse` (the script imports the installed package, not
the tree's `src`) and, in the environment, the LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and
LANGFUSE_BASE_URL of a Langfuse project (a local one:
docs/develop/observability.md#langfuse); their values are never printed.
All data is invented. The run sends its prompts, replies and tool arguments and results (content
capture is on for this demo project only), masked for credentials and personal data.

Cost: the spans carry the cost of each model call computed from `PRICES` below. They are
ILLUSTRATIVE prices, not what any provider charges: put the prices you pay there.

Exit code 0 when the run finished, 1 otherwise, 2 when a Langfuse key is missing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any

from awa import Agent, RunResult, Sequential, UsageLimits, tool
from awa.contracts.events import ModelResponseEvent, RunEvent
from awa.contracts.usage import ModelPrice, PriceTable
from awa.persistence import StateInMemorySaver
from awa.runtime import use_global_subscriber
from awa.testing import FakeChatModel, tool_call
from awa.tools import create_tool_approval_wrapper

TENANT = "demo-shop"
MAX_REQUESTS = 6
"""Hard model-call limit of the run (it stops when it is reached)."""
PRICES = PriceTable(prices={}, default=ModelPrice(input_per_mtok=1.25, output_per_mtok=10.0))
"""Illustrative prices per million tokens for every model, only so the cost columns are filled."""


class Ledger:
    """Counts the model calls and tokens of every run of the process (a global subscriber)."""

    def __init__(self) -> None:
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def __call__(self, event: RunEvent) -> None:
        if isinstance(event, ModelResponseEvent):
            self.calls += 1
            self.input_tokens += event.input_tokens
            self.output_tokens += event.output_tokens


def scripted(script: Sequence[Any]) -> FakeChatModel:
    """The scripted model of one agent."""

    return FakeChatModel(list(script), model_name="scripted-demo")


# ------------------------------------------------------------------ the invoice desk

INVOICES = {
    "INV-2041": ("Nordwind Office GmbH", "PO-7710", 1180.00, "open"),
    "INV-2042": ("Alpen Maschinenbau AG", "PO-7725", 7450.00, "open"),
    "INV-2043": ("Nordwind Papier KG", "PO-7731", 342.50, "paid"),
}
LEDGER_QUERY = "SELECT supplier, po_number, amount_eur, status FROM invoices WHERE invoice_id = ?"


def invoice_desk(invoice: str) -> Sequential:
    """The invoice desk of the tour: the checker reads the ledger, the clerk releases the invoice."""

    supplier, po, amount, status = INVOICES[invoice]

    @tool
    def invoice_ledger_query(query: str, parameters: list[str]) -> str:
        """Run a read-only SQL query on the invoice ledger and return the rows as JSON."""

        rows = [
            {"supplier": s, "po_number": p, "amount_eur": a, "status": st}
            for key, (s, p, a, st) in INVOICES.items()
            if key in parameters
        ]
        return json.dumps(rows)

    @tool
    def finance_approval(invoice_id: str, amount_eur: float) -> str:
        """Release an invoice for payment; from 5,000 EUR the head of finance approves first."""

        return f"Invoice {invoice_id} ({amount_eur:,.2f} EUR) is released for payment."

    checker = Agent(
        "invoice_checker",
        description="Looks the invoice up in the ledger and checks its purchase order.",
        instructions=(
            "Look the invoice up in the invoices table with invoice_ledger_query. Report the "
            "supplier, the purchase order, the amount in EUR and the status."
        ),
        tools=[invoice_ledger_query],
        model=scripted(
            [
                tool_call("invoice_ledger_query", {"query": LEDGER_QUERY, "parameters": [invoice]}),
                f"Invoice {invoice} from {supplier}: {amount:,.2f} EUR, purchase order {po}, "
                f"status {status}.",
            ]
        ),
    )
    clerk = Agent(
        "payment_clerk",
        description="Releases checked, open invoices for payment.",
        instructions=(
            "For an open invoice call finance_approval with its number and amount, then "
            "confirm that it is released for payment. Never release a paid invoice again."
        ),
        tools=[
            create_tool_approval_wrapper(finance_approval, requires_approval="amount_eur >= 5000")
        ],
        model=scripted(
            [
                tool_call("finance_approval", {"invoice_id": invoice, "amount_eur": amount}),
                f"Invoice {invoice} from {supplier} ({amount:,.2f} EUR) is released for payment.",
            ]
        ),
    )
    return Sequential("invoice_desk", [checker, clerk], checkpointer=StateInMemorySaver())


async def invoice(cap: int) -> RunResult[Any]:
    number = "INV-2042"
    desk = invoice_desk(number)
    metadata = {
        "awa.tenant": TENANT,
        "awa.request_id": f"req-{number.lower()}",
        "awa.blueprint": "invoice_desk",
    }
    task = f"Please release invoice {number} for payment."
    limits = UsageLimits(max_requests=cap)
    paused = await desk.run(task, metadata=metadata, usage_limits=limits)
    if not paused.is_interrupted or paused.thread_id is None:
        return paused
    decision = {"action": "approve", "reviewer": "head-of-finance"}
    return await desk.resume(paused.thread_id, decision, metadata=metadata, usage_limits=limits)


def connect() -> Any:
    from awa.observability import connect_langfuse

    return connect_langfuse(
        capture_content=True,  # the demo project only: all data here is invented
        tags=["awa-showcase", "scripted"],
        metadata={"showcase": "scripted"},
        prices=PRICES,
    )


LANGFUSE_KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")


def missing_langfuse_keys(environ: Mapping[str, str]) -> list[str]:
    """The names of the Langfuse settings that `environ` lacks (names only, never values).

    The address is required, so that the run never falls back to the hosted Langfuse.
    """

    missing = [name for name in LANGFUSE_KEYS if not environ.get(name, "").strip()]
    if not any(environ.get(name, "").strip() for name in ("LANGFUSE_BASE_URL", "LANGFUSE_HOST")):
        missing.append("LANGFUSE_BASE_URL")
    return missing


async def run(cap: int) -> int:
    connection = connect()
    ledger = Ledger()
    try:
        with use_global_subscriber(connection.subscriber), use_global_subscriber(ledger):
            started = time.perf_counter()
            try:
                result = await invoice(cap)
                outcome, failed = " ".join(str(result.output).split())[:160], False
            except Exception as exc:  # noqa: BLE001 - report it with the totals
                outcome, failed = f"FAILED {type(exc).__name__}: {exc}"[:200], True
            print(
                f"invoice: {ledger.calls} calls, {time.perf_counter() - started:.1f} s: {outcome}"
            )
    finally:
        connection.shutdown()
    print(
        f"total: {ledger.calls} model calls, {ledger.input_tokens} input and "
        f"{ledger.output_tokens} output tokens; traces sent to {connection.endpoint}"
    )
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cap", type=int, default=MAX_REQUESTS, help="most model calls of the run")
    args = parser.parse_args(argv)
    if missing := missing_langfuse_keys(os.environ):
        print(f"error: set {', '.join(missing)} in the environment", file=sys.stderr)
        return 2
    return asyncio.run(run(args.cap))


if __name__ == "__main__":
    sys.exit(main())
