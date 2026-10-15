"""Approvals and limits: deterministic code decides what runs, what gets in and out, and when to stop.

- An approval makes a tool call wait for a person. Refunds above a threshold
  pause the run with a LangGraph interrupt; the agent's checkpointer keeps the
  paused state, the caller inspects `result.interrupts`, and
  `agent.resume(thread_id, decision)` continues the same run. A rejected call
  does not raise: the agent is told why and answers accordingly. Small refunds
  run without asking.
- Input guardrails reject requests before any model call (here: secrets and
  oversized input); output guardrails reject answers before they are returned
  (here: a custom check that blocks email addresses).
- `UsageLimits` caps model calls and tokens for a whole run tree, and
  `max_steps` caps the model calls of one agent run.
- `harden_tool` gives tools a timeout, turns their exceptions into error
  results the model can recover from, and truncates oversized output.

Run:
    pip install -e ".[anthropic]"
    export ANTHROPIC_API_KEY=...
    python examples/05_approvals_and_limits.py
    # No model key? Run its offline tests instead:
    #   python -m pytest tests/examples/test_05_approvals_and_limits.py
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool

from _support import require_model_credentials
from awa import Agent, GuardrailTripwireError, RunResult, UsageLimits, tool
from awa.errors import MaxStepsExceededError, UsageLimitExceededError
from awa.guardrails import BlocklistGuardrail, GuardrailResult, LengthGuardrail
from awa.persistence import StateInMemorySaver
from awa.tools import create_tool_approval_wrapper, harden_tool

APPROVAL_THRESHOLD_EUR = 100.0
DEFAULT_LIMITS = UsageLimits(max_requests=8, max_total_tokens=40_000)
MAX_STEPS = 6


@dataclass
class PaymentsGateway:
    """In-memory stand-in for a payments API."""

    refunds: list[tuple[str, float]] = field(default_factory=list)

    def refund_tool(self) -> BaseTool:
        """The refund tool, bound to this gateway."""

        @tool
        def issue_refund(order_id: str, amount_eur: float, reason: str) -> str:
            """Refund part or all of an order to the customer's original payment method."""

            self.refunds.append((order_id, amount_eur))
            return f"Refunded EUR {amount_eur:.2f} for order {order_id}."

        return issue_refund


def needs_approval(args: dict[str, Any]) -> bool:
    """Refunds above the threshold need a human decision."""

    return float(args["amount_eur"]) > APPROVAL_THRESHOLD_EUR


def build_refunds_agent(gateway: PaymentsGateway | None = None) -> Agent:
    """Build the refunds agent with an approval-gated refund tool."""

    if gateway is None:
        gateway = PaymentsGateway()
    return Agent(
        "refunds_agent",
        description="Handles customer refund requests.",
        instructions=(
            "Issue the refund the customer asks for with issue_refund. If a refund is "
            "rejected by a reviewer, tell the customer it could not be approved and why."
        ),
        tools=[
            create_tool_approval_wrapper(gateway.refund_tool(), requires_approval=needs_approval)
        ],
        checkpointer=StateInMemorySaver(),
    )


async def ask_reviewer(pending: dict[str, Any]) -> dict[str, str]:
    """Show a pending approval on the console and read the reviewer's decision."""

    request = pending["value"]
    print(f"\nApproval needed: {request['tool']}({request['args']})")
    answer = await asyncio.to_thread(input, "Approve? [y/N] ")
    if answer.strip().lower() in ("y", "yes"):
        return {"action": "approve"}
    note = await asyncio.to_thread(input, "Reason for rejecting: ")
    return {"action": "reject", "note": note.strip() or "not approved"}


async def handle(agent: Agent, request: str) -> RunResult[str]:
    """Run a request, asking a reviewer for every pending approval."""

    result = await agent.run(request)
    while result.is_interrupted:
        assert result.thread_id is not None
        decision = await ask_reviewer(result.interrupts[0])
        result = await agent.resume(result.thread_id, decision)
    return result


CUSTOMERS = {"C-17": {"plan": "business", "since": "2023-04-01", "open_tickets": 1}}
ARTICLES = {
    "refund policy": "Refunds are possible within 30 days of purchase for annual plans. "
    "Monthly plans can be cancelled at any time but are not refunded pro rata.",
    "refund processing": "Approved refunds reach the original payment method within 5 to 10 "
    "business days. Card refunds may show as a pending credit before they settle.",
    "refund exceptions": "Usage-based add-ons and consumed credits are never refunded. Refunds "
    "for plans bought through a reseller are handled by the reseller.",
    "plan upgrades": "Upgrades take effect immediately; the price difference is charged pro rata.",
}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class NoEmailAddresses:
    """Output guardrail: blocks answers that contain an email address."""

    name = "no_email_addresses"

    def check(self, candidate: Any) -> GuardrailResult:
        if _EMAIL.search(str(candidate)):
            return GuardrailResult(
                passed=False,
                reason="the answer contains an email address",
                guardrail_name=self.name,
            )
        return GuardrailResult(passed=True, guardrail_name=self.name)


@tool
def account_lookup(customer_id: str) -> str:
    """Return a customer's plan, customer-since date and number of open tickets."""

    return json.dumps({"customer_id": customer_id, **CUSTOMERS[customer_id.strip().upper()]})


@tool
def search_help_center(query: str) -> str:
    """Search the help center and return every matching article."""

    words = query.lower().split()
    hits = {title: text for title, text in ARTICLES.items() if any(w in title for w in words)}
    return json.dumps(hits or {"result": "no matching article"})


def build_support_agent() -> Agent:
    """Build the support agent with guardrails, a step cap and hardened tools."""

    return Agent(
        "support_agent",
        description="Answers account and billing questions from the help center.",
        instructions=(
            "Answer from the customer's account and the help center only. If a tool "
            "returns an error, correct the input or say what you could not look up."
        ),
        tools=[
            harden_tool(account_lookup, timeout=5, on_error="return"),
            harden_tool(search_help_center, timeout=5, max_output_chars=400),
        ],
        input_guardrails=[
            BlocklistGuardrail(["password", "api key", "card number"], name="secrets"),
            LengthGuardrail(max_length=4_000, name="input_length"),
        ],
        output_guardrails=[NoEmailAddresses()],
        max_steps=MAX_STEPS,
    )


async def answer(agent: Agent, question: str, limits: UsageLimits = DEFAULT_LIMITS) -> str:
    """Answer `question`, turning blocked or runaway runs into a safe reply."""

    try:
        result = await agent.run(question, usage_limits=limits)
    except GuardrailTripwireError as exc:
        return f"Blocked by {exc.result.guardrail_name}: {exc.result.reason}"
    except (UsageLimitExceededError, MaxStepsExceededError) as exc:
        return f"Stopped: {exc}"
    return result.output


async def main() -> None:
    require_model_credentials()
    gateway = PaymentsGateway()
    refunds_agent = build_refunds_agent(gateway)
    for request in (
        "Please refund EUR 25 for order A-1001, the item arrived scratched.",
        "Refund the full EUR 480 for order A-1002, I never received it.",
    ):
        result = await handle(refunds_agent, request)
        print(f"{result.output}\n")
    print(f"Refunds executed: {gateway.refunds}\n")

    support_agent = build_support_agent()
    for question in (
        "I'm customer C-17 on an annual plan. Can I still get a refund?",
        "My password is hunter2, please reset my account.",
    ):
        print(f"Q: {question}\nA: {await answer(support_agent, question)}\n")

    tight = UsageLimits(max_requests=1)
    print(f"With {tight!r}:\n{await answer(support_agent, 'How do refunds work?', tight)}")


if __name__ == "__main__":
    asyncio.run(main())
