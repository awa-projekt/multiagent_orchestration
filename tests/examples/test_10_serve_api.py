"""Offline tests for `examples/10_serve_api.py`."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage

from _helpers.examples import load_example
from awa.testing import FunctionChatModel, ModelCall, tool_call

CUSTOMER = {"Authorization": "Bearer c7-change-me"}
OTHER_CUSTOMER = {"Authorization": "Bearer c9-change-me"}
REVIEWER = {"Authorization": "Bearer rev-change-me"}


def refund_desk_model() -> FunctionChatModel:
    """Looks the order up, refunds its total, then reports the tool result."""

    def respond(call: ModelCall) -> Any:
        last = call.last_message
        if not isinstance(last, ToolMessage):
            return tool_call("lookup_order", {"order_id": "A-1002"})
        if last.name == "lookup_order":
            if "No order" in str(last.content):
                return "I could not find that order."
            order = json.loads(str(last.content))
            return tool_call(
                "issue_refund",
                {"order_id": order["order_id"], "amount_eur": order["total"], "reason": "lost"},
            )
        return f"Done: {last.content}"

    return FunctionChatModel(respond)


def client(app: Any) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://example")


async def test_refund_pauses_until_a_reviewer_with_the_role_approves() -> None:
    example = load_example("10_serve_api")
    gateway = example.PaymentsGateway()
    agent = example.build_agent(gateway)
    app = example.build_app(agent)
    body = {"input": "Refund order A-1002."}

    with agent.override(model=refund_desk_model()):
        async with client(app) as http:
            paused = await http.post("/v1/systems/refunds/runs", json=body, headers=CUSTOMER)
            thread = paused.json()["thread_id"]
            pending = await http.get(
                f"/v1/systems/refunds/threads/{thread}/interrupts", headers=CUSTOMER
            )
            decision = {"decision": {"action": "approve"}}
            self_approval = await http.post(
                f"/v1/systems/refunds/threads/{thread}/resume", json=decision, headers=CUSTOMER
            )
            assert gateway.refunds == []
            done = await http.post(
                f"/v1/systems/refunds/threads/{thread}/resume", json=decision, headers=REVIEWER
            )

    assert paused.json()["status"] == "interrupted"
    [request] = pending.json()["interrupts"]
    assert request["value"]["tool"] == "issue_refund"
    assert request["value"]["args"]["amount_eur"] == 480.0
    assert self_approval.status_code == 403
    assert done.status_code == 200
    assert done.json()["output"] == "Done: Refunded EUR 480.00 for order A-1002."
    assert gateway.refunds == [("A-1002", 480.0)]


async def test_customers_only_see_their_own_orders_and_threads() -> None:
    example = load_example("10_serve_api")
    agent = example.build_agent()
    app = example.build_app(agent)

    with agent.override(model=refund_desk_model()):
        async with client(app) as http:
            mine = await http.post(
                "/v1/systems/refunds/runs", json={"input": "Refund A-1002."}, headers=CUSTOMER
            )
            other = await http.post(
                "/v1/systems/refunds/runs", json={"input": "Refund A-1002."}, headers=OTHER_CUSTOMER
            )
            smuggled = await http.post(
                "/v1/systems/refunds/runs",
                json={"input": "Refund A-1002.", "deps": {"customer_id": "c-7"}},
                headers=OTHER_CUSTOMER,
            )
            peek = await http.get(
                f"/v1/systems/refunds/threads/{mine.json()['thread_id']}/interrupts",
                headers=OTHER_CUSTOMER,
            )
            limits = await http.post(
                "/v1/systems/refunds/runs",
                json={"input": "Refund A-1002.", "usage_limits": {"max_requests": 1}},
                headers=CUSTOMER,
            )
            anonymous = await http.post("/v1/systems/refunds/runs", json={"input": "x"})

    assert other.json()["output"] == "I could not find that order."
    assert smuggled.status_code == 400
    assert peek.status_code == 404
    assert limits.status_code == 429
    assert anonymous.status_code == 401


async def test_streams_events_and_runs_async() -> None:
    example = load_example("10_serve_api")
    agent = example.build_agent()

    def small_refund(call: ModelCall) -> Any:
        last = call.last_message
        if not isinstance(last, ToolMessage):
            return tool_call(
                "issue_refund", {"order_id": "A-1001", "amount_eur": 25, "reason": "x"}
            )
        return "Refunded."

    body = {"input": "Refund order A-1001."}
    with agent.override(model=FunctionChatModel(small_refund)):
        async with client(example.build_app(agent)) as http:
            response = await http.post(
                "/v1/systems/refunds/runs?stream=true", json=body, headers=CUSTOMER
            )
            accepted = await http.post(
                "/v1/systems/refunds/runs?mode=async", json=body, headers=CUSTOMER
            )
            for _ in range(100):
                state = (await http.get(accepted.headers["location"], headers=CUSTOMER)).json()
                if state["status"] != "running":
                    break
                await asyncio.sleep(0.02)

    names = [
        line.removeprefix("event: ")
        for line in response.text.splitlines()
        if line.startswith("event: ")
    ]
    assert names[0] == "agent_started" and names[-1] == "run_completed"
    assert "tool_call" in names and "tool_result" in names
    assert accepted.status_code == 202
    assert state["status"] == "completed" and state["result"]["output"] == "Refunded."


async def test_inside_the_shop_app_the_shops_login_decides_who_may_do_what() -> None:
    example = load_example("10_serve_api")
    gateway = example.PaymentsGateway()
    agent = example.build_agent(gateway)
    alice, bob, reviewer = ({"X-Session": s} for s in ("s-c7", "s-c9", "s-rev"))
    desk = "/ai/agents/systems/refunds"
    body = {"input": "Refund order A-1002."}

    with agent.override(model=refund_desk_model()):
        async with client(example.build_shop_app(agent)) as http:
            own = await http.get("/orders", headers=alice)
            paused = await http.post(f"{desk}/runs", json=body, headers=alice)
            other = await http.post(f"{desk}/runs", json=body, headers=bob)
            anonymous = await http.post(f"{desk}/runs", json=body)
            decision = {"decision": {"action": "approve"}}
            resume = f"{desk}/threads/{paused.json()['thread_id']}/resume"
            self_approval = await http.post(resume, json=decision, headers=alice)
            done = await http.post(resume, json=decision, headers=reviewer)

    assert [order["order_id"] for order in own.json()["orders"]] == ["A-1001", "A-1002"]
    assert paused.json()["status"] == "interrupted"
    assert other.json()["output"] == "I could not find that order."
    assert anonymous.status_code == 401 and anonymous.json() == {"detail": "Please log in"}
    assert self_approval.status_code == 403
    assert done.status_code == 200 and gateway.refunds == [("A-1002", 480.0)]


def test_the_shops_lifespan_wraps_the_drain() -> None:
    example = load_example("10_serve_api")
    app = example.build_shop_app()
    with TestClient(app) as http:
        assert example.SHOP_DB.connected
        assert http.get("/ai/agents/ready").json()["status"] == "ready"
    assert not example.SHOP_DB.connected
    assert "/ai/agents/systems/{name}/runs" in app.openapi()["paths"]
