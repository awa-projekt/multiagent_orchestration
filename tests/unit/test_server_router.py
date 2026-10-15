"""Tests for `awa.server.create_router`: agents mounted in an existing FastAPI app."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from awa import Agent
from awa.automation import Automation
from awa.errors import AuthenticationError, ProcessRunStateError, RunNotFoundError
from awa.server import (
    ApiKeyAuthenticator,
    Principal,
    ServedSystem,
    create_app,
    create_automation_router,
    create_router,
    current_principal,
    current_request_id,
    drain,
    install_error_handler,
    router_lifespan,
)
from awa.server.errors import describe_error
from awa.testing import FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper

BASE = "http://testserver"

USERS = {
    "alice-token": Principal("alice", tenant="acme", roles={"approver"}),
    "bob-token": Principal("bob", tenant="acme"),
    "eve-token": Principal("eve", tenant="other"),
}


@asynccontextmanager
async def client_for(app: FastAPI, **headers: str) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, headers=headers) as client:
        yield client


def as_user(token: str) -> dict[str, str]:
    return {"X-Session": token}


# The host app's own auth: a session header resolved to its user.
async def current_user(x_session: Annotated[str | None, Header()] = None) -> Principal:
    user = USERS.get(x_session or "")
    if user is None:
        raise HTTPException(status_code=401, detail="host login required")
    return user


async def agent_caller(user: Annotated[Principal, Depends(current_user)]) -> Principal:
    return user


def echo_agent(name: str = "echo") -> Agent:
    return Agent(
        name,
        description="Echoes the request.",
        model=FunctionChatModel(lambda call: f"echo: {call.last_message.content}"),
    )


def refunds_agent(executed: list[float]) -> Agent:
    @tool
    def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order."""
        executed.append(amount)
        caller = current_principal()
        return f"refunded {amount} for {order_id} (caller {caller.id if caller else None})"

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


def host_app(**options: Any) -> FastAPI:
    app = FastAPI()

    @app.get("/orders")
    async def orders(user: Annotated[Principal, Depends(current_user)]) -> dict[str, Any]:
        return {"orders": [], "user": user.id}

    router = create_router({"echo": echo_agent()}, auth=agent_caller, **options)
    app.include_router(router, prefix="/ai")
    return app


# ---------------------------------------------------------------------- mounting and auth


async def test_router_serves_runs_next_to_the_host_routes() -> None:
    app = host_app()
    async with client_for(app) as client:
        own = await client.get("/orders", headers=as_user("bob-token"))
        listing = await client.get("/ai/agents/systems", headers=as_user("bob-token"))
        run = await client.post(
            "/ai/agents/systems/echo/runs", json={"input": "hi"}, headers=as_user("bob-token")
        )
        health = await client.get("/ai/agents/health")
    assert own.json() == {"orders": [], "user": "bob"}
    assert [s["name"] for s in listing.json()["systems"]] == ["echo"]
    assert run.status_code == 200 and run.json()["text"] == "echo: hi"
    assert run.json()["status"] == "completed"
    assert health.json() == {"status": "ok"}


async def test_host_auth_dependency_errors_keep_the_host_format() -> None:
    app = host_app()
    async with client_for(app) as client:
        missing = await client.post("/ai/agents/systems/echo/runs", json={"input": "hi"})
    # The host's HTTPException goes to the host's handlers.
    assert missing.status_code == 401
    assert missing.json() == {"detail": "host login required"}


async def test_library_errors_of_the_auth_dependency_use_the_awa_error_shape() -> None:
    async def strict_caller(x_key: Annotated[str | None, Header()] = None) -> Principal:
        if x_key != "k":
            raise AuthenticationError("Missing or invalid key")
        return Principal("svc")

    app = FastAPI()
    app.include_router(create_router(echo_agent(), auth=strict_caller))
    async with client_for(app) as client:
        refused = await client.get("/agents/systems", headers={"X-Request-ID": "req-42"})
        allowed = await client.get("/agents/systems", headers={"X-Key": "k"})
    assert refused.status_code == 401
    error = refused.json()["error"]
    assert error["code"] == "awa.authentication" and error["request_id"] == "req-42"
    assert refused.headers["WWW-Authenticate"] == "Bearer"
    assert allowed.status_code == 200


async def test_auth_dependency_must_return_a_principal(caplog: pytest.LogCaptureFixture) -> None:
    app = FastAPI()
    app.include_router(create_router(echo_agent(), auth=lambda: {"id": "x"}))
    async with client_for(app) as client:
        answer = await client.get("/agents/systems")
    assert answer.status_code == 500 and "Principal" not in answer.text  # details hidden
    assert "TypeError" in caplog.text and "Principal" in caplog.text


async def test_authenticator_and_anonymous_access_work_as_in_create_app() -> None:
    app = FastAPI()
    keys = ApiKeyAuthenticator({"secret": Principal("svc", tenant="acme")})
    app.include_router(create_router(echo_agent(), authenticator=keys, prefix="/keyed"))
    app.include_router(create_router(echo_agent("open"), prefix="/open"))
    async with client_for(app) as client:
        denied = await client.get("/keyed/systems")
        keyed = await client.get("/keyed/systems", headers={"Authorization": "Bearer secret"})
        anonymous = await client.post("/open/systems/open/runs", json={"input": "x"})
    assert denied.status_code == 401 and denied.json()["error"]["code"] == "awa.authentication"
    assert keyed.status_code == 200
    assert anonymous.json()["text"] == "echo: x"


async def test_request_ids_are_bound_per_request_and_released() -> None:
    seen: list[str | None] = []

    def respond(call: ModelCall) -> str:
        seen.append(current_request_id())
        return "ok"

    agent = Agent("probe", description="Probe.", model=FunctionChatModel(respond))
    app = FastAPI()
    app.include_router(create_router(agent))
    async with client_for(app) as client:
        first = await client.post(
            "/agents/systems/probe/runs", json={"input": "a"}, headers={"X-Request-ID": "req-1"}
        )
        second = await client.post("/agents/systems/probe/runs", json={"input": "b"})
    assert first.headers["X-Request-ID"] == "req-1" and first.json()["request_id"] == "req-1"
    generated = second.headers["X-Request-ID"]
    assert generated.startswith("req_") and second.json()["request_id"] == generated
    assert seen == ["req-1", generated]
    assert current_request_id() is None and current_principal() is None


async def test_invalid_requests_get_the_awa_validation_error() -> None:
    app = host_app()
    async with client_for(app) as client:
        invalid = await client.post(
            "/ai/agents/systems/echo/runs", json={"input": ""}, headers=as_user("bob-token")
        )
        unknown = await client.post(
            "/ai/agents/systems/nope/runs", json={"input": "x"}, headers=as_user("bob-token")
        )
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "awa.validation"
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "awa.system_not_found"


# ---------------------------------------------------------------------- runs


async def test_async_runs_point_to_the_mounted_status_url() -> None:
    app = host_app()
    headers = as_user("bob-token")
    async with client_for(app) as client:
        accepted = await client.post(
            "/ai/agents/systems/echo/runs?mode=async", json={"input": "later"}, headers=headers
        )
        assert accepted.status_code == 202
        status_url = accepted.json()["status_url"]
        assert status_url == f"/ai/agents/runs/{accepted.json()['run_id']}"
        assert accepted.headers["Location"] == status_url
        for _ in range(100):
            state = (await client.get(status_url, headers=headers)).json()
            if state["status"] != "running":
                break
            await asyncio.sleep(0.01)
        other_tenant = await client.get(status_url, headers=as_user("eve-token"))
        listing = await client.get("/ai/agents/runs", headers=headers)
    assert state["status"] == "completed" and state["result"]["text"] == "echo: later"
    assert other_tenant.status_code == 404
    assert [run["run_id"] for run in listing.json()["runs"]] == [state["run_id"]]


async def test_streamed_runs_send_server_sent_events() -> None:
    app = host_app()
    async with client_for(app) as client:
        response = await client.post(
            "/ai/agents/systems/echo/runs?stream=true",
            json={"input": "stream me"},
            headers=as_user("bob-token"),
        )
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [
        block.split("\n")[0].removeprefix("event: ")
        for block in response.text.split("\n\n")
        if block.startswith("event: ")
    ]
    assert events[0] == "agent_started" and events[-1] == "run_completed"
    last = response.text.strip().split("\n\n")[-1]
    data = json.loads(last.split("data: ", 1)[1])
    assert data["result"]["text"] == "echo: stream me"
    assert data["result"]["request_id"] == response.headers["X-Request-ID"]


async def test_approvals_pause_and_resume_with_the_host_principal_roles() -> None:
    executed: list[float] = []
    app = FastAPI()
    router = create_router(
        {"refunds": ServedSystem(refunds_agent(executed), approver_roles={"approver"})},
        auth=agent_caller,
    )
    app.include_router(router, prefix="/ai")
    async with client_for(app) as client:
        paused = await client.post(
            "/ai/agents/systems/refunds/runs",
            json={"input": "refund A-1"},
            headers=as_user("bob-token"),
        )
        thread = paused.json()["thread_id"]
        base = f"/ai/agents/systems/refunds/threads/{thread}"
        pending = await client.get(f"{base}/interrupts", headers=as_user("bob-token"))
        hidden = await client.get(f"{base}/interrupts", headers=as_user("eve-token"))
        forbidden = await client.post(
            f"{base}/resume", json={"decision": {"action": "approve"}}, headers=as_user("bob-token")
        )
        resumed = await client.post(
            f"{base}/resume",
            json={"decision": {"action": "approve"}},
            headers=as_user("alice-token"),
        )
        threads = await client.get(
            "/ai/agents/systems/refunds/threads", headers=as_user("bob-token")
        )
    assert paused.json()["status"] == "interrupted"
    assert len(pending.json()["interrupts"]) == 1
    assert hidden.status_code == 404
    assert forbidden.status_code == 403
    assert resumed.status_code == 200 and resumed.json()["status"] == "completed"
    assert "refunded 480" in resumed.json()["text"] and executed == [480]
    assert [t["thread_id"] for t in threads.json()["threads"]] == [thread]


async def test_two_routers_in_one_app_keep_their_own_state_and_openapi() -> None:
    app = FastAPI()
    sales = create_router(echo_agent("sales"), prefix="/sales")
    support = create_router(echo_agent("support"), prefix="/support")
    app.include_router(sales)
    app.include_router(support)
    async with client_for(app) as client:
        found = await client.post("/sales/systems/sales/runs", json={"input": "x"})
        missing = await client.post("/sales/systems/support/runs", json={"input": "x"})
    assert found.status_code == 200 and missing.status_code == 404
    paths = app.openapi()["paths"]
    assert "/sales/systems/{name}/runs" in paths and "/support/systems/{name}/runs" in paths


# ---------------------------------------------------------------------- lifespan and drain


def test_router_lifespan_wraps_the_host_lifespan_and_drains() -> None:
    events: list[str] = []
    router = create_router(echo_agent())

    @asynccontextmanager
    async def resources(app: FastAPI) -> AsyncIterator[None]:
        events.append("open")
        yield
        events.append("close")

    app = FastAPI(lifespan=router_lifespan(router, lifespan=resources))
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get("/agents/ready").json()["status"] == "ready"
    assert events == ["open", "close"]
    assert router.awa_server.runs.draining  # type: ignore[attr-defined]


def test_including_the_router_drains_it_at_shutdown() -> None:
    router = create_router(echo_agent())
    app = FastAPI()
    app.include_router(router, prefix="/ai")
    with TestClient(app) as client:
        assert client.post("/ai/agents/systems/echo/runs", json={"input": "x"}).status_code == 200
    assert router.awa_server.runs.draining  # type: ignore[attr-defined]


async def test_drain_accepts_a_router_and_refuses_new_runs() -> None:
    router = create_router(echo_agent())
    app = FastAPI()
    app.include_router(router)
    assert await drain(router, timeout=1) == 0
    async with client_for(app) as client:
        refused = await client.post("/agents/systems/echo/runs", json={"input": "x"})
        ready = await client.get("/agents/ready")
    assert refused.status_code == 503 and ready.status_code == 503
    with pytest.raises(TypeError, match="create_router"):
        router_lifespan(FastAPI().router)


def test_create_router_validates_its_arguments() -> None:
    with pytest.raises(ValueError, match="prefix"):
        create_router(echo_agent(), prefix="agents")
    with pytest.raises(ValueError, match="prefix"):
        create_router(echo_agent(), prefix="/agents/")
    with pytest.raises(ValueError, match="not several"):
        create_router(echo_agent(), auth=agent_caller, api_key="k")
    with pytest.raises(TypeError, match="dependency"):
        create_router(echo_agent(), auth="nope")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="thread_scope"):
        create_router(echo_agent(), thread_scope="world")  # type: ignore[arg-type]
    assert create_router(echo_agent(), prefix="").routes


# ---------------------------------------------------------------------- with automations


def test_create_app_serves_automations_with_the_app_authenticator() -> None:
    automation = Automation(echo_agent(), name="tickets", input="{payload.text}")
    app = create_app(echo_agent(), api_key="secret", automations=[automation])
    with TestClient(app) as client:
        denied = client.get("/v1/automations")
        listed = client.get("/v1/automations", headers={"Authorization": "Bearer secret"})
        queued = client.post(
            "/v1/automations/tickets/events",
            json={"payload": {"text": "printer on fire"}},
            headers={"Authorization": "Bearer secret"},
        )
    assert denied.status_code == 401 and denied.json()["error"]["code"] == "awa.authentication"
    assert [a["name"] for a in listed.json()["automations"]] == ["tickets"]
    assert queued.status_code == 202


async def test_both_routers_mount_into_an_existing_app() -> None:
    keys = ApiKeyAuthenticator({"svc-key": Principal("svc", tenant="acme", roles={"operator"})})
    automation = Automation(echo_agent(), name="tickets", input="{payload.text}")
    app = FastAPI()
    app.include_router(create_router(echo_agent(), authenticator=keys), prefix="/ai")
    app.include_router(
        create_automation_router([automation], authenticator=keys, prefix=""), prefix="/ai"
    )
    headers = {"Authorization": "Bearer svc-key"}
    async with client_for(app) as client:
        run = await client.post(
            "/ai/agents/systems/echo/runs", json={"input": "x"}, headers=headers
        )
        denied = await client.get("/ai/automations")
        queued = await client.post(
            "/ai/automations/tickets/events", json={"payload": {"text": "t"}}, headers=headers
        )
        missing = await client.get("/ai/process-runs/nope", headers=headers)
    assert run.json()["text"] == "echo: x"
    assert denied.status_code == 401 and denied.json()["error"]["code"] == "awa.authentication"
    assert queued.status_code == 202
    assert missing.status_code == 404


async def test_install_error_handler_answers_library_errors_of_the_host_routes() -> None:
    app = FastAPI()

    @app.get("/mine")
    async def mine() -> None:
        raise RunNotFoundError("No run 'x'")

    install_error_handler(app)
    async with client_for(app) as client:
        answer = await client.get("/mine")
    assert answer.status_code == 404 and answer.json()["error"]["type"] == "RunNotFoundError"


def test_process_run_state_conflicts_are_409() -> None:
    info = describe_error(ProcessRunStateError("The run already succeeded"))
    assert info.status == 409 and info.code == "awa.process_run_state"
