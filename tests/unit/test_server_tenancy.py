"""Principals, authenticators, tenant isolation and thread ownership in `awa.server`."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver

from awa import Agent
from awa.errors import ConfigurationError
from awa.memory import AgentMemory, MemoryScope
from awa.persistence import SqliteCheckpointer
from awa.runtime import current_run_context
from awa.server import (
    ApiKeyAuthenticator,
    Authenticator,
    BearerTokenAuthenticator,
    InMemoryThreadIndex,
    Principal,
    ServedSystem,
    ThreadOwner,
    create_app,
    create_router,
    current_principal,
)
from awa.server._auth import ANONYMOUS
from awa.server.errors import AuthenticationError
from awa.server.mcp import create_mcp_server
from awa.testing import FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper

BASE = "http://testserver"

ALICE = Principal("alice", tenant="acme")
BOB = Principal("bob", tenant="acme")
REVIEWER = Principal("rita", tenant="acme", roles={"approver"})
MALLORY = Principal("mallory", tenant="globex", roles={"approver"})
KEYS = {"k-alice": ALICE, "k-bob": BOB, "k-rita": REVIEWER, "k-mallory": MALLORY}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@asynccontextmanager
async def client_for(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        yield client


def refund_tool(decisions: list[Any]) -> BaseTool:
    @tool
    def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order."""
        return f"refunded {amount} for {order_id}"

    return create_tool_approval_wrapper(issue_refund)


def refunds_agent(decisions: list[Any] | None = None) -> Agent:
    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"result: {call.last_message.content}"
        return tool_call("issue_refund", {"order_id": "A-1", "amount": 480})

    return Agent(
        "refunds",
        model=FunctionChatModel(respond),
        tools=[refund_tool(decisions if decisions is not None else [])],
        checkpointer=InMemorySaver(),
    )


def echo_agent(name: str = "echo") -> Agent:
    return Agent(name, model=FunctionChatModel(lambda call: f"echo: {call.last_message.content}"))


class FakeRequest:
    def __init__(self, **headers: str) -> None:
        self.headers = {key.lower(): value for key, value in headers.items()}


# ---------------------------------------------------------------------- principals


def test_principal_normalises_roles_and_freezes_attributes() -> None:
    principal = Principal("u-1", tenant="acme", roles=["approver", "approver"], attributes={"a": 1})
    assert principal.roles == frozenset({"approver"})
    assert principal.has_role("admin", "approver") and not principal.has_role("admin")
    assert principal.describe() == {"id": "u-1", "tenant": "acme", "roles": ["approver"]}
    with pytest.raises(TypeError):
        principal.attributes["b"] = 2  # type: ignore[index]
    with pytest.raises(ValueError, match="id"):
        Principal(" ")
    with pytest.raises(ValueError, match="tenant"):
        Principal("x", tenant="")
    with pytest.raises(TypeError, match="roles"):
        Principal("x", roles="approver")


# ---------------------------------------------------------------------- authenticators


async def test_api_key_authenticator_maps_keys_to_principals() -> None:
    authenticator = ApiKeyAuthenticator({"k1": ALICE, "k2": "svc"})
    assert isinstance(authenticator, Authenticator)
    assert await authenticator.authenticate(FakeRequest(Authorization="Bearer k1")) is ALICE
    service = await authenticator.authenticate(FakeRequest(Authorization="bearer  k2 "))
    assert service == Principal("svc")
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic k1"}):
        with pytest.raises(AuthenticationError):
            await authenticator.authenticate(FakeRequest(**headers))
    assert "k1" not in repr(authenticator)
    with pytest.raises(ValueError, match="at least one"):
        ApiKeyAuthenticator({})
    with pytest.raises(ValueError, match="non-empty"):
        ApiKeyAuthenticator({" ": ALICE})


async def test_api_key_authenticator_from_digests_and_custom_header() -> None:
    digest = hashlib.sha256(b"s3cret").hexdigest()
    authenticator = ApiKeyAuthenticator.from_digests({digest: BOB}, header="X-API-Key")
    assert await authenticator.authenticate(FakeRequest(**{"X-API-Key": "s3cret"})) is BOB
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate(FakeRequest(Authorization="Bearer s3cret"))
    with pytest.raises(ValueError, match="64 hex"):
        ApiKeyAuthenticator.from_digests({"abc": BOB})
    assert authenticator.principals == (BOB,)


async def test_bearer_token_authenticator_delegates_to_the_verifier(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def verify(token: str) -> Principal | None:
        if token == "expired":
            raise RuntimeError("signature expired for sk-secret")
        return Principal(token.removeprefix("jwt-"), tenant="acme") if token != "none" else None

    authenticator = BearerTokenAuthenticator(verify)
    principal = await authenticator.authenticate(FakeRequest(Authorization="Bearer jwt-ada"))
    assert principal == Principal("ada", tenant="acme")
    with caplog.at_level(logging.INFO, logger="awa.server"):
        for token in ("expired", "none"):
            with pytest.raises(AuthenticationError, match="Invalid bearer token"):
                await authenticator.authenticate(FakeRequest(Authorization=f"Bearer {token}"))
    assert "sk-secret" not in caplog.text
    with pytest.raises(AuthenticationError, match="Missing"):
        await authenticator.authenticate(FakeRequest())
    sync = BearerTokenAuthenticator(lambda token: Principal("sync"))
    assert (await sync.authenticate(FakeRequest(Authorization="Bearer t"))).id == "sync"
    with pytest.raises(TypeError):
        BearerTokenAuthenticator("not callable")  # type: ignore[arg-type]


async def test_authenticator_is_used_by_the_app_and_conflicts_with_api_key() -> None:
    app = create_app(echo_agent(), authenticator=ApiKeyAuthenticator(KEYS))
    async with client_for(app) as client:
        ok = await client.post("/v1/systems/echo/runs", json={"input": "x"}, headers=auth("k-bob"))
        denied = await client.get("/v1/systems", headers=auth("k-eve"))
        public = await client.get("/v1/ready")
    assert ok.status_code == 200 and denied.status_code == 401 and public.status_code == 200
    assert denied.headers["www-authenticate"] == "Bearer"
    with pytest.raises(ValueError, match="either api_key or authenticator"):
        create_app(echo_agent(), api_key="k", authenticator=ApiKeyAuthenticator(KEYS))
    with pytest.raises(TypeError, match="authenticate"):
        create_app(echo_agent(), authenticator=object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------- tenant isolation


async def test_tenants_cannot_see_list_or_resume_each_others_threads() -> None:
    app = create_app(refunds_agent(), authenticator=ApiKeyAuthenticator(KEYS))
    async with client_for(app) as client:
        paused = await client.post(
            "/v1/systems/refunds/runs", json={"input": "refund"}, headers=auth("k-alice")
        )
        thread = paused.json()["thread_id"]
        base = f"/v1/systems/refunds/threads/{thread}"
        foreign_read = await client.get(f"{base}/interrupts", headers=auth("k-mallory"))
        foreign_resume = await client.post(
            f"{base}/resume", json={"decision": {"action": "approve"}}, headers=auth("k-mallory")
        )
        foreign_continue = await client.post(
            "/v1/systems/refunds/runs",
            json={"input": "more", "thread_id": thread},
            headers=auth("k-mallory"),
        )
        foreign_list = await client.get("/v1/systems/refunds/threads", headers=auth("k-mallory"))
        colleague_read = await client.get(f"{base}/interrupts", headers=auth("k-bob"))
        own_list = await client.get("/v1/systems/refunds/threads", headers=auth("k-bob"))
        missing = await client.get(
            "/v1/systems/refunds/threads/unknown/interrupts", headers=auth("k-mallory")
        )

    assert paused.json()["status"] == "interrupted"
    for response in (foreign_read, foreign_resume, foreign_continue):
        assert response.status_code == 404
        assert response.json()["error"]["type"] == "ThreadNotFoundError"
    # A foreign thread is indistinguishable from a missing one.
    assert (
        foreign_read.json()["error"]["message"].replace(thread, "unknown")
        == (missing.json()["error"]["message"])
    )
    assert foreign_list.json() == {"threads": []}
    assert colleague_read.status_code == 200  # thread_scope="tenant": the tenant shares it
    [listed] = own_list.json()["threads"]
    assert listed["thread_id"] == thread and listed["principal"] == "alice"


async def test_principal_scope_limits_threads_to_their_creator_and_approvers() -> None:
    served = ServedSystem(refunds_agent(), approver_roles={"approver"})
    app = create_app(served, authenticator=ApiKeyAuthenticator(KEYS), thread_scope="principal")
    async with client_for(app) as client:
        paused = await client.post(
            "/v1/systems/refunds/runs", json={"input": "refund"}, headers=auth("k-alice")
        )
        thread = paused.json()["thread_id"]
        base = f"/v1/systems/refunds/threads/{thread}"
        colleague = await client.get(f"{base}/interrupts", headers=auth("k-bob"))
        colleague_list = await client.get("/v1/systems/refunds/threads", headers=auth("k-bob"))
        owner_resume = await client.post(
            f"{base}/resume", json={"decision": {"action": "approve"}}, headers=auth("k-alice")
        )
        reviewer_list = await client.get("/v1/systems/refunds/threads", headers=auth("k-rita"))
        reviewer = await client.post(
            f"{base}/resume", json={"decision": {"action": "approve"}}, headers=auth("k-rita")
        )
    assert colleague.status_code == 404 and colleague_list.json() == {"threads": []}
    # The owner can see the thread but lacks the approver role.
    assert owner_resume.status_code == 403
    assert owner_resume.json()["error"]["type"] == "PermissionDeniedError"
    assert [t["thread_id"] for t in reviewer_list.json()["threads"]] == [thread]
    assert reviewer.status_code == 200 and reviewer.json()["status"] == "completed"


async def test_shared_scope_and_threads_without_an_owner_record() -> None:
    saver = InMemorySaver()
    agent = refunds_agent().clone(checkpointer=saver)
    shared = create_app(agent, authenticator=ApiKeyAuthenticator(KEYS), thread_scope="shared")
    async with client_for(shared) as client:
        paused = await client.post(
            "/v1/systems/refunds/runs", json={"input": "refund"}, headers=auth("k-alice")
        )
        thread = paused.json()["thread_id"]
        other = await client.get(
            f"/v1/systems/refunds/threads/{thread}/interrupts", headers=auth("k-mallory")
        )
    assert other.status_code == 200

    # A new app (a restart) has no ownership records: such threads belong to
    # the default tenant only.
    single = create_app(agent)
    tenants = create_app(agent, authenticator=ApiKeyAuthenticator(KEYS))
    path = f"/v1/systems/refunds/threads/{thread}/interrupts"
    async with client_for(single) as client:
        assert (await client.get(path)).status_code == 200
    async with client_for(tenants) as client:
        assert (await client.get(path, headers=auth("k-alice"))).status_code == 404


async def test_client_chosen_thread_ids_are_claimed_by_their_first_user() -> None:
    index = InMemoryThreadIndex()
    app = create_app(refunds_agent(), authenticator=ApiKeyAuthenticator(KEYS), thread_index=index)
    body = {"input": "refund", "thread_id": "case-7"}
    async with client_for(app) as client:
        first = await client.post("/v1/systems/refunds/runs", json=body, headers=auth("k-alice"))
        hijack = await client.post("/v1/systems/refunds/runs", json=body, headers=auth("k-mallory"))
    assert first.status_code == 200 and hijack.status_code == 404
    owner = await index.owner("refunds", "case-7")
    assert owner is not None and (owner.tenant, owner.principal) == ("acme", "alice")


# ---------------------------------------------------------------------- reviewer identity


async def test_resume_records_the_reviewer_in_the_decision_and_audit_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: list[Any] = []

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "done"
        return tool_call("issue_refund", {"order_id": "A-1", "amount": 480})

    @tool
    def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order."""
        return "refunded"

    gated = create_tool_approval_wrapper(
        issue_refund, valid_actor=lambda decision: seen.append(dict(decision)) or True
    )
    agent = Agent(
        "refunds", model=FunctionChatModel(respond), tools=[gated], checkpointer=InMemorySaver()
    )
    app = create_app(
        ServedSystem(agent, approver_roles={"approver"}), authenticator=ApiKeyAuthenticator(KEYS)
    )
    async with client_for(app) as client:
        paused = await client.post(
            "/v1/systems/refunds/runs", json={"input": "x"}, headers=auth("k-alice")
        )
        thread = paused.json()["thread_id"]
        forged = {"action": "approve", "reviewer": {"id": "ceo"}}
        with caplog.at_level(logging.INFO, logger="awa.server.audit"):
            resumed = await client.post(
                f"/v1/systems/refunds/threads/{thread}/resume",
                json={"decision": forged},
                headers=auth("k-rita"),
            )
    assert resumed.status_code == 200
    # The server's identity replaces whatever the client claimed.
    assert seen == [
        {"action": "approve", "reviewer": {"id": "rita", "tenant": "acme", "roles": ["approver"]}}
    ]
    [record] = [r for r in caplog.records if getattr(r, "event", None) == "approval_submitted"]
    assert record.reviewer["id"] == "rita" and record.thread_id == thread
    assert record.decision == "approve"


# ---------------------------------------------------------------------- principal deps


async def test_principal_deps_scope_memory_per_caller_end_to_end() -> None:
    memory = AgentMemory(scope="user", require_user=True)

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "noted"
        return tool_call("remember", {"content": f"prefers {call.last_message.content}"})

    assistant = Agent("assistant", model=FunctionChatModel(respond), memory=memory)
    served = ServedSystem(
        assistant,
        allowed_deps={"locale", "user_id"},
        principal_deps=lambda caller: {"user_id": caller.id, "tenant": caller.tenant},
    )
    app = create_app(served, authenticator=ApiKeyAuthenticator(KEYS))
    async with client_for(app) as client:
        alice = await client.post(
            "/v1/systems/assistant/runs", json={"input": "EUR"}, headers=auth("k-alice")
        )
        bob = await client.post(
            "/v1/systems/assistant/runs", json={"input": "USD"}, headers=auth("k-bob")
        )
        spoofed = await client.post(
            "/v1/systems/assistant/runs",
            json={"input": "x", "deps": {"user_id": "alice"}},
            headers=auth("k-bob"),
        )
    assert alice.status_code == 200 and bob.status_code == 200
    alice_items = await memory.recall(
        "", owner=MemoryScope("assistant", "alice", tenant=ALICE.tenant)
    )
    bob_items = await memory.recall("", owner=MemoryScope("assistant", "bob", tenant=BOB.tenant))
    assert [item.content for item in alice_items] == ["prefers EUR"]
    assert [item.content for item in bob_items] == ["prefers USD"]
    # Identity-derived keys cannot be sent by clients, even if listed as allowed.
    assert spoofed.status_code == 400
    assert "set by the server" in spoofed.json()["error"]["message"]


async def test_principal_reaches_tools_and_async_principal_deps() -> None:
    seen: list[Any] = []

    @tool
    def whoami() -> str:
        """Return the caller."""
        ctx = current_run_context()
        caller = current_principal()
        seen.append((ctx.deps if ctx else None, caller.id if caller else None))
        return "ok"

    def respond(call: ModelCall) -> Any:
        return "done" if isinstance(call.last_message, ToolMessage) else tool_call("whoami")

    async def derive(caller: Principal) -> dict[str, Any]:
        return {"user_id": caller.id}

    agent = Agent("who", model=FunctionChatModel(respond), tools=[whoami])
    app = create_app(
        ServedSystem(agent, principal_deps=derive, deps_factory=lambda sent: ("deps", sent)),
        authenticator=ApiKeyAuthenticator(KEYS),
    )
    async with client_for(app) as client:
        response = await client.post(
            "/v1/systems/who/runs", json={"input": "x"}, headers=auth("k-bob")
        )
    assert response.status_code == 200
    assert seen == [(("deps", {"user_id": "bob"}), "bob")]
    assert current_principal() is None  # reset after the request


async def test_deps_factory_value_errors_are_request_errors() -> None:
    def build(sent: dict[str, Any]) -> Any:
        if not str(sent.get("order", "")).startswith("A-"):
            raise ValueError("order must start with A-")
        return sent

    app = create_app(ServedSystem(echo_agent(), allowed_deps={"order"}, deps_factory=build))
    async with client_for(app) as client:
        bad = await client.post(
            "/v1/systems/echo/runs", json={"input": "x", "deps": {"order": "B-1"}}
        )
    assert bad.status_code == 400
    assert bad.json()["error"] == {
        "type": "InvalidRequestError",
        "code": "awa.invalid_request",
        "message": "Invalid deps: order must start with A-",
        "retryable": False,
        "request_id": bad.headers["x-request-id"],
    }


async def test_anonymous_principal_without_authentication() -> None:
    index = InMemoryThreadIndex()
    app = create_app(refunds_agent(), thread_index=index)
    async with client_for(app) as client:
        paused = (await client.post("/v1/systems/refunds/runs", json={"input": "x"})).json()
    owner = await index.owner("refunds", paused["thread_id"])
    assert owner == ThreadOwner(tenant=None, principal=ANONYMOUS.id, created_at=owner.created_at)


async def test_a_resumed_run_continues_for_the_thread_owner() -> None:
    seen: list[Any] = []

    @tool
    def issue_refund(order_id: str) -> str:
        """Refund an order."""
        ctx = current_run_context()
        seen.append(ctx.deps if ctx else None)
        return "refunded"

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "done"
        return tool_call("issue_refund", {"order_id": "A-1"})

    agent = Agent(
        "refunds",
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(issue_refund)],
        checkpointer=InMemorySaver(),
    )
    served = ServedSystem(
        agent,
        principal_deps=lambda caller: {"customer_id": caller.id, "tenant": caller.tenant},
        approver_roles={"approver"},
    )
    app = create_app(served, authenticator=ApiKeyAuthenticator(KEYS))
    async with client_for(app) as client:
        paused = await client.post(
            "/v1/systems/refunds/runs", json={"input": "x"}, headers=auth("k-alice")
        )
        resumed = await client.post(
            f"/v1/systems/refunds/threads/{paused.json()['thread_id']}/resume",
            json={"decision": {"action": "approve"}},
            headers=auth("k-rita"),
        )
    assert resumed.status_code == 200
    assert seen == [{"customer_id": "alice", "tenant": "acme"}]


OWNER_KEYS = {"ann": Principal("ann", tenant="acme"), "nt1": Principal("nt1")}
APPROVE = {"decision": {"action": "approve"}}


def refunds(executed: list[float], checkpointer: Any) -> Agent:
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
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(issue_refund)],
        checkpointer=checkpointer,
    )


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_a_durable_checkpointer_with_the_in_memory_default_index_is_refused(tmp_path: Path) -> None:
    agent = refunds([], SqliteCheckpointer(tmp_path / "threads.db"))
    with pytest.raises(ConfigurationError, match="PostgresThreadIndex"):
        create_app({"refunds": agent}, authenticator=ApiKeyAuthenticator(OWNER_KEYS))
    with pytest.raises(ConfigurationError, match="PostgresThreadIndex"):
        create_router({"refunds": agent}, auth=lambda: OWNER_KEYS["ann"])
    with pytest.raises(ConfigurationError, match="PostgresThreadIndex"):
        create_mcp_server({"refunds": agent})
    # Chosen explicitly, or with one caller only, it is accepted.
    create_app(
        {"refunds": agent},
        authenticator=ApiKeyAuthenticator(OWNER_KEYS),
        thread_index=InMemoryThreadIndex(),
    )
    create_app({"refunds": agent}, api_key="k")
    create_app({"refunds": agent})
    create_mcp_server({"refunds": agent}, thread_index=InMemoryThreadIndex())


def test_after_a_restart_threads_without_a_record_are_refused_to_every_caller() -> None:
    executed: list[float] = []
    agent = refunds(executed, InMemorySaver())  # stands in for a checkpointer that survives
    auth = ApiKeyAuthenticator(OWNER_KEYS)
    with TestClient(create_app({"refunds": ServedSystem(agent)}, authenticator=auth)) as client:
        made = client.post(
            "/v1/systems/refunds/runs", json={"input": "refund"}, headers=bearer("ann")
        )
    thread = made.json()["thread_id"]
    restarted = create_app({"refunds": ServedSystem(agent)}, authenticator=auth)
    with TestClient(restarted) as client:
        read = client.get(f"/v1/systems/refunds/threads/{thread}/interrupts", headers=bearer("nt1"))
        resumed = client.post(
            f"/v1/systems/refunds/threads/{thread}/resume", json=APPROVE, headers=bearer("nt1")
        )
        continued = client.post(
            "/v1/systems/refunds/runs",
            json={"input": "go on", "thread_id": thread},
            headers=bearer("nt1"),
        )
    assert made.json()["status"] == "interrupted"
    assert (read.status_code, resumed.status_code, continued.status_code) == (404, 404, 404)
    assert executed == []


def test_a_single_caller_keeps_its_threads_after_a_restart() -> None:
    executed: list[float] = []
    agent = refunds(executed, InMemorySaver())
    with TestClient(create_app({"refunds": agent}, api_key="k")) as client:
        made = client.post(
            "/v1/systems/refunds/runs", json={"input": "refund"}, headers=bearer("k")
        )
    thread = made.json()["thread_id"]
    with TestClient(create_app({"refunds": agent}, api_key="k")) as client:
        resumed = client.post(
            f"/v1/systems/refunds/threads/{thread}/resume", json=APPROVE, headers=bearer("k")
        )
    assert resumed.status_code == 200, resumed.text
    assert executed == [480]


def test_shared_thread_scope_still_opens_threads_without_a_record() -> None:
    executed: list[float] = []
    agent = refunds(executed, InMemorySaver())
    auth = ApiKeyAuthenticator(OWNER_KEYS)
    options: dict[str, Any] = {"authenticator": auth, "thread_scope": "shared"}
    with TestClient(create_app({"refunds": agent}, **options)) as client:
        made = client.post(
            "/v1/systems/refunds/runs", json={"input": "refund"}, headers=bearer("ann")
        )
    with TestClient(create_app({"refunds": agent}, **options)) as client:
        resumed = client.post(
            f"/v1/systems/refunds/threads/{made.json()['thread_id']}/resume",
            json=APPROVE,
            headers=bearer("nt1"),
        )
    assert resumed.status_code == 200 and executed == [480]


def test_a_router_with_an_auth_dependency_refuses_threads_without_a_record() -> None:
    from fastapi import FastAPI

    executed: list[float] = []
    agent = refunds(executed, InMemorySaver())
    agent_thread = "made-elsewhere"
    with TestClient(create_app({"refunds": agent})) as client:  # e.g. a job, before a restart
        client.post("/v1/systems/refunds/runs", json={"input": "refund", "thread_id": agent_thread})
    host = FastAPI()
    host.include_router(
        create_router(
            {"refunds": agent}, auth=lambda: OWNER_KEYS["nt1"], thread_index=InMemoryThreadIndex()
        )
    )
    with TestClient(host) as client:
        read = client.get(f"/agents/systems/refunds/threads/{agent_thread}/interrupts")
        resumed = client.post(
            f"/agents/systems/refunds/threads/{agent_thread}/resume", json=APPROVE
        )
    assert (read.status_code, resumed.status_code) == (404, 404)
    assert executed == []
