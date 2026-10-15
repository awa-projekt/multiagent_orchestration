"""Blueprints with connectors and risky tools: `connectors:` and `connector:` references, `awa connect`, risky combinations, `tool_risks` and MCP lock files."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool, StructuredTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from test_blueprints_project import ENVIRONMENT, SECRET, scripted

from awa.agents import Agent
from awa.blueprints import Blueprint, BlueprintError, design_project
from awa.blueprints._connect import _reads_only, kept_safety, run_connect
from awa.blueprints._mcp import attach_mcp_locks
from awa.core.dotenv import read_env_file
from awa.errors import ConfigurationError
from awa.mcp import MCPLock, MCPServerLock, MCPToolPin
from awa.models import ModelRegistry
from awa.testing import FunctionChatModel, ModelCall, tool_call
from awa.tools.approval import APPROVAL_METADATA_KEY
from awa.tools.risk import mark_tool_risk


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith(("JIRA", "BILLING")):
            monkeypatch.delenv(name)


def blueprint(tools: list[str], **fields: Any) -> Blueprint:
    return Blueprint.from_dict(
        {
            "name": "demo",
            "agents": [{"name": "clerk", "instructions": "Work.", "tools": tools}],
            **fields,
        }
    )


def messages(bp: Blueprint) -> list[str]:
    return [f"{issue.severity}: {issue.path}: {issue.message}" for issue in bp.check()]


def test_unknown_connectors_and_operations_are_errors() -> None:
    issues = messages(blueprint(["connector:nope", "connector:jira.fly"]))
    assert any("error: agents[0].tools[0]: unknown connector 'nope'" in m for m in issues)
    assert any("error: agents[0].tools[1]" in m and "no operation 'fly'" in m for m in issues)


def test_missing_variables_are_warned_by_name() -> None:
    issues = messages(blueprint(["connector:jira"]))
    assert (
        "warning: connectors.jira: connector 'jira' is not configured: set JIRA_URL, "
        "JIRA_EMAIL, JIRA_API_TOKEN (see 'awa connectors show jira'); its tools fail until then"
    ) in issues


def test_write_operations_without_approval_are_warned() -> None:
    ungated = blueprint(
        ["connector:jira.create_issue"], connectors={"jira": {"requires_approval": False}}
    )
    assert any("without approval (requires_approval: false)" in m for m in messages(ungated))
    approved = blueprint(
        ["connector:jira.create_issue"],
        connectors={"jira": {"requires_approval": False}},
        approvals=[{"tool": "connector:jira"}],
    )
    assert not any("without approval" in m or "no agent uses" in m for m in messages(approved))
    assert blueprint(["connector:jira.create_issue"]).approval_agents() == ["clerk"]
    assert blueprint(["connector:jira"]).approval_agents() == []


def test_tool_risks_of_one_read_operation_count_for_the_whole_connector() -> None:
    read = blueprint(
        ["connector:jira"], tool_risks={"connector:jira.search_issues": ["untrusted_input"]}
    )
    assert not any("no agent uses" in m for m in messages(read))
    write = blueprint(
        ["connector:jira"], tool_risks={"connector:jira.create_issue": ["external_action"]}
    )
    assert any("no agent uses tool 'connector:jira.create_issue'" in m for m in messages(write))


def test_approval_agents_read_a_local_spec_next_to_the_blueprint(tmp_path: Path) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "billing.yaml").write_text(
        "openapi: 3.0.3\n"
        "info: {title: Billing, version: '1'}\n"
        "servers: [{url: 'https://billing.example.test'}]\n"
        "paths:\n"
        "  /invoices:\n"
        "    post: {operationId: create_invoice, summary: Create., "
        "responses: {'200': {description: ok}}}\n",
        encoding="utf-8",
    )
    (folder / "system.yaml").write_text(
        "name: shop\n"
        "connectors:\n  billing: {use: openapi, spec: billing.yaml}\n"
        "agents:\n  - name: clerk\n    instructions: Bill.\n"
        "    tools: [connector:billing.create_invoice]\n",
        encoding="utf-8",
    )
    bp = Blueprint.from_yaml_file(folder / "system.yaml")  # the working directory is tmp_path
    assert bp.approval_agents() == ["clerk"]
    with pytest.raises(BlueprintError, match="pass a checkpointer"):
        bp.build()


def test_reading_and_ungated_acting_is_a_risk_warning() -> None:
    bp = blueprint(
        ["connector:jira", "connector:slack.post_message"],
        connectors={"slack": {"requires_approval": False}},
    )
    assert any("lethal trifecta" in m and "connector:slack.post_message" in m for m in messages(bp))
    gated = blueprint(["connector:jira", "connector:slack.post_message"])
    assert not any("trifecta" in m for m in messages(gated))


def test_connector_sections_are_checked() -> None:
    issues = messages(
        blueprint(
            ["connector:jira"], connectors={"jira": {"colour": "red"}, "crm": {"use": "rest"}}
        )
    )
    assert any("unknown option 'colour'" in m for m in issues)
    assert "warning: connectors.crm: connector 'crm' is declared but no agent uses its tools" in (
        issues
    )
    # A malformed option is an issue, not a crash of the check.
    malformed = messages(blueprint(["connector:jira"], connectors={"jira": {"include": 5}}))
    assert any("include must be an operation name or a list of them" in m for m in malformed)


def test_rest_instances_from_the_blueprint_are_checked_offline() -> None:
    bp = blueprint(
        ["connector:billing.get_invoice", "connector:billing.nothing"],
        connectors={
            "billing": {
                "use": "rest",
                "operations": [
                    {
                        "name": "get_invoice",
                        "description": "Read an invoice.",
                        "path": "/i/{id}",
                        "params": [{"name": "id", "description": "Id.", "in": "path"}],
                    }
                ],
            }
        },
    )
    issues = messages(bp)
    assert any("tools[1]" in m and "no operation 'nothing'" in m for m in issues)
    assert any("set BILLING_URL" in m for m in issues)


def workspace(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    (folder / "data").mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(folder / "data" / "orders.db")) as db, db:
        db.execute("CREATE TABLE orders (id INTEGER, status TEXT)")
        db.execute("INSERT INTO orders VALUES (7, 'shipped')")
    (folder / "system.yaml").write_text(
        "name: shop\n"
        "connectors:\n  orders: {use: workspace, root: data, database: orders.db}\n"
        "agents:\n  - name: clerk\n    instructions: Answer order questions.\n"
        "    tools: [connector:orders.query, connector:orders.execute]\n",
        encoding="utf-8",
    )
    return folder


async def test_a_blueprint_runs_on_its_local_workspace(tmp_path: Path) -> None:
    folder = workspace(tmp_path)
    bp = Blueprint.from_yaml_file(folder / "system.yaml")
    assert not [i for i in bp.check() if i.severity == "error"]

    def respond(call: ModelCall) -> Any:
        if call.index == 0:
            return tool_call("orders_query", {"query": "SELECT status FROM orders WHERE id = 7"})
        return f"Answer from: {call.last_message.content}"

    models = ModelRegistry()
    models.register("default", FunctionChatModel(respond))
    agent = bp.build(model_registry=models, checkpointer=InMemorySaver())
    assert isinstance(agent, Agent)
    tools = {tool.name: tool for tool in agent.tools}
    assert set(tools) == {"orders_query", "orders_execute"}
    assert tools["orders_execute"].metadata[APPROVAL_METADATA_KEY] is True
    result = await agent.run("Where is order 7?")
    assert "shipped" in result.output


def test_blueprint_approvals_replace_the_connector_gate(tmp_path: Path) -> None:
    folder = workspace(tmp_path)
    bp = Blueprint.from_yaml_file(folder / "system.yaml").model_copy(
        update={
            "approvals": Blueprint.from_dict(
                {
                    "name": "x",
                    "agents": [{"name": "a", "instructions": "b"}],
                    "approvals": [{"tool": "connector:orders.execute"}],
                }
            ).approvals
        }
    )
    models = ModelRegistry()
    models.register("default", FunctionChatModel(lambda call: "ok"))
    agent = bp.build(model_registry=models, checkpointer=InMemorySaver())
    assert isinstance(agent, Agent)
    execute = next(tool for tool in agent.tools if tool.name == "orders_execute")
    assert APPROVAL_METADATA_KEY not in execute.metadata  # the blueprint's approval wraps it


VARIABLES = ("ORDERS_DATABASE_URL", "SLACK_BOT_TOKEN")


@pytest.fixture
async def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in VARIABLES:
        monkeypatch.setenv(name, "")  # restored (removed) after the test
    design = await design_project("Orders.", model=scripted(), environment=ENVIRONMENT)
    design.write(tmp_path)
    return tmp_path


class ConnectorTester:
    def __init__(self, passed: bool = True) -> None:
        self.passed = passed
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, name: str, config: Mapping[str, Any]) -> tuple[bool, str]:
        self.calls.append((name, dict(config)))
        return self.passed, "answered" if self.passed else "401 Unauthorized"


def answers(values: dict[str, str]) -> Any:
    def secret(prompt: str) -> str:
        return next((value for name, value in values.items() if name in prompt), "")

    return secret


async def test_connect_saves_settings_tests_and_switches_the_blueprint(project: Path) -> None:
    out = io.StringIO()
    tester = ConnectorTester()
    values = {"ORDERS_DATABASE_URL": "sqlite:///orders.db", "SLACK_BOT_TOKEN": SECRET}

    code = run_connect(project, secret=answers(values), out=out, test=tester)

    assert code == 0
    blueprint = Blueprint.from_yaml(project / "blueprint.yaml")
    assert blueprint.connectors["orders"]["use"] == "sql"
    assert blueprint.connectors["orders"]["env"] == {"DATABASE_URL": "ORDERS_DATABASE_URL"}
    assert blueprint.agent("desk").tools[:2] == [
        "connector:orders.query",
        "connector:orders.execute",
    ]
    assert [name for name, _ in tester.calls] == ["orders", "tickets", "issues"]
    env = read_env_file(project / ".env")
    assert env["SLACK_BOT_TOKEN"] == SECRET
    assert env["ORDERS_DATABASE_URL"] == "sqlite:///orders.db"
    assert SECRET not in out.getvalue()
    assert "designed by awa" in (project / "blueprint.yaml").read_text(encoding="utf-8")

    again = io.StringIO()
    assert run_connect(project, secret=answers({}), out=again, test=tester) == 0
    assert again.getvalue().count("connected.") == 3


async def test_a_failed_test_leaves_the_workspace_in_place(project: Path) -> None:
    out = io.StringIO()
    values = {"ORDERS_DATABASE_URL": "postgresql://db/orders", "SLACK_BOT_TOKEN": SECRET}

    code = run_connect(project, secret=answers(values), out=out, test=ConnectorTester(passed=False))

    assert code == 1
    assert (
        Blueprint.from_yaml(project / "blueprint.yaml").connectors["orders"]["use"] == "workspace"
    )
    assert "the connection test failed: 401 Unauthorized" in out.getvalue()


async def test_skipped_settings_are_reported_as_missing(project: Path) -> None:
    out = io.StringIO()
    tester = ConnectorTester()

    code = run_connect(project, secret=answers({}), out=out, test=tester)

    assert code == 1
    assert "still missing: ORDERS_DATABASE_URL" in out.getvalue()
    assert [name for name, _ in tester.calls] == ["issues"]  # jira needs nothing more


def test_a_folder_without_connect_md_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="no CONNECT.md"):
        run_connect(tmp_path)


async def test_connect_keeps_safety_options_and_rewrites_instructions(project: Path) -> None:
    path = project / "blueprint.yaml"
    blueprint = Blueprint.from_yaml(path)
    data = blueprint.to_dict()
    data["connectors"]["issues"] = {**data["connectors"]["issues"], "read_only": True}
    desk = next(agent for agent in data["agents"] if agent["name"] == "desk")
    stand_in = next(ref for ref in desk["tools"] if ref.startswith("connector:issues."))
    desk["instructions"] = f"Look the issue up with {stand_in} first."
    path.write_text(Blueprint.from_dict(data).to_yaml(), encoding="utf-8")
    values = {"ORDERS_DATABASE_URL": "sqlite:///orders.db", "SLACK_BOT_TOKEN": SECRET}

    run_connect(project, secret=answers(values), out=io.StringIO(), test=ConnectorTester())

    connected = Blueprint.from_yaml(path)
    assert connected.connectors["issues"]["use"] == "jira"
    assert connected.connectors["issues"]["read_only"] is True
    instructions = connected.agent("desk").instructions
    assert stand_in not in instructions
    assert "connector:issues" in instructions


def test_kept_safety_keeps_read_only_and_known_operations() -> None:
    stand_in = {"use": "workspace", "read_only": True, "include": ["query", "list_messages"]}

    kept = kept_safety(stand_in, {"use": "outlook"})

    assert kept == {"use": "outlook", "read_only": True, "include": ["list_messages"]}
    assert kept_safety(stand_in, {"use": "outlook", "read_only": False})["read_only"] is False


def test_only_read_stand_in_tools_make_the_real_system_read_only() -> None:
    connectors = {"outlook": {"use": "workspace", "root": "data", "database": "outlook.db"}}

    assert _reads_only({"connector:outlook.query": ["connector:outlook"]}, connectors)
    assert not _reads_only({"connector:outlook.execute": ["connector:outlook"]}, connectors)
    assert not _reads_only({}, connectors)


def test_check_warns_about_a_stale_tool_in_instructions() -> None:
    blueprint = Blueprint.from_dict(
        {
            "name": "triage",
            "connectors": {"outlook": {"use": "outlook", "read_only": True}},
            "agents": [
                {
                    "name": "reader",
                    "instructions": "Read mail with connector:outlook.query, then answer.",
                    "tools": ["connector:outlook.list_messages"],
                }
            ],
        }
    )

    found = [issue.message for issue in blueprint.check()]

    assert any("mention connector:outlook.query" in message for message in found)


@tool
def fetch_page(url: str) -> str:
    """Fetch a web page."""

    return "page"


@tool
def send_email(to: str, body: str) -> str:
    """Send an e-mail."""

    return "sent"


@tool
def lookup_customer(customer_id: str) -> str:
    """Look up a customer."""

    return "ACME"


def blueprint_risk(tools: list[str], **extra: Any) -> Blueprint:
    return Blueprint.from_dict(
        {
            "name": "desk",
            "mcp_servers": [{"name": "web", "url": "https://web.example.com/mcp"}],
            "agents": [{"name": "assistant", "instructions": "Help.", "tools": tools}],
            **extra,
        }
    )


def risk_warnings(bp: Blueprint, **kwargs: Any) -> list[str]:
    return [
        f"{issue.path}: {issue.message}"
        for issue in bp.check(**kwargs)
        if issue.severity == "warning" and "untrusted content" in issue.message
    ]


def test_untrusted_input_with_an_ungated_action_warns() -> None:
    [warning] = risk_warnings(blueprint_risk(["fetch_page", "send_email"]))
    assert warning.startswith(
        "agents[0].tools: agent 'assistant' reads untrusted content (fetch_page) and can act "
        "without approval (send_email)"
    )
    assert "Gate the actions with an approval" in warning
    assert "trifecta" not in warning


def test_the_full_trifecta_is_named() -> None:
    [warning] = risk_warnings(blueprint_risk(["mcp:web", "lookup_customer", "send_email"]))
    assert "reads untrusted content (mcp:web)" in warning
    assert "reads private data (lookup_customer)" in warning and "lethal trifecta" in warning


def test_an_approval_gates_the_action() -> None:
    gated = blueprint_risk(["fetch_page", "send_email"], approvals=[{"tool": "send_email"}])
    assert risk_warnings(gated) == []
    assert risk_warnings(blueprint_risk(["fetch_page", "lookup_customer"])) == []
    assert risk_warnings(blueprint_risk(["send_email", "lookup_customer"])) == []


def test_tool_risks_correct_the_heuristics() -> None:
    quiet = blueprint_risk(
        ["fetch_page", "send_email"], tool_risks={"send_email": {"external_action": False}}
    )
    assert risk_warnings(quiet) == []
    loud = blueprint_risk(
        ["read_notes", "archive"],
        tool_risks={"read_notes": ["untrusted_input"], "archive": ["external_action"]},
    )
    assert len(risk_warnings(loud)) == 1
    unused = blueprint_risk(["fetch_page"], tool_risks={"nothing": ["private_data"]})
    assert "tool_risks.nothing: no agent uses tool 'nothing'" in [
        f"{i.path}: {i.message}" for i in unused.check()
    ]


def test_tool_annotations_are_read_when_tools_are_given() -> None:
    bp = blueprint_risk(["reader", "send_email"])
    assert risk_warnings(bp) == []  # "reader" says nothing by its name
    reader = StructuredTool.from_function(func=lambda url: url, name="reader", description="R.")
    annotated = mark_tool_risk(reader, untrusted_input=True)
    assert len(risk_warnings(bp, tools=[annotated, send_email])) == 1


def mcp_tool(server: str, name: str, **metadata: Any) -> BaseTool:
    async def run(**kwargs: Any) -> str:
        return "x"

    return StructuredTool(
        name=f"{server}_{name}",
        description=name,
        args_schema={"type": "object", "properties": {}},
        coroutine=run,
        metadata={"mcp_server": server, "mcp_tool": name, **metadata},
    )


def test_mcp_tools_count_as_untrusted_and_their_actions_are_found() -> None:
    bp = blueprint_risk(["mcp:web"])
    assert risk_warnings(bp) == []  # without loaded tools, the actions are unknown
    loaded = [mcp_tool("web", "search"), mcp_tool("web", "post_comment")]
    [warning] = risk_warnings(bp, tools=loaded)
    assert "untrusted content (mcp:web/search, mcp:web/post_comment)" in warning
    assert "without approval (mcp:web/post_comment)" in warning
    gated = blueprint_risk(["mcp:web"], approvals=[{"tool": "mcp:web/post_comment"}])
    assert risk_warnings(gated, tools=loaded) == []
    whole = blueprint_risk(["mcp:web"], approvals=[{"tool": "mcp:web"}])
    assert risk_warnings(whole, tools=loaded) == []
    trusted = blueprint_risk(
        ["mcp:web/search", "send_email"], tool_risks={"mcp:web": {"untrusted_input": False}}
    )
    assert risk_warnings(trusted) == []
    destructive = [mcp_tool("web", "archive", destructiveHint=True)]
    assert len(risk_warnings(bp, tools=destructive)) == 1


def test_tool_risks_round_trip_through_yaml() -> None:
    bp = blueprint_risk(
        ["fetch_page"], tool_risks={"fetch_page": ["untrusted_input", "external_action"]}
    )
    assert bp.to_dict()["tool_risks"] == {
        "fetch_page": {"untrusted_input": True, "external_action": True}
    }
    assert Blueprint.from_yaml_text(bp.to_yaml()) == bp
    with pytest.raises(BlueprintError, match="unknown risk flag"):
        blueprint_risk([], tool_risks={"x": ["egress"]})


def write_blueprint(folder: Path, **server: Any) -> Path:
    path = folder / "desk.yaml"
    data = {
        "name": "desk",
        "mcp_servers": [
            {"name": "crm", "command": "crm-mcp", **server},
            {"name": "erp", "command": "erp"},
        ],
        "agents": [{"name": "a", "tools": ["mcp:crm", "mcp:erp"]}],
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def pins(digit: str) -> MCPServerLock:
    return MCPServerLock(tools={"lookup": MCPToolPin(digest="sha256:" + digit * 64)})


def test_a_lock_file_next_to_the_blueprint_pins_its_servers(tmp_path: Path) -> None:
    path = write_blueprint(tmp_path)
    loaded = Blueprint.from_yaml_file(path)
    assert attach_mcp_locks(loaded, path) is loaded  # no lock file: unchanged
    MCPLock(servers={"crm": pins("1")}).write(tmp_path / "mcp.lock.json")
    attached = attach_mcp_locks(loaded, path)
    crm, erp = attached.mcp_servers
    assert crm.pinned() == pins("1") and erp.pinned() is None
    assert attached.to_dict() == loaded.to_dict()  # pins are not serialised


def test_a_relative_lock_path_is_resolved_against_the_blueprint(tmp_path: Path) -> None:
    (tmp_path / "locks").mkdir()
    MCPLock(servers={"crm": pins("2")}).write(tmp_path / "locks" / "crm.json")
    path = write_blueprint(tmp_path, lock="locks/crm.json")
    attached = attach_mcp_locks(Blueprint.from_yaml_file(path), path)
    assert attached.mcp_servers[0].pinned() == pins("2")
    assert attached.to_dict()["mcp_servers"][0]["lock"] == "locks/crm.json"

    missing = write_blueprint(tmp_path, lock="locks/none.json")
    with pytest.raises(BlueprintError, match="not found"):
        attach_mcp_locks(Blueprint.from_yaml_file(missing), missing)
    MCPLock(servers={"erp": pins("3")}).write(tmp_path / "locks" / "erp.json")
    other = write_blueprint(tmp_path, lock="locks/erp.json")
    with pytest.raises(BlueprintError, match="no entry for server 'crm'"):
        attach_mcp_locks(Blueprint.from_yaml_file(other), other)
