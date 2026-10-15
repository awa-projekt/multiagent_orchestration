"""`awa mcp serve` as a real process, driven by the official MCP client over stdio and HTTP.

The server is the real command line (started through `tests/fixtures/mcp_launcher.py`, which only
swaps the model for a script); the client is the `mcp` SDK's client with its real transports. Every
side effect is checked across the process boundary, in a file the served tools write.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from _helpers.polling import poll

LAUNCHER = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_launcher.py"
DOCS = Path(__file__).resolve().parents[2] / "docs" / "develop" / "mcp.md"
PINNED_REVISION = "2025-11-25"
"""The MCP protocol revision the docs name (`docs/develop/mcp.md`); update both together."""
KEY = "process-test-key"

TOOLS = '''
import os
from pathlib import Path

from langchain_core.tools import tool


@tool
def refund(order_id: str, amount: int) -> str:
    """Refund an order."""
    with Path(os.environ["MCP_LOG"]).open("a", encoding="utf-8") as log:
        log.write(f"{order_id}:{amount}\\n")
    return f"refunded {amount} for {order_id}"


@tool
def lookup(key: str) -> str:
    """Look a value up."""
    return f"value of {key}"
'''

DESK = """
name: desk
description: Refunds orders after approval.
agents:
  - name: clerk
    description: Refunds orders.
    tools: [refund]
approvals:
  - tool: refund
"""

LIMITED = """
name: limited
description: Looks values up, at most two model calls per run.
agents:
  - name: reader
    description: Looks values up.
    tools: [lookup]
limits: {max_requests: 2}
"""

Answer = Callable[[types.ElicitRequestParams], types.ElicitResult]
Started = tuple[ClientSession, types.InitializeResult]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "tools.py").write_text(TOOLS, encoding="utf-8")
    (tmp_path / "desk.yaml").write_text(DESK, encoding="utf-8")
    (tmp_path / "limited.yaml").write_text(LIMITED, encoding="utf-8")
    return tmp_path


def refunds(workspace: Path) -> list[str]:
    log = workspace / "refunds.log"
    return log.read_text("utf-8").split() if log.exists() else []


def server_env(workspace: Path, script: str) -> dict[str, str]:
    return {
        **os.environ,
        "MCP_LOG": str(workspace / "refunds.log"),
        "MCP_SCRIPT": script,
        "MCP_API_KEY": KEY,
    }


def serve_args(blueprint: str, *more: str) -> list[str]:
    return [str(LAUNCHER), "mcp", "serve", blueprint, "--tools", "tools.py", *more]


def accept(approve: bool) -> Answer:
    def answer(params: types.ElicitRequestParams) -> types.ElicitResult:
        assert "refund" in params.message and "A-1" in params.message
        return types.ElicitResult(action="accept", content={"approve": approve, "note": "ok"})

    return answer


def callback(answer: Answer | None) -> Any:
    if answer is None:
        return None

    async def elicit(context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
        return answer(params)

    return elicit


@asynccontextmanager
async def stdio_session(
    workspace: Path, blueprint: str = "desk.yaml", *, script: str = "refund", answer: Answer | None
) -> AsyncIterator[Started]:
    params = StdioServerParameters(
        command=sys.executable,
        args=serve_args(blueprint),
        env=server_env(workspace, script),
        cwd=workspace,
    )
    with (workspace / "stdio.err").open("w") as errlog:
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(read, write, elicitation_callback=callback(answer)) as session,
        ):
            yield session, await session.initialize()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def http_server(workspace: Path, blueprint: str, script: str) -> Iterator[str]:
    """`awa mcp serve --transport http` on a free port, with an API key; yields the endpoint URL."""

    port = free_port()
    http = ["--transport", "http", "--port", str(port), "--api-key-env", "MCP_API_KEY"]
    with (workspace / "http.err").open("w") as errlog:
        process = subprocess.Popen(
            [sys.executable, *serve_args(blueprint, *http)],
            cwd=workspace,
            env=server_env(workspace, script),
            stdout=subprocess.DEVNULL,
            stderr=errlog,
        )
        try:

            def listening() -> bool:
                assert process.poll() is None, (workspace / "http.err").read_text("utf-8")
                with socket.socket() as probe:
                    return probe.connect_ex(("127.0.0.1", port)) == 0

            poll(listening, timeout=60, interval=0.1, what="the MCP server listening")
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@asynccontextmanager
async def http_session(
    url: str, *, key: str = KEY, answer: Answer | None
) -> AsyncIterator[Started]:
    async with (
        httpx.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60) as client,
        streamable_http_client(url, http_client=client) as (read, write, _),
        ClientSession(read, write, elicitation_callback=callback(answer)) as session,
    ):
        yield session, await session.initialize()


def payload(result: types.CallToolResult) -> dict[str, Any]:
    assert not result.isError, result.content
    assert result.structuredContent is not None
    summary = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert summary["status"] == result.structuredContent["status"]
    return result.structuredContent


async def check_listing(session: ClientSession, started: types.InitializeResult) -> None:
    assert started.protocolVersion == PINNED_REVISION
    assert started.serverInfo.name == "desk"
    assert started.capabilities.tools is not None
    listed = {tool.name: tool for tool in (await session.list_tools()).tools}
    assert set(listed) == {"run_clerk", "resume_clerk"}
    assert listed["run_clerk"].inputSchema["required"] == ["input"]
    assert set(listed["run_clerk"].inputSchema["properties"]) == {"input", "thread_id"}
    assert listed["resume_clerk"].inputSchema["required"] == ["thread_id", "decision"]


async def check_elicitation(session: ClientSession, workspace: Path) -> None:
    approved = payload(await session.call_tool("run_clerk", {"input": "Refund order A-1"}))
    assert approved["status"] == "completed" and "refunded 480 for A-1" in approved["text"]
    assert refunds(workspace) == ["A-1:480"]


async def check_resume(session: ClientSession, workspace: Path) -> None:
    paused = payload(await session.call_tool("run_clerk", {"input": "Refund order A-1"}))
    assert paused["status"] == "interrupted" and refunds(workspace) == []
    assert paused["interrupts"][0]["value"]["tool"] == "refund"
    rejected = payload(
        await session.call_tool(
            "resume_clerk",
            {"thread_id": paused["thread_id"], "decision": {"action": "reject", "note": "no"}},
        )
    )
    assert rejected["status"] == "completed" and refunds(workspace) == []
    again = payload(await session.call_tool("run_clerk", {"input": "Refund order A-1"}))
    approve = {"thread_id": again["thread_id"], "decision": {"action": "approve"}}
    done = payload(await session.call_tool("resume_clerk", approve))
    assert done["status"] == "completed" and "refunded 480" in done["text"]
    assert refunds(workspace) == ["A-1:480"]
    repeated = await session.call_tool("resume_clerk", approve)
    assert repeated.isError and refunds(workspace) == ["A-1:480"]


async def check_limits(session: ClientSession) -> None:
    result = await session.call_tool("run_reader", {"input": "Look up a"})
    assert result.isError
    assert "max_requests=2" in result.content[0].text  # type: ignore[union-attr]


# ---------------------------------------------------------------------- stdio


@pytest.mark.slow
async def test_stdio_lists_the_tools_and_asks_the_client_user_for_approval(workspace: Path) -> None:
    async with stdio_session(workspace, answer=accept(True)) as (session, started):
        await check_listing(session, started)
        await check_elicitation(session, workspace)


@pytest.mark.slow
async def test_stdio_a_client_without_elicitation_resumes_with_the_tool(workspace: Path) -> None:
    async with stdio_session(workspace, answer=None) as (session, _):
        await check_resume(session, workspace)


@pytest.mark.slow
async def test_stdio_stops_every_run_at_the_limits_of_the_blueprint(workspace: Path) -> None:
    async with stdio_session(workspace, "limited.yaml", script="loop", answer=None) as (session, _):
        await check_limits(session)


# ---------------------------------------------------------------------- HTTP


@pytest.mark.slow
async def test_http_lists_the_tools_and_asks_the_client_user_for_approval(workspace: Path) -> None:
    with http_server(workspace, "desk.yaml", "refund") as url:
        async with http_session(url, answer=accept(True)) as (session, started):
            await check_listing(session, started)
            await check_elicitation(session, workspace)


@pytest.mark.slow
async def test_http_a_client_without_elicitation_resumes_with_the_tool(workspace: Path) -> None:
    with http_server(workspace, "desk.yaml", "refund") as url:
        async with http_session(url, answer=None) as (session, _):
            await check_resume(session, workspace)


@pytest.mark.slow
async def test_http_stops_every_run_at_the_limits_of_the_blueprint(workspace: Path) -> None:
    with http_server(workspace, "limited.yaml", "loop") as url:
        async with http_session(url, answer=None) as (session, _):
            await check_limits(session)


NEGOTIATED = {
    PINNED_REVISION: PINNED_REVISION,
    "2025-06-18": "2025-06-18",
    "2025-03-26": "2025-03-26",
    "2024-11-05": "2024-11-05",
    "2099-01-01": PINNED_REVISION,  # newer than the server knows: it offers its own
}


@pytest.mark.slow
async def test_http_negotiates_the_protocol_revision_a_client_asks_for(workspace: Path) -> None:
    headers = {"Authorization": f"Bearer {KEY}", "Accept": "application/json, text/event-stream"}
    answers = {}
    with http_server(workspace, "desk.yaml", "refund") as url:
        async with httpx.AsyncClient(timeout=30) as client:
            for asked in NEGOTIATED:
                params = {
                    "protocolVersion": asked,
                    "capabilities": {},
                    "clientInfo": {"name": "raw-client", "version": "1"},
                }
                body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
                reply = await client.post(url, json=body, headers=headers)
                assert reply.status_code == 200
                data = next(line for line in reply.text.splitlines() if line.startswith("data:"))
                answers[asked] = json.loads(data.removeprefix("data:"))["result"]["protocolVersion"]
    assert answers == NEGOTIATED


@pytest.mark.slow
async def test_http_refuses_clients_without_the_key(workspace: Path) -> None:
    with http_server(workspace, "desk.yaml", "refund") as url:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        headers = {"Accept": "application/json, text/event-stream"}
        async with httpx.AsyncClient(timeout=30) as client:
            missing = await client.post(url, json=body, headers=headers)
            wrong = await client.post(
                url, json=body, headers={**headers, "Authorization": "Bearer nope"}
            )
        assert missing.status_code == wrong.status_code == 401
        assert missing.json()["error"]["code"] == "awa.authentication"
        with pytest.raises(BaseException) as refused:  # noqa: PT011 - the client raises a group
            async with http_session(url, key="nope", answer=None):
                pytest.fail("the server accepted a wrong key")
        assert "401" in repr(refused.value)
    assert refunds(workspace) == []


@pytest.mark.slow
async def test_the_client_configuration_of_the_docs_starts_the_server(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `mcpServers` snippet of `docs/develop/mcp.md`, with its paths filled in, works as is."""

    page = DOCS.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", page, re.S)
    block = next(b for b in blocks if "mcpServers" in b)
    block = block.replace("/path/to/support.yaml", (workspace / "desk.yaml").as_posix())
    block = block.replace("/path/to/tools.py", (workspace / "tools.py").as_posix())
    entry = json.loads(block)["mcpServers"]["support"]
    assert entry["command"] == "awa"
    monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")
    params = StdioServerParameters(**entry, cwd=workspace)
    with (workspace / "stdio.err").open("w") as errlog:
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(read, write) as session,
        ):
            started = await session.initialize()
            listed = {tool.name for tool in (await session.list_tools()).tools}
    assert started.protocolVersion == PINNED_REVISION
    assert listed == {"run_clerk", "resume_clerk"}
