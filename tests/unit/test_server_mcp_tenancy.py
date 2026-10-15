"""MCP serving keeps thread ownership like the HTTP API: no caller reaches a thread it does not own.

Each test replays a tenancy attack end to end (a real MCP client over the
streamable HTTP transport, the HTTP API where a surface is crossed) and checks that the gated
refund never runs for the wrong caller, while the owner keeps working.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from awa import Agent
from awa.server import ApiKeyAuthenticator, InMemoryThreadIndex, Principal, ServedSystem, create_app
from awa.server.mcp import BearerGate, create_mcp_server
from awa.testing import FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper
from awa.workflows import Sequential

BASE = "http://127.0.0.1:8000"
KEYS = {
    "ann": Principal("ann", tenant="acme"),
    "rev": Principal("rev", tenant="acme", roles={"approver"}),
    "zed": Principal("zed", tenant="globex", roles={"approver"}),
    "nt1": Principal("nt1"),
    "nt2": Principal("nt2"),
}
APPROVE = {"action": "approve"}


def refund_tool(executed: list[float]) -> Any:
    @tool
    def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order."""
        executed.append(amount)
        return f"refunded {amount} for {order_id}"

    return create_tool_approval_wrapper(issue_refund)


def respond(call: ModelCall) -> Any:
    if isinstance(call.last_message, ToolMessage):
        return f"result: {call.last_message.content}"
    return tool_call("issue_refund", {"order_id": "A-1", "amount": 480})


def refunds(executed: list[float], checkpointer: Any = None) -> Agent:
    return Agent(
        "refunds",
        description="Issues refunds.",
        model=FunctionChatModel(respond),
        tools=[refund_tool(executed)],
        checkpointer=checkpointer or InMemorySaver(),
    )


def threads_of(saver: InMemorySaver) -> set[str]:
    return {saved.config["configurable"]["thread_id"] for saved in saver.list(None)}


def refund_workflow(executed: list[float]) -> Sequential:
    clerk = Agent("clerk", model=FunctionChatModel(respond), tools=[refund_tool(executed)])
    return Sequential("refunds", [clerk], checkpointer=InMemorySaver())


@asynccontextmanager
async def mcp_serving(served: Any, **options: Any) -> AsyncIterator[BearerGate]:
    server = create_mcp_server({"refunds": served}, **options)
    app = server.streamable_http_app()
    gate = BearerGate(app, ApiKeyAuthenticator(dict(KEYS)))
    async with app.router.lifespan_context(app):
        yield gate


@asynccontextmanager
async def session(gate: BearerGate, key: str) -> AsyncIterator[ClientSession]:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gate),
        base_url=BASE,
        headers={"Authorization": f"Bearer {key}"},
    )
    async with (
        client,
        streamable_http_client(f"{BASE}/mcp", http_client=client) as (read, write, _),
        ClientSession(read, write) as opened,
    ):
        await opened.initialize()
        yield opened


def body(result: types.CallToolResult) -> dict[str, Any]:
    assert not result.isError, result.content
    assert result.structuredContent is not None
    return result.structuredContent


def text(result: types.CallToolResult) -> str:
    return str(result.content[0].text)  # type: ignore[union-attr]


async def resume(caller: ClientSession, thread_id: str) -> types.CallToolResult:
    return await caller.call_tool("resume_refunds", {"thread_id": thread_id, "decision": APPROVE})


def http(agent: Any, **options: Any) -> httpx.AsyncClient:
    app = create_app({"refunds": agent}, authenticator=ApiKeyAuthenticator(dict(KEYS)), **options)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------- crossing MCP and HTTP


async def test_a_tenantless_mcp_caller_cannot_resume_a_thread_a_tenant_made_over_http() -> None:
    executed: list[float] = []
    agent = refunds(executed)
    async with http(ServedSystem(agent)) as client:
        made = await client.post(
            "/v1/systems/refunds/runs",
            json={"input": "refund", "thread_id": "ticket-2001"},
            headers=bearer("ann"),
        )
    assert made.json()["status"] == "interrupted"
    async with mcp_serving(ServedSystem(agent)) as gate, session(gate, "nt1") as stranger:
        attempt = await resume(stranger, "ticket-2001")
        continued = await stranger.call_tool(
            "run_refunds", {"input": "refund", "thread_id": "ticket-2001"}
        )
    assert attempt.isError and "ticket-2001" in text(attempt)
    # Naming the id opens a thread of the stranger's own, not acme's.
    assert body(continued)["status"] == "interrupted"
    assert executed == []


async def test_a_tenantless_http_caller_cannot_read_or_resume_a_tenants_mcp_thread() -> None:
    executed: list[float] = []
    saver = InMemorySaver()
    agent = refunds(executed, saver)
    async with mcp_serving(ServedSystem(agent)) as gate, session(gate, "ann") as owner:
        paused = body(
            await owner.call_tool("run_refunds", {"input": "refund", "thread_id": "ticket-1042"})
        )
    assert paused["thread_id"] == "ticket-1042"
    (thread,) = threads_of(saver)
    assert thread.endswith(".ticket-1042")
    async with http(ServedSystem(agent)) as client:
        read = await client.get(
            f"/v1/systems/refunds/threads/{thread}/interrupts", headers=bearer("nt1")
        )
        resumed = await client.post(
            f"/v1/systems/refunds/threads/{thread}/resume",
            json={"decision": APPROVE},
            headers=bearer("nt1"),
        )
        claimed = await client.post(
            "/v1/systems/refunds/runs",
            json={"input": "go on", "thread_id": thread},
            headers=bearer("nt1"),
        )
    assert read.status_code == 404 and resumed.status_code == 404 and claimed.status_code == 404
    assert executed == []


async def test_a_shared_thread_index_keeps_one_owner_across_mcp_and_http() -> None:
    executed: list[float] = []
    saver = InMemorySaver()
    agent = refunds(executed, saver)
    index = InMemoryThreadIndex()
    async with (
        mcp_serving(ServedSystem(agent), thread_index=index) as gate,
        session(gate, "ann") as owner,
    ):
        paused = body(await owner.call_tool("run_refunds", {"input": "refund"}))
    (thread,) = threads_of(saver)
    async with http(ServedSystem(agent), thread_index=index) as client:
        stranger = await client.get(
            f"/v1/systems/refunds/threads/{thread}/interrupts", headers=bearer("nt1")
        )
        own = await client.get(
            f"/v1/systems/refunds/threads/{thread}/interrupts", headers=bearer("ann")
        )
    assert paused["status"] == "interrupted"
    assert stranger.status_code == 404 and own.status_code == 200, own.text


# ---------------------------------------------------------------- callers without tenant


async def test_tenantless_callers_get_a_namespace_of_their_own() -> None:
    executed: list[float] = []
    saver = InMemorySaver()
    agent = refunds(executed, saver)
    async with mcp_serving(ServedSystem(agent)) as gate:
        async with session(gate, "nt1") as first:
            paused = body(await first.call_tool("run_refunds", {"input": "refund"}))
            named = body(
                await first.call_tool(
                    "run_refunds", {"input": "refund", "thread_id": "t0123456789abcdef.guess"}
                )
            )
        async with session(gate, "ann") as tenant:
            other_tenant = await resume(tenant, paused["thread_id"])
    assert named["thread_id"] == "t0123456789abcdef.guess"
    assert all(re.match(r"t[0-9a-f]{16}\.", thread) for thread in threads_of(saver))
    assert f"t0000000000000000.{paused['thread_id']}" in threads_of(saver)
    assert other_tenant.isError and executed == []


async def test_an_existing_thread_without_an_ownership_record_is_refused() -> None:
    executed: list[float] = []
    saver = InMemorySaver()
    agent = refunds(executed, saver)
    async with mcp_serving(ServedSystem(agent)) as gate, session(gate, "nt1") as first:
        paused = body(await first.call_tool("run_refunds", {"input": "refund"}))
    # A new server (a restart) with the same checkpointer has no record of the thread.
    async with mcp_serving(ServedSystem(agent)) as gate, session(gate, "nt2") as later:
        resumed = await resume(later, paused["thread_id"])
        continued = await later.call_tool(
            "run_refunds", {"input": "go on", "thread_id": paused["thread_id"]}
        )
    assert resumed.isError and continued.isError and executed == []
    assert "no thread" in text(resumed)


# ---------------------------------------------------------------- workflows


async def test_workflow_threads_are_scoped_and_the_owner_can_resume_them() -> None:
    executed: list[float] = []
    async with mcp_serving(ServedSystem(refund_workflow(executed))) as gate:
        async with session(gate, "ann") as owner:
            paused = body(await owner.call_tool("run_refunds", {"input": "refund"}))
        thread = paused["thread_id"]
        async with session(gate, "nt1") as stranger:
            attempt = await resume(stranger, thread)
        async with session(gate, "zed") as other_tenant:
            foreign = await resume(other_tenant, thread)
        assert attempt.isError and foreign.isError and executed == []
        # The refusal names the thread as the caller knows it, never the internal prefix.
        assert not re.search(r"t[0-9a-f]{16}\.", text(attempt) + text(foreign))
        async with session(gate, "ann") as owner:
            done = body(await resume(owner, thread))
    assert paused["status"] == "interrupted" and done["status"] == "completed"
    assert done["thread_id"] == thread and executed == [480]


# ---------------------------------------------------------------- sessions


async def test_a_session_answers_only_the_principal_that_opened_it() -> None:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    opening = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"elicitation": {}},
            "clientInfo": {"name": "rev", "version": "1"},
        },
    }
    listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    async with (
        mcp_serving(ServedSystem(refunds([]))) as gate,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url=BASE) as client,
    ):
        opened = await client.post("/mcp", json=opening, headers={**headers, **bearer("rev")})
        session_id = opened.headers["mcp-session-id"]
        joined = {**headers, "mcp-session-id": session_id}
        await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**joined, **bearer("rev")},
        )
        stranger = await client.post("/mcp", json=listing, headers={**joined, **bearer("ann")})
        own = await client.post("/mcp", json=listing, headers={**joined, **bearer("rev")})
    assert stranger.status_code == 403 and "run_refunds" not in stranger.text
    assert own.status_code == 200 and "run_refunds" in own.text


async def test_an_http_thread_named_like_an_mcp_namespace_stays_its_owners() -> None:
    executed: list[float] = []
    agent = refunds(executed)
    index = InMemoryThreadIndex()
    async with http(ServedSystem(agent), thread_index=index) as client:
        made = await client.post(
            "/v1/systems/refunds/runs",
            json={"input": "refund", "thread_id": "t0000000000000000.shared"},
            headers=bearer("ann"),
        )
    assert made.json()["status"] == "interrupted"
    async with (
        mcp_serving(ServedSystem(agent), thread_index=index) as gate,
        session(gate, "nt1") as stranger,
    ):
        attempt = await resume(stranger, "shared")
        continued = await stranger.call_tool(
            "run_refunds", {"input": "go on", "thread_id": "shared"}
        )
    assert attempt.isError and continued.isError and executed == []
