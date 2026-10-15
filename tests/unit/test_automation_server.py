"""The automation router: signed webhooks, events, deliveries and process-run operations."""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from _helpers.automation import ScriptedSystem
from awa import Agent
from awa.automation import Automation, Webhook, wait_for_event
from awa.server import ApiKeyAuthenticator, Principal, create_app
from awa.server._automation import create_automation_router
from awa.testing import FakeChatModel, tool_call

KEYS = {
    "acme-key": Principal("acme-app", tenant="acme", roles=["operator"]),
    "beta-key": Principal("beta-app", tenant="beta"),
}


@tool
def wait_for_reply(invoice: str) -> str:
    """Wait for the supplier's reply."""

    reply = wait_for_event(f"reply:{invoice}")
    return f"reply {reply}"


def make_client(automation: Automation, **options: Any) -> TestClient:
    app = create_app(ScriptedSystem("desk"), authenticator=ApiKeyAuthenticator(KEYS))
    app.include_router(create_automation_router([automation], **options))
    return TestClient(app)


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_signed_webhooks_start_runs_once() -> None:
    hook = Webhook("invoices", secret="s3cret", dedup="{payload.no}")
    automation = Automation(
        ScriptedSystem(), name="ap", triggers=[hook], input="Invoice {payload.no}"
    )
    client = make_client(automation)
    body = json.dumps({"no": "RE-1"}).encode()
    headers = hook.sign(body, timestamp=int(time.time()))

    first = client.post("/v1/automations/ap/webhooks/invoices", content=body, headers=headers)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "queued" and first.json()["duplicate"] is False
    again = client.post("/v1/automations/ap/webhooks/invoices", content=body, headers=headers)
    assert again.status_code == 200 and again.json()["duplicate"] is True

    bad = client.post(
        "/v1/automations/ap/webhooks/invoices",
        content=body,
        headers={**headers, "X-Awa-Signature": "sha256=" + "0" * 64},
    )
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "awa.webhook_verification"
    missing = client.post("/v1/automations/ap/webhooks/nope", content=body, headers=headers)
    assert missing.status_code == 404
    assert client.post("/v1/automations/zz/webhooks/invoices", content=body).status_code == 404


def test_events_runs_and_tenancy() -> None:
    automation = Automation(ScriptedSystem(), name="ap", input="{payload.text}")
    client = make_client(automation)
    assert client.post("/v1/automations/ap/events", json={"payload": {}}).status_code == 401

    created = client.post(
        "/v1/automations/ap/events",
        json={"payload": {"text": "hello"}},
        headers={**auth("acme-key"), "Idempotency-Key": "k-1"},
    )
    assert created.status_code == 202
    run_id = created.json()["id"]
    duplicate = client.post(
        "/v1/automations/ap/events",
        json={"payload": {"text": "hello"}},
        headers={**auth("acme-key"), "Idempotency-Key": "k-1"},
    )
    assert duplicate.status_code == 200 and duplicate.json()["id"] == run_id

    listed = client.get("/v1/process-runs", headers=auth("acme-key")).json()["runs"]
    assert [r["id"] for r in listed] == [run_id]
    assert client.get("/v1/process-runs", headers=auth("beta-key")).json()["runs"] == []
    assert client.get(f"/v1/process-runs/{run_id}", headers=auth("beta-key")).status_code == 404
    shown = client.get(f"/v1/process-runs/{run_id}", headers=auth("acme-key")).json()
    assert shown["tenant"] == "acme" and shown["actor"] == "acme-app" and "payload" not in shown
    bad_filter = client.get("/v1/process-runs?status=weird", headers=auth("acme-key"))
    assert bad_filter.status_code == 400
    names = client.get("/v1/automations", headers=auth("acme-key")).json()
    assert names["automations"][0]["name"] == "ap"


def test_a_caller_without_tenant_sees_its_runs_behind_newer_tenant_runs() -> None:
    automation = Automation(ScriptedSystem(), name="ap", input="{payload.text}")
    keys = {**KEYS, "plain-key": Principal("plain-app")}
    app = create_app(ScriptedSystem("desk"), authenticator=ApiKeyAuthenticator(keys))
    app.include_router(create_automation_router([automation]))
    client = TestClient(app)
    own = client.post(
        "/v1/automations/ap/events", json={"payload": {"text": "a"}}, headers=auth("plain-key")
    ).json()["id"]
    for text in ("b", "c", "d"):
        client.post(
            "/v1/automations/ap/events", json={"payload": {"text": text}}, headers=auth("acme-key")
        )

    listed = client.get("/v1/process-runs?limit=1", headers=auth("plain-key")).json()["runs"]

    assert [run["id"] for run in listed] == [own]


async def test_operations_roles_and_conflicts() -> None:
    automation = Automation(ScriptedSystem(), name="ap")
    client = make_client(automation, operator_roles=["operator"])
    run_id = client.post(
        "/v1/automations/ap/events", json={"payload": {}}, headers=auth("acme-key")
    ).json()["id"]
    forbidden = client.post(f"/v1/process-runs/{run_id}/cancel", json={}, headers=auth("beta-key"))
    assert forbidden.status_code == 403
    cancelled = client.post(
        f"/v1/process-runs/{run_id}/cancel", json={"reason": "dup"}, headers=auth("acme-key")
    )
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    again = client.post(f"/v1/process-runs/{run_id}/cancel", json={}, headers=auth("acme-key"))
    assert again.status_code == 409
    retried = client.post(f"/v1/process-runs/{run_id}/retry", headers=auth("acme-key"))
    assert retried.status_code == 200 and retried.json()["status"] == "queued"
    no_approval = client.post(
        f"/v1/process-runs/{run_id}/decision",
        json={"decision": {"action": "approve"}},
        headers=auth("acme-key"),
    )
    assert no_approval.status_code == 409


async def test_deliveries_resume_waiting_runs() -> None:
    saver = InMemorySaver()
    agent = Agent(
        "clerk",
        model=FakeChatModel([tool_call("wait_for_reply", {"invoice": "I-1"}), "Thanks."]),
        tools=[wait_for_reply],
        checkpointer=saver,
    )
    automation = Automation(agent, name="ap")
    hook = Webhook("replies", secret="s", deliver_key="reply:{payload.invoice}")
    automation.triggers.append(hook)
    client = make_client(automation)
    client.post("/v1/automations/ap/events", json={"payload": {}}, headers=auth("acme-key"))
    [paused] = await automation.run_pending()
    assert paused.status == "waiting"

    missing = client.post(
        "/v1/automations/ap/deliveries",
        json={"key": "reply:other", "require_waiting": True},
        headers=auth("acme-key"),
    )
    assert missing.status_code == 404
    kept = client.post(
        "/v1/automations/ap/deliveries", json={"key": "reply:later"}, headers=auth("acme-key")
    )
    assert kept.status_code == 202 and kept.json()["kept"] is True

    body = json.dumps({"invoice": "I-1", "text": "ok"}).encode()
    delivered = client.post(
        "/v1/automations/ap/webhooks/replies",
        content=body,
        headers=hook.sign(body, timestamp=int(time.time())),
    )
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["delivered"] == [paused.id]
    [done] = await automation.run_pending()
    assert done.status == "succeeded"


async def test_a_tenant_cannot_deliver_to_another_tenant_s_waiting_run() -> None:
    agent = Agent(
        "clerk",
        model=FakeChatModel([tool_call("wait_for_reply", {"invoice": "I-1"})]),
        tools=[wait_for_reply],
        checkpointer=InMemorySaver(),
    )
    automation = Automation(agent, name="ap")
    client = make_client(automation)
    client.post("/v1/automations/ap/events", json={"payload": {}}, headers=auth("acme-key"))
    [paused] = await automation.run_pending()

    foreign = client.post(
        "/v1/automations/ap/deliveries", json={"key": "reply:I-1"}, headers=auth("beta-key")
    )

    assert foreign.status_code == 202 and foreign.json() == {
        "key": "reply:I-1",
        "delivered": [],
        "kept": True,
    }
    stored = await automation.store.get(paused.id)
    assert stored is not None and stored.status == "waiting"
    own = client.post(
        "/v1/automations/ap/deliveries", json={"key": "reply:I-1"}, headers=auth("acme-key")
    )
    assert own.json()["delivered"] == [paused.id]


def test_router_on_a_plain_app_is_anonymous_without_an_authenticator() -> None:
    automation = Automation(ScriptedSystem(), name="ap")
    app = FastAPI()
    app.include_router(create_automation_router({"jobs": automation}, prefix="/api"))
    client = TestClient(app)
    response = client.post("/api/automations/jobs/events", json={"payload": {"a": 1}})
    assert response.status_code == 202


@pytest.mark.parametrize("path", ["/v1/process-runs", "/v1/automations"])
def test_listing_needs_authentication(path: str) -> None:
    client = make_client(Automation(ScriptedSystem(), name="ap"))
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("how", ["dedup_key", "Idempotency-Key"])
def test_dedup_keys_belong_to_the_tenant_that_sent_them(how: str) -> None:
    automation = Automation(ScriptedSystem(), name="ap", input="{payload.text}")
    client = make_client(automation)

    def send(key: str, text: str) -> Any:
        body: dict[str, Any] = {"payload": {"text": text}}
        headers = auth(key)
        if how == "dedup_key":
            body["dedup_key"] = "inv-1"
        else:
            headers["Idempotency-Key"] = "inv-1"
        return client.post("/v1/automations/ap/events", json=body, headers=headers)

    alice, bob, again = send("acme-key", "a"), send("beta-key", "b"), send("acme-key", "a")

    assert alice.status_code == 202 and bob.status_code == 202, bob.text
    assert bob.json()["id"] != alice.json()["id"] and bob.json()["duplicate"] is False
    assert again.status_code == 200 and again.json()["id"] == alice.json()["id"]


def test_openapi_lists_every_answer_of_the_automation_routes() -> None:
    automation = Automation(
        ScriptedSystem(), name="ap", triggers=[Webhook("invoices", secret="s")], input="x"
    )
    paths = make_client(automation).app.openapi()["paths"]  # type: ignore[attr-defined]

    def codes(path: str, method: str = "post") -> set[str]:
        return set(paths[f"/v1{path}"][method]["responses"])

    assert {"200", "202", "401", "404", "413"} <= codes("/automations/{name}/webhooks/{trigger}")
    assert {"200", "202", "401", "404"} <= codes("/automations/{name}/events")
    assert {"200", "202", "401", "404"} <= codes("/automations/{name}/deliveries")
    assert {"401", "404"} <= codes("/process-runs/{run_id}", "get")
    for action in ("retry", "cancel", "decision"):
        assert {"401", "403", "404", "409"} <= codes(f"/process-runs/{{run_id}}/{action}")
