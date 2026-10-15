"""`awa mcp lock|list|serve`: pin and inspect a blueprint's MCP servers, or serve it as one."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awa.cli._common import (
    EXIT_ERROR,
    EXIT_OK,
    LOOPBACK_HOSTS,
    Commands,
    add_blueprint_argument,
    add_command,
    add_tools_options,
    print_table,
)
from awa.core.config import env_value

if TYPE_CHECKING:
    from awa._internal.upstream.tools import BaseTool
    from awa.blueprints import Blueprint
    from awa.mcp import MCPServer, MCPServerLock


def add_mcp(commands: Commands) -> None:
    from awa.mcp import MCP_LOCK_FILENAME

    command = add_command(commands, "mcp")
    actions = command.add_subparsers(dest="mcp_command", required=True, metavar="action")

    lock = actions.add_parser("lock", help="pin the tool definitions in mcp.lock.json")
    add_blueprint_argument(lock)
    lock.add_argument("--server", action="append", default=[], help="only this server (repeatable)")
    lock.add_argument(
        "--output", "-o", help=f"lock file to write (default: {MCP_LOCK_FILENAME} next to it)"
    )
    lock.add_argument(
        "--tools",
        action="store_true",
        help="also record each tool's description and input schema, for reviewable diffs",
    )
    lock.set_defaults(handler=_lock)

    listing = actions.add_parser("list", help="list the tools and their pin status")
    add_blueprint_argument(listing)
    listing.add_argument(
        "--server", action="append", default=[], help="only this server (repeatable)"
    )
    listing.add_argument("--lock", help="compare with this lock file instead")
    listing.set_defaults(handler=_list)

    serve = actions.add_parser("serve", help="[preview] offer a blueprint's system to MCP clients")
    add_blueprint_argument(serve)
    add_tools_options(serve)
    serve.add_argument(
        "--expose-tools",
        action="store_true",
        help="also expose the loaded tools that need no approval as MCP tools",
    )
    serve.add_argument(
        "--transport", choices=("stdio", "http"), default="stdio", help="stdio (default) or http"
    )
    serve.add_argument("--host", default="127.0.0.1", help="interface for http (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8000, help="port for http (default 8000)")
    serve.add_argument("--path", default="/mcp", help="endpoint path for http (default /mcp)")
    serve.add_argument(
        "--api-key-env",
        metavar="VAR",
        help="environment variable holding the bearer key http clients must send",
    )
    serve.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help="allow http on a non-loopback --host without --api-key-env (anyone who can reach "
        "the port can run the system)",
    )
    serve.add_argument(
        "--no-elicit",
        dest="elicit",
        action="store_false",
        help="never ask the client's user for approvals; return interrupted results",
    )
    serve.set_defaults(handler=_serve)


def _lock(args: argparse.Namespace) -> int:
    from awa._internal import aio as asyncio_compat
    from awa.mcp import MCP_LOCK_FILENAME, MCPLock, lock_mcp_tools

    path = Path(args.blueprint)
    servers = _servers(_load(path).mcp_servers, args.server)
    output = Path(args.output) if args.output else path.parent / MCP_LOCK_FILENAME
    lock = asyncio_compat.run(lock_mcp_tools(servers, definitions=args.tools))
    if args.server and output.is_file():
        lock = MCPLock.read(output).merged(lock)
    lock.write(output)
    for server in servers:
        entry = lock.servers[server.name]
        version = f" (version {entry.server_version})" if entry.server_version else ""
        print(f"{server.name}: pinned {len(entry.tools)} tool(s){version}")
    print(f"wrote {output}")
    return EXIT_OK


def _list(args: argparse.Namespace) -> int:
    from awa._internal import aio as asyncio_compat
    from awa.mcp import MCPLock, lock_mcp_tools

    path = Path(args.blueprint)
    blueprint = _load(path)
    servers = _servers(blueprint.mcp_servers, args.server)
    override = MCPLock.read(args.lock) if args.lock else None
    current = asyncio_compat.run(lock_mcp_tools(servers, definitions=True))
    rows: list[tuple[str, ...]] = [("SERVER", "TOOL", "MCP NAME", "PIN", "NOTES")]
    drifted = False
    for server in servers:
        pin = override.servers.get(server.name) if override is not None else server.pinned()
        now = current.servers[server.name]
        for mcp_name, tool in now.tools.items():
            status = _status(pin, mcp_name, tool.digest)
            drifted = drifted or status in ("changed", "new", "unpinned")
            rows.append(
                (server.name, server.tool_name(mcp_name), mcp_name, status, _notes(server, tool))
            )
        for mcp_name in sorted(set(pin.tools) - set(now.tools)) if pin is not None else ():
            drifted = True
            rows.append((server.name, "-", mcp_name, "removed", ""))
    print_table(rows)
    if drifted:
        print("tools changed or are not pinned; review them, then run 'awa mcp lock'")
        return EXIT_ERROR
    return EXIT_OK


def _load(path: Path) -> Blueprint:
    from awa.blueprints import Blueprint
    from awa.blueprints._mcp import attach_mcp_locks

    return attach_mcp_locks(Blueprint.from_yaml(path), path)


def _servers(declared: Sequence[MCPServer], names: Sequence[str]) -> list[MCPServer]:
    from awa.errors import MCPError

    if not declared:
        raise MCPError("The blueprint declares no mcp_servers")
    known = [server.name for server in declared]
    unknown = [name for name in names if name not in known]
    if unknown:
        raise MCPError(f"Unknown MCP server(s) {', '.join(unknown)} (declared: {', '.join(known)})")
    return [server for server in declared if not names or server.name in names]


def _status(pin: MCPServerLock | None, mcp_name: str, digest: str) -> str:
    if pin is None:
        return "unpinned"
    if mcp_name not in pin.tools:
        return "new"
    return "pinned" if pin.tools[mcp_name].digest == digest else "changed"


def _notes(server: MCPServer, tool: Any) -> str:
    limit = server.max_description_chars
    length = len(tool.description or "")
    if limit is not None and length > limit:
        return f"description truncated ({length} > {limit} characters)"
    return ""


def _serve(args: argparse.Namespace) -> int:
    """`awa mcp serve`: build the blueprint and serve it until interrupted."""

    from awa.errors import ConfigurationError, MissingConfigurationError
    from awa.server.mcp import build_blueprint_system, exposable_tools, serve_as_mcp

    api_key: str | None = None
    if args.api_key_env:
        api_key = (env_value(args.api_key_env) or "").strip() or None
        if api_key is None:
            raise MissingConfigurationError(
                f"--api-key-env names {args.api_key_env}, but that variable is not set or empty"
            )
    if args.transport == "http" and api_key is None and args.host not in LOOPBACK_HOSTS:
        if not args.allow_unauthenticated:
            raise ConfigurationError(
                f"awa mcp serve without an API key serves on this computer only; --host "
                f"{args.host} would let anyone who can reach the port run the system. Use "
                "--api-key-env, or add --allow-unauthenticated if that is intended."
            )
        print(
            f"warning: serving MCP on {args.host} without an API key; anyone who can reach "
            "the port can run the system. Use --api-key-env.",
            file=sys.stderr,
        )
    served, loaded, blueprint = build_blueprint_system(
        args.blueprint, tools=args.tools, skills=args.skills
    )
    exposed: list[BaseTool] = []
    if args.expose_tools:
        exposed, notes = exposable_tools(loaded, blueprint)
        for note in notes:
            print(note, file=sys.stderr)
    serve_as_mcp(
        {served.system.name: served},
        transport=args.transport,
        host=args.host,
        port=args.port,
        path=args.path,
        api_key=api_key,
        name=blueprint.name,
        instructions=getattr(blueprint, "description", None) or None,
        tools=exposed,
        elicit_approvals=args.elicit,
        allow_unauthenticated=args.allow_unauthenticated,
    )
    return EXIT_OK
