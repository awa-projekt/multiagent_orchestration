"""MCP tools: plug a system the company already runs into agents with one line.

`mcp_crm_server.py` stands in for an existing CRM behind an MCP server. An
`MCPServer` says how to start (or reach) it and which tools to use;
`MCPToolset` keeps the session open while the agent works and stops the
server afterwards. Every tool is prefixed with the server name
(`crm_find_customer`), the destructive `delete_customer` is filtered out, and
`${CRM_REGION:-emea}` is read from the environment when the server starts.

The same server in a blueprint: `mcp_servers:` declares it, `tools: [mcp:crm]`
gives an agent all of its (selected) tools, and an approval on
`mcp:crm/create_ticket` makes a human confirm every new ticket.
`Blueprint.abuild` connects and builds in one call.

Run:
    pip install -e ".[anthropic,mcp]"
    export ANTHROPIC_API_KEY=...
    python examples/09_mcp_tools.py
    # No model key? Run its offline tests instead:
    #   python -m pytest tests/examples/test_09_mcp_tools.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from _support import require_model_credentials
from awa import Agent, Blueprint, RunResult
from awa.mcp import MCPServer, MCPToolset
from awa.persistence import StateInMemorySaver

SERVER_SCRIPT = Path(__file__).resolve().parent / "mcp_crm_server.py"

CRM = MCPServer(
    name="crm",
    command=sys.executable,
    args=[str(SERVER_SCRIPT)],
    env={"CRM_REGION": "${CRM_REGION:-emea}"},
    exclude=["delete_*"],
)
"""The CRM server: started over stdio, all tools except the destructive ones."""

DESK_BLUEPRINT = f"""
name: account-desk
description: Answers account questions from the CRM and opens tickets.
mcp_servers:
  - name: crm
    command: {json.dumps(sys.executable)}
    args: [{json.dumps(str(SERVER_SCRIPT))}]
    env: {{CRM_REGION: "${{CRM_REGION:-emea}}"}}
    exclude: [delete_*]
agents:
  - name: account_manager
    description: Knows every customer and their open tickets.
    instructions: |
      Answer from the CRM only. Look the customer up first. Open a ticket only
      when the customer reports a new problem.
    tools: [mcp:crm]
approvals:
  - tool: mcp:crm/create_ticket
"""


def build_account_manager(tools: Sequence[BaseTool]) -> Agent:
    """An agent that answers account questions with the CRM's tools."""

    return Agent(
        "account_manager",
        description="Knows every customer and their open tickets.",
        instructions="Answer from the CRM only. Look the customer up first.",
        tools=tools,
    )


def desk_blueprint() -> Blueprint:
    """The account desk as a blueprint that declares the CRM server itself."""

    return Blueprint.from_yaml(DESK_BLUEPRINT)


async def build_desk() -> Agent:
    """Connect to the CRM and build the desk; its tools open a session per call."""

    desk = await desk_blueprint().abuild(checkpointer=StateInMemorySaver())
    if not isinstance(desk, Agent):
        raise TypeError(f"The desk should build an Agent, got {type(desk).__name__}")
    return desk


async def ask_reviewer(pending: dict[str, Any]) -> dict[str, str]:
    """Show a pending approval on the console and read the reviewer's decision."""

    request = pending["value"]
    print(f"\nApproval needed: {request['tool']}({request['args']})")
    answer = await asyncio.to_thread(input, "Approve? [y/N] ")
    if answer.strip().lower() in ("y", "yes"):
        return {"action": "approve"}
    return {"action": "reject", "note": "rejected by the reviewer"}


async def handle(system: Agent, request: str) -> RunResult[Any]:
    """Run a request, asking a reviewer for every pending approval."""

    result = await system.run(request)
    while result.is_interrupted:
        assert result.thread_id is not None
        result = await system.resume(result.thread_id, await ask_reviewer(result.interrupts[0]))
    return result


async def main() -> None:
    require_model_credentials()

    async with MCPToolset(CRM) as tools:
        print("CRM tools:", ", ".join(tool.name for tool in tools))
        manager = build_account_manager(tools)
        result = await manager.run("Which open tickets does ACME have?")
        print(f"\n{result.output}\n")

    desk = await build_desk()
    result = await handle(desk, "Globex says their last invoice went to the wrong address.")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
