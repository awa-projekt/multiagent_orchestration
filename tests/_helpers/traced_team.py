"""A scripted team for the tracing tests: delegation, a failed tool call, a handoff and an approval.

The models are scripted (`awa.testing.FakeChatModel`) and all data is invented. `build_team()`
returns the team, and `run_scenario(team)` plays it: the run pauses for an approval, which is
approved, and both results are returned.
"""

from __future__ import annotations

from awa import Agent, RunResult, Team, tool
from awa.contracts.usage import ModelPrice, PriceTable
from awa.persistence import StateInMemorySaver
from awa.testing import FakeChatModel, tool_call
from awa.tools import create_tool_approval_wrapper

MODEL_NAME = "demo-model"
PRICES = PriceTable(prices={MODEL_NAME: ModelPrice(input_per_mtok=3.0, output_per_mtok=15.0)})
"""Made-up prices of the scripted model, so the trace shows a cost."""
TASK = "Customer 4711 asks for a refund of 80 EUR for order A-7."
METADATA = {"awa.tenant": "demo-shop", "awa.request_id": "req-demo-1", "awa.actor": "demo-user"}
"""Request metadata of the run; tracing sends it with every span (filter by it in Langfuse)."""
ORDERS = {"A-7": "A-7: Widget, 80.00 EUR, delivered damaged"}


@tool  # a failure becomes a tool error the model sees, not the end of the run
def lookup_order(order_id: str) -> str:
    """Return the order with its amount."""

    if order_id not in ORDERS:
        raise ValueError(f"no order {order_id}")
    return ORDERS[order_id]


@tool
def issue_refund(order_id: str, amount: float) -> str:
    """Refund an order. Irreversible."""

    return f"Refunded {amount:.2f} EUR for {order_id}."


def build_team(**team_options: object) -> Team:
    """A lead, a researcher (delegation, one tool) and a payments agent (handoff, approval)."""

    researcher = Agent(
        "researcher",
        description="Looks up orders.",
        role="specialist",
        tools=[lookup_order],
        model=FakeChatModel(
            [
                tool_call("lookup_order", {"order_id": "A-1"}),  # a typo: the tool call fails
                tool_call("lookup_order", {"order_id": "A-7"}),
                "Order A-7 was damaged.",
            ],
            model_name=MODEL_NAME,
        ),
    )
    payments = Agent(
        "payments",
        description="Issues refunds after a person approved them.",
        role="specialist",
        tools=[create_tool_approval_wrapper(issue_refund, requires_approval=lambda a: True)],
        model=FakeChatModel(
            [
                tool_call("issue_refund", {"order_id": "A-7", "amount": 80.0}),
                "The refund of 80 EUR is on its way.",
            ],
            model_name=MODEL_NAME,
        ),
    )
    lead = Agent(
        "lead",
        description="Handles customer requests.",
        role="orchestrator",
        model=FakeChatModel(
            [
                tool_call("delegate_task", {"agent": "researcher", "task": "Check order A-7"}),
                tool_call("transfer_to_payments", {"reason": "Refund approved by policy"}),
            ],
            model_name=MODEL_NAME,
        ),
    )
    return Team(
        "desk",
        orchestrator=lead,
        members=[researcher, payments],
        handoffs={"lead": ["payments"]},
        checkpointer=StateInMemorySaver(),
        **team_options,  # type: ignore[arg-type]
    )


async def run_scenario(team: Team) -> tuple[RunResult, RunResult]:
    """Run until the approval pause, approve, and return (paused result, finished result)."""

    paused = await team.run(TASK, metadata=METADATA)
    if not paused.is_interrupted or paused.thread_id is None:
        raise RuntimeError("the scripted run was expected to pause for an approval")
    done = await team.resume(
        paused.thread_id, {"action": "approve", "reviewer": "ops-lead"}, metadata=METADATA
    )
    return paused, done
