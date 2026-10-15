"""Who decided, and for whom the resumed run works: the same on MCP, HTTP and automation routes.

Replays two reviewer attacks: an MCP resume derived `principal_deps` from
the reviewer instead of the thread's owner, and a client could name the recorded reviewer of a
decision itself (over MCP and on `POST /v1/process-runs/{id}/decision`).
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from test_server_mcp_tenancy import body, mcp_serving, session

from awa import Agent
from awa.automation import Automation
from awa.contracts.events import ApprovalDecidedEvent
from awa.runtime import current_run_context
from awa.runtime.ambient import subscribe_globally
from awa.server import ApiKeyAuthenticator, Principal, ServedSystem, create_app
from awa.server.jobs import JobRegistry, JobSpec, execute
from awa.testing import FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper


def refunds(seen: list[Any]) -> Agent:
    @tool
    def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order."""
        ctx = current_run_context()
        seen.append(getattr(ctx, "deps", None))
        return f"refunded {amount} for {order_id}"

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"result: {call.last_message.content}"
        return tool_call("issue_refund", {"order_id": "A-1", "amount": 480})

    return Agent(
        "refunds",
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(issue_refund)],
        checkpointer=InMemorySaver(),
    )


def decided(events: list[Any]) -> list[str | None]:
    return [event.reviewer for event in events if isinstance(event, ApprovalDecidedEvent)]


async def test_an_mcp_resume_works_for_the_thread_owner_and_records_the_real_reviewer() -> None:
    seen: list[Any] = []
    events: list[Any] = []
    served = ServedSystem(
        refunds(seen), approver_roles={"approver"}, principal_deps=lambda p: {"user_id": p.id}
    )
    unsubscribe = subscribe_globally(events.append)
    try:
        async with mcp_serving(served) as gate:
            async with session(gate, "ann") as owner:
                paused = body(await owner.call_tool("run_refunds", {"input": "refund"}))
            async with session(gate, "rev") as reviewer:
                done = body(
                    await reviewer.call_tool(
                        "resume_refunds",
                        {
                            "thread_id": paused["thread_id"],
                            "decision": {"action": "approve", "reviewer": "cfo-jane", "actor": "x"},
                        },
                    )
                )
    finally:
        unsubscribe()
    assert done["status"] == "completed"
    assert seen == [{"user_id": "ann"}]
    assert decided(events) == ["rev"]


async def test_a_decision_job_derives_deps_from_the_owner_it_names() -> None:
    seen: list[Any] = []
    served = ServedSystem(refunds(seen), principal_deps=lambda p: {"user_id": p.id})
    registry = JobRegistry({"refunds": served})
    who = {"tenant": "acme"}
    await execute(
        JobSpec(system="refunds", input="x", thread_id="t-9", actor="ann", **who), registry
    )
    done = await execute(
        JobSpec(
            system="refunds",
            thread_id="t-9",
            decision={"action": "approve"},
            actor="rev",
            owner="ann",
            **who,
        ),
        registry,
    )
    assert done.status == "completed" and seen == [{"user_id": "ann"}]


AUTOMATION_KEYS = {
    "acme": Principal("acme-app", tenant="acme"),
    "acme-rev": Principal("acme-rev", tenant="acme", roles=["approver"]),
    "ops": Principal("ops-bot", roles=["approver"]),  # no tenant
}


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def paused_automation_run() -> tuple[TestClient, Automation, str]:
    agent = refunds([])
    automation = Automation(agent, name="ap")
    app = create_app(
        {"desk": ServedSystem(agent, approver_roles={"approver"})},
        authenticator=ApiKeyAuthenticator(AUTOMATION_KEYS),
        automations=[automation],
    )
    client = TestClient(app)
    made = client.post("/v1/automations/ap/events", json={"payload": {}}, headers=bearer("acme"))
    await automation.run_pending()
    run_id = made.json()["id"]
    assert client.get(f"/v1/process-runs/{run_id}", headers=bearer("acme")).json()["status"] == (
        "waiting"
    )
    return client, automation, run_id


async def test_the_automation_decision_records_the_authenticated_caller() -> None:
    events: list[Any] = []
    client, automation, run_id = await paused_automation_run()
    unsubscribe = subscribe_globally(events.append)
    try:
        answer = client.post(
            f"/v1/process-runs/{run_id}/decision",
            json={"decision": {"action": "approve", "reviewer": "cfo-jane"}},
            headers=bearer("acme-rev"),
        )
        await automation.run_pending()
    finally:
        unsubscribe()
    assert answer.status_code == 200, answer.text
    assert decided(events) == ["acme-rev"]


async def test_a_caller_without_tenant_neither_sees_nor_decides_a_tenants_runs() -> None:
    client, automation, run_id = await paused_automation_run()
    listed = client.get("/v1/process-runs", headers=bearer("ops")).json()["runs"]
    read = client.get(f"/v1/process-runs/{run_id}", headers=bearer("ops"))
    answer = client.post(
        f"/v1/process-runs/{run_id}/decision",
        json={"decision": {"action": "approve"}},
        headers=bearer("ops"),
    )
    assert [run["id"] for run in listed] == []
    assert read.status_code == 404 and answer.status_code == 404
    run = await automation.store.get(run_id)
    assert run is not None and run.status == "waiting"
