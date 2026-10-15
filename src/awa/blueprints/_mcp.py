"""MCP tool references in blueprints: `mcp:<server>` and `mcp:<server>/<tool>`."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from awa._internal.upstream.tools import BaseTool
from awa.blueprints.errors import BlueprintError
from awa.errors import MCPError
from awa.mcp import MCP_LOCK_FILENAME, MCPLock, MCPServer

if TYPE_CHECKING:
    from awa.blueprints.schema import Blueprint

MCP_PREFIX = "mcp:"
"""Prefix of a tool reference naming the tools of a declared MCP server."""


def is_mcp_reference(reference: str) -> bool:
    """Return whether `reference` names MCP tools (`mcp:<server>[/<tool>]`)."""

    return reference.startswith(MCP_PREFIX)


def parse_mcp_reference(reference: str) -> tuple[str, str | None]:
    """Split `mcp:<server>[/<tool>]` into the server name and the tool name (or `None`)."""

    server, separator, tool = reference[len(MCP_PREFIX) :].partition("/")
    return server, tool if separator else None


def uses_tool(tools: Sequence[str], reference: str) -> bool:
    """Whether an agent's `tools` include `reference` directly or through its server (`mcp:crm`, `connector:jira`)."""

    if reference in tools:
        return True
    if reference.startswith("connector:") and "." not in reference:
        return any(tool.startswith(f"{reference}.") for tool in tools)
    if is_mcp_reference(reference):
        server, tool = parse_mcp_reference(reference)
        return tool is not None and f"{MCP_PREFIX}{server}" in tools
    return False


def loaded_tools(tools: Sequence[BaseTool], server: str) -> list[BaseTool]:
    """Return the tools in `tools` that were loaded from the MCP server `server`."""

    return [tool for tool in tools if (tool.metadata or {}).get("mcp_server") == server]


def mcp_tool_name(tool: BaseTool) -> str:
    """Return the name the MCP server gave `tool`."""

    return str((tool.metadata or {}).get("mcp_tool", tool.name))


def attach_mcp_locks(blueprint: Blueprint, source: str | os.PathLike[str]) -> Blueprint:
    """Pin the MCP servers of a blueprint loaded from the file `source`.

    A server's `lock` path resolves against the blueprint's folder; a server with neither `pin`
    nor `lock` is pinned from the `mcp.lock.json` next to the blueprint when it has an entry.
    Pins are not serialized. Returns `blueprint` itself when nothing changed.

    Raises:
        BlueprintError: If a lock file cannot be read, is invalid, or a server's `lock` has no
            entry for it.
    """

    folder = Path(source).parent
    nearby = folder / MCP_LOCK_FILENAME
    cache: dict[Path, MCPLock] = {}

    def read(path: Path) -> MCPLock:
        if path not in cache:
            try:
                cache[path] = MCPLock.read(path)
            except MCPError as exc:
                raise BlueprintError(str(exc)) from None
        return cache[path]

    servers: list[MCPServer] = []
    changed = False
    for server in blueprint.mcp_servers:
        if server.pin is None and server.lock is not None:
            path = Path(server.lock)
            path = path if path.is_absolute() else folder / path
            entry = read(path).servers.get(server.name)
            if entry is None:
                raise BlueprintError(
                    f"MCP lock file '{path}' has no entry for server {server.name!r}; run "
                    "'awa mcp lock' to pin its tools"
                )
        elif server.pin is None and nearby.is_file():
            entry = read(nearby).servers.get(server.name)
        else:
            entry = None
        if entry is not None:
            # `model_copy` keeps `lock` next to the pin, so the blueprint still dumps as written.
            server = server.model_copy(update={"pin": entry})
            changed = True
        servers.append(server)
    if not changed:
        return blueprint
    return blueprint.model_copy(update={"mcp_servers": servers})
