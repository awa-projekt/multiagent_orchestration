"""Approvals over HTTP: one approval applied once, and workflows served on threads with pending approvals and partial resumes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from test_server_mcp_tenancy import APPROVE, KEYS, body, mcp_serving, session

from awa import Agent
from awa.server import ApiKeyAuthenticator, InMemoryThreadIndex, ServedSystem, create_app
from awa.server._serialize import pending_interrupts, pending_state
from awa.server.jobs import JobRegistry, JobSpec, execute
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper
from awa.workflows import Parallel, Sequential

SLOW = 0.3


def refunds(
    executed: list[float],
    *,
    stacked: bool = False,
    fail_first: bool = False,
    checkpointer: InMemorySaver | None = None,
) -> Agent:
    failures = [fail_first]

    @tool
    async def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order."""
        await asyncio.sleep(SLOW)
        if failures[0]:
            failures[0] = False
            raise RuntimeError("the payment service is down")
        executed.append(amount)
        return f"refunded {amount} for {order_id}"

    gated = create_tool_approval_wrapper(issue_refund)
    if stacked:
        gated = create_tool_approval_wrapper(gated)

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"result: {call.last_message.content}"
        return tool_call("issue_refund", {"order_id": "A-1", "amount": 480})

    return Agent(
        "refunds",
        model=FunctionChatModel(respond),
        tools=[gated],
        checkpointer=checkpointer or InMemorySaver(),
    )


async def test_two_concurrent_mcp_resumes_apply_the_approval_once() -> None:
    executed: list[float] = []
    async with mcp_serving(ServedSystem(refunds(executed))) as gate:
        async with session(gate, "ann") as owner:
            paused = body(await owner.call_tool("run_refunds", {"input": "refund"}))
        arguments = {"thread_id": paused["thread_id"], "decision": APPROVE}
        async with session(gate, "rev") as first, session(gate, "rev") as second:
            results = await asyncio.gather(
                first.call_tool("resume_refunds", arguments),
                second.call_tool("resume_refunds", arguments),
            )
    assert sorted(result.isError for result in results) == [False, True]
    refused = next(result for result in results if result.isError)
    assert "already being answered" in str(refused.content[0].text)  # type: ignore[union-attr]
    assert executed == [480]


async def test_two_concurrent_decision_jobs_apply_the_approval_once() -> None:
    executed: list[float] = []
    agent = refunds(executed)
    registry = JobRegistry({"refunds": agent})
    paused = await execute(JobSpec(system="refunds", input="refund", thread_id="t-1"), registry)
    decision = {"system": "refunds", "thread_id": "t-1", "decision": APPROVE}
    first, second = await asyncio.gather(
        execute(JobSpec(**decision), registry), execute(JobSpec(**decision), registry)
    )
    assert paused.status == "interrupted"
    assert sorted([first.status, second.status]) == ["completed", "failed"]
    refused = first if first.status == "failed" else second
    assert (refused.error or {}).get("type") == "SystemBusyError"
    assert executed == [480]


async def test_two_http_workers_sharing_an_index_apply_the_approval_once() -> None:
    executed: list[float] = []
    agent = refunds(executed)
    index = InMemoryThreadIndex()
    auth = ApiKeyAuthenticator(dict(KEYS))
    workers = [create_app({"refunds": agent}, authenticator=auth, thread_index=index) for _ in "ab"]
    clients = [
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
        for app in workers
    ]
    headers = {"Authorization": "Bearer rev"}
    async with clients[0], clients[1]:
        made = await clients[0].post(
            "/v1/systems/refunds/runs", json={"input": "refund"}, headers=headers
        )
        path = f"/v1/systems/refunds/threads/{made.json()['thread_id']}/resume"
        answers = await asyncio.gather(
            *(client.post(path, json={"decision": APPROVE}, headers=headers) for client in clients)
        )
    assert sorted(answer.status_code for answer in answers) == [200, 409]
    assert executed == [480]


async def test_stacked_approvals_of_one_step_are_each_answered_once() -> None:
    executed: list[float] = []
    agent = refunds(executed, stacked=True)
    registry = JobRegistry({"refunds": agent})
    await execute(JobSpec(system="refunds", input="refund", thread_id="t-2"), registry)
    decision = JobSpec(system="refunds", thread_id="t-2", decision=APPROVE)
    second_gate = await execute(decision, registry)
    done = await execute(decision.model_copy(update={"job_id": "job_again"}), registry)
    assert second_gate.status == "interrupted"
    assert done.status == "completed" and executed == [480]


async def test_a_failed_resume_can_be_retried() -> None:
    executed: list[float] = []
    agent = refunds(executed, fail_first=True)
    registry = JobRegistry({"refunds": agent})
    await execute(JobSpec(system="refunds", input="refund", thread_id="t-3"), registry)
    decision = JobSpec(system="refunds", thread_id="t-3", decision=APPROVE)
    failed = await execute(decision, registry)
    retried = await execute(decision.model_copy(update={"job_id": "job_retry"}), registry)
    assert failed.status == "failed" and retried.status == "completed"
    assert executed == [480]


async def test_the_version_of_a_paused_state_changes_once_it_is_answered() -> None:
    saver = InMemorySaver()
    agent = refunds([], stacked=True, checkpointer=saver)
    await agent.run("refund", thread_id="t-4")
    before = await pending_state(saver, "t-4")
    again = await pending_state(saver, "t-4")
    await agent.resume("t-4", APPROVE)
    after = await pending_state(saver, "t-4")
    assert before is not None and again is not None and after is not None
    assert before.version == again.version
    assert after.interrupts and after.version != before.version


async def test_a_resume_the_server_refuses_to_start_can_be_answered_again() -> None:
    executed: list[float] = []
    app = create_app({"refunds": refunds(executed)}, authenticator=ApiKeyAuthenticator(dict(KEYS)))
    server = app.state.awa_server
    headers = {"Authorization": "Bearer rev"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        made = await client.post(
            "/v1/systems/refunds/runs", json={"input": "refund"}, headers=headers
        )
        path = f"/v1/systems/refunds/threads/{made.json()['thread_id']}/resume"
        start = server.runs.start
        server.runs.start = _refuse  # the server starts draining after the claim was taken
        refused = await client.post(path, json={"decision": APPROVE}, headers=headers)
        server.runs.start = start
        answered = await client.post(path, json={"decision": APPROVE}, headers=headers)
    assert refused.status_code == 503
    assert answered.status_code == 200, answered.text
    assert executed == [480]


def _refuse(handle: object) -> None:
    from awa.server.errors import ServiceUnavailableError

    raise ServiceUnavailableError("The server is shutting down; retry on another instance")


@tool
def send(text: str) -> str:
    """Send a text."""

    return f"sent {text}"


def sender(name: str) -> Agent:
    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"{name} done"
        return tool_call("send", {"text": name})

    return Agent(name, model=FunctionChatModel(respond), tools=[create_tool_approval_wrapper(send)])


@asynccontextmanager
async def client_for(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def test_a_workflow_with_a_checkpointer_runs_on_a_thread_and_resumes() -> None:
    saver = InMemorySaver()
    pipeline = Sequential(
        "pipeline",
        [Agent("draft", model=FakeChatModel(["x"])), sender("mailer")],
        checkpointer=saver,
    )

    async with client_for(create_app(pipeline, checkpointer=saver)) as client:
        paused = await client.post(
            "/v1/systems/pipeline/runs", json={"input": "go", "thread_id": "wf-1"}
        )
        pending = await client.get("/v1/systems/pipeline/threads/wf-1/interrupts")
        done = await client.post(
            "/v1/systems/pipeline/threads/wf-1/resume", json={"decision": {"action": "approve"}}
        )

    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "interrupted" and paused.json()["thread_id"] == "wf-1"
    assert [i["value"]["tool"] for i in pending.json()["interrupts"]] == ["send"]
    assert done.json()["output"] == "mailer done"


async def test_a_workflow_without_a_checkpointer_keeps_no_threads() -> None:
    pipeline = Sequential("pipeline", [Agent("draft", model=FakeChatModel(["x"]))])

    async with client_for(create_app(pipeline, checkpointer=InMemorySaver())) as client:
        refused = await client.post(
            "/v1/systems/pipeline/runs", json={"input": "go", "thread_id": "wf-1"}
        )

    assert refused.status_code == 400 and "keeps no threads" in refused.text


async def test_answered_interrupts_of_a_partial_resume_are_not_pending_anymore() -> None:
    saver = InMemorySaver()
    fanout = Parallel("fanout", [sender("a"), sender("b")], checkpointer=saver)

    paused = await fanout.run("go", thread_id="par-1")
    first, second = (item["id"] for item in paused.interrupts)
    before = await pending_interrupts(saver, "par-1")
    again = await fanout.resume("par-1", {first: {"action": "approve"}})
    after = await pending_interrupts(saver, "par-1")

    assert before is not None and {item["id"] for item in before} == {first, second}
    assert [item["id"] for item in again.interrupts] == [second]
    assert after is not None and [item["id"] for item in after] == [second]


def test_threads_with_their_own_event_loops_claim_one_key_once() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from awa.server import InMemoryIdempotencyCache, ThreadOwner

    cache, index = InMemoryIdempotencyCache(), InMemoryThreadIndex()

    def claim(n: int) -> tuple[bool, str]:
        first = asyncio.run(cache.begin("scope", "key", "fingerprint", 60.0)) is None
        owner = asyncio.run(index.claim("system", "t-1", ThreadOwner("acme", f"user-{n}")))
        return first, owner.principal

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(32)))

    assert sum(first for first, _ in results) == 1
    assert len({principal for _, principal in results}) == 1
