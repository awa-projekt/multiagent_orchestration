"""The MCP client seam behind `awa.mcp._client`.

`langchain-mcp-adapters` with the `mcp` SDK (the `mcp` extra of awa) connects to MCP servers and
turns their tools into LangChain tools. awa keeps its own naming, selection, pinning, framing and
redaction.

Nothing here is imported by `import awa`; the SDK loads on first use.

Example:
    ```python
    from awa._compat.mcp import adapters_usable

    adapters_usable()  # whether the `mcp` extra is installed
    ```
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, TextIO, cast

from awa._internal.install import install_hint
from awa.errors import OptionalDependencyError

# Stateless adapter tools open their own session in an interceptor; the
# adapters package only requires *some* connection to accept a tool without a
# session. It is never used.
_UNUSED_CONNECTION: dict[str, Any] = {"transport": "stdio", "command": "unused", "args": []}


class MCPSession(Protocol):
    """The session surface awa uses."""

    async def initialize(self) -> Any:
        """Initialize; the result carries the server's `serverInfo`."""

    async def list_tools(self, cursor: str | None = None) -> Any:
        """One page of tools: `.tools` and `.nextCursor`."""

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool and return the raw result."""


CallInNewSession = Callable[[str, dict[str, Any]], Awaitable[Any]]
"""Calls a tool in a new, short-lived session (awa opens and closes it)."""


@dataclass(frozen=True)
class ConvertedTool:
    """What awa needs of a converted tool.

    Attributes:
        args_schema: The tool's argument schema.
        metadata: The adapters' tool metadata (MCP annotations as top-level camelCase keys).
        call: `await call(runtime, arguments)` runs the tool and returns
            `(content, artifact)`; an error the server reports raises
            `ToolException`.
    """

    args_schema: Any
    metadata: dict[str, Any]
    call: Callable[[Any, dict[str, Any]], Awaitable[tuple[Any, Any]]]


def adapters_usable() -> bool:
    """Whether `langchain-mcp-adapters` and the `mcp` SDK import."""

    try:
        import langchain_mcp_adapters.tools  # noqa: F401
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


def require_adapters() -> None:
    """Raise `OptionalDependencyError` unless the `mcp` extra is installed."""

    if not adapters_usable():
        raise OptionalDependencyError(
            f"MCP integration requires the optional extra: {install_hint('mcp')}"
        )


def tool_input_schema(raw: Any) -> dict[str, Any]:
    """The input schema (`inputSchema`) of a listed MCP tool, `{}` when it has none."""

    value = getattr(raw, "inputSchema", None)
    return dict(value) if value is not None else {}


@asynccontextmanager
async def connect(
    connection: dict[str, Any], *, errlog: TextIO | None
) -> AsyncIterator[MCPSession]:
    """Open an uninitialised session for an expanded connection config."""

    if connection["transport"] == "stdio":
        # Started here rather than by the adapters, which would expand
        # `${...}` in the (already expanded) env values a second time and
        # log values they cannot expand.
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        parameters = StdioServerParameters(
            command=connection["command"],
            args=connection["args"],
            env=connection["env"],
            cwd=connection["cwd"],
        )
        async with (
            stdio_client(parameters, errlog=cast(Any, errlog)) as (read, write),
            ClientSession(read, write, **connection["session_kwargs"]) as session,
        ):
            yield cast(MCPSession, session)
    else:
        from langchain_mcp_adapters.sessions import create_session

        async with create_session(cast(Any, connection)) as session:
            yield cast(MCPSession, session)


async def convert_tool(
    raw: Any, *, session: MCPSession | None, call_in_new_session: CallInNewSession, server_name: str
) -> ConvertedTool:
    """Convert a listed tool; without `session`, every call opens a new one."""

    from langchain_core.tools import StructuredTool
    from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool

    per_call: dict[str, Any] = {}
    if session is None:

        async def interceptor(request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
            return await call_in_new_session(request.name, request.args)

        per_call = {"connection": _UNUSED_CONNECTION, "tool_interceptors": [interceptor]}
    converted = convert_mcp_tool_to_langchain_tool(
        cast(Any, session), raw, server_name=server_name, handle_tool_errors=True, **per_call
    )
    assert isinstance(converted, StructuredTool) and converted.coroutine is not None
    coroutine = converted.coroutine

    async def call(runtime: Any, arguments: dict[str, Any]) -> tuple[Any, Any]:
        content, artifact = await coroutine(runtime=runtime, **arguments)
        return content, artifact

    metadata = dict(converted.metadata or {})
    return ConvertedTool(args_schema=converted.args_schema, metadata=metadata, call=call)
