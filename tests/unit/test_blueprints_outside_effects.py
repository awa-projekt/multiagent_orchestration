"""A designed system never sends or changes anything outside on its own: a tool that acts gets an
approval unless the design opts out with act_without_approval."""

from __future__ import annotations

from typing import Any

from langchain_core.messages import SystemMessage, ToolMessage

from awa import tool
from awa.blueprints import AcceptanceDesign, BlueprintDesign, design_project
from awa.blueprints._build_loop import evaluate_project
from awa.blueprints._resolve import ToolCatalog
from awa.blueprints._risk import ungated_action_issues
from awa.blueprints.schema import Blueprint
from awa.contracts.events import ToolResultEvent
from awa.testing import FunctionChatModel, ModelCall, structured_output, tool_call


@tool
def send_status_mail(recipient: str, text: str) -> str:
    """Send a status mail to a recipient.

    Args:
        recipient: The e-mail address.
        text: The mail text.
    """

    return "sent"


@tool
def look_up_order(order_id: str) -> str:
    """Look up the status of an order.

    Args:
        order_id: The order number.
    """

    return "shipped"


ACCEPTANCE: dict[str, Any] = {
    "criteria": ["A customer gets the status by mail once a person agreed."],
    "cases": [
        {
            "id": "status-mail",
            "input": "Tell anna@example.org that order 1002 shipped.",
            "expectations": [
                {"kind": "tool_called", "value": "send_status_mail"},
                {"kind": "approval", "value": "send_status_mail"},
            ],
            "decisions": [{"action": "approve", "tool": "send_status_mail"}],
        }
    ],
}


def drafted(*, approvals: list[str]) -> dict[str, Any]:
    return {
        "name": "status_desk",
        "description": "Tells customers the status of their order.",
        "entrypoint": "agent",
        "agents": [
            {
                "name": "desk",
                "description": "Answers.",
                "instructions": "Look the order up, then send the customer a status mail.",
                "tools": ["look_up_order", "send_status_mail"],
            }
        ],
        "approvals": approvals,
    }


def system_prompt(call: ModelCall) -> str:
    first = call.messages[0]
    return str(first.content) if isinstance(first, SystemMessage) else ""


def architect(*drafts: dict[str, Any]) -> FunctionChatModel:
    """Test designer and architect in one model; the architect gives `drafts` in turn."""

    left = list(drafts)

    def respond(call: ModelCall) -> Any:
        if system_prompt(call).startswith("You write the acceptance tests"):
            return structured_output(AcceptanceDesign, ACCEPTANCE)
        return structured_output(BlueprintDesign, left.pop(0) if len(left) > 1 else left[0])

    return FunctionChatModel(respond)


async def test_a_draft_that_forgets_the_approval_is_gated_without_a_repair_round() -> None:
    model = architect(drafted(approvals=[]))

    project = await design_project(
        "Tell customers the status of their order by mail.",
        model=model,
        environment=None,
        tools=[send_status_mail, look_up_order],
    )

    architects = [c for c in model.calls if system_prompt(c).startswith("You design multi-agent")]
    assert len(architects) == 1  # the approval is inferred from the verb, not repaired
    assert [a.tool for a in project.blueprint.approvals] == ["send_status_mail"]


async def test_act_without_approval_lets_a_sending_tool_run_on_purpose() -> None:
    model = architect({**drafted(approvals=[]), "act_without_approval": ["send_status_mail"]})

    project = await design_project(
        "Tell customers the status of their order by mail, without a person.",
        model=model,
        environment=None,
        tools=[send_status_mail, look_up_order],
    )

    assert project.blueprint.approvals == []
    assert project.blueprint.tool_risks["send_status_mail"].external_action is False


def blueprint(*, approvals: list[dict[str, Any]], tools: list[str]) -> Blueprint:
    return Blueprint.from_dict(
        {
            "name": "desk",
            "agents": {"desk": {"description": "Answers.", "tools": tools}},
            "approvals": approvals,
        }
    )


def issues(bp: Blueprint) -> list[str]:
    catalog = ToolCatalog.from_source([send_status_mail, look_up_order])
    return [i.message for i in ungated_action_issues(bp, catalog)]


def test_an_approval_a_condition_or_a_read_makes_the_tool_fine() -> None:
    both = ["look_up_order", "send_status_mail"]
    assert issues(blueprint(approvals=[], tools=["look_up_order"])) == []
    assert issues(blueprint(approvals=[{"tool": "send_status_mail"}], tools=both)) == []
    conditional = [{"tool": "send_status_mail", "when": "text != ''"}]
    assert issues(blueprint(approvals=conditional, tools=both)) == []
    assert len(issues(blueprint(approvals=[], tools=both))) == 1


def test_a_tool_the_blueprint_marks_as_no_outside_effect_is_not_one() -> None:
    @tool
    def refund_limit(amount: float) -> str:
        """Whether a refund needs a manager.

        Args:
            amount: The amount in EUR.
        """

        return "true" if amount > 50 else "false"

    catalog = ToolCatalog.from_source([refund_limit])
    bp = blueprint(approvals=[], tools=["refund_limit"])
    marked = Blueprint.from_dict(
        {
            **bp.model_dump(exclude_none=True),
            "tool_risks": {"refund_limit": {"external_action": False}},
        }
    )

    assert len(ungated_action_issues(bp, catalog)) == 1  # "refund" reads like a payment
    assert ungated_action_issues(marked, catalog) == []


async def test_a_rule_of_the_design_is_marked_as_computing_only_whatever_it_is_called() -> None:
    """A rule only computes, even when its name reads like an action (`reply_action`): the design
    marks it so, and `awa validate` does not ask for an approval."""

    plain = {
        "name": "is_known_category",
        "description": "Whether the category is one we handle.",
        "arguments": [{"name": "category", "type": "string", "description": "The category."}],
        "expression": "category in ('question', 'complaint')",
        "examples": [
            {"arguments": '{"category": "question"}', "result": "true"},
            {"arguments": '{"category": "spam"}', "result": "false"},
        ],
    }
    rule = {
        "name": "reply_action",
        "description": "Whether a mail needs a reply.",
        "arguments": [{"name": "category", "type": "string", "description": "The category."}],
        "expression": "category in ('question', 'complaint')",
        "examples": [
            {"arguments": '{"category": "question"}', "result": "true"},
            {"arguments": '{"category": "newsletter"}', "result": "false"},
        ],
    }
    acceptance = {
        **ACCEPTANCE,
        "rules": [rule, plain],
        "cases": [
            {
                "id": "question",
                "input": "A customer asks where order 1002 is.",
                "expectations": [{"kind": "tool_called", "value": "reply_action"}],
            }
        ],
    }
    agents = [
        {
            "name": "desk",
            "description": "Answers.",
            "instructions": "Ask reply_action whether the mail needs a reply.",
            "tools": ["reply_action", "is_known_category"],
        }
    ]
    draft = {**drafted(approvals=[]), "agents": agents}

    def respond(call: ModelCall) -> Any:
        if system_prompt(call).startswith("You write the acceptance tests"):
            return structured_output(AcceptanceDesign, acceptance)
        return structured_output(BlueprintDesign, draft)

    model = FunctionChatModel(respond)
    project = await design_project("Answer mail.", model=model, environment=None)

    assert list(project.blueprint.tool_risks) == ["reply_action"]  # a rule named plainly needs none
    assert project.blueprint.tool_risks["reply_action"].external_action is False
    assert project.blueprint.approvals == []  # no person is asked before a computation
    found = project.blueprint.check(tools=project.own_tools())
    assert not [i for i in found if "reply_action" in i.message]


def test_a_connector_write_is_gated_by_its_own_default() -> None:
    bp = Blueprint.from_dict(
        {
            "name": "desk",
            "agents": {"desk": {"description": "Answers.", "tools": ["connector:outbox.execute"]}},
            "connectors": {"outbox": {"use": "workspace", "root": "data", "database": "o.db"}},
        }
    )

    assert ungated_action_issues(bp, ToolCatalog()) == []


OUTBOX_ACCEPTANCE: dict[str, Any] = {
    "criteria": ["A reply leaves only after a person approved it."],
    "systems": [
        {
            "name": "Outbound e-mail",
            "key": "outbound_email",
            "description": "Replies to customers.",
            "kind": "email",
            "writes": True,
            "tables": [
                {
                    "name": "outgoing_messages",
                    "description": "One reply.",
                    "columns": [{"name": "recipient"}, {"name": "body"}],
                    "rows": [],
                }
            ],
        }
    ],
    "cases": [
        {
            "id": "reply",
            "input": "Answer anna@example.org: your invoice copy is attached.",
            "expectations": [{"kind": "approval", "value": "outbound_email_execute"}],
        }
    ],
}

OUTBOX_BLUEPRINT: dict[str, Any] = {
    "name": "support_mail",
    "description": "Answers support mail.",
    "entrypoint": "agent",
    "agents": [
        {
            "name": "dispatcher",
            "description": "Sends the reply.",
            "instructions": "Write the reply into the outgoing messages.",
            "tools": ["connector:outbound_email.execute"],
        }
    ],
}


async def test_the_send_of_a_stand_in_outbox_waits_for_a_person_and_does_not_run() -> None:
    """The declared send tool is gated by the design itself (not by anything the model wrote):
    asked to send, the run stops at the approval and the tool never ran."""

    def respond(call: ModelCall) -> Any:
        system = system_prompt(call)
        if system.startswith("You write the acceptance tests"):
            return structured_output(AcceptanceDesign, OUTBOX_ACCEPTANCE)
        if system.startswith("You design multi-agent systems"):
            return structured_output(BlueprintDesign, OUTBOX_BLUEPRINT)
        if any(isinstance(m, ToolMessage) for m in call.messages):
            return "Sent."
        statement = "INSERT INTO outgoing_messages VALUES ('anna@example.org', 'copy attached')"
        return tool_call("outbound_email_execute", {"statement": statement})

    model = FunctionChatModel(respond)
    project = await design_project("Answer support mail.", model=model, environment=None)

    assert [a.tool for a in project.blueprint.approvals] == ["connector:outbound_email.execute"]
    report = await evaluate_project(project, model=model)

    result = report.cases[0]
    assert result.approvals == ["outbound_email_execute"]
    assert not [e for e in result.events if isinstance(e, ToolResultEvent)]
