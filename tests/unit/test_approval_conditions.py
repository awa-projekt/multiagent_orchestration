"""Conditional approvals: `@tool(approval="amount >= 5000")` and blueprint `approvals: when:`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import ToolException
from langchain_core.tools import tool as langchain_tool
from langgraph.checkpoint.memory import InMemorySaver

from awa import Agent, tool
from awa.blueprints import Blueprint, BlueprintDesign, BlueprintError
from awa.blueprints.architect import ARCHITECT_INSTRUCTIONS
from awa.errors import ConfigurationError
from awa.evals import Dataset, evaluate
from awa.kernel.conditions import predicate
from awa.models import ModelRegistry
from awa.testing import FunctionChatModel, ModelCall, tool_call
from awa.tools import ApprovalCondition, create_tool_approval_wrapper, tool_spec


@langchain_tool
def book_invoice(invoice_id: str, amount: float, currency: str = "EUR") -> str:
    """Book an invoice in the ledger."""

    return f"booked {invoice_id} over {amount:g} {currency}"


@langchain_tool
def fetch_page(url: str) -> str:
    """Fetch a web page."""

    return "page"


@langchain_tool
def send_email(to: str, body: str) -> str:
    """Send an e-mail."""

    return "sent"


# ---------------------------------------------------------------------- the condition


def test_a_condition_decides_on_the_arguments() -> None:
    condition = ApprovalCondition("amount >= 5000 and currency in ['EUR', 'CHF']")

    assert condition.names == {"amount", "currency"}
    assert condition({"amount": 6200, "currency": "EUR"}) is True
    assert condition({"amount": "6200", "currency": "EUR"}) is True  # numeric text
    assert condition({"amount": 4999.99, "currency": "EUR"}) is False
    assert condition({"amount": 9000, "currency": "USD"}) is False
    assert ApprovalCondition("  amount   >= 5000 ").expression == "amount >= 5000"
    assert repr(condition) == "ApprovalCondition(\"amount >= 5000 and currency in ['EUR', 'CHF']\")"
    assert ApprovalCondition("amount > 1") == ApprovalCondition("amount  > 1")


def test_a_condition_bound_to_a_tool_uses_its_defaults_and_checks_names() -> None:
    bound = ApprovalCondition("currency == 'EUR' and amount >= 5000").for_tool(book_invoice)
    assert bound({"invoice_id": "INV-1", "amount": 6000}) is True  # currency defaults to EUR

    with pytest.raises(ConfigurationError, match="reads total, which is not an argument"):
        ApprovalCondition("total > 5").for_tool(book_invoice)
    shared = ApprovalCondition("total > 5").for_tool(book_invoice, strict=False)
    assert shared({"invoice_id": "INV-1", "amount": 1}) is False  # a missing name is null
    assert ApprovalCondition("x > 1", arguments=["x"]).unknown_names(["y"]) == ["x"]
    given = ApprovalCondition("amount >= 5000", defaults={"amount": 9000}).for_tool(book_invoice)
    assert given({"invoice_id": "INV-1"}) is True  # the given default fills the missing amount


def test_a_condition_that_cannot_be_evaluated_asks_for_approval() -> None:
    assert ApprovalCondition("amount * 2 > 100")({"amount": "a lot"}) is True


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("", "needs an expression"),
        ("amount >=", "invalid expression"),
        ("__import__('os').system('x')", "not allowed|unknown function"),
        ("amount.real > 1", "Attribute is not allowed"),
        ("open('x')", "unknown function 'open'"),
        ("[x for x in amount]", "is not allowed"),
    ],
)
def test_only_the_safe_expression_language_is_accepted(expression: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        ApprovalCondition(expression)


# ---------------------------------------------------------------------- @tool and the wrapper


def ledger_model(call: ModelCall) -> Any:
    request = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
    if isinstance(call.last_message, ToolMessage):
        return str(call.last_message.content)
    amount = float(request.split()[-1])
    return tool_call("book_invoice", {"invoice_id": "INV-7", "amount": amount})


async def test_tool_approval_takes_a_condition() -> None:
    @tool("book_invoice", approval="amount >= 5000")
    def book(invoice_id: str, amount: float) -> str:
        """Book an invoice.

        Args:
            invoice_id: The invoice.
            amount: Total in EUR.
        """

        return f"booked {invoice_id} over {amount:g} EUR"

    spec = tool_spec(book)
    assert spec is not None
    assert (spec.approval, spec.approval_when) == ("conditional", "amount >= 5000")
    agent = Agent(
        "clerk", model=FunctionChatModel(ledger_model), tools=[book], checkpointer=InMemorySaver()
    )

    below = await agent.run("Book 4999")
    above = await agent.run("Book 5000")

    assert not below.is_interrupted and below.output == "booked INV-7 over 4999 EUR"
    [pending] = above.interrupts
    assert pending["value"]["tool"] == "book_invoice"
    assert pending["value"]["condition"] == "amount >= 5000"
    resumed = await agent.resume(above.thread_id or "", {"action": "approve"})
    assert resumed.output == "booked INV-7 over 5000 EUR"


def test_tool_approval_conditions_are_validated_when_the_tool_is_made() -> None:
    def book(invoice_id: str, amount: float) -> str:
        """Book an invoice."""

        return invoice_id

    with pytest.raises(ConfigurationError, match="reads total, which is not an argument"):
        tool(book, approval="total >= 5000")
    with pytest.raises(ConfigurationError, match="invalid expression"):
        tool(book, approval="amount >=")
    with pytest.raises(ConfigurationError, match="a bool, a condition or a predicate"):
        tool(book, approval=3)  # type: ignore[arg-type]


async def test_the_approval_wrapper_takes_a_condition() -> None:
    gated = create_tool_approval_wrapper(book_invoice, requires_approval="amount > 100")
    assert await gated.ainvoke({"invoice_id": "INV-1", "amount": 50}) == "booked INV-1 over 50 EUR"
    with pytest.raises(ConfigurationError, match="reads total"):
        create_tool_approval_wrapper(book_invoice, requires_approval="total > 100")


# ---------------------------------------------------------------------- blueprints


def blueprint(when: str, *, tools: list[str] | None = None, **extra: Any) -> Blueprint:
    return Blueprint.from_dict(
        {
            "name": "ledger",
            "agents": [
                {
                    "name": "clerk",
                    "description": "Books invoices.",
                    "instructions": "Book invoices.",
                    "tools": tools or ["book_invoice"],
                }
            ],
            "approvals": [{"tool": (tools or ["book_invoice"])[-1], "when": when}],
            **extra,
        }
    )


def errors(bp: Blueprint, **kwargs: Any) -> list[str]:
    return [f"{i.path}: {i.message}" for i in bp.check(**kwargs) if i.severity == "error"]


def test_check_validates_the_condition_and_its_names() -> None:
    assert errors(blueprint("amount >= 5000"), tools=[book_invoice]) == []
    assert errors(blueprint("amount >= 5000")) == []  # arguments unknown without the tools

    [unknown] = errors(blueprint("total >= 5000"), tools=[book_invoice])
    assert unknown == (
        "approvals[0].when: condition 'total >= 5000' reads total, which the arguments of "
        "'book_invoice' do not have (arguments: amount, currency, invoice_id)"
    )
    [malformed] = errors(blueprint("amount >="), tools=[book_invoice])
    assert malformed.startswith("approvals[0].when: Invalid approval condition 'amount >='")
    assert "such as 'amount >= 5000'" in malformed
    [reference] = errors(blueprint("my-module:needs"), tools=[book_invoice])
    assert "invalid import reference 'my-module:needs'" in reference
    assert blueprint("my_module:needs").approvals[0].condition is None
    assert blueprint("status == 'a:b'").approvals[0].condition == "status == 'a:b'"


def test_check_knows_the_arguments_of_connector_operations() -> None:
    extra = {"connectors": {"jira": {"requires_approval": True}}}
    ok = blueprint("issue_type == 'Bug'", tools=["connector:jira.create_issue"], **extra)
    assert errors(ok) == []
    wrong = blueprint("priority == 1", tools=["connector:jira.create_issue"], **extra)
    [error] = errors(wrong)
    assert "reads priority, which the arguments of 'connector:jira.create_issue'" in error
    assert "project_key" in error


def test_a_conditionally_gated_action_counts_as_gated_for_the_risk_check() -> None:
    gated = Blueprint.from_dict(
        {
            "name": "desk",
            "agents": [{"name": "assistant", "tools": ["fetch_page", "send_email"]}],
            "approvals": [{"tool": "send_email", "when": "not endswith(to, '@example.com')"}],
        }
    )
    warnings = [i.message for i in gated.check(tools=[fetch_page, send_email])]
    assert not any("untrusted content" in message for message in warnings)

    @tool(approval="not endswith(to, '@example.com')", risk=["external_action"])
    def mail(to: str, body: str) -> str:
        """Send an e-mail."""

        return "sent"

    self_gated = Blueprint.from_dict(
        {"name": "desk", "agents": [{"name": "assistant", "tools": ["fetch_page", "mail"]}]}
    )
    warnings = [i.message for i in self_gated.check(tools=[fetch_page, mail])]
    assert not any("untrusted content" in message for message in warnings)


async def test_a_blueprint_condition_gates_only_the_calls_it_holds_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ModelRegistry()
    registry.register("default", FunctionChatModel(ledger_model))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    bp = blueprint("amount >= 5000")

    def system() -> Any:
        return bp.build(tools=[book_invoice], checkpointer=InMemorySaver())

    report = await evaluate(
        system,
        Dataset.from_yaml(
            """
            cases:
              - id: below
                input: Book 4999.99
                scorers: ["no_approval:book_invoice", "contains:over 4999.99 EUR"]
              - id: at-the-threshold
                input: Book 5000
                decisions: [approve]
                scorers: ["approval_granted:book_invoice", "contains:over 5000 EUR"]
              - id: above-rejected
                input: Book 12000
                decisions: [{action: reject, note: Needs a second quote.}]
                scorers: ["approval_rejected:book_invoice", "contains:second quote"]
            """
        ),
    )

    assert report.pass_rate == 1.0, report.to_markdown()


LEDGER_TOOLS = '''
from langchain_core.tools import tool


@tool
def book(invoice_id: str, amount: float) -> str:
    """Book an invoice."""
    return invoice_id
'''


def test_a_condition_on_an_imported_tool_is_checked_when_it_is_built(tmp_path: Path) -> None:
    (tmp_path / "ledger_tools.py").write_text(LEDGER_TOOLS, encoding="utf-8")
    reference = f"{(tmp_path / 'ledger_tools.py').as_posix()}:book"
    good = blueprint("amount >= 5000", tools=[reference])
    assert errors(good) == []  # an import reference is only known when it is imported
    good.build(checkpointer=InMemorySaver(), allow_imports=True)

    with pytest.raises(BlueprintError, match="reads total, which is not an argument"):
        blueprint("total >= 5000", tools=[reference]).build(
            checkpointer=InMemorySaver(), allow_imports=True
        )


def test_the_architect_can_write_conditional_approvals() -> None:
    design = BlueprintDesign.model_validate(
        {
            "name": "ledger",
            "description": "Books invoices.",
            "entrypoint": "agent",
            "agents": [
                {
                    "name": "clerk",
                    "description": "Books invoices.",
                    "instructions": "Book.",
                    "tools": ["book_invoice", "send_email"],
                }
            ],
            "approvals": ["book_invoice", "send_email"],
            "conditional_approvals": [{"tool": "book_invoice", "when": " amount >=  5000 "}],
        }
    )

    approvals = design.to_blueprint().approvals
    instructions = " ".join(ARCHITECT_INSTRUCTIONS.split())
    assert "put the tool in conditional_approvals instead, with when" in instructions

    assert [(a.tool, a.when) for a in approvals] == [
        ("send_email", None),
        ("book_invoice", "amount >= 5000"),
    ]

    blank = BlueprintDesign.model_validate(
        {
            **design.model_dump(),
            "approvals": [],
            "conditional_approvals": [{"tool": "send_email", "when": " "}],
        }
    )
    assert [(a.tool, a.when) for a in blank.to_blueprint().approvals] == [("send_email", None)]


@pytest.mark.parametrize(
    "arguments",
    [{}, {"amount": None}, {"amount": "5.000"}, {"amount": "5,000"}, {"amount": " 1.234 "}],
    ids=["left out", "null", "5.000", "5,000", "1.234"],
)
def test_a_condition_that_would_have_to_guess_asks_for_approval(arguments: dict[str, Any]) -> None:
    """A missing amount or `5.000` (5 or 5000?) must not slip past `amount >= 5000`."""

    assert ApprovalCondition("amount >= 5000", arguments=["amount"])(arguments) is True


@pytest.mark.parametrize(
    ("arguments", "needs_approval"),
    [
        ({"amount": 4999}, False),
        ({"amount": "4999"}, False),
        ({"amount": "4.999,50"}, False),
        ({"amount": "6.200,00"}, True),
        ({"amount": 6200.0}, True),
    ],
)
def test_clear_numbers_still_decide_normally(
    arguments: dict[str, Any], needs_approval: bool
) -> None:
    assert ApprovalCondition("amount >= 5000", arguments=["amount"])(arguments) is needs_approval


def test_a_null_check_is_not_a_guess() -> None:
    condition = ApprovalCondition(
        "currency == null or amount > 100", arguments=["currency", "amount"]
    )

    assert condition({"currency": None, "amount": 5}) is True
    assert condition({"currency": "EUR", "amount": 5}) is False


@pytest.mark.parametrize(
    "amount",
    [float("nan"), float("inf"), [9800], {"EUR": 9800}, True, "1e309", "about 9800"],
    ids=["nan", "inf", "list", "object", "bool", "overflow-text", "words"],
)
@pytest.mark.parametrize(
    "condition",
    ["amount_eur >= 5000", "amount_eur > 1000 and amount_eur < 20000", "5000 <= amount_eur"],
)
def test_a_value_that_is_not_a_finite_number_asks_for_approval(
    amount: object, condition: str
) -> None:
    assert ApprovalCondition(condition)({"amount_eur": amount}) is True


@pytest.mark.parametrize("expression", ["x in [5000]", "x not in [5000]"])
def test_a_list_check_with_an_ambiguous_number_asks_for_approval(expression: str) -> None:
    with pytest.raises(ToolException):
        predicate(expression, strict=True).evaluate(lambda name: "5.000")


def test_only_a_second_parameter_without_default_is_the_run_context() -> None:
    from awa.tools.approval import takes_context

    def with_context(args: dict[str, Any], ctx: Any) -> bool:
        return True

    def with_option(args: dict[str, Any], limit: int = 5) -> bool:
        return True

    def with_rest(*parts: Any) -> bool:
        return True

    assert takes_context(with_context) and takes_context(with_rest)
    assert not takes_context(with_option)
