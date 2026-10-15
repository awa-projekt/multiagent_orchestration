"""Offline tests for `examples/09_mcp_tools.py`: a real stdio MCP server, fake models."""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from _helpers.examples import load_example
from awa.testing import FakeChatModel, tool_call

pytest.importorskip("langchain_mcp_adapters")


def tool_texts(result: Any) -> list[str]:
    """The tool outputs, taken out of the `<tool_output>` frame MCP tools add."""

    texts = []
    for message in result.messages:
        if isinstance(message, ToolMessage):
            content = message.content
            text = content if isinstance(content, str) else "".join(b["text"] for b in content)
            assert text.startswith("<tool_output ") and text.endswith("\n</tool_output>")
            texts.append(json.loads(text.split("\n", 1)[1].rsplit("\n", 1)[0])["content"])
    return texts


async def test_agent_uses_the_crm_tools_through_a_toolset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRM_REGION", "dach")
    example = load_example("09_mcp_tools")
    async with example.MCPToolset(example.CRM) as tools:
        assert [t.name for t in tools] == [
            "crm_create_ticket",
            "crm_find_customer",
            "crm_list_open_tickets",
        ]
        manager = example.build_account_manager(tools)
        model = FakeChatModel(
            [
                tool_call("crm_find_customer", {"name": "acme"}),
                tool_call("crm_list_open_tickets", {"customer_id": "C-100"}),
                "ACME has two open tickets: T-1 and T-2.",
            ]
        )
        with manager.override(model=model):
            result = await manager.run("Which open tickets does ACME have?")
    assert result.output == "ACME has two open tickets: T-1 and T-2."
    customer, tickets = tool_texts(result)
    assert json.loads(customer) == {
        "customer_id": "C-100",
        "name": "ACME Corp",
        "tier": "gold",
        "region": "dach",
    }
    assert [t["id"] for t in json.loads(tickets)["tickets"]] == ["T-1", "T-2"]


async def test_blueprint_desk_needs_approval_for_new_tickets() -> None:
    example = load_example("09_mcp_tools")
    blueprint = example.desk_blueprint()
    assert blueprint.check() == []
    desk = await example.build_desk()
    assert "crm_delete_customer" not in [t.name for t in desk.tools]
    model = FakeChatModel(
        [
            tool_call("crm_find_customer", {"name": "globex"}),
            tool_call(
                "crm_create_ticket", {"customer_id": "C-200", "subject": "Invoice address wrong"}
            ),
            "I opened ticket T-3 for Globex.",
        ]
    )
    with desk.override(model=model):
        paused = await desk.run("Globex says their last invoice went to the wrong address.")
        assert paused.is_interrupted
        assert paused.interrupts[0]["value"]["tool"] == "crm_create_ticket"
        assert paused.thread_id is not None
        result = await desk.resume(paused.thread_id, {"action": "approve"})
    assert result.output == "I opened ticket T-3 for Globex."
    ticket = json.loads(tool_texts(result)[-1])
    assert ticket == {"id": "T-3", "customer_id": "C-200", "subject": "Invoice address wrong"}
