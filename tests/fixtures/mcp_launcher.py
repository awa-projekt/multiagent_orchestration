"""Starts the real `awa` command line with a scripted model, for tests that serve over MCP.

`python tests/fixtures/mcp_launcher.py mcp serve desk.yaml --tools tools.py` is exactly
`awa mcp serve desk.yaml --tools tools.py`, except that the model named `default` is a script
instead of a provider, so a served process can be driven without a network or a key. The script is
chosen by `MCP_SCRIPT`: `refund` (default) asks for the `refund` tool once and reports its result;
`loop` calls the `lookup` tool again and again (to meet a blueprint's limits).
"""

from __future__ import annotations

import os
import sys
from typing import Any

from langchain_core.messages import ToolMessage

from awa.cli import main
from awa.models import ModelRegistry
from awa.testing import FunctionChatModel, ModelCall, tool_call


def respond(call: ModelCall) -> Any:
    if os.environ.get("MCP_SCRIPT") == "loop":
        return tool_call("lookup", {"key": "a"})
    if isinstance(call.last_message, ToolMessage):
        return f"result: {call.last_message.content}"
    return tool_call("refund", {"order_id": "A-1", "amount": 480})


if __name__ == "__main__":
    ModelRegistry.default().register("default", FunctionChatModel(respond))
    sys.exit(main(sys.argv[1:]))
