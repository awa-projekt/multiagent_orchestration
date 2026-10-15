"""Contract of the MCP client behind `awa._compat.mcp` / `awa.mcp`, pinned against the installed
`langchain-mcp-adapters`."""

from __future__ import annotations

import inspect

import pytest


def broken(awa_module: str, what: str) -> str:
    return f"Upstream contract broken: {what}. awa depends on it in {awa_module}."


def test_adapters_convert_tool_signature_and_metadata() -> None:
    adapters = pytest.importorskip("langchain_mcp_adapters.tools")
    sessions = pytest.importorskip("langchain_mcp_adapters.sessions")
    mcp_types = pytest.importorskip("mcp.types")
    parameters = inspect.signature(adapters.convert_mcp_tool_to_langchain_tool).parameters
    for name in ("session", "tool", "connection", "tool_interceptors", "server_name"):
        assert name in parameters, broken(
            "awa._compat.mcp", f"convert_mcp_tool_to_langchain_tool({name}=)"
        )
    assert "handle_tool_errors" in parameters
    assert callable(sessions.create_session), broken(
        "awa._compat.mcp (HTTP sessions)", "langchain_mcp_adapters.sessions.create_session"
    )

    raw = mcp_types.Tool(
        name="delete_item",
        description="Delete an item.",
        inputSchema={"type": "object", "properties": {"item_id": {"type": "string"}}},
        annotations=mcp_types.ToolAnnotations(destructiveHint=True, readOnlyHint=False),
    )
    converted = adapters.convert_mcp_tool_to_langchain_tool(
        None,
        raw,
        connection={"transport": "stdio", "command": "unused", "args": []},
        server_name="demo",
        handle_tool_errors=True,
    )
    assert converted.metadata["destructiveHint"] is True, broken(
        "awa.tools.risk (MCP annotations mark external actions)",
        "the adapters put MCP annotations at the top of the tool metadata (camelCase)",
    )
    assert converted.response_format == "content_and_artifact"
    assert "runtime" in inspect.signature(converted.coroutine).parameters, broken(
        "awa._compat.mcp", "the converted coroutine takes runtime="
    )
