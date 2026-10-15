"""The MCP *server* side of the `mcp` SDK, behind `awa.server.mcp`.

`awa.server.mcp` exposes awa systems as tools of an MCP server
built on the SDK's FastMCP. This module is its only contact with the SDK:
it imports the server classes on first use and raises
`OptionalDependencyError` with the install hint when the `mcp` extra is
missing. Nothing here is imported by `import awa`.

Example:
    ```python
    from awa._compat.mcp_server import load_server_sdk

    sdk = load_server_sdk()
    server = sdk.FastMCP("awa")
    ```
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from typing import Any

from awa._internal.install import install_hint
from awa.errors import OptionalDependencyError


@dataclass(frozen=True)
class ServerSDK:
    """The SDK names an MCP server built by awa uses."""

    FastMCP: Any
    Tool: Any
    ToolError: Any
    func_metadata: Any
    CallToolResult: Any
    TextContent: Any
    ToolAnnotations: Any


@cache
def load_server_sdk() -> ServerSDK:
    """Import the MCP server classes of the `mcp` SDK.

    Raises:
        OptionalDependencyError: If the `mcp` extra is not installed.
    """

    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.fastmcp.exceptions import ToolError
        from mcp.server.fastmcp.tools import Tool
        from mcp.server.fastmcp.utilities.func_metadata import func_metadata
        from mcp.types import CallToolResult, TextContent, ToolAnnotations
    except ImportError as exc:
        raise OptionalDependencyError(
            f"Serving systems over MCP requires the optional extra: {install_hint('mcp')}"
        ) from exc
    return ServerSDK(
        FastMCP=FastMCP,
        Tool=Tool,
        ToolError=ToolError,
        func_metadata=func_metadata,
        CallToolResult=CallToolResult,
        TextContent=TextContent,
        ToolAnnotations=ToolAnnotations,
    )


def _client_params(context: Any) -> Any:
    """The `initialize` parameters of the current request's client, or `None` outside a request."""

    try:
        return context.session.client_params
    except (AttributeError, LookupError, ValueError):
        return None


def client_supports_elicitation(context: Any) -> bool:
    """Whether the client of the current request declared the elicitation capability."""

    capabilities = getattr(_client_params(context), "capabilities", None)
    return getattr(capabilities, "elicitation", None) is not None


def client_name(context: Any) -> str | None:
    """The name the client of the current request gave in its `initialize` request."""

    params = _client_params(context)
    info = getattr(params, "clientInfo", None) or getattr(params, "client_info", None)
    name = getattr(info, "name", None)
    return name if isinstance(name, str) else None
