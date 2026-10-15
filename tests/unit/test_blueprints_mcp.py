"""Blueprints with MCP servers: `mcp_servers`, `mcp:` tool references, check, build and abuild."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, StructuredTool, tool
from langgraph.checkpoint.memory import InMemorySaver

from awa.agents import Agent
from awa.blueprints import Blueprint, BlueprintError, aload
from awa.testing import FakeChatModel, tool_call

SERVER_SCRIPT = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_test_server.py"


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order."""

    return f"order {order_id}: shipped"


def fake_mcp_tool(server: str, name: str, *, prefix: str | None = None) -> BaseTool:
    """A tool shaped like one loaded from an MCP server (no connection needed)."""

    async def run(**kwargs: Any) -> str:
        return f"{server}.{name}({json.dumps(kwargs, sort_keys=True)})"

    return StructuredTool(
        name=f"{server if prefix is None else prefix}_{name}",
        description=f"{name} on {server}",
        args_schema={"type": "object", "properties": {"customer_id": {"type": "string"}}},
        coroutine=run,
        metadata={"mcp_server": server, "mcp_tool": name},
    )


CRM_TOOLS = [fake_mcp_tool("crm", "lookup_customer"), fake_mcp_tool("crm", "delete_customer")]


def blueprint(tools: list[str], **extra: Any) -> Blueprint:
    data: dict[str, Any] = {
        "name": "desk",
        "mcp_servers": [
            {"name": "crm", "command": "crm-mcp", "exclude": ["drop_*"]},
            {"name": "tickets", "url": "https://tickets.example.com/mcp"},
        ],
        "agents": [{"name": "support", "instructions": "Help.", "tools": tools}],
        **extra,
    }
    return Blueprint.from_dict(data)


def errors(bp: Blueprint, **kwargs: Any) -> list[str]:
    return [f"{i.path}: {i.message}" for i in bp.check(**kwargs) if i.severity == "error"]


def test_mcp_servers_parse_from_yaml_and_round_trip_without_secrets() -> None:
    bp = Blueprint.from_yaml(
        """
name: desk
mcp_servers:
  - name: tickets
    url: https://tickets.example.com/mcp
    headers: {Authorization: "Bearer ${TICKETS_TOKEN}"}
    include: [search_*]
agents:
  - name: support
    tools: [mcp:tickets]
"""
    )
    server = bp.mcp_servers[0]
    assert (server.name, server.transport, server.include) == ("tickets", "http", ["search_*"])
    data = bp.to_dict()
    assert data["mcp_servers"] == [
        {
            "name": "tickets",
            "transport": "http",
            "url": "https://tickets.example.com/mcp",
            "headers": {"Authorization": "Bearer ${TICKETS_TOKEN}"},
            "include": ["search_*"],
        }
    ]
    assert Blueprint.from_yaml(bp.to_yaml()) == bp
    assert [s.name for s in bp.active_mcp_servers()] == ["tickets"]


def test_invalid_server_config_is_a_blueprint_error() -> None:
    with pytest.raises(BlueprintError, match=r"mcp_servers\[0\]"):
        Blueprint.from_dict(
            {"name": "d", "mcp_servers": [{"name": "crm"}], "agents": [{"name": "a"}]}
        )


def test_check_accepts_valid_mcp_references_offline() -> None:
    bp = blueprint(["mcp:crm", "mcp:tickets/search", "lookup_order"])
    assert bp.check() == []
    # A tool catalog does not need to contain MCP tools; they are loaded later.
    assert errors(bp, tools=[lookup_order]) == []


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        ("mcp:erp", "unknown MCP server 'erp' (declared in mcp_servers: crm, tickets)"),
        ("mcp:", "invalid MCP reference 'mcp:'"),
        ("mcp:crm/", "invalid MCP reference 'mcp:crm/'"),
        ("mcp:crm/a/b", "invalid MCP reference 'mcp:crm/a/b'"),
        ("mcp:crm/drop_table", "tool 'drop_table' is left out by the include/exclude filters"),
    ],
)
def test_check_reports_bad_mcp_references(reference: str, message: str) -> None:
    found = errors(blueprint([reference, "mcp:tickets"]))
    assert len(found) == 1 and found[0].startswith(f"agents[0].tools[0]: {message}")


def test_check_reports_overlapping_references_duplicate_and_unused_servers() -> None:
    bp = blueprint(["mcp:crm", "mcp:crm/lookup_customer"])
    assert errors(bp) == [
        "agents[0].tools[1]: 'mcp:crm/lookup_customer' is already included by 'mcp:crm'"
    ]
    warnings = [i.message for i in bp.check() if i.severity == "warning"]
    assert warnings == ["MCP server 'tickets' is declared but no agent uses its tools"]

    twice = Blueprint.from_dict(
        {
            "name": "d",
            "mcp_servers": [{"name": "crm", "command": "a"}, {"name": "crm", "command": "b"}],
            "agents": [{"name": "a", "tools": ["mcp:crm"]}],
        }
    )
    assert errors(twice) == ["mcp_servers[1].name: duplicate MCP server name 'crm'"]


def test_mcp_references_are_not_import_references() -> None:
    bp = blueprint(["mcp:crm", "mcp:tickets"])
    system = bp.build(tools=[*CRM_TOOLS, fake_mcp_tool("tickets", "search")])
    assert isinstance(system, Agent)


def test_approvals_on_mcp_tools_count_agents_listing_the_whole_server() -> None:
    bp = blueprint(
        ["mcp:crm", "mcp:tickets"],
        approvals=[{"tool": "mcp:crm/delete_customer"}, {"tool": "mcp:tickets/nothing"}],
    )
    assert bp.approval_agents() == ["support"]
    assert errors(bp) == []
    unused = blueprint(["mcp:tickets"], approvals=[{"tool": "mcp:crm/delete_customer"}])
    assert "approvals[0].tool: no agent uses tool 'mcp:crm/delete_customer'" in errors(unused)


def test_build_without_loaded_mcp_tools_explains_abuild() -> None:
    with pytest.raises(BlueprintError, match=r"no tools of MCP server 'crm' were passed.*abuild"):
        blueprint(["mcp:crm", "mcp:tickets"]).build(tools=[lookup_order])


def test_build_resolves_mcp_references_from_loaded_tools() -> None:
    tickets = [fake_mcp_tool("tickets", "search"), fake_mcp_tool("tickets", "close")]
    system = blueprint(["lookup_order", "mcp:crm", "mcp:tickets/search"]).build(
        tools=[lookup_order, *CRM_TOOLS, *tickets]
    )
    assert isinstance(system, Agent)
    assert [t.name for t in system.tools] == [
        "lookup_order",
        "crm_lookup_customer",
        "crm_delete_customer",
        "tickets_search",
    ]
    with pytest.raises(BlueprintError, match="has no tool 'archive' .*close, search"):
        blueprint(["mcp:crm", "mcp:tickets/archive"]).build(tools=[*CRM_TOOLS, *tickets])


async def test_approval_gates_one_tool_of_a_whole_server() -> None:
    bp = blueprint(["mcp:crm", "mcp:tickets"], approvals=[{"tool": "mcp:crm/delete_customer"}])
    system = bp.build(
        tools=[*CRM_TOOLS, fake_mcp_tool("tickets", "search")], checkpointer=InMemorySaver()
    )
    assert isinstance(system, Agent)
    model = FakeChatModel(
        [
            tool_call("crm_lookup_customer", {"customer_id": "C-1"}),
            tool_call("crm_delete_customer", {"customer_id": "C-1"}),
            "Deleted.",
        ]
    )
    with system.override(model=model):
        result = await system.run("Delete C-1")
        assert result.is_interrupted
        assert result.interrupts[0]["value"]["tool"] == "crm_delete_customer"
        assert result.thread_id is not None
        done = await system.resume(result.thread_id, {"action": "approve"})
    assert done.output == "Deleted."
    contents = [str(m.content) for m in done.messages if isinstance(m, ToolMessage)]
    assert contents == [
        'crm.lookup_customer({"customer_id": "C-1"})',
        'crm.delete_customer({"customer_id": "C-1"})',
    ]


def crm_blueprint_yaml() -> str:
    return f"""
name: crm-desk
mcp_servers:
  - name: crm
    command: {json.dumps(sys.executable)}
    args: [{json.dumps(str(SERVER_SCRIPT))}]
    env: {{CRM_REGION: "${{CRM_TEST_REGION:-emea}}"}}
    include: [lookup_customer, get.region]
  - name: unused
    command: never-started
agents:
  - name: support
    instructions: Answer from the CRM.
    tools: [lookup_order, "mcp:crm"]
"""


async def test_aload_connects_and_the_agent_calls_mcp_tools() -> None:
    pytest.importorskip("langchain_mcp_adapters")
    system = await aload(crm_blueprint_yaml(), tools=[lookup_order])
    assert isinstance(system, Agent)
    assert [t.name for t in system.tools] == [
        "lookup_order",
        "crm_get_region",
        "crm_lookup_customer",
    ]
    model = FakeChatModel(
        [
            [
                tool_call("crm_lookup_customer", {"customer_id": "C-2"}),
                tool_call("crm_get_region", {}),
            ],
            "Globex, emea.",
        ]
    )
    with system.override(model=model):
        result = await system.run("Who is C-2?")
    contents = [m.content for m in result.messages if isinstance(m, ToolMessage)]
    # MCP tool output reaches the model framed as external data.
    frames = [json.loads(c[0]["text"].split("\n", 1)[1].rsplit("\n", 1)[0]) for c in contents]
    assert [f["content"] for f in frames] == ["Globex (silver tier)", "emea"]
    assert {f["source"] for f in frames} == {"mcp:crm"}


async def test_abuild_checks_before_connecting_and_reports_server_failures() -> None:
    pytest.importorskip("langchain_mcp_adapters")
    broken = Blueprint.from_dict(
        {
            "name": "d",
            "mcp_servers": [{"name": "crm", "command": "awa-no-such-mcp-server-cmd"}],
            "agents": [{"name": "a", "tools": ["mcp:crm", "mcp:erp"]}],
        }
    )
    with pytest.raises(BlueprintError, match="unknown MCP server 'erp'"):
        await broken.abuild()
    unreachable = Blueprint.from_dict(
        {
            "name": "d",
            "mcp_servers": [{"name": "crm", "command": "awa-no-such-mcp-server-cmd"}],
            "agents": [{"name": "a", "tools": ["mcp:crm"]}],
        }
    )
    with pytest.raises(BlueprintError, match="Cannot load MCP tools: .*'crm'"):
        await unreachable.abuild()


async def test_abuild_without_mcp_servers_is_build() -> None:
    bp = Blueprint.from_dict({"name": "d", "agents": [{"name": "a", "tools": ["lookup_order"]}]})
    system = await bp.abuild(tools=[lookup_order])
    assert isinstance(system, Agent) and [t.name for t in system.tools] == ["lookup_order"]
