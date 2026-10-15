"""design_project: acceptance tests first, a real local workspace, rules, connection steps."""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from _helpers.fake_secrets import fake_secret
from awa.blueprints import (
    AcceptanceDesign,
    ApprovalStepDesign,
    BlueprintDesign,
    BlueprintError,
    RuleDesign,
    design_project,
)
from awa.blueprints import _project_files as files
from awa.blueprints._build_loop import evaluate_project
from awa.blueprints._project import (
    TEST_DESIGNER_INSTRUCTIONS,
    RuleArgument,
    RuleExample,
    acceptance_problems,
)
from awa.blueprints._project_files import exec_generated_tools, rule_failures, rules_module
from awa.cli import main
from awa.connectors import ConnectorStatus, EnvironmentReport, ProviderStatus, connector_tools
from awa.testing import FunctionChatModel, ModelCall, structured_output, tool_call

SECRET = fake_secret("sk-", "very-secret-value-123")

ACCEPTANCE: dict[str, Any] = {
    "criteria": ["Order questions are answered with the status.", "Refunds wait for a human."],
    "systems": [
        {
            "name": "Order database",
            "key": "orders",
            "description": "The shop's orders.",
            "kind": "database",
            "writes": True,
            "tables": [
                {
                    "name": "orders",
                    "description": "One order.",
                    "columns": [
                        {"name": "order_id"},
                        {"name": "status"},
                        {"name": "total", "type": "real"},
                        {"name": "express", "type": "boolean"},
                    ],
                    "rows": ["1002, shipped, 49.9, yes", '1003, "open, waiting", 120, no'],
                }
            ],
        },
        {
            "name": "Helpdesk",
            "key": "tickets",
            "description": "Support tickets.",
            "connector": "slack",
            "tables": [
                {
                    "name": "tickets",
                    "description": "A ticket.",
                    "columns": [{"name": "ticket_id"}, {"name": "subject"}],
                    "rows": ["T-1, Late delivery"],
                }
            ],
        },
        {
            "name": "Issue tracker",
            "key": "issues",
            "description": "Engineering issues.",
            "connector": "connector:jira.search_issues",
            "tables": [
                {
                    "name": "issues",
                    "description": "An issue.",
                    "columns": [{"name": "issue_key"}],
                    "rows": ["SHOP-1"],
                }
            ],
        },
    ],
    "rules": [
        {
            "name": "needs_approval",
            "description": "Refunds above 50 EUR need a manager.",
            "arguments": [{"name": "amount", "description": "EUR"}],
            "expression": "amount > 50",
            "examples": [
                {"arguments": '{"amount": 60}', "result": "true"},
                {"arguments": '{"amount": 10}', "result": "false"},
            ],
        }
    ],
    "cases": [
        {
            "id": "Order status!",
            "input": "Where is order 1002?",
            "expectations": [
                {"kind": "contains", "value": "shipped"},
                {"kind": "tool_called", "value": "orders_query"},
                {"kind": "tool_called", "value": "orders_query"},
            ],
        },
        {
            "id": "refund",
            "input": "Refund order 1003.",
            "expectations": [{"kind": "approval", "value": "orders_execute"}],
        },
    ],
}

BLUEPRINT: dict[str, Any] = {
    "name": "order_desk",
    "description": "Answers order questions and refunds orders.",
    "entrypoint": "agent",
    "agents": [
        {
            "name": "desk",
            "description": "Answers customers.",
            "instructions": "Look orders up in the orders table; refund on request.",
            "tools": [
                "connector:orders.query",
                "connector:orders.execute",
                "connector:tickets.query",
                "connector:issues.query",
                "needs_approval",
            ],
        }
    ],
}

ENVIRONMENT = EnvironmentReport(
    providers=(ProviderStatus("anthropic", True),),
    connectors=(
        ConnectorStatus("slack", "Slack", "communication", False, ("SLACK_BOT_TOKEN",)),
        ConnectorStatus("jira", "Jira", "ticketing", True, (), ("jira.search_issues",)),
    ),
)


def system_prompt(call: ModelCall) -> str:
    first = call.messages[0]
    return str(first.content) if isinstance(first, SystemMessage) else ""


def scripted(
    acceptance: list[dict[str, Any]] | None = None, blueprint: dict[str, Any] | None = None
) -> FunctionChatModel:
    """Test designer, architect and the designed desk agent in one model."""

    answers = list(acceptance or [ACCEPTANCE])

    def respond(call: ModelCall) -> Any:
        system = system_prompt(call)
        if system.startswith("You write the acceptance tests"):
            return structured_output(
                AcceptanceDesign, answers.pop(0) if len(answers) > 1 else answers[0]
            )
        if system.startswith("You design multi-agent systems"):
            return structured_output(BlueprintDesign, blueprint or BLUEPRINT)
        request = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
        results = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]
        if "Refund" in request:
            if not results:
                return tool_call("needs_approval", {"amount": 120})
            statement = "UPDATE orders SET status = 'refunded' WHERE order_id = '1003'"
            return tool_call("orders_execute", {"statement": statement})
        if not results:
            sql = "SELECT status FROM orders WHERE order_id = ?"
            return tool_call("orders_query", {"query": sql, "parameters": ["1002"]})
        return f"Order 1002: {results[-1]}"

    return FunctionChatModel(respond)


async def test_acceptance_tests_come_first_and_the_design_is_built_against_them() -> None:
    model = scripted()

    project = await design_project(
        "Answer order questions; refunds need approval.", model=model, environment=ENVIRONMENT
    )

    designer, architect = model.calls[0], model.calls[1]
    assert system_prompt(designer).startswith("You write the acceptance tests")
    brief = str(designer.last_message.content)
    assert "## Your systems\n" in brief and "jira (Jira): jira.search_issues" in brief
    prompt = str(architect.last_message.content)
    assert "## Eval cases (design the smallest system" in prompt
    assert "- order-status: 'Where is order 1002?' -> contains:shipped; tool:orders_query" in prompt
    assert (
        "- connector:orders.execute (tool orders_execute) [changes data; approval added]" in prompt
    )
    assert "waits for a human's approval by itself: tell agents to call it" in prompt
    assert "never tell it to ask for approval" in " ".join(system_prompt(architect).split())
    assert "- needs_approval: Refunds above 50 EUR need a manager." in prompt
    assert "orders (One order.): order_id text, status text, total real, express boolean" in prompt
    assert [case.id for case in project.evals] == ["order-status", "refund"]
    assert project.evals.name == "order_desk"
    assert [a.tool for a in project.blueprint.approvals] == ["connector:orders.execute"]
    assert sorted(project.blueprint.connectors) == ["issues", "orders", "tickets"]
    assert {"orders": project.blueprint.connectors["orders"]} == {
        "orders": {
            "use": "workspace",
            "root": "data",
            "database": "orders.db",
            "description": "Order database: The shop's orders. Tables: orders (One order.): "
            "order_id text, status text, total real, express boolean.",
        }
    }


async def test_every_system_gets_a_connection_step() -> None:
    project = await design_project("Orders.", model=scripted(), environment=ENVIRONMENT)

    orders, tickets, issues = project.connections
    assert (orders.status, orders.connector, orders.env_vars) == (
        "map",
        "sql",
        ("ORDERS_DATABASE_URL",),
    )
    assert orders.config["use"] == "sql"
    assert orders.config["env"] == {"DATABASE_URL": "ORDERS_DATABASE_URL"}
    assert orders.switch == {}  # the sql connector has the workspace's operations
    assert orders.tools == (
        "connector:orders.schema",
        "connector:orders.query",
        "connector:orders.execute",
    )
    assert (tickets.status, tickets.connector, tickets.env_vars) == (
        "configure",
        "slack",
        ("SLACK_BOT_TOKEN",),
    )
    assert tickets.config == {"use": "slack"}
    assert tickets.switch == {
        "connector:tickets.schema": ("connector:tickets",),
        "connector:tickets.query": ("connector:tickets",),
    }
    assert (issues.status, issues.connector, issues.env_vars) == ("ready", "jira", ())
    assert "`awa connectors test slack`" in tickets.instructions
    assert "the agents keep their tools" in orders.instructions
    assert "`connector:tickets.query` -> `connector:tickets`" in tickets.instructions


LOCAL_AND_SQL = EnvironmentReport(
    providers=ENVIRONMENT.providers,
    connectors=(
        *ENVIRONMENT.connectors,
        ConnectorStatus("workspace", "Local workspace", "files", True, (), ("workspace.query",)),
        ConnectorStatus("sql", "SQL database", "database", True, (), ("sql.query",)),
    ),
)


def with_orders_connector(connector: str) -> dict[str, Any]:
    acceptance = copy.deepcopy(ACCEPTANCE)
    acceptance["systems"][0]["connector"] = connector
    return acceptance


async def test_the_local_workspace_never_stands_in_for_a_real_system() -> None:
    model = scripted([with_orders_connector("workspace")])

    project = await design_project("Orders.", model=model, environment=LOCAL_AND_SQL)

    brief = str(model.calls[0].last_message.content)
    assert "workspace (Local workspace)" not in brief and "sql (SQL database)" in brief
    orders = project.connections[0]
    assert (orders.status, orders.connector, orders.env_vars) == (
        "map",
        "sql",
        ("ORDERS_DATABASE_URL",),
    )


async def test_a_configured_sql_connector_still_needs_the_system_s_own_database() -> None:
    project = await design_project(
        "Orders.", model=scripted([with_orders_connector("sql")]), environment=LOCAL_AND_SQL
    )

    orders = project.connections[0]
    assert (orders.status, orders.connector, orders.env_vars) == (
        "configure",
        "sql",
        ("ORDERS_DATABASE_URL",),
    )
    assert "ORDERS_DATABASE_URL" in orders.instructions


@pytest.mark.parametrize(
    ("change", "path", "problem"),
    [
        (
            {"decisions": [{"action": "approve"}, {"action": "approve"}]},
            "cases[1].decisions",
            "2 decision(s) but 1 approval expectation(s)",
        ),
        (
            {
                "expectations": [
                    {"kind": "approval", "value": "orders_execute"},
                    {"kind": "approval", "value": "orders_execute"},
                    {"kind": "approval", "value": "orders_execute"},
                ]
            },
            "cases[1].decisions",
            "the run ends paused at approval 1 (orders_execute) and never reaches the ones after",
        ),
        (
            {
                "expectations": [
                    {"kind": "approval", "value": "orders_execute"},
                    {"kind": "contains", "value": "refunded"},
                ]
            },
            "cases[1].expectations",
            "ends paused at orders_execute without an answer to check with contains",
        ),
        (
            {
                "expectations": [
                    {"kind": "approval", "value": "orders_execute"},
                    {"kind": "no_approval", "value": "orders_execute"},
                ],
                "decisions": [{"action": "approve"}],
            },
            "cases[1].expectations",
            "orders_execute cannot both wait and not wait for a human",
        ),
        (
            {"expectations": [{"kind": "no_approval", "value": "orders_query"}]},
            "cases[1].expectations[0]",
            "no_approval expects a writing tool of a system (writes: true) or an approval step",
        ),
        (
            {"decisions": [{"action": "reject", "note": "No.", "tool": "orders_query"}]},
            "cases[1].decisions[0].tool",
            "'orders_query' has no approval expectation in this case",
        ),
    ],
)
def test_impossible_decisions_are_sent_back(
    change: dict[str, Any], path: str, problem: str
) -> None:
    draft = copy.deepcopy(ACCEPTANCE)
    draft["cases"][1].update(change)

    problems = acceptance_problems(AcceptanceDesign.model_validate(draft), set())

    assert [(p.path, problem in p.message) for p in problems] == [(path, True)]


@pytest.mark.parametrize(
    ("value", "sent_back"),
    [
        # a sentence that asks a person to act is sent back
        ("ask HR to check", True),
        ("Security lead approval rejected", True),
        ("shipped", False),  # one status word
        ("Over budget", False),  # two words
        ("Late delivery", False),  # sample data
        ("my broken blender", False),  # the request
        ("the budget freeze", False),  # a decision note
        ("total 1,190.00 EUR", False),  # a computed amount
    ],
)
def test_contains_checks_text_the_answer_can_quote(value: str, sent_back: bool) -> None:
    draft = copy.deepcopy(ACCEPTANCE)
    draft["cases"][1]["expectations"].append({"kind": "contains", "value": value})
    draft["cases"][1]["input"] = "Refund order 1003, my broken blender."
    draft["cases"][1]["decisions"] = [{"action": "reject", "note": "Wait for The Budget Freeze."}]

    problems = acceptance_problems(AcceptanceDesign.model_validate(draft), set())

    expected = [("cases[1].expectations[1]", True)] if sent_back else []
    found = [(p.path, "wording the answer may phrase differently" in p.message) for p in problems]
    assert found == expected


def test_a_case_follows_the_process_through_its_approvals_to_the_answer() -> None:
    draft = copy.deepcopy(ACCEPTANCE)
    draft["cases"][1]["expectations"] += [
        {"kind": "contains", "value": "refunded"},
        {"kind": "judge", "value": "Confirms the refund."},
    ]
    draft["cases"][1]["decisions"] = [{"action": "approve"}]

    assert acceptance_problems(AcceptanceDesign.model_validate(draft), set()) == []
    # The last approval may stay unanswered: the case then ends paused there.
    acceptance_with_two = copy.deepcopy(ACCEPTANCE)
    acceptance_with_two["cases"][1]["expectations"].append(
        {"kind": "approval", "value": "orders_execute"}
    )
    acceptance_with_two["cases"][1]["decisions"] = [{"action": "reject", "note": "No."}]
    assert acceptance_problems(AcceptanceDesign.model_validate(acceptance_with_two), set()) == []


def test_the_designer_is_told_that_decisions_carry_a_run_through_its_approvals() -> None:
    instructions = " ".join(TEST_DESIGNER_INSTRUCTIONS.split())

    assert "decisions answer the pauses in order" in instructions
    assert "each naming the tool it answers" in instructions
    assert "The judge sees only the request and the final answer" in instructions
    assert "the run stops at the first" not in instructions.lower()
    assert "approval step" in instructions and "total_eur >= 5000" in instructions


APPROVAL_STEP: dict[str, Any] = {
    "name": "manager_approval",
    "description": "A manager approves refunds from 100 EUR.",
    "arguments": [
        {"name": "order_id", "type": "string", "description": "The order."},
        {"name": "amount", "description": "Refund in EUR."},
    ],
    "when": "amount >= 100",
    "examples": [
        {"arguments": '{"order_id": "1003", "amount": 120}', "asks": True},
        {"arguments": '{"order_id": "1002", "amount": 49.9}', "asks": False},
    ],
}


@pytest.mark.parametrize(
    ("change", "path", "problem"),
    [
        ({"when": "total >= 100"}, "approvals[0].when", "reads total, which is not an argument"),
        ({"when": "amount >="}, "approvals[0].when", "invalid expression"),
        (
            {"examples": [{"arguments": '{"amount": 99}', "asks": True}]},
            "approvals[0].examples[0]",
            "the example says it asks, the condition disagrees",
        ),
        (
            {"examples": [{"arguments": '{"amount": 120}', "asks": True}]},
            "approvals[0].examples",
            "example where the person is asked and one where not",
        ),
        (
            {"examples": [{"arguments": "[1]", "asks": True}]},
            "approvals[0].examples[0].arguments",
            "give a JSON object",
        ),
        (
            {"when": "", "examples": [{"arguments": "{}", "asks": False}]},
            "approvals[0].examples[0]",
            "without a condition the person is always asked",
        ),
        ({"name": "orders_query"}, "approvals[0].name", "the tool name 'orders_query' is taken"),
        (
            {"examples": [{"arguments": '{"amount": NaN}', "asks": False}]},
            "approvals[0].examples[0].arguments",
            "NaN is not a JSON value",
        ),
    ],
)
def test_approval_steps_are_checked(change: dict[str, Any], path: str, problem: str) -> None:
    draft = copy.deepcopy(ACCEPTANCE)
    draft["approvals"] = [{**APPROVAL_STEP, **change}]

    problems = acceptance_problems(AcceptanceDesign.model_validate(draft), set())

    assert [(p.path, problem in p.message) for p in problems] == [(path, True)]


async def test_invalid_acceptance_tests_are_sent_back_with_every_problem() -> None:
    broken = copy.deepcopy(ACCEPTANCE)
    broken["systems"][0]["tables"][0]["rows"].append("1004, open")
    broken["rules"][0]["examples"][0]["result"] = "false"
    broken["cases"][0]["expectations"].append({"kind": "tool_called", "value": "lookup_order"})
    broken["cases"][1]["expectations"] = [{"kind": "approval", "value": "orders_query"}]
    model = scripted([broken, ACCEPTANCE])

    await design_project("Orders.", model=model, environment=None)

    repair = str(model.calls[1].last_message.content)
    assert "## Problems to fix" in repair
    assert "systems[0].tables[0].rows: row 3 has 2 value(s), the table has 4 column(s)" in repair
    assert "cases[0].expectations[3]: unknown tool 'lookup_order'" in repair
    assert "cases[1].expectations[0]: approval expects a writing tool of a system" in repair


async def test_rule_examples_are_checked_by_running_the_rule() -> None:
    wrong = copy.deepcopy(ACCEPTANCE)
    wrong["rules"][0]["examples"][0]["result"] = "false"
    model = scripted([wrong, ACCEPTANCE])

    await design_project("Orders.", model=model, environment=None)

    assert "rules[0].examples[0]: expected False, the rule gives True" in str(
        model.calls[1].last_message.content
    )


async def test_the_designer_gives_up_after_max_repairs() -> None:
    broken = {**ACCEPTANCE, "cases": []}

    with pytest.raises(BlueprintError, match="after 1 repair round") as caught:
        await design_project("Orders.", model=scripted([broken]), environment=None, max_repairs=1)

    assert [issue.path for issue in caught.value.issues] == ["cases"]


@pytest.mark.parametrize(
    ("expression", "problem"),
    [
        ("amount.__class__", "Attribute is not allowed"),
        ("__import__('os')", "only abs, max, min, round may be called"),
        ("[x for x in amount]", "ListComp is not allowed"),
        ("note * 1000000", "arithmetic on text or lists is not allowed"),
        ("amount in [0] * 1000000000", "arithmetic on text or lists is not allowed"),
        ("max('a', 'b') * 300000000 == note", "arithmetic on text or lists is not allowed"),
        ("(note if amount else 'x') * 300000000 == note", "arithmetic on text or lists is not"),
        ("round(amount, -1000000000000000)", "round takes its digits as a whole number"),
        ("round(amount, amount)", "round takes its digits as a whole number"),
        ("amount > 1e16", "numbers may be at most 1e15"),
        ("total > 5", "unknown name 'total'"),
        ("amount >", "invalid expression"),
    ],
)
def test_rule_expressions_are_limited_to_safe_arithmetic(expression: str, problem: str) -> None:
    rule = RuleDesign(
        name="check",
        description="A check.",
        arguments=[{"name": "amount"}, {"name": "note", "type": "string"}],
        expression=expression,
        examples=[],
    )

    assert problem in files.rule_problem(rule)


def test_an_argument_may_not_take_the_name_of_a_rule_function() -> None:
    rule = RuleDesign(
        name="check",
        description="A check.",
        arguments=[{"name": "max"}],
        expression="max > 5",
        examples=[],
    )

    assert "rename it (max_value)" in files.rule_problem(rule)


def test_generated_code_and_data_refuse_names_that_are_not_identifiers(tmp_path: Path) -> None:
    draft = AcceptanceDesign.model_validate(ACCEPTANCE)
    draft.systems[0].key = "../orders"
    draft.rules[0].name = "check(): pass\nimport os\ndef x"

    with pytest.raises(BlueprintError, match="not a snake_case name"):
        files.rules_module(draft.rules)
    with pytest.raises(BlueprintError, match="not a snake_case name"):
        files.write_workspace(tmp_path, draft.systems)
    paths = [issue.path for issue in acceptance_problems(draft, set())]
    assert paths[:2] == ["systems[0].key", "rules[0].name"]


def test_the_items_list_of_a_rule_tool_checks_every_value_type() -> None:
    rule = RuleDesign(
        name="large",
        description="Whether an amount is large.",
        arguments=[{"name": "amount"}, {"name": "note", "type": "string"}],
        expression="amount > 100",
        examples=[],
    )
    (tool,) = files.rule_tools([rule])

    results = json.loads(
        tool.invoke(
            {
                "items": [
                    {"amount": "false", "note": "a"},
                    {"amount": True, "note": "a"},
                    {"amount": 150, "note": 3},
                    {"amount": 150, "note": "a"},
                ]
            }
        )
    )

    assert results == [
        {"error": "amount must be float"},
        {"error": "amount must be float"},
        {"error": "note must be str"},
        True,
    ]


def test_a_division_by_zero_fails_only_its_own_item() -> None:
    rule = RuleDesign(
        name="share",
        description="The share of an amount per person.",
        arguments=[{"name": "amount"}, {"name": "people"}],
        expression="amount / people",
        examples=[],
    )
    (tool,) = files.rule_tools([rule])

    results = json.loads(
        tool.invoke({"items": [{"amount": 10, "people": 0}, {"amount": 10, "people": 2}]})
    )

    assert results == [{"error": "ZeroDivisionError: division by zero"}, 5.0]


def test_every_helper_of_the_generated_tools_is_reserved() -> None:
    helpers = set(re.findall(r"^def _(\w+)\(", files._BATCH_HELPERS, flags=re.MULTILINE))

    assert helpers and helpers <= files.RESERVED_NAMES


@pytest.mark.parametrize("name", sorted(files.RESERVED_NAMES))
def test_names_the_generated_tools_use_are_reserved(name: str) -> None:
    draft = AcceptanceDesign.model_validate(ACCEPTANCE)
    draft.rules[0].name = name

    with pytest.raises(BlueprintError, match="reserved name"):
        files.rules_module(draft.rules)
    problems = {issue.path: issue.message for issue in acceptance_problems(draft, set())}
    assert "reserved name" in problems["rules[0].name"]

    draft = AcceptanceDesign.model_validate(ACCEPTANCE)
    draft.rules[0].arguments[0].name = name
    paths = [issue.path for issue in acceptance_problems(draft, set())]
    assert "rules[0].arguments" in paths


def test_rules_that_do_not_compile_are_an_issue_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = AcceptanceDesign.model_validate(ACCEPTANCE)

    def broken(rules: Any) -> Any:
        raise SyntaxError("invalid syntax")

    monkeypatch.setattr(files, "rule_failures", broken)
    problems = {issue.path: issue.message for issue in acceptance_problems(draft, set())}

    assert "do not compile" in problems["rules"]


async def test_the_project_folder_is_complete_and_holds_no_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", SECRET)
    project = await design_project("Orders.", model=scripted(), environment=ENVIRONMENT)

    project.write(tmp_path)

    written = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    assert written == [
        ".env.example",
        ".gitignore",
        "CONNECT.md",
        "README.md",
        "blueprint.yaml",
        "data/issues.db",
        "data/orders.db",
        "data/tickets.db",
        "evals/cases.yaml",
        "tests/test_tools.py",
        "tools.py",
    ]
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()
    with sqlite3.connect(tmp_path / "data" / "orders.db") as db:
        rows = db.execute("SELECT order_id, status, total, express FROM orders").fetchall()
    assert rows == [("1002", "shipped", 49.9, 1), ("1003", "open, waiting", 120.0, 0)]
    tools = (tmp_path / "tools.py").read_text(encoding="utf-8")
    assert "TODO" not in tools and "return amount > 50" in tools
    env = (tmp_path / ".env.example").read_text(encoding="utf-8")
    assert "ORDERS_DATABASE_URL=\n" in env and "SLACK_BOT_TOKEN=\n" in env
    ignored = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert {".env", ".env.bak", ".awa/"} <= set(ignored)
    steps = files.parse_steps((tmp_path / "CONNECT.md").read_text(encoding="utf-8"))
    assert [step["status"] for step in steps] == ["map", "configure", "ready"]
    # A marker edited into something else is skipped, not fatal for `awa connect`.
    assert files.parse_steps('<!-- awa:step {"a": } --> <!-- awa:step {"system": "x"} -->') == [
        {"system": "x"}
    ]
    assert (
        main(["validate", str(tmp_path / "blueprint.yaml"), "--tools", str(tmp_path / "tools.py")])
        == 0
    )


async def test_the_written_rule_tests_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = await design_project("Orders.", model=scripted(), environment=None)
    project.write(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "tools", raising=False)

    namespace: dict[str, Any] = {"__file__": str(tmp_path / "tests" / "test_tools.py")}
    source = (tmp_path / "tests" / "test_tools.py").read_text(encoding="utf-8")
    exec(compile(source, "test_tools.py", "exec"), namespace)
    for case in namespace["CASES"]:
        namespace["test_rule"](*case)


async def test_the_workspace_connector_runs_real_queries_on_the_sample_data() -> None:
    project = await design_project("Orders.", model=scripted(), environment=None)

    with files.workspace(project.systems) as root:
        blueprint = files.on_workspace(project.blueprint, root)
        [query] = connector_tools(
            "orders", config={**blueprint.connectors["orders"], "include": ["query"]}
        )
        answer = query.invoke({"query": "SELECT status FROM orders WHERE order_id = '1003'"})

    assert query.name == "orders_query" and "open, waiting" in answer


STEPPED = copy.deepcopy(ACCEPTANCE)
STEPPED["approvals"] = [APPROVAL_STEP]
STEPPED["cases"] = [
    {
        "id": "large refund",
        "input": "Refund order 1003.",
        "expectations": [
            {"kind": "approval", "value": "manager_approval"},
            {"kind": "approval", "value": "orders_execute"},
            {"kind": "contains", "value": "refunded"},
        ],
        "decisions": [{"action": "approve"}, {"action": "approve"}],
    },
    {
        "id": "small refund",
        "input": "Refund order 1002.",
        "expectations": [
            {"kind": "no_approval", "value": "manager_approval"},
            {"kind": "approval", "value": "orders_execute"},
            {"kind": "contains", "value": "refunded"},
        ],
        "decisions": [{"action": "approve"}],
    },
    {
        "id": "refused refund",
        "input": "Refund order 1003 today.",
        "expectations": [
            {"kind": "approval", "value": "manager_approval"},
            {"kind": "tool_not_called", "value": "orders_execute"},
            {"kind": "contains", "value": "Over budget"},
        ],
        "decisions": [{"action": "reject", "note": "Over budget.", "tool": "manager_approval"}],
    },
]
STEPPED_BLUEPRINT = copy.deepcopy(BLUEPRINT)
STEPPED_BLUEPRINT["agents"][0]["tools"].append("manager_approval")


def stepped() -> FunctionChatModel:
    """The desk asks the manager's approval step, then refunds; a rejection ends the case."""

    def respond(call: ModelCall) -> Any:
        system = system_prompt(call)
        if system.startswith("You write the acceptance tests"):
            return structured_output(AcceptanceDesign, STEPPED)
        if system.startswith("You design multi-agent systems"):
            return structured_output(BlueprintDesign, STEPPED_BLUEPRINT)
        request = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
        results = [str(m.content) for m in call.messages if isinstance(m, ToolMessage)]
        order, amount = ("1003", 120) if "1003" in request else ("1002", 49.9)
        if not results:
            return tool_call("manager_approval", {"order_id": order, "amount": amount})
        if "rejected" in results[-1]:
            return f"Not refunded: {results[-1]}"
        if len(results) == 1:
            statement = f"UPDATE orders SET status = 'refunded' WHERE order_id = '{order}'"
            return tool_call("orders_execute", {"statement": statement})
        return f"Order {order} refunded."

    return FunctionChatModel(respond)


async def test_an_approval_step_waits_for_a_person_only_where_its_condition_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = stepped()
    project = await design_project("Refunds from 100 EUR need a manager.", model=model)

    approvals = {a.tool: a.when for a in project.blueprint.approvals}
    assert approvals == {"connector:orders.execute": None, "manager_approval": "amount >= 100"}
    architect = next(c for c in model.calls if system_prompt(c).startswith("You design"))
    brief = str(architect.last_message.content)
    assert "- manager_approval: A manager approves refunds from 100 EUR. (waits for the " in brief
    assert "when amount >= 100; otherwise approved straight away)" in brief
    assert "decisions: approve, approve" in brief
    assert project.evals["large-refund"].decisions == ({"action": "approve"},) * 2
    # A decision names the pause it answers, so reordered or parallel calls get it right.
    assert "decisions: reject manager_approval ('Over budget.')" in brief
    assert project.evals["refused-refund"].decisions == (
        {"action": "reject", "tool": "manager_approval", "note": "Over budget."},
    )

    report = await evaluate_project(project, model=model)

    assert report.pass_rate == 1.0, report.to_markdown()
    large, small, refused = report.cases
    assert large.approvals == ["manager_approval", "orders_execute"]
    assert small.approvals == ["orders_execute"]
    assert refused.decisions == ["reject:manager_approval"]

    project.write(tmp_path)
    tools = (tmp_path / "tools.py").read_text(encoding="utf-8")
    assert "def manager_approval(order_id: str, amount: float) -> str:" in tools
    assert "waits for the person's decision when amount >= 100" in tools
    cases = (tmp_path / "evals" / "cases.yaml").read_text(encoding="utf-8")
    assert "decisions:" in cases and "note: Over budget." in cases
    assert "tool: manager_approval" in cases
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "tools", raising=False)
    namespace: dict[str, Any] = {"__file__": str(tmp_path / "tests" / "test_tools.py")}
    source = (tmp_path / "tests" / "test_tools.py").read_text(encoding="utf-8")
    exec(compile(source, "test_tools.py", "exec"), namespace)
    for case in namespace["CASES"]:
        namespace["test_rule"](*case)
    for step in namespace["APPROVALS"]:
        namespace["test_approval"](*step)
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "the rules and approval steps" in readme
    assert "## Run it automatically" in readme and "awa add trigger blueprint.yaml" in readme
    assert "awa runs decide" in readme and "--tools" not in readme


def test_approval_steps_alone_make_a_tools_file() -> None:
    step = ApprovalStepDesign.model_validate({**APPROVAL_STEP, "when": ""})

    source = files.rules_module([], [step])
    [tool] = files.rule_tools([], [step])

    assert "every time" in source and '__all__ = ["manager_approval"]' in source
    assert json.loads(tool.invoke({"order_id": "1", "amount": 1})) == {
        "approved": True,
        "order_id": "1",
        "amount": 1.0,
    }


async def test_a_condition_never_ungates_a_change_to_a_system() -> None:
    conditional = {
        **BLUEPRINT,
        "conditional_approvals": [
            {"tool": "connector:orders.execute", "when": "contains(statement, 'DELETE')"}
        ],
    }

    project = await design_project(
        "Orders.", model=scripted(blueprint=conditional), environment=None
    )

    assert [(a.tool, a.when) for a in project.blueprint.approvals] == [
        ("connector:orders.execute", None)
    ]


REPEATED = copy.deepcopy(ACCEPTANCE)
REPEATED["cases"][1] = {
    "id": "refund all",
    "input": "Refund orders 1002 and 1003.",
    "expectations": [
        {"kind": "approval", "value": "orders_execute"},
        {"kind": "contains", "value": "refunded"},
    ],
    "decisions": [{"action": "approve", "tool": "orders_execute", "repeat": True}],
}


def refund_each() -> FunctionChatModel:
    """The desk refunds each order with its own call: one pause per order."""

    def respond(call: ModelCall) -> Any:
        system = system_prompt(call)
        if system.startswith("You write the acceptance tests"):
            return structured_output(AcceptanceDesign, REPEATED)
        if system.startswith("You design multi-agent systems"):
            return structured_output(BlueprintDesign, BLUEPRINT)
        request = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
        done = sum(isinstance(m, ToolMessage) for m in call.messages)
        orders = ["1002", "1003"] if "1002" in request else ["1002"]
        if done < len(orders):
            statement = f"UPDATE orders SET status = 'refunded' WHERE order_id = '{orders[done]}'"
            return tool_call("orders_execute", {"statement": statement})
        return "Orders refunded." if len(orders) > 1 else "Order 1002: shipped."

    return FunctionChatModel(respond)


async def test_a_repeating_decision_answers_every_pause_of_its_tool() -> None:
    model = refund_each()
    instructions = " ".join(TEST_DESIGNER_INSTRUCTIONS.split())
    assert "repeat: true" in instructions
    project = await design_project("Refund orders.", model=model, environment=None)

    assert project.evals["refund-all"].decisions == (
        {"action": "approve", "tool": "orders_execute", "repeat": True},
    )
    architect = next(c for c in model.calls if system_prompt(c).startswith("You design"))
    assert "decisions: approve orders_execute (every pause)" in str(architect.last_message.content)
    report = await evaluate_project(project, model=model)
    refund = next(r for r in report.cases if r.case_id == "refund-all")
    assert refund.approvals == ["orders_execute", "orders_execute"], report.to_markdown()
    assert refund.status == "passed"


@pytest.mark.parametrize(
    ("decisions", "path", "problem"),
    [
        ([{"action": "approve", "repeat": True}], "decisions[0].repeat", "names its tool"),
        (
            [
                {"action": "approve", "tool": "orders_execute", "repeat": True},
                {"action": "reject", "tool": "orders_execute", "repeat": True},
            ],
            "decisions[1].repeat",
            "'orders_execute' has two repeating decisions",
        ),
    ],
)
def test_invalid_repeating_decisions_are_sent_back(
    decisions: list[dict[str, Any]], path: str, problem: str
) -> None:
    draft = copy.deepcopy(REPEATED)
    draft["cases"][1]["decisions"] = decisions

    problems = acceptance_problems(AcceptanceDesign.model_validate(draft), set())

    assert [(p.path, problem in p.message) for p in problems] == [(f"cases[1].{path}", True)]


def test_a_rule_tool_decides_many_cases_and_reports_a_bad_item_on_its_own() -> None:
    rule = RuleDesign(
        name="is_urgent",
        description="An action is due within three days.",
        arguments=[
            RuleArgument(name="action", type="boolean"),
            RuleArgument(name="days", type="integer"),
        ],
        expression="action and days >= 0 and days <= 3",
        examples=[RuleExample(arguments='{"action": true, "days": 1}', result="true")],
    )
    assert rule_failures([rule]) == []
    tool = exec_generated_tools(rules_module([rule]))[0]
    items = [
        {"id": "m1", "action": True, "days": 2},  # an id to match the item by is ignored
        {"id": "m2", "action": True, "days": None},  # a missing value fails only this item
        {"id": "m3", "action": True, "days": 9},
    ]
    assert json.loads(tool.invoke({"items": items})) == [True, {"error": "missing days"}, False]
    assert json.loads(tool.invoke({"action": True})) == {"error": "missing days"}


async def test_writing_again_keeps_an_own_gitignore(tmp_path: Path) -> None:
    project = await design_project("Orders.", model=scripted(), environment=ENVIRONMENT)
    (tmp_path / ".gitignore").write_text("own\n", encoding="utf-8")

    project.write(tmp_path)

    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "own\n"
