"""`awa.server.mcp`: awa systems as MCP tools, and the caller bound to approver roles, tenants and fail-closed defaults."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session

from awa import Agent
from awa.cli import build_parser
from awa.contracts import AgentCard
from awa.contracts.results import RunResult
from awa.errors import ConfigurationError
from awa.models import ModelRegistry
from awa.server import ApiKeyAuthenticator, Principal, ServedSystem
from awa.server import mcp as mcp_server
from awa.server.mcp import (
    BearerGate,
    build_blueprint_system,
    create_mcp_server,
    exposable_tools,
    serve_as_mcp,
)
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper

ElicitAnswer = Callable[[types.ElicitRequestParams], types.ElicitResult]


@asynccontextmanager
async def connect(server: Any, answer: ElicitAnswer | None = None) -> AsyncIterator[ClientSession]:
    callback = None
    if answer is not None:

        async def elicit(context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
            return answer(params)

        callback = elicit
    async with create_connected_server_and_client_session(
        server._mcp_server, elicitation_callback=callback, raise_exceptions=True
    ) as session:
        yield session


def echo_agent(name: str = "echo", checkpointer: InMemorySaver | None = None) -> Agent:
    def respond(call: ModelCall) -> str:
        return f"echo: {call.last_message.content} ({len(call.messages)} messages)"

    return Agent(
        name,
        description="Echoes the request.",
        model=FunctionChatModel(respond),
        checkpointer=checkpointer,
    )


def refunds_agent(executed: list[float]) -> Agent:
    @tool
    def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order."""
        executed.append(amount)
        return f"refunded {amount} for {order_id}"

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"result: {call.last_message.content}"
        return tool_call("issue_refund", {"order_id": "A-1", "amount": 480})

    return Agent(
        "refunds",
        description="Issues refunds.",
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(issue_refund)],
        checkpointer=InMemorySaver(),
    )


def structured(result: types.CallToolResult) -> dict[str, Any]:
    assert not result.isError, result.content
    assert result.structuredContent is not None
    summary = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert summary["status"] == result.structuredContent["status"]
    return result.structuredContent


def accept(approve: bool) -> ElicitAnswer:
    def answer(params: types.ElicitRequestParams) -> types.ElicitResult:
        assert "issue_refund" in params.message
        return types.ElicitResult(action="accept", content={"approve": approve, "note": "ok"})

    return answer


# ---------------------------------------------------------------------- listing and running


async def test_systems_are_listed_as_run_and_resume_tools() -> None:
    server = create_mcp_server(
        {"echo": echo_agent(), "chat": echo_agent("chat", InMemorySaver())}, name="acme"
    )
    async with connect(server) as session:
        listed = {item.name: item for item in (await session.list_tools()).tools}
    assert set(listed) == {"run_echo", "run_chat", "resume_chat"}
    run_echo = listed["run_echo"]
    assert run_echo.inputSchema["required"] == ["input"]
    assert set(run_echo.inputSchema["properties"]) == {"input"}
    assert "Echoes the request." in (run_echo.description or "")
    assert run_echo.annotations is not None and run_echo.annotations.readOnlyHint is False
    assert set(listed["run_chat"].inputSchema["properties"]) == {"input", "thread_id"}
    assert listed["resume_chat"].inputSchema["required"] == ["thread_id", "decision"]


async def test_run_tools_return_the_result_and_continue_threads() -> None:
    server = create_mcp_server(echo_agent("chat", InMemorySaver()))
    async with connect(server) as session:
        first = structured(await session.call_tool("run_chat", {"input": "hello"}))
        second = structured(
            await session.call_tool("run_chat", {"input": "again", "thread_id": first["thread_id"]})
        )
    assert first["status"] == "completed" and first["text"].startswith("echo: hello")
    assert second["thread_id"] == first["thread_id"]
    assert "(3 messages)" in second["text"]


async def test_structured_input_follows_the_card_schema() -> None:
    schema = {
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
    }

    class OrderDesk:
        name = "orders"
        card = AgentCard(name="orders", description="Order status.", input_schema=schema)

        async def run(self, input: Any, **options: Any) -> RunResult[Any]:
            order = json.loads(input)["order_id"]
            return RunResult(run_id="run_1", agent="orders", output=f"{order}: shipped")

    server = create_mcp_server(OrderDesk())
    async with connect(server) as session:
        listed = (await session.list_tools()).tools
        called = structured(await session.call_tool("run_orders", {"input": {"order_id": "A-7"}}))
    assert listed[0].inputSchema["properties"]["input"] == schema
    assert called["output"] == "A-7: shipped"


async def test_failures_and_bad_arguments_are_tool_errors_without_internals() -> None:
    def boom(call: ModelCall) -> str:
        raise RuntimeError("password=hunter2")

    failing = Agent("boom", description="Fails.", model=FunctionChatModel(boom))
    server = create_mcp_server([failing, echo_agent()])
    async with connect(server) as session:
        failed = await session.call_tool("run_boom", {"input": "x"})
        missing = await session.call_tool("run_echo", {})
        no_threads = await session.call_tool("run_echo", {"input": "x", "thread_id": "t-1"})
    assert failed.isError and "hunter2" not in failed.content[0].text  # type: ignore[union-attr]
    assert "InternalError" in failed.content[0].text  # type: ignore[union-attr]
    assert missing.isError and "Invalid arguments" in missing.content[0].text  # type: ignore[union-attr]
    assert no_threads.isError and "keeps no threads" in no_threads.content[0].text  # type: ignore[union-attr]


async def test_served_system_rules_apply() -> None:
    seen: list[Any] = []

    def respond(call: ModelCall) -> str:
        from awa.runtime import current_run_context

        ctx = current_run_context()
        assert ctx is not None
        seen.append((ctx.deps, ctx.metadata.get("mcp_client")))
        return "ok"

    agent = Agent("ctx", description="Context.", model=FunctionChatModel(respond))
    served = ServedSystem(agent, deps_factory=lambda sent: {"db": "server-side", **sent})
    async with connect(create_mcp_server({"ctx": served})) as session:
        structured(await session.call_tool("run_ctx", {"input": "x"}))
    assert seen == [({"db": "server-side"}, "mcp")]


# ---------------------------------------------------------------------- approvals


async def test_approvals_without_elicitation_pause_and_resume_by_tool() -> None:
    executed: list[float] = []
    server = create_mcp_server(refunds_agent(executed))
    async with connect(server) as session:
        paused = structured(await session.call_tool("run_refunds", {"input": "refund A-1"}))
        assert paused["status"] == "interrupted" and executed == []
        assert paused["interrupts"][0]["value"]["tool"] == "issue_refund"
        done = structured(
            await session.call_tool(
                "resume_refunds",
                {"thread_id": paused["thread_id"], "decision": {"action": "approve"}},
            )
        )
        again = await session.call_tool(
            "resume_refunds", {"thread_id": paused["thread_id"], "decision": {"action": "approve"}}
        )
    assert done["status"] == "completed" and "refunded 480" in done["text"]
    assert executed == [480]
    assert again.isError


async def test_elicitation_asks_the_client_user_and_resumes_in_one_call() -> None:
    executed: list[float] = []
    server = create_mcp_server(refunds_agent(executed))
    async with connect(server, accept(True)) as session:
        approved = structured(await session.call_tool("run_refunds", {"input": "refund A-1"}))
    assert approved["status"] == "completed" and executed == [480]

    rejected_log: list[float] = []
    async with connect(create_mcp_server(refunds_agent(rejected_log)), accept(False)) as session:
        rejected = structured(await session.call_tool("run_refunds", {"input": "refund A-1"}))
    assert rejected["status"] == "completed" and rejected_log == []


async def test_an_elicited_run_reports_the_usage_of_every_round() -> None:
    async with connect(create_mcp_server(refunds_agent([])), accept(True)) as session:
        whole = structured(await session.call_tool("run_refunds", {"input": "refund A-1"}))
    async with connect(create_mcp_server(refunds_agent([]))) as session:
        paused = structured(await session.call_tool("run_refunds", {"input": "refund A-1"}))
        rest = structured(
            await session.call_tool(
                "resume_refunds",
                {"thread_id": paused["thread_id"], "decision": {"action": "approve"}},
            )
        )
    assert paused["usage"]["requests"] == rest["usage"]["requests"] == 1  # each part alone
    assert whole["usage"]["requests"] == 2  # the tool call used both
    assert (
        whole["usage"]["total_tokens"]
        == paused["usage"]["total_tokens"] + rest["usage"]["total_tokens"]
    )


async def test_declined_elicitation_rejects_and_cancelled_leaves_the_run_paused() -> None:
    declined_log: list[float] = []

    def decline(params: types.ElicitRequestParams) -> types.ElicitResult:
        return types.ElicitResult(action="decline")

    async with connect(create_mcp_server(refunds_agent(declined_log)), decline) as session:
        declined = structured(await session.call_tool("run_refunds", {"input": "refund"}))
    assert declined["status"] == "completed" and declined_log == []

    def cancel(params: types.ElicitRequestParams) -> types.ElicitResult:
        return types.ElicitResult(action="cancel")

    async with connect(create_mcp_server(refunds_agent([])), cancel) as session:
        paused = structured(await session.call_tool("run_refunds", {"input": "refund"}))
    assert paused["status"] == "interrupted"

    no_elicit = create_mcp_server(refunds_agent([]), elicit_approvals=False)
    async with connect(no_elicit, accept(True)) as session:
        kept = structured(await session.call_tool("run_refunds", {"input": "refund"}))
    assert kept["status"] == "interrupted"


# ---------------------------------------------------------------------- direct tools


async def test_tools_can_be_exposed_directly() -> None:
    @tool
    def lookup_order(order_id: str) -> str:
        """Return an order's status."""
        if order_id == "bad":
            raise ValueError("unknown order")
        return f"{order_id}: shipped"

    server = create_mcp_server(echo_agent(), tools=[lookup_order])
    async with connect(server) as session:
        listed = {item.name: item for item in (await session.list_tools()).tools}
        found = await session.call_tool("lookup_order", {"order_id": "A-7"})
        bad = await session.call_tool("lookup_order", {"order_id": "bad"})
    assert listed["lookup_order"].inputSchema["properties"]["order_id"]["type"] == "string"
    assert found.content[0].text == "A-7: shipped"  # type: ignore[union-attr]
    assert bad.isError and "unknown order" in bad.content[0].text  # type: ignore[union-attr]

    @tool("run_echo")
    def clash() -> str:
        """Clashes with the run tool."""
        return ""

    with pytest.raises(ValueError, match="run_echo"):
        create_mcp_server(echo_agent(), tools=[clash])


# ---------------------------------------------------------------------- serving and CLI


def test_serve_as_mcp_runs_stdio_or_guarded_http(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[Any] = []
    fastmcp = type(create_mcp_server(echo_agent()))
    monkeypatch.setattr(fastmcp, "run", lambda self, transport: ran.append(transport))
    serve_as_mcp(echo_agent())
    assert ran == ["stdio"]

    import uvicorn

    served: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: served.append((app, options)))
    serve_as_mcp(echo_agent(), transport="http", port=8765, api_key="k")
    app, options = served[0]
    assert isinstance(app, BearerGate) and options["port"] == 8765
    with pytest.raises(ValueError, match="transport"):
        serve_as_mcp(echo_agent(), transport="sse")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not both"):
        serve_as_mcp(
            echo_agent(), api_key="k", authenticator=ApiKeyAuthenticator({"x": Principal("x")})
        )


def test_serve_as_mcp_serves_a_durable_system_with_a_thread_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.persistence import SqliteCheckpointer
    from awa.server import InMemoryThreadIndex

    ran: list[Any] = []
    fastmcp = type(create_mcp_server(echo_agent()))
    monkeypatch.setattr(fastmcp, "run", lambda self, transport: ran.append(transport))
    durable = echo_agent(checkpointer=SqliteCheckpointer(tmp_path / "c.db"))  # type: ignore[arg-type]

    with pytest.raises(ConfigurationError, match="thread_index"):
        serve_as_mcp(durable)
    serve_as_mcp(durable, thread_index=InMemoryThreadIndex())

    assert ran == ["stdio"]


async def test_bearer_gate_refuses_requests_without_the_key() -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    gate = BearerGate(app, ApiKeyAuthenticator({"k": Principal("client")}))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gate), base_url="http://t"
    ) as client:
        refused = await client.post("/mcp")
        allowed = await client.post("/mcp", headers={"Authorization": "Bearer k"})
    assert refused.status_code == 401 and refused.json()["error"]["code"] == "awa.authentication"
    assert allowed.text == "ok"


TOOLS_MODULE = '''
from langchain_core.tools import tool

CALLS = []


@tool
def pay(order_id: str) -> str:
    """Pay an order."""
    CALLS.append(order_id)
    return f"paid {order_id}"


@tool
def order_status(order_id: str) -> str:
    """Look up an order."""
    return f"{order_id}: open"


@tool
def send_email(to: str, text: str) -> str:
    """Send an e-mail."""
    return "sent"


import awa


@awa.tool(approval=True)
def archive_note(note_id: str) -> str:
    """Archive a note."""
    return "archived"
'''

GATED = """
name: payments
description: Pays orders after approval.
agents:
  - name: cashier
    description: Pays orders.
    tools: [pay, order_status, send_email]
approvals:
  - tool: pay
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "mcp_serve_tools.py").write_text(TOOLS_MODULE, encoding="utf-8")
    (tmp_path / "gated.yaml").write_text(GATED, encoding="utf-8")
    monkeypatch.delitem(sys.modules, "mcp_serve_tools", raising=False)
    return tmp_path


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> ModelRegistry:
    registry = ModelRegistry()
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    return registry


def parser() -> argparse.ArgumentParser:
    return build_parser(["mcp"])


async def test_blueprints_serve_with_approvals_and_safe_tools_only(
    workspace: Path, models: ModelRegistry
) -> None:
    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "9"}), "Paid."]))
    served, loaded, blueprint = build_blueprint_system("gated.yaml", tools=["mcp_serve_tools.py"])
    exposed, notes = exposable_tools(loaded, blueprint)
    assert [tool.name for tool in exposed] == ["order_status"]
    assert notes == [
        "not exposed: pay (needs approval)",
        "not exposed: send_email (acts on the outside world)",
        "not exposed: archive_note (needs approval)",  # gates itself: needs a paused run
    ]
    server = create_mcp_server({"cashier": served}, tools=exposed)
    async with connect(server) as session:
        paused = structured(await session.call_tool("run_cashier", {"input": "Pay 9"}))
        done = structured(
            await session.call_tool(
                "resume_cashier",
                {"thread_id": paused["thread_id"], "decision": {"action": "approve"}},
            )
        )
    assert paused["status"] == "interrupted" and done["output"] == "Paid."
    assert sys.modules["mcp_serve_tools"].CALLS == ["9"]


def test_awa_mcp_serve_parses_options_and_serves(
    workspace: Path, models: ModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr(
        mcp_server, "serve_as_mcp", lambda systems, **options: captured.append((systems, options))
    )
    args = parser().parse_args(
        ["mcp", "serve", "gated.yaml", "--tools", "mcp_serve_tools.py", "--expose-tools"]
    )
    assert args.handler.__name__ == "_serve"
    assert (args.transport, args.host, args.port, args.elicit) == ("stdio", "127.0.0.1", 8000, True)
    assert args.handler(args) == 0
    systems, options = captured[0]
    assert list(systems) == ["cashier"] and options["transport"] == "stdio"
    assert [tool.name for tool in options["tools"]] == ["order_status"]
    assert options["name"] == "payments" and options["api_key"] is None

    monkeypatch.setenv("MCP_TEST_KEY", "secret")
    http = parser().parse_args(
        [
            "mcp",
            "serve",
            "gated.yaml",
            "--tools",
            "mcp_serve_tools.py",
            "--transport",
            "http",
            "--api-key-env",
            "MCP_TEST_KEY",
            "--no-elicit",
        ]
    )
    assert http.handler(http) == 0
    _, options = captured[1]
    assert options["api_key"] == "secret" and options["elicit_approvals"] is False
    assert options["tools"] == []
    from awa.errors import MissingConfigurationError

    missing = parser().parse_args(["mcp", "serve", "gated.yaml", "--api-key-env", "NO_SUCH_KEY_X"])
    with pytest.raises(MissingConfigurationError, match="NO_SUCH_KEY_X"):
        missing.handler(missing)

    open_host = ["mcp", "serve", "gated.yaml", "--tools", "mcp_serve_tools.py"]
    open_host += ["--transport", "http", "--host", "0.0.0.0"]
    refused = parser().parse_args(open_host)
    with pytest.raises(ConfigurationError, match="--allow-unauthenticated"):
        refused.handler(refused)
    allowed = parser().parse_args([*open_host, "--allow-unauthenticated"])
    assert allowed.handler(allowed) == 0
    assert captured[-1][1]["allow_unauthenticated"] is True


BASE = "http://127.0.0.1:8000"
USER = Principal("ann", tenant="acme")
REVIEWER = Principal("rev", tenant="acme", roles={"approver"})
OTHER_TENANT_REVIEWER = Principal("zed", tenant="globex", roles={"approver"})
KEYS = {"user": USER, "reviewer": REVIEWER, "zed": OTHER_TENANT_REVIEWER}


def refunds(executed: list[float]) -> Agent:
    @tool
    def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order."""
        executed.append(amount)
        return f"refunded {amount} for {order_id}"

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"result: {call.last_message.content}"
        return tool_call("issue_refund", {"order_id": "A-1", "amount": 480})

    return Agent(
        "refunds",
        description="Issues refunds.",
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(issue_refund)],
        checkpointer=InMemorySaver(),
    )


class Callers:
    """One MCP server over HTTP; `session(key)` is a client authenticated as `KEYS[key]`."""

    def __init__(self, gate: BearerGate) -> None:
        self.gate = gate

    @asynccontextmanager
    async def session(self, key: str) -> AsyncIterator[ClientSession]:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.gate),
            base_url=BASE,
            headers={"Authorization": f"Bearer {key}"},
        )
        async with (
            client,
            streamable_http_client(f"{BASE}/mcp", http_client=client) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session


@asynccontextmanager
async def serving(served: Any) -> AsyncIterator[Callers]:
    server = create_mcp_server({"refunds": served})
    app = server.streamable_http_app()
    gate = BearerGate(app, ApiKeyAuthenticator(dict(KEYS)))
    async with app.router.lifespan_context(app):
        yield Callers(gate)


def body(result: types.CallToolResult) -> dict[str, Any]:
    assert not result.isError, result.content
    assert result.structuredContent is not None
    return result.structuredContent


async def resume(session: ClientSession, thread_id: str) -> types.CallToolResult:
    return await session.call_tool(
        "resume_refunds", {"thread_id": thread_id, "decision": {"action": "approve"}}
    )


def text(result: types.CallToolResult) -> str:
    return str(result.content[0].text)  # type: ignore[union-attr]


# ---------------------------------------------------------------- approver roles


async def test_a_caller_without_the_approver_role_cannot_resume() -> None:
    executed: list[float] = []
    served = ServedSystem(refunds(executed), approver_roles={"approver"})
    async with serving(served) as callers:
        async with callers.session("user") as user:
            paused = body(await user.call_tool("run_refunds", {"input": "refund"}))
            refused = await resume(user, paused["thread_id"])
        assert refused.isError and "approver" in text(refused)
        assert executed == []
        async with callers.session("reviewer") as reviewer:
            done = body(await resume(reviewer, paused["thread_id"]))
    assert done["status"] == "completed" and executed == [480]


@pytest.mark.parametrize(
    ("caller", "asked", "status"), [(USER, 0, "interrupted"), (REVIEWER, 1, "completed")]
)
async def test_elicitation_is_only_offered_to_a_caller_with_the_approver_role(
    monkeypatch: pytest.MonkeyPatch, caller: Principal, asked: int, status: str
) -> None:
    # The in-memory transport has no HTTP request, so the gate's principal is set directly.
    monkeypatch.setattr(mcp_server, "_caller", lambda context: caller)
    executed: list[float] = []
    served = ServedSystem(refunds(executed), approver_roles={"approver"})
    server = create_mcp_server({"refunds": served})
    questions: list[Any] = []

    async def elicit(context: Any, params: Any) -> types.ElicitResult:
        questions.append(params)
        return types.ElicitResult(action="accept", content={"approve": True, "note": ""})

    async with create_connected_server_and_client_session(
        server._mcp_server, elicitation_callback=elicit, raise_exceptions=True
    ) as session:
        result = body(await session.call_tool("run_refunds", {"input": "refund"}))
    assert len(questions) == asked and result["status"] == status
    assert executed == ([480] if asked else [])


async def test_without_a_known_caller_roles_are_never_met() -> None:
    executed: list[float] = []
    served = ServedSystem(refunds(executed), approver_roles={"approver"})
    server = create_mcp_server({"refunds": served})
    asked: list[Any] = []

    async def elicit(context: Any, params: Any) -> types.ElicitResult:
        asked.append(params)
        return types.ElicitResult(action="accept", content={"approve": True, "note": ""})

    async with create_connected_server_and_client_session(
        server._mcp_server, elicitation_callback=elicit, raise_exceptions=True
    ) as session:
        paused = body(await session.call_tool("run_refunds", {"input": "refund"}))
        refused = await resume(session, paused["thread_id"])
    assert asked == [] and paused["status"] == "interrupted"
    assert refused.isError and executed == []


async def test_without_roles_any_caller_may_resume_as_before() -> None:
    executed: list[float] = []
    async with serving(ServedSystem(refunds(executed))) as callers, callers.session("user") as user:
        paused = body(await user.call_tool("run_refunds", {"input": "refund"}))
        done = body(await resume(user, paused["thread_id"]))
    assert done["status"] == "completed" and executed == [480]


# ---------------------------------------------------------------- tenant scoping


async def test_threads_of_one_tenant_are_not_reachable_from_another() -> None:
    executed: list[float] = []
    served = ServedSystem(refunds(executed), approver_roles={"approver"})
    async with serving(served) as callers:
        async with callers.session("user") as user:
            paused = body(await user.call_tool("run_refunds", {"input": "refund"}))
        thread = paused["thread_id"]
        async with callers.session("zed") as stranger:
            attempt = await resume(stranger, thread)
        assert attempt.isError and executed == []
        async with callers.session("reviewer") as reviewer:  # same tenant: may answer
            done = body(await resume(reviewer, thread))
    assert done["status"] == "completed" and done["thread_id"] == thread and executed == [480]


async def test_a_thread_id_cannot_be_forged_across_tenants() -> None:
    executed: list[float] = []
    served = ServedSystem(refunds(executed), approver_roles={"approver"})
    async with serving(served) as callers:
        async with callers.session("user") as user:
            paused = body(await user.call_tool("run_refunds", {"input": "refund"}))
        async with callers.session("zed") as stranger:
            # naming the other tenant's thread id continues a thread of the stranger's own
            other = body(
                await stranger.call_tool(
                    "run_refunds", {"input": "refund", "thread_id": paused["thread_id"]}
                )
            )
            attempt = await resume(stranger, "t0123456789abcdef.forged")
    assert other["status"] == "interrupted" and executed == []
    assert attempt.isError and executed == []


async def test_a_caller_without_tenant_naming_a_tenant_scoped_id_stays_in_its_namespace() -> None:
    # Every caller's ids get its namespace's prefix: a shaped id is just a name of its own.
    server = create_mcp_server({"refunds": refunds([])})
    async with create_connected_server_and_client_session(
        server._mcp_server, raise_exceptions=True
    ) as session:
        guessed = body(
            await session.call_tool(
                "run_refunds", {"input": "x", "thread_id": "t0123456789abcdef.guess"}
            )
        )
    assert guessed["status"] == "interrupted"
    assert guessed["thread_id"] == "t0123456789abcdef.guess"


# ---------------------------------------------------------------- the gate and the entry point


async def test_the_gate_hands_the_authenticated_principal_to_the_app() -> None:
    seen: list[Any] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope.get("awa.principal"))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    gate = BearerGate(app, ApiKeyAuthenticator({"k": USER}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url=BASE) as client:
        await client.post("/mcp", headers={"Authorization": "Bearer k"})
    assert seen == [USER]


def test_roles_cannot_be_enforced_over_stdio_or_a_single_api_key() -> None:
    served = ServedSystem(refunds([]), approver_roles={"approver"})
    with pytest.raises(ConfigurationError, match="authenticator"):
        serve_as_mcp(served)
    with pytest.raises(ConfigurationError, match="authenticator"):
        serve_as_mcp(served, transport="http", api_key="k")
    with pytest.raises(ConfigurationError, match="authenticator"):
        serve_as_mcp(served, transport="http")


def test_serving_on_an_open_interface_without_authentication_is_refused_unless_allowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import uvicorn

    served: list[str] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: served.append(options["host"]))
    caplog.set_level(logging.WARNING)
    with pytest.raises(ConfigurationError, match="allow_unauthenticated"):
        serve_as_mcp(refunds([]), transport="http", host="0.0.0.0")  # noqa: S104
    assert served == []
    serve_as_mcp(refunds([]), transport="http", host="0.0.0.0", allow_unauthenticated=True)  # noqa: S104
    assert any("without authentication" in record.getMessage() for record in caplog.records)
    caplog.clear()
    serve_as_mcp(refunds([]), transport="http", host="127.0.0.1")
    serve_as_mcp(refunds([]), transport="http", host="0.0.0.0", api_key="k")  # noqa: S104
    assert caplog.records == []
