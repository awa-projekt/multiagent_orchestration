"""Driver of scenario 8: the supplier-order blueprint served by `awa mcp serve`, called by a real client.

The server is a separate process (the real `awa mcp serve`; offline through `mcp_launcher.py`, which
only swaps the model and the network for scripts) and the client is the official `mcp` SDK client
over its real stdio or streamable HTTP transport. A case names the transport and how the pending
approvals are answered: through elicitation (the client's user is asked inside the tool call) or
with the `resume_<name>` tool (a client without elicitation).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

HERE = Path(__file__).resolve().parent
SERVED = "02_supplier_orders"
"""The scenario whose blueprint is served."""
SYSTEM = "order_checker"
PROTOCOL_REVISION = "2025-11-25"
"""The MCP revision the docs pin (`docs/develop/mcp.md`); the client must negotiate it."""
CHECKS = ("tools_listed", "protocol_revision", "approvals_answered", "completed", "output_matches")
KEY_VARIABLE = "BENCH_MCP_KEY"
KEY = "bench-mcp-key"  # the bearer key of the benchmark's server (not a secret)
MAX_ROUNDS = 20


def server_command(offline: bool, *more: str) -> list[str]:
    """`awa mcp serve` of the served blueprint, started the way the mode needs."""

    start = [str(HERE / "mcp_launcher.py"), SERVED] if offline else ["-m", "awa"]
    return [sys.executable, *start, "mcp", "serve", "blueprint.yaml", "--tools", "tools.py", *more]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _stderr_file(work: Path, name: str) -> Iterator[Any]:
    with (work / name).open("w", encoding="utf-8") as errlog:
        yield errlog


async def _listening(process: subprocess.Popen[bytes], port: int, work: Path) -> None:
    for _ in range(600):
        if process.poll() is not None:
            tail = (work / "server.err").read_text("utf-8", errors="replace")[-300:]
            raise RuntimeError(f"the server stopped: {tail}")
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        await asyncio.sleep(0.1)
    raise TimeoutError("the server did not listen within 60 s")


Started = tuple[ClientSession, types.InitializeResult]


@asynccontextmanager
async def _session(
    transport: str, offline: bool, work: Path, elicitation: Any
) -> AsyncIterator[Started]:
    """A client session on a freshly started server process; the process ends with the context."""

    env = {**os.environ, KEY_VARIABLE: KEY}
    if transport == "stdio":
        params = StdioServerParameters(
            command=sys.executable, args=server_command(offline)[1:], env=env, cwd=work
        )
        with _stderr_file(work, "server.err") as errlog:
            async with (
                stdio_client(params, errlog=errlog) as (read, write),
                ClientSession(read, write, elicitation_callback=elicitation) as session,
            ):
                yield session, await session.initialize()
        return
    port = _free_port()
    command = server_command(
        offline, "--transport", "http", "--port", str(port), "--api-key-env", KEY_VARIABLE
    )
    with _stderr_file(work, "server.err") as errlog:
        process = subprocess.Popen(
            command, cwd=work, env=env, stdout=subprocess.DEVNULL, stderr=errlog
        )
        try:
            await _listening(process, port, work)
            async with (
                httpx.AsyncClient(headers={"Authorization": f"Bearer {KEY}"}, timeout=120) as http,
                streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as (
                    read,
                    write,
                    _,
                ),
                ClientSession(read, write, elicitation_callback=elicitation) as session,
            ):
                yield session, await session.initialize()
        finally:
            process.terminate()
            try:
                await asyncio.to_thread(process.wait, 15)
            except subprocess.TimeoutExpired:
                process.kill()


def _body(result: types.CallToolResult) -> dict[str, Any]:
    if result.isError or result.structuredContent is None:
        text = result.content[0].text if result.content else ""  # type: ignore[union-attr]
        raise RuntimeError(f"the tool call failed: {text[:200]}")
    return dict(result.structuredContent)


def mismatches(output: Any, expected: dict[str, Any]) -> list[str]:
    """The expected fields the served answer does not have (`json_fields` of the evals)."""

    if isinstance(output, str):
        try:
            output = json.loads(output)
        except json.JSONDecodeError:
            return ["the output is not a JSON object"]
    if not isinstance(output, dict):
        return ["the output is not a JSON object"]
    return [
        f"{key}={json.dumps(output.get(key), default=str)[:60]}"
        for key, wanted in expected.items()
        if output.get(key) != wanted
    ]


async def play(
    metadata: dict[str, Any], request: str, expected: dict[str, Any], offline: bool, work: Path
) -> tuple[dict[str, tuple[bool, str]], int, int]:
    """One run of a case: the checks with a note each, the model calls and the tokens used."""

    approve = metadata["decision"] == "approve"
    decision: dict[str, Any] = (
        {"action": "approve"}
        if approve
        else {"action": "reject", "note": "The supplier keeps the date."}
    )
    answered = 0

    async def elicit(context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
        nonlocal answered
        answered += 1
        note = decision.get("note", "approved in the benchmark")
        return types.ElicitResult(action="accept", content={"approve": approve, "note": note})

    by_elicitation = metadata["answer"] == "elicitation"
    async with _session(
        metadata["transport"], offline, work, elicit if by_elicitation else None
    ) as (session, started):
        names = {tool.name for tool in (await session.list_tools()).tools}
        body = _body(await session.call_tool(f"run_{SYSTEM}", {"input": request}))
        usage = [body.get("usage") or {}]
        for _ in range(MAX_ROUNDS):
            if body["status"] != "interrupted" or by_elicitation:
                break
            pending = {str(item["id"]): decision for item in body["interrupts"]}
            answered += len(pending)
            answer = next(iter(pending.values())) if len(pending) == 1 else pending
            arguments = {"thread_id": body["thread_id"], "decision": answer}
            body = _body(await session.call_tool(f"resume_{SYSTEM}", arguments))
            usage.append(body.get("usage") or {})
    wanted_tools = {f"run_{SYSTEM}", f"resume_{SYSTEM}"}
    wrong = mismatches(body.get("output"), expected)
    checks = {
        "tools_listed": (names == wanted_tools, f"tools {sorted(names)}"),
        "protocol_revision": (
            started.protocolVersion == PROTOCOL_REVISION,
            f"negotiated {started.protocolVersion}",
        ),
        "approvals_answered": (
            answered == len(expected["flagged_orders"]),
            f"{answered} approvals answered, {len(expected['flagged_orders'])} orders flagged",
        ),
        "completed": (body["status"] == "completed", f"status {body['status']}"),
        "output_matches": (not wrong, f"mismatched: {', '.join(wrong)}"),
    }
    # An elicited tool call reports the usage of all its rounds; each resume call its own part.
    return (
        checks,
        sum(int(u.get("requests", 0)) for u in usage),
        sum(int(u.get("total_tokens", 0)) for u in usage),
    )


def new_workdir() -> Path:
    """A temporary copy of the served scenario (blueprint, tools, data) to start the server in."""

    work = Path(tempfile.mkdtemp(prefix="awa-bench-8-"))
    shutil.copytree(
        HERE / SERVED, work, dirs_exist_ok=True, ignore=shutil.ignore_patterns("cases.yaml", "*.md")
    )
    return work


__all__ = ["CHECKS", "KEY", "PROTOCOL_REVISION", "SERVED", "mismatches", "new_workdir", "play"]
