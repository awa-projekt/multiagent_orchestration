"""Tests for how served runs use the runtime: run ids, request metadata, timeouts,
cancellation through `awa.runtime.cancel`, the audit log and data-subject links."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from awa import Agent
from awa.observability import AuditLog, InMemoryAuditSink
from awa.persistence.erasure import erase_subject
from awa.persistence.retention import SubjectIndex
from awa.runtime import current_run_context
from awa.server import ApiKeyAuthenticator, InMemoryThreadIndex, Principal, ServedSystem, create_app
from awa.server.errors import describe_error, is_safe_error_type
from awa.testing import FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper

KEYS = {
    "k-alice": Principal("alice", tenant="acme"),
    "k-rita": Principal("rita", tenant="acme", roles={"approver"}),
}


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@asynccontextmanager
async def client_for(app: FastAPI, **headers: str) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", headers=headers
    ) as client:
        yield client


def frames(body: str) -> list[tuple[str, Any]]:
    parsed: list[tuple[str, Any]] = []
    for block in body.split("\n\n"):
        if block.strip() and not block.startswith(":"):
            fields = dict(line.split(": ", 1) for line in block.splitlines())
            parsed.append((fields["event"], json.loads(fields["data"])))
    return parsed


class Gate:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def agent(self, name: str = "slow") -> Agent:
        @tool
        async def hold() -> str:
            """Wait until released."""
            self.started.set()
            await self.release.wait()
            return "held"

        def respond(call: ModelCall) -> Any:
            return "finished" if isinstance(call.last_message, ToolMessage) else tool_call("hold")

        return Agent(name, model=FunctionChatModel(respond), tools=[hold])


def context_probe(seen: list[dict[str, Any]]) -> BaseTool:
    @tool
    def probe() -> str:
        """Record the run's context."""
        ctx = current_run_context()
        assert ctx is not None
        seen.append({"run_id": ctx.root_run_id, **ctx.metadata})
        return "ok"

    return probe


def probing_agent(seen: list[dict[str, Any]], *, gated: bool = False) -> Agent:
    probe = context_probe(seen)

    def respond(call: ModelCall) -> Any:
        return "done" if isinstance(call.last_message, ToolMessage) else tool_call("probe")

    return Agent(
        "probe",
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(probe) if gated else probe],
        checkpointer=InMemorySaver() if gated else None,
    )


# ---------------------------------------------------------------------- ids and metadata


async def test_runs_carry_the_request_identity_and_the_server_run_id() -> None:
    seen: list[dict[str, Any]] = []
    app = create_app(probing_agent(seen), authenticator=ApiKeyAuthenticator(KEYS))
    async with client_for(app, **auth("k-alice")) as client:
        sync = await client.post(
            "/v1/systems/probe/runs", json={"input": "x"}, headers={"X-Request-ID": "r-1"}
        )
        accepted = await client.post(
            "/v1/systems/probe/runs?mode=async",
            json={"input": "x"},
            headers={"X-Request-ID": "r-2"},
        )
        for _ in range(100):
            state = (await client.get(accepted.headers["location"])).json()
            if state["status"] != "running":
                break
            await asyncio.sleep(0.02)
        streamed = await client.post(
            "/v1/systems/probe/runs/stream", json={"input": "x"}, headers={"X-Request-ID": "r-3"}
        )
    assert [entry["awa.request_id"] for entry in seen] == ["r-1", "r-2", "r-3"]
    assert {(entry["awa.actor"], entry["awa.tenant"]) for entry in seen} == {("alice", "acme")}
    assert seen[0]["run_id"] == sync.json()["run_id"]
    # The 202 names the run's own id at once: the server assigned it.
    assert seen[1]["run_id"] == accepted.json()["run_id"] == state["result"]["run_id"]
    assert seen[2]["run_id"] == frames(streamed.text)[0][1]["run_id"]


async def test_resumed_runs_have_the_reviewer_as_actor() -> None:
    seen: list[dict[str, Any]] = []
    served = ServedSystem(probing_agent(seen, gated=True), approver_roles={"approver"})
    app = create_app(served, authenticator=ApiKeyAuthenticator(KEYS))
    async with client_for(app) as client:
        paused = await client.post(
            "/v1/systems/probe/runs", json={"input": "x"}, headers=auth("k-alice")
        )
        resumed = await client.post(
            f"/v1/systems/probe/threads/{paused.json()['thread_id']}/resume",
            json={"decision": {"action": "approve"}},
            headers={**auth("k-rita"), "X-Request-ID": "review-1"},
        )
    assert resumed.status_code == 200
    assert (seen[-1]["awa.actor"], seen[-1]["awa.request_id"]) == ("rita", "review-1")


# ---------------------------------------------------------------------- timeouts


async def test_run_timeout_stops_runs_with_504() -> None:
    gate = Gate()
    served = ServedSystem(gate.agent(), run_timeout=0.1)
    async with client_for(create_app(served)) as client:
        sync = await client.post("/v1/systems/slow/runs", json={"input": "x"})
        streamed = await client.post("/v1/systems/slow/runs/stream", json={"input": "x"})
        accepted = await client.post("/v1/systems/slow/runs?mode=async", json={"input": "x"})
        for _ in range(100):
            state = (await client.get(accepted.headers["location"])).json()
            if state["status"] != "running":
                break
            await asyncio.sleep(0.02)
    error = sync.json()["error"]
    assert sync.status_code == 504
    assert (error["type"], error["code"], error["retryable"]) == (
        "RunTimeoutError",
        "awa.run_timeout",
        True,
    )
    events = frames(streamed.text)
    assert "run_cancelled" in [name for name, _ in events]
    last = events[-1][1]
    assert (last["status"], last["error_type"], last["retryable"]) == (504, "RunTimeoutError", True)
    assert "timeout" in last["message"].lower()  # not redacted
    assert state["status"] == "failed" and state["error"]["type"] == "RunTimeoutError"


def test_cancel_and_timeout_errors_are_shown_in_events() -> None:
    assert is_safe_error_type("RunCancelledError") and is_safe_error_type("RunTimeoutError")
    from awa.errors import RunCancelledError, RunTimeoutError

    cancelled = describe_error(RunCancelledError("stopped", reason="x", cancelled_by="ops"))
    assert (cancelled.status, cancelled.retryable) == (409, False)
    timed_out = describe_error(RunTimeoutError("slow", run_id="r", elapsed=2, limit=1))
    assert (timed_out.status, timed_out.retryable) == (504, True)


# ---------------------------------------------------------------------- cancellation


async def test_cancel_stops_the_run_tree_and_names_who_cancelled() -> None:
    gates = [Gate(), Gate()]
    app = create_app(
        [gates[0].agent("sync"), gates[1].agent("streamy")], authenticator=ApiKeyAuthenticator(KEYS)
    )
    async with client_for(app, **auth("k-alice")) as client:
        sync = asyncio.create_task(client.post("/v1/systems/sync/runs", json={"input": "x"}))
        stream = asyncio.create_task(
            client.post("/v1/systems/streamy/runs/stream", json={"input": "x"})
        )
        for gate in gates:
            await asyncio.wait_for(gate.started.wait(), timeout=5)
        runs = {run["mode"]: run for run in (await client.get("/v1/runs")).json()["runs"]}
        for run in runs.values():
            assert (await client.delete(f"/v1/runs/{run['run_id']}")).status_code == 200
        sync_response, stream_response = await sync, await stream
    assert set(runs) == {"sync", "stream"}
    assert sync_response.status_code == 409
    assert sync_response.json()["error"]["type"] == "RunCancelledError"
    events = frames(stream_response.text)
    [cancelled] = [data for name, data in events if name == "run_cancelled"]
    assert cancelled["cancelled_by"] == "alice"
    assert events[-1][1]["status"] == 409 and "alice" in events[-1][1]["message"]


# ---------------------------------------------------------------------- audit and privacy


async def test_audit_log_records_served_runs_with_identity() -> None:
    sink = InMemoryAuditSink()
    seen: list[dict[str, Any]] = []
    served = ServedSystem(probing_agent(seen, gated=True), approver_roles={"approver"})
    app = create_app(served, authenticator=ApiKeyAuthenticator(KEYS), audit_log=AuditLog(sink))
    async with client_for(app) as client:
        paused = await client.post(
            "/v1/systems/probe/runs",
            json={"input": "x"},
            headers={**auth("k-alice"), "X-Request-ID": "r-9"},
        )
        await client.post(
            f"/v1/systems/probe/threads/{paused.json()['thread_id']}/resume",
            json={"decision": {"action": "approve"}},
            headers=auth("k-rita"),
        )
    records = sink.read()
    run_records = [r for r in records if r.request_id == "r-9"]
    assert run_records and {(r.actor, r.tenant) for r in run_records} == {("alice", "acme")}
    [submitted] = [r for r in records if r.event_type == "approval_submitted"]
    assert (submitted.actor, submitted.tenant, submitted.data["decision"]) == (
        "rita",
        "acme",
        "approve",
    )


async def test_threads_are_linked_to_their_subject_for_erasure() -> None:
    store = InMemoryStore()
    saver = InMemorySaver()
    seen: list[dict[str, Any]] = []
    agent = probing_agent(seen, gated=True).clone(checkpointer=saver)
    index = InMemoryThreadIndex()
    app = create_app(
        agent,
        authenticator=ApiKeyAuthenticator(KEYS),
        subject_index=SubjectIndex(store),
        thread_index=index,
    )
    async with client_for(app, **auth("k-alice")) as client:
        paused = await client.post("/v1/systems/probe/runs", json={"input": "x"})
        thread = paused.json()["thread_id"]
        assert (await SubjectIndex(store).links("alice")).threads == (thread,)
        report = await erase_subject("alice", store=store, checkpointer=saver)
        for erased in report.ids.get("threads", []):
            await index.forget("probe", erased)
        gone = await client.get(f"/v1/systems/probe/threads/{thread}/interrupts")
    assert report.ids["threads"] == [thread]
    assert gone.status_code == 404
