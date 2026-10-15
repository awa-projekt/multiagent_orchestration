"""MCP (Runtime layer): the tools of Model Context Protocol servers as agent tools (extra `mcp`).

`MCPServer` describes one server (stdio command or http url); `load_mcp_tools` returns tools that
open a short-lived session per call; `MCPToolset` keeps one session per server open inside
`async with`; `lock_mcp_tools` and `MCPLock` pin tool definitions. Serving your own system over
MCP is `awa.server.mcp`.
"""

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute

if TYPE_CHECKING:
    from awa.errors import MCPError, MCPToolDriftError
    from awa.mcp._client import (
        MCP_LOCK_FILENAME,
        MCPDrift,
        MCPLock,
        MCPServer,
        MCPServerLock,
        MCPToolPin,
        MCPToolset,
        load_mcp_tools,
        lock_mcp_tools,
    )

_HOMES = homes(
    {
        "awa.mcp._client": (
            "MCP_LOCK_FILENAME MCPDrift MCPLock MCPServer MCPServerLock MCPToolPin MCPToolset"
            " load_mcp_tools lock_mcp_tools"
        ),
        "awa.errors": "MCPError MCPToolDriftError",
    }
)

__all__ = [
    "MCP_LOCK_FILENAME",
    "MCPDrift",
    "MCPError",
    "MCPLock",
    "MCPServer",
    "MCPServerLock",
    "MCPToolDriftError",
    "MCPToolPin",
    "MCPToolset",
    "load_mcp_tools",
    "lock_mcp_tools",
]


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _HOMES, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
