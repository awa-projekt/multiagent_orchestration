"""Teams: an orchestrator delegates to members, or hands the conversation over.

The market desk's orchestrator delegates the independent parts of a question to
two members in the same turn (they run concurrently) and writes the
recommendation from their results. `TeamLimits` bounds delegation in code, and
`result.usage.by_agent` shows what every agent consumed.

The support desk passes control instead: its triage agent transfers the
conversation to the billing member, which answers the customer directly. With a
checkpointer the team persists each thread, so the follow-up message of the
same `thread_id` goes straight to the agent that held control last.

Run:
    pip install -e ".[anthropic]"
    export ANTHROPIC_API_KEY=...
    python examples/03_team.py
    # No model key? Run its offline tests instead:
    #   python -m pytest tests/examples/test_03_team.py
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from _support import require_model_credentials
from awa import Agent, Team, TeamLimits
from awa.persistence import StateInMemorySaver

MARKET_DATA: dict[str, dict[str, float]] = {
    "home batteries": {"market_size_eur_bn": 4.2, "annual_growth_pct": 18.5},
    "heat pumps": {"market_size_eur_bn": 11.8, "annual_growth_pct": 9.1},
}
COMPETITORS: dict[str, list[dict[str, str]]] = {
    "home batteries": [
        {"name": "VoltHaus", "position": "premium, installer network"},
        {"name": "Cellar Energy", "position": "low price, online sales"},
    ],
    "heat pumps": [{"name": "ThermaNord", "position": "market leader, broad range"}],
}


def market_figures(segment: str) -> str:
    """Return market size (EUR bn) and annual growth (%) for a market segment."""

    figures = MARKET_DATA.get(segment.strip().lower())
    if figures is None:
        return json.dumps({"segment": segment, "error": "no data", "known": sorted(MARKET_DATA)})
    return json.dumps({"segment": segment, **figures})


def competitor_profiles(segment: str) -> str:
    """Return the main competitors in a market segment and how they are positioned."""

    profiles = COMPETITORS.get(segment.strip().lower())
    if profiles is None:
        return json.dumps({"segment": segment, "error": "no data", "known": sorted(COMPETITORS)})
    return json.dumps({"segment": segment, "competitors": profiles})


def build_market_desk() -> Team:
    """Build the market desk: a lead analyst and two members."""

    market_analyst = Agent(
        "market_analyst",
        description="Sizes markets and growth from market data.",
        instructions="Answer with figures from market_figures only, and name the segment.",
        tools=[market_figures],
        capabilities=["market-sizing"],
    )
    competitor_analyst = Agent(
        "competitor_analyst",
        description="Profiles the competitors in a market segment.",
        instructions="Summarize each competitor's positioning in one line, from your tool only.",
        tools=[competitor_profiles],
        capabilities=["competitive-analysis"],
    )
    lead = Agent(
        "lead_analyst",
        description="Answers market-entry questions by coordinating specialist analysts.",
        instructions=(
            "You advise on market entry. Delegate market sizing and competitor analysis to "
            "the specialists in the same turn, then write a recommendation of at most five "
            "sentences that cites their figures."
        ),
        skills=["orchestration-planning", "delegation-briefs"],
    )
    return Team(
        "market_desk",
        orchestrator=lead,
        members=[market_analyst, competitor_analyst],
        limits=TeamLimits(max_parallel_delegations=4, max_calls_per_member=3),
    )


@dataclass
class Invoice:
    """One invoice and how many times the customer was charged for it."""

    invoice_id: str
    customer: str
    amount_eur: float
    times_charged: int


@dataclass
class BillingSystem:
    """In-memory stand-in for a billing API."""

    invoices: dict[str, Invoice]
    refunds: list[str] = field(default_factory=list)

    def tools(self) -> list[Callable[[str], str]]:
        """The billing agent's tools, bound to this billing system."""

        def lookup_invoice(invoice_id: str) -> str:
            """Return an invoice with its amount and how many times it was charged."""

            invoice = self.invoices.get(invoice_id.strip().upper())
            if invoice is None:
                return json.dumps({"invoice_id": invoice_id, "error": "unknown invoice"})
            return json.dumps(asdict(invoice))

        def refund_duplicate_charge(invoice_id: str) -> str:
            """Refund one duplicate charge of an invoice that was charged more than once."""

            invoice = self.invoices.get(invoice_id.strip().upper())
            if invoice is None or invoice.times_charged < 2:
                return "No duplicate charge to refund."
            invoice.times_charged -= 1
            self.refunds.append(invoice.invoice_id)
            return f"Refunded EUR {invoice.amount_eur:.2f} for {invoice.invoice_id}."

        return [lookup_invoice, refund_duplicate_charge]


def sample_billing_system() -> BillingSystem:
    """A billing system with one invoice that was charged twice."""

    return BillingSystem({"INV-2201": Invoice("INV-2201", "C-17", 49.0, times_charged=2)})


def build_support_desk(billing_system: BillingSystem | None = None) -> Team:
    """Build the support desk: triage plus billing and technical specialists."""

    if billing_system is None:
        billing_system = sample_billing_system()
    triage = Agent(
        "triage",
        description="Greets customers and routes them to the right specialist.",
        instructions=(
            "You are the first contact of customer support. Transfer billing questions "
            "(charges, invoices, refunds) to billing and product problems to tech_support. "
            "Answer only greetings and questions about which team does what yourself."
        ),
    )
    billing = Agent(
        "billing",
        description="Resolves charges, invoices and refunds.",
        instructions=(
            "Resolve billing issues with your tools. Refund a duplicate charge only when the "
            "customer asks for it. Transfer anything that is not about billing back to triage."
        ),
        tools=billing_system.tools(),
    )
    tech_support = Agent(
        "tech_support",
        description="Troubleshoots product and account problems.",
        instructions=(
            "Troubleshoot step by step. Transfer anything that is not a technical problem "
            "back to triage."
        ),
    )
    return Team(
        "support_desk",
        orchestrator=triage,
        members=[billing, tech_support],
        delegates={},
        handoffs={
            "triage": ["billing", "tech_support"],
            "billing": ["triage"],
            "tech_support": ["triage"],
        },
        checkpointer=StateInMemorySaver(),
    )


async def main() -> None:
    require_model_credentials()
    market_desk = build_market_desk()
    print(market_desk.to_mermaid(), end="\n\n")
    result = await market_desk.run("Should we enter the home battery market in 2027?")
    print(result.output, end="\n\n")
    usage = result.usage
    print(
        f"Total: {usage.requests} model calls, {usage.total_tokens} tokens, "
        f"{usage.tool_calls} tool calls, {result.duration_ms} ms"
    )
    for agent, agent_usage in sorted(usage.by_agent.items()):
        print(f"  {agent:<20} {agent_usage.requests:>3} calls {agent_usage.total_tokens:>7} tokens")

    billing_system = sample_billing_system()
    support_desk = build_support_desk(billing_system)
    first = await support_desk.run("Hi, I was charged twice for invoice INV-2201.")
    print(f"\n[{' -> '.join(first.handoff_path)}] {first.output}")
    follow_up = await support_desk.run(
        "Yes please, refund the second charge.", thread_id=first.thread_id
    )
    print(f"[{' -> '.join(follow_up.handoff_path)}] {follow_up.output}")
    print(f"Refunded invoices: {billing_system.refunds}")


if __name__ == "__main__":
    asyncio.run(main())
