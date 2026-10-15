"""`awa.quick`: teams, workflows and networks from names and tasks."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import SystemMessage, ToolMessage

from awa import Agent, AgentNetwork, Blueprint, Sequential, Team, quick
from awa.errors import ConfigurationError
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call
from awa.tools import tool

LOOKED_UP: list[str] = []


@tool
def lookup_po(po_number: str) -> str:
    """Look up a purchase order."""
    LOOKED_UP.append(po_number)
    return f"PO {po_number}: 120.00 EUR"


@tool(approval=True, risk=["external_action"])
def book_payment(invoice_id: str) -> str:
    """Book the payment of an invoice."""
    return f"booked {invoice_id}"


@pytest.fixture(autouse=True)
def _reset() -> None:
    LOOKED_UP.clear()


def _system_prompt(call: ModelCall) -> str:
    first = call.messages[0]
    assert isinstance(first, SystemMessage)
    return str(first.text)


def test_a_team_from_names_and_tasks_runs() -> None:
    def respond(call: ModelCall) -> Any:
        prompt = _system_prompt(call)
        if prompt.startswith("Validates"):
            return "Totals match."
        if not any(isinstance(m, ToolMessage) for m in call.messages):
            return tool_call("delegate_task", {"agent": "checker", "task": "Check INV-1."})
        return "INV-1 is fine."

    desk = quick.team(
        "Invoice processing",
        lead="coordinator",
        members={
            "extractor": "Reads incoming invoices and extracts the fields.",
            "checker": "Validates the totals against the purchase order.",
        },
        model=FunctionChatModel(respond),
    )

    assert isinstance(desk, Team) and desk.name == "invoice_processing"
    assert desk.orchestrator.name == "coordinator"
    assert [m.name for m in desk.members] == ["extractor", "checker"]
    assert desk.run_sync("Process INV-1.").output == "INV-1 is fine."
    report = quick.explain(desk)
    assert report.for_agent("checker").tools == ("builtin:calculator",)
    assert report.blueprint is not None and report.blueprint.description == "Invoice processing"


def test_the_lead_can_be_one_of_the_members() -> None:
    desk = quick.team(
        "Support",
        lead="triage",
        members={"triage": "Routes support tickets by urgency.", "writer": "Writes replies."},
    )

    assert desk.orchestrator.description == "Routes support tickets by urgency."
    assert [m.name for m in desk.members] == ["writer"]


def test_members_can_be_options_or_agents_and_tools_objects() -> None:
    reviewer = Agent("reviewer", description="Reviews the booking.", model=FakeChatModel(["ok"]))
    desk = quick.team(
        "Payables",
        members={
            "checker": {
                "task": "Checks invoices against the purchase order.",
                "tools": [lookup_po],
                "skills": ["structured-output"],
                "auto": False,
            },
            "reviewer": reviewer,
        },
    )

    members = {m.name: m for m in desk.members}
    checker = members["checker"]
    assert isinstance(checker, Agent)
    assert checker.card.tools == ("lookup_po",) and checker.card.skills == ("structured-output",)
    assert members["reviewer"] is reviewer
    report = quick.explain(desk)
    assert report.for_agent("reviewer").note == "given as an Agent: used as it is"
    assert not report.for_agent("checker").auto


async def test_tools_that_wait_for_approval_get_an_in_memory_checkpointer() -> None:
    def respond(call: ModelCall) -> Any:
        answered = any(isinstance(m, ToolMessage) for m in call.messages)
        if "Books approved payments." in _system_prompt(call).split("\n")[1]:
            return "booked" if answered else tool_call("book_payment", {"invoice_id": "INV-1"})
        if answered:
            return "done"
        return tool_call("delegate_task", {"agent": "payer", "task": "Pay INV-1."})

    desk = quick.team(
        "Payables",
        members={"payer": {"task": "Books approved payments.", "tools": [book_payment]}},
        model=FunctionChatModel(respond),
    )

    paused = await desk.run("Pay INV-1.")
    assert paused.is_interrupted and paused.thread_id is not None
    assert paused.interrupts[0]["value"]["tool"] == "book_payment"
    done = await desk.resume(paused.thread_id, {"action": "approve"})
    assert done.output == "done"


def test_a_workflow_runs_its_steps_in_order() -> None:
    model = FakeChatModel(["clauses", "flags", "summary"])

    pipeline = quick.workflow(
        "Contract review",
        steps={
            "reader": "Extracts the clauses of the contract.",
            "reviewer": "Flags clauses that deviate from the playbook.",
            "writer": "Writes a short summary for the legal team.",
        },
        model=model,
    )

    assert isinstance(pipeline, Sequential)
    assert pipeline.run_sync("Review the NDA.").output == "summary"
    assert [_system_prompt(c).split("\n")[1].split(". ", 1)[1] for c in model.calls] == [
        "Extracts the clauses of the contract.",
        "Flags clauses that deviate from the playbook.",
        "Writes a short summary for the legal team.",
    ]


def test_a_network_gives_teammates_their_names_as_capabilities() -> None:
    research = quick.network(
        "Market scan",
        teammates={"pricing": "Compares competitor prices.", "reviews": "Summarises reviews."},
    )

    assert isinstance(research, AgentNetwork)
    blueprint = quick.explain(research).blueprint
    assert blueprint is not None and blueprint.agent("pricing").capabilities == ["pricing"]
    assert blueprint.network is not None and blueprint.network.lead == "lead"


def test_names_are_made_valid() -> None:
    desk = quick.team("  Accounts payable!  ", members={"Invoice checker": "Checks invoices."})

    assert desk.name == "accounts_payable"
    assert [m.name for m in desk.members] == ["invoice_checker"]


def test_blueprint_returns_the_equivalent_blueprint() -> None:
    spec = quick.blueprint(
        "team",
        "Invoice processing",
        {"extractor": "Reads invoices.", "checker": "Validates totals."},
        lead="coordinator",
        model="fast",
    )

    assert isinstance(spec, Blueprint) and spec.auto and spec.defaults.model == "fast"
    assert Blueprint.from_yaml_text(spec.to_yaml()) == spec
    assert spec.team is not None and spec.team.orchestrator == "coordinator"


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: quick.team("123", members={"a": "x"}), "start with a letter"),
        (lambda: quick.team("x", members={"a": 3}), "task text"),  # type: ignore[dict-item]
        (lambda: quick.team("x", members={"a": {"job": "x"}}), "Unknown option"),  # type: ignore[dict-item]
        (lambda: quick.team("x", members={"A b": "x", "a_b": "y"}), "Two agents"),
        (lambda: quick.workflow("x", steps={}), "at least one step"),
        (lambda: quick.team("x", members={"a": Agent("b")}), "given under the name"),
        (
            lambda: quick.blueprint("team", "x", {"a": {"task": "t", "tools": [lookup_po]}}),
            "cannot write tool objects",
        ),
        (lambda: quick.blueprint("workflow", "x", {"a": "t"}, lead="a"), "no lead"),
        (lambda: quick.blueprint("team", "x", {"a": Agent("a")}), "cannot write Agent objects"),
    ],
)
def test_invalid_input_is_a_configuration_error(call: Any, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        call()


def test_two_different_tools_with_one_name_are_refused() -> None:
    other = tool(lambda po_number: "x", name="lookup_po", description="Another lookup.")

    with pytest.raises(ConfigurationError, match="Two different tools"):
        quick.team("x", members={"a": {"task": "t", "tools": [lookup_po, other]}})
