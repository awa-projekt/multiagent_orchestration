"""`awa.server.create_app`: endpoints, SSE, auth, errors, approvals in HTTP terms, concurrency, failure modes and global subscribers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, MutableMapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from awa import Agent, AgentNetwork, Sequential, Team, UsageLimits
from awa.contracts import RUN_EVENT_ADAPTER, AgentCard, ErrorEvent, RunResult, TextDeltaEvent
from awa.contracts.events import AgentStartedEvent, RunEvent
from awa.contracts.protocols import AgentInput
from awa.runtime import current_run_context, global_subscribers
from awa.server import ServedSystem, create_app, create_router, router_lifespan
from awa.server._auth import Principal
from awa.server._config import stamp_reviewer
from awa.server._serialize import http_interrupts
from awa.server._server import _decision_summary, _EventBuffer
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, structured_output, tool_call
from awa.tools import create_tool_approval_wrapper

BASE = "http://testserver"


@asynccontextmanager
async def client_for(app: FastAPI, **headers: str) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE, headers=headers) as client:
        yield client


def frames(body: str) -> list[tuple[str, Any]]:
    """Parse an SSE body into `(event, data)` pairs; comments become `(":", text)`."""

    parsed: list[tuple[str, Any]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        if block.startswith(":"):
            parsed.append((":", block[1:].strip()))
            continue
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        parsed.append((fields["event"], json.loads(fields["data"])))
    return parsed


def echo_agent(name: str = "echo", **kwargs: Any) -> Agent:
    return Agent(
        name,
        description="Echoes the request.",
        model=FunctionChatModel(lambda call: f"echo: {call.last_message.content}", **kwargs),
    )


class Refund(BaseModel):
    order_id: str
    amount: float


def refund_tool(executed: list[float]) -> BaseTool:
    @tool
    def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order."""
        executed.append(amount)
        return f"refunded {amount} for {order_id}"

    return create_tool_approval_wrapper(issue_refund)


def refunds_agent(executed: list[float], checkpointer: InMemorySaver | None = None) -> Agent:
    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return f"result: {call.last_message.content}"
        return tool_call("issue_refund", {"order_id": "A-1", "amount": 480})

    return Agent(
        "refunds",
        description="Issues refunds.",
        model=FunctionChatModel(respond),
        tools=[refund_tool(executed)],
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver(),
    )


def blocking_tool(started: asyncio.Event, release: asyncio.Event, log: list[str]) -> BaseTool:
    @tool
    async def hold() -> str:
        """Wait until released."""
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            log.append("cancelled")
            raise
        log.append("released")
        return "held"

    return hold


def holding_agent(name: str, tool_: BaseTool) -> Agent:
    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "finished"
        return tool_call("hold")

    return Agent(name, description="Holds.", model=FunctionChatModel(respond), tools=[tool_])


# ---------------------------------------------------------------------- discovery


async def test_health_is_public_and_systems_are_listed() -> None:
    lead = echo_agent("lead")
    team = Team("desk", orchestrator=lead, members=[echo_agent("helper")])
    app = create_app({"echo": echo_agent(), "desk": team}, api_key="secret")
    async with client_for(app) as client:
        health = await client.get("/v1/health")
        assert health.status_code == 200 and health.json() == {"status": "ok"}
        listing = await client.get("/v1/systems", headers={"Authorization": "Bearer secret"})
    assert listing.status_code == 200
    systems = listing.json()["systems"]
    assert [(s["name"], s["kind"]) for s in systems] == [("echo", "agent"), ("desk", "team")]
    assert AgentCard.model_validate(systems[0]["card"]).description == "Echoes the request."


async def test_describe_agent_team_network_and_workflow() -> None:
    typed = Agent("typed", model=FakeChatModel(["x"]), output_type=Refund)
    team = Team("desk", orchestrator=echo_agent("lead"), members=[echo_agent("helper")])
    net = AgentNetwork("net", lead=echo_agent("boss"), teammates=[echo_agent("mate")])
    flow = Sequential("flow", [echo_agent("one"), echo_agent("two")])
    app = create_app([typed, team, net, ServedSystem(flow, allowed_deps={"tenant"})])
    async with client_for(app) as client:
        agent = (await client.get("/v1/systems/typed")).json()
        desk = (await client.get("/v1/systems/desk")).json()
        network = (await client.get("/v1/systems/net")).json()
        workflow = (await client.get("/v1/systems/flow")).json()
        missing = await client.get("/v1/systems/ghost")

    assert agent["kind"] == "agent" and agent["topology"] is None and agent["mermaid"] is None
    assert agent["output_schema"]["properties"].keys() == {"order_id", "amount"}
    assert agent["streaming"] and agent["resume"] and not agent["threads"]
    assert desk["topology"]["delegates"] == {"lead": ["helper"]}
    assert desk["mermaid"].startswith("flowchart")
    assert network["kind"] == "network" and network["exclusive"] is True
    assert network["topology"]["teammates"] == ["mate"]
    assert workflow["topology"] == {"sequential": ["one", "two"], "name": "flow"}
    assert "one --> two;" in workflow["mermaid"]
    # Workflows pause on approvals and resume (with a checkpointer).
    assert workflow["accepts_deps"] == ["tenant"] and workflow["resume"]
    assert missing.status_code == 404
    error = missing.json()["error"]
    assert error["type"] == "SystemNotFoundError" and error["code"] == "awa.system_not_found"
    assert error["message"] == "No system named 'ghost' is served here"
    assert error["retryable"] is False
    assert error["request_id"] == missing.headers["x-request-id"]


async def test_unknown_routes_and_openapi_toggle() -> None:
    async with client_for(create_app(echo_agent())) as client:
        assert (await client.get("/openapi.json")).status_code == 404
        response = await client.get("/v2/nothing")
    assert response.status_code == 404 and response.json()["error"]["type"] == "NotFound"
    async with client_for(create_app(echo_agent(), docs=True)) as client:
        assert (await client.get("/openapi.json")).status_code == 200
        assert (await client.get("/docs")).status_code == 404  # no page loading a CDN script
    async with client_for(create_app(echo_agent(), docs=True, api_key="k")) as client:
        assert (await client.get("/openapi.json")).status_code == 401
        assert (await client.get("/openapi.json", headers={"Authorization": "Bearer k"})).is_success


async def test_allowed_hosts_refuse_other_host_headers() -> None:
    app = create_app(echo_agent(), allowed_hosts=["testserver", "[::1]"])
    async with client_for(app) as client:
        assert (await client.get("/v1/systems")).status_code == 200
        assert (await client.get("/v1/systems", headers={"Host": "[::1]:8000"})).status_code == 200
        refused = await client.get("/v1/systems", headers={"Host": "evil.example:8000"})
    assert refused.status_code == 421 and refused.json()["error"]["code"] == "awa.host_not_allowed"


async def test_an_ipv6_host_matches_with_or_without_brackets() -> None:
    app = create_app(echo_agent(), allowed_hosts=["testserver", "::1"])
    async with client_for(app) as client:
        allowed = await client.get("/v1/systems", headers={"Host": "[::1]:8000"})
    assert allowed.status_code == 200


def test_create_app_validates_its_arguments() -> None:
    with pytest.raises(ValueError, match="at least one system"):
        create_app({})
    with pytest.raises(ValueError, match="Invalid system name"):
        create_app({"bad name": echo_agent()})
    with pytest.raises(ValueError, match="served under the name 'echo'"):
        create_app([echo_agent(), echo_agent()])
    with pytest.raises(TypeError, match="AgentLike"):
        create_app({"x": object()})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="api_key"):
        create_app(echo_agent(), api_key=" ")


# ---------------------------------------------------------------------- runs


async def test_run_returns_the_result_as_json() -> None:
    app = create_app(echo_agent())
    async with client_for(app) as client:
        response = await client.post("/v1/systems/echo/runs", json={"input": "hello"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed" and body["output"] == "echo: hello"
    assert body["agent"] == "echo" and body["text"] == "echo: hello"
    assert body["usage"]["requests"] == 1 and body["usage"]["total_tokens"] > 0
    assert body["thread_id"] is None and body["interrupts"] == []
    assert body["run_id"].startswith("run_") and body["handoff_path"] == ["echo"]


async def test_run_accepts_messages_and_serialises_typed_output() -> None:
    def respond(call: ModelCall) -> Any:
        kinds = [type(m).__name__ for m in call.messages if not isinstance(m, SystemMessage)]
        assert kinds == ["HumanMessage", "AIMessage", "HumanMessage"]
        return structured_output(Refund, {"order_id": "A-7", "amount": 12.5})

    agent = Agent("typed", model=FunctionChatModel(respond), output_type=Refund)
    messages = [
        {"role": "user", "content": "Refund A-7"},
        {"role": "assistant", "content": "How much?"},
        {"role": "user", "content": "12.50"},
    ]
    async with client_for(create_app(agent)) as client:
        response = await client.post("/v1/systems/typed/runs", json={"input": messages})
    assert response.status_code == 200, response.text
    assert response.json()["output"] == {"order_id": "A-7", "amount": 12.5}


async def test_invalid_requests_are_rejected_with_422() -> None:
    async with client_for(create_app(echo_agent())) as client:
        empty = await client.post("/v1/systems/echo/runs", json={"input": ""})
        extra = await client.post("/v1/systems/echo/runs", json={"input": "x", "model": "gpt"})
        thread = await client.post(
            "/v1/systems/echo/runs", json={"input": "x", "thread_id": "../etc"}
        )
        messages = await client.post(
            "/v1/systems/echo/runs", json={"input": [{"role": "wizard", "content": "x"}]}
        )
    for response in (empty, extra, thread, messages):
        assert response.status_code == 422
        assert response.json()["error"]["type"] == "ValidationError"


async def test_deps_are_refused_unless_allowed() -> None:
    seen: list[Any] = []

    @tool
    def whoami() -> str:
        """Return the tenant."""
        ctx = current_run_context()
        assert ctx is not None
        seen.append(ctx.deps)
        return "ok"

    def respond(call: ModelCall) -> Any:
        return "done" if isinstance(call.last_message, ToolMessage) else tool_call("whoami")

    def build() -> Agent:
        return Agent("who", model=FunctionChatModel(respond), tools=[whoami])

    closed = create_app(build())
    factory = create_app(
        ServedSystem(
            build(), allowed_deps={"tenant"}, deps_factory=lambda sent: {"server": "db", **sent}
        )
    )
    plain = create_app(ServedSystem(build(), allowed_deps=["tenant"]))
    body = {"input": "who?", "deps": {"tenant": "acme"}}
    async with client_for(closed) as client:
        refused = await client.post("/v1/systems/who/runs", json=body)
    async with client_for(factory) as client:
        accepted = await client.post("/v1/systems/who/runs", json=body)
        without = await client.post("/v1/systems/who/runs", json={"input": "who?"})
        unknown = await client.post(
            "/v1/systems/who/runs", json={"input": "x", "deps": {"admin": True}}
        )
    async with client_for(plain) as client:
        passed = await client.post("/v1/systems/who/runs", json=body)

    assert refused.status_code == 400
    assert refused.json()["error"]["type"] == "InvalidRequestError"
    assert refused.json()["error"]["message"] == "System 'who' does not accept deps from requests"
    assert accepted.status_code == 200 and without.status_code == 200
    assert unknown.status_code == 400 and "admin" in unknown.json()["error"]["message"]
    assert passed.status_code == 200
    assert seen == [{"server": "db", "tenant": "acme"}, {"server": "db"}, {"tenant": "acme"}]


async def test_usage_limits_from_request_and_ceiling_map_to_429() -> None:
    def respond(call: ModelCall) -> Any:
        return "done" if isinstance(call.last_message, ToolMessage) else tool_call("whoami")

    @tool
    def whoami() -> str:
        """Return a name."""
        return "ada"

    agent = Agent("limited", model=FunctionChatModel(respond), tools=[whoami])
    capped = ServedSystem(
        Agent("capped", model=FunctionChatModel(respond), tools=[whoami]),
        usage_limits=UsageLimits(max_requests=1),
    )
    async with client_for(create_app([agent, capped])) as client:
        limited = await client.post(
            "/v1/systems/limited/runs", json={"input": "x", "usage_limits": {"max_requests": 1}}
        )
        fine = await client.post("/v1/systems/limited/runs", json={"input": "x"})
        loosened = await client.post(
            "/v1/systems/capped/runs", json={"input": "x", "usage_limits": {"max_requests": 10}}
        )
    assert limited.status_code == 429
    assert limited.json()["error"]["type"] == "UsageLimitExceededError"
    assert fine.status_code == 200
    assert loosened.status_code == 429


async def test_errors_are_mapped_and_internals_are_not_leaked() -> None:
    def explode(call: ModelCall) -> Any:
        raise RuntimeError("provider down, key sk-live-123")

    broken = Agent("broken", model=FunctionChatModel(explode))
    async with client_for(create_app([broken, echo_agent()])) as client:
        crashed = await client.post("/v1/systems/broken/runs", json={"input": "x"})
        no_threads = await client.post(
            "/v1/systems/echo/runs", json={"input": "x", "thread_id": "t-1"}
        )
    assert crashed.status_code == 500
    assert crashed.json()["error"] == {
        "type": "InternalError",
        "code": "awa.internal",
        "message": "The server failed to process the request.",
        "retryable": False,
        "request_id": crashed.headers["x-request-id"],
    }
    assert "sk-live" not in crashed.text
    assert no_threads.status_code == 400
    assert no_threads.json()["error"]["type"] == "InvalidRequestError"
    assert "keeps no threads" in no_threads.json()["error"]["message"]


# ---------------------------------------------------------------------- auth and CORS


async def test_bearer_api_key_is_required_when_configured() -> None:
    app = create_app(echo_agent(), api_key="s3cret")
    async with client_for(app) as client:
        missing = await client.get("/v1/systems")
        wrong = await client.post(
            "/v1/systems/echo/runs", json={"input": "x"}, headers={"Authorization": "Bearer nope"}
        )
        basic = await client.get("/v1/systems", headers={"Authorization": "Basic s3cret"})
        right = await client.post(
            "/v1/systems/echo/runs", json={"input": "x"}, headers={"Authorization": "Bearer s3cret"}
        )
    for response in (missing, wrong, basic):
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.json()["error"]["type"] == "AuthenticationError"
    assert right.status_code == 200


async def test_auth_is_off_without_a_key_and_cors_is_opt_in() -> None:
    preflight = {
        "Origin": "https://ops.example.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    async with client_for(create_app(echo_agent())) as client:
        assert (await client.get("/v1/systems")).status_code == 200
        closed = await client.options("/v1/systems/echo/runs", headers=preflight)
    assert "access-control-allow-origin" not in closed.headers

    app = create_app(echo_agent(), cors_origins=["https://ops.example.com"])
    async with client_for(app) as client:
        opened = await client.options("/v1/systems/echo/runs", headers=preflight)
    assert opened.status_code == 200
    assert opened.headers["access-control-allow-origin"] == "https://ops.example.com"


# ---------------------------------------------------------------------- streaming


async def test_stream_frames_every_event_and_ends_with_run_completed() -> None:
    agent = echo_agent(stream_chunk_size=3)
    async with client_for(create_app(agent)) as client:
        by_query = await client.post("/v1/systems/echo/runs?stream=true", json={"input": "hello"})
        by_path = await client.post("/v1/systems/echo/runs/stream", json={"input": "hello"})
    for response in (by_query, by_path):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = frames(response.text)
        names = [name for name, _ in events]
        assert names[0] == "agent_started" and names[-1] == "run_completed"
        assert "text_delta" in names and "agent_finished" in names
        for name, data in events:
            assert data["type"] == name
            if name != "run_completed":
                RUN_EVENT_ADAPTER.validate_python(data)
        deltas = "".join(data["delta"] for name, data in events if name == "text_delta")
        assert deltas == "echo: hello"
        result = events[-1][1]["result"]
        assert result["output"] == "echo: hello" and result["status"] == "completed"


async def test_stream_failure_ends_with_one_error_event() -> None:
    def explode(call: ModelCall) -> Any:
        raise RuntimeError("secret detail")

    app = create_app([Agent("broken", model=FunctionChatModel(explode)), echo_agent()])
    async with client_for(app) as client:
        crashed = await client.post("/v1/systems/broken/runs/stream", json={"input": "x"})
        rejected = await client.post(
            "/v1/systems/echo/runs/stream", json={"input": "x", "thread_id": "t-1"}
        )
    events = frames(crashed.text)
    assert crashed.status_code == 200 and "secret detail" not in crashed.text
    errors = [data for name, data in events if name == "error"]
    assert len(errors) == 1 and events[-1][0] == "error"
    assert errors[0]["status"] == 500 and errors[0]["error_type"] == "InternalError"
    assert errors[0]["run_id"] == events[0][1]["run_id"] and errors[0]["depth"] == 0
    assert RUN_EVENT_ADAPTER.validate_python(errors[0]).type == "error"

    # Requests the system cannot take are refused before the stream starts.
    assert rejected.status_code == 400
    assert rejected.json()["error"]["type"] == "InvalidRequestError"


async def test_stream_sends_keep_alive_pings_while_idle() -> None:
    started, release, log = asyncio.Event(), asyncio.Event(), list[str]()
    agent = holding_agent("slow", blocking_tool(started, release, log))

    async def release_later() -> None:
        await started.wait()
        await asyncio.sleep(0.2)
        release.set()

    app = create_app(agent, sse_ping_interval=0.05)
    async with client_for(app) as client:
        releaser = asyncio.create_task(release_later())
        response = await client.post("/v1/systems/slow/runs/stream", json={"input": "x"})
        await releaser
    events = frames(response.text)
    assert (":", "ping") in events and events[-1][0] == "run_completed"


class PlainSystem:
    """An `AgentLike` without `stream` or `resume`."""

    name = "plain"
    card = AgentCard(name="plain", description="Answers plainly.")

    async def run(
        self,
        input: AgentInput,
        *,
        deps: Any = None,
        usage_limits: UsageLimits | None = None,
        thread_id: str | None = None,
        history: Sequence[BaseMessage | dict[str, Any]] | None = None,
    ) -> RunResult[Any]:
        return RunResult(run_id="run_plain", agent="plain", output={"answer": 42})


class LimitsProbe(PlainSystem):
    """Records the limits a run is started with."""

    def __init__(self) -> None:
        self.seen: list[UsageLimits | None] = []

    async def run(
        self,
        input: AgentInput,
        *,
        deps: Any = None,
        usage_limits: UsageLimits | None = None,
        thread_id: str | None = None,
        history: Sequence[BaseMessage | dict[str, Any]] | None = None,
    ) -> RunResult[Any]:
        self.seen.append(usage_limits)
        return await super().run(input)


async def test_request_limits_keep_the_ceiling_depth_and_per_tool_limits() -> None:
    probe = LimitsProbe()
    ceiling = UsageLimits(max_depth=2, per_tool_limits={"send_email": 1, "search": 5})
    requested = {
        "max_requests": 5,
        "max_depth": 4,
        "per_tool_limits": {"search": 2, "send_email": 9},
    }
    async with client_for(create_app(ServedSystem(probe, usage_limits=ceiling))) as client:
        response = await client.post(
            "/v1/systems/plain/runs", json={"input": "x", "usage_limits": requested}
        )
    assert response.status_code == 200
    [limits] = probe.seen
    assert limits is not None
    assert limits.max_requests == 5
    assert limits.max_depth == 2
    assert limits.per_tool_limits == {"send_email": 1, "search": 2}


async def test_systems_without_stream_or_resume() -> None:
    async with client_for(create_app(PlainSystem())) as client:
        description = (await client.get("/v1/systems/plain")).json()
        streamed = await client.post("/v1/systems/plain/runs/stream", json={"input": "x"})
        resumed = await client.post(
            "/v1/systems/plain/threads/t-1/resume", json={"decision": {"action": "approve"}}
        )
        interrupts = await client.get("/v1/systems/plain/threads/t-1/interrupts")
    assert description["kind"] == "custom" and not description["streaming"]
    [(name, data)] = frames(streamed.text)
    assert name == "run_completed" and data["result"]["output"] == {"answer": 42}
    assert resumed.status_code == 400 and "does not support resuming" in resumed.text
    assert interrupts.status_code == 400 and "keeps no threads" in interrupts.text


# ---------------------------------------------------------------------- approvals


async def test_approval_flow_pause_list_and_resume() -> None:
    executed: list[float] = []
    app = create_app(refunds_agent(executed))
    async with client_for(app) as client:
        paused = (await client.post("/v1/systems/refunds/runs", json={"input": "refund"})).json()
        thread = paused["thread_id"]
        listed = await client.get(f"/v1/systems/refunds/threads/{thread}/interrupts")
        resumed = await client.post(
            f"/v1/systems/refunds/threads/{thread}/resume", json={"decision": {"action": "approve"}}
        )
        after = await client.get(f"/v1/systems/refunds/threads/{thread}/interrupts")
        again = await client.post(
            f"/v1/systems/refunds/threads/{thread}/resume", json={"decision": {"action": "approve"}}
        )
        unknown = await client.get("/v1/systems/refunds/threads/nope/interrupts")
        unknown_resume = await client.post(
            "/v1/systems/refunds/threads/nope/resume", json={"decision": "yes"}
        )
        no_decision = await client.post(f"/v1/systems/refunds/threads/{thread}/resume", json={})

    assert paused["status"] == "interrupted" and paused["output"] is None
    assert paused["interrupts"][0]["value"]["tool"] == "issue_refund"
    assert listed.status_code == 200
    assert listed.json() == {"thread_id": thread, "interrupts": paused["interrupts"]}
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"
    assert resumed.json()["output"] == "result: refunded 480.0 for A-1"
    assert executed == [480.0]
    assert after.json()["interrupts"] == []
    assert again.status_code == 409
    assert again.json()["error"]["type"] == "NoPendingInterruptError"
    assert unknown.status_code == 404 and unknown_resume.status_code == 404
    assert no_decision.status_code == 422


async def test_rejection_and_team_approvals_resume_through_the_team() -> None:
    executed: list[float] = []
    member = refunds_agent(executed)
    member = Agent(
        "payments",
        description="Issues refunds.",
        model=FakeChatModel(
            [tool_call("issue_refund", {"order_id": "A-1", "amount": 480}), "Refund handled."]
        ),
        tools=[refund_tool(executed)],
    )
    lead = Agent(
        "lead",
        model=FakeChatModel(
            [tool_call("delegate_task", {"agent": "payments", "task": "Refund A-1"}), "Done."]
        ),
    )
    team = Team("desk", orchestrator=lead, members=[member], checkpointer=InMemorySaver())
    async with client_for(create_app(team)) as client:
        paused = (await client.post("/v1/systems/desk/runs", json={"input": "refund"})).json()
        thread = paused["thread_id"]
        listed = (await client.get(f"/v1/systems/desk/threads/{thread}/interrupts")).json()
        resumed = await client.post(
            f"/v1/systems/desk/threads/{thread}/resume",
            json={"decision": {"action": "reject", "note": "too large"}},
        )
    assert paused["status"] == "interrupted"
    assert listed["interrupts"][0]["value"]["args"] == {"order_id": "A-1", "amount": 480.0}
    assert resumed.status_code == 200 and resumed.json()["output"] == "Done."
    assert executed == []


class Wrapped:
    """A custom system around an agent: it exposes no checkpointer of its own."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self.name = "wrapped"
        self.card = AgentCard(name="wrapped")

    async def run(self, input: AgentInput, **kwargs: Any) -> RunResult[Any]:
        return await self._agent.run(input, **kwargs)

    async def resume(self, thread_id: str, decision: Any, **kwargs: Any) -> RunResult[Any]:
        return await self._agent.resume(thread_id, decision, **kwargs)


async def test_server_checkpointer_is_read_for_systems_without_their_own() -> None:
    saver = InMemorySaver()
    executed: list[float] = []
    system = Wrapped(refunds_agent(executed, saver))
    async with client_for(create_app(system)) as client:
        description = (await client.get("/v1/systems/wrapped")).json()
    assert description["threads"] is False

    async with client_for(create_app(system, checkpointer=saver)) as client:
        paused = (await client.post("/v1/systems/wrapped/runs", json={"input": "x"})).json()
        listed = await client.get(f"/v1/systems/wrapped/threads/{paused['thread_id']}/interrupts")
        resumed = await client.post(
            f"/v1/systems/wrapped/threads/{paused['thread_id']}/resume",
            json={"decision": {"action": "approve"}},
        )
    assert len(listed.json()["interrupts"]) == 1
    assert resumed.json()["status"] == "completed" and executed == [480.0]


# ---------------------------------------------------------------------- concurrency


async def test_requests_run_concurrently() -> None:
    arrived: list[str] = []
    both = asyncio.Event()

    @tool
    async def rendezvous(who: str) -> str:
        """Wait until two runs are inside the tool at the same time."""
        arrived.append(who)
        if len(arrived) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=5)
        return f"met {who}"

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return str(call.last_message.content)
        return tool_call("rendezvous", {"who": str(call.last_message.content)})

    agent = Agent("meet", model=FunctionChatModel(respond), tools=[rendezvous])
    async with client_for(create_app(agent)) as client:
        first, second = await asyncio.gather(
            client.post("/v1/systems/meet/runs", json={"input": "a"}),
            client.post("/v1/systems/meet/runs", json={"input": "b"}),
        )
    assert {first.json()["output"], second.json()["output"]} == {"met a", "met b"}


async def test_network_is_exclusive_and_answers_409_while_busy() -> None:
    started, release, log = asyncio.Event(), asyncio.Event(), list[str]()
    lead = holding_agent("lead", blocking_tool(started, release, log))
    net = AgentNetwork("net", lead=lead, teammates=[echo_agent("mate")])
    async with client_for(create_app(net)) as client:
        first = asyncio.create_task(client.post("/v1/systems/net/runs", json={"input": "go"}))
        await asyncio.wait_for(started.wait(), timeout=5)
        busy = await client.post("/v1/systems/net/runs", json={"input": "again"})
        busy_stream = await client.post("/v1/systems/net/runs/stream", json={"input": "again"})
        release.set()
        done = await first
        started.clear()
        later = await client.post("/v1/systems/net/runs", json={"input": "later"})
    assert busy.status_code == 409 and busy_stream.status_code == 409
    assert busy.json()["error"]["type"] == "SystemBusyError"
    assert done.status_code == 200 and later.status_code == 200


async def test_second_run_on_a_running_thread_gets_409() -> None:
    started, release, log = asyncio.Event(), asyncio.Event(), list[str]()
    agent = holding_agent("worker", blocking_tool(started, release, log))
    agent = agent.clone(checkpointer=InMemorySaver())
    body = {"input": "x", "thread_id": "case-42"}
    async with client_for(create_app(agent)) as client:
        first = asyncio.create_task(client.post("/v1/systems/worker/runs", json=body))
        await asyncio.wait_for(started.wait(), timeout=5)
        clash = await client.post("/v1/systems/worker/runs", json=body)
        other = asyncio.create_task(
            client.post("/v1/systems/worker/runs", json={"input": "y", "thread_id": "case-43"})
        )
        release.set()
        assert (await first).status_code == 200
        assert (await other).status_code == 200
    assert clash.status_code == 409 and "case-42" in clash.json()["error"]["message"]


# ---------------------------------------------------------------------- disconnects


def _other_tasks() -> set[asyncio.Task[Any]]:
    return {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}


async def _stream_then_disconnect(
    app: FastAPI, path: str, *, spec_version: str, disconnect: asyncio.Event
) -> list[bytes]:
    """Drive the ASGI app directly and disconnect once `disconnect` is set.

    With ASGI spec 2.0 the server reports the disconnect through `receive`;
    with 2.4 the next `send` fails, as with a real closed socket.
    """

    body = json.dumps({"input": "x"}).encode()
    chunks: list[bytes] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            if disconnect.is_set() and spec_version == "2.4":
                raise ConnectionResetError("connection reset")
            chunks.append(message["body"])

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"host", b"testserver")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "state": {},
    }
    try:
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
    except ConnectionResetError:
        pass
    except Exception as exc:  # ClientDisconnect
        assert type(exc).__name__ == "ClientDisconnect"
    return chunks


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
async def test_client_disconnect_cancels_the_run_cleanly(spec_version: str) -> None:
    started, release, log = asyncio.Event(), asyncio.Event(), list[str]()
    agent = holding_agent("hold", blocking_tool(started, release, log))
    app = create_app(ServedSystem(agent, exclusive=True), sse_ping_interval=0.05)
    before = _other_tasks()

    chunks = await _stream_then_disconnect(
        app, "/v1/systems/hold/runs/stream", spec_version=spec_version, disconnect=started
    )

    await asyncio.sleep(0)
    events = [chunk for chunk in chunks if not chunk.startswith(b": ping")]  # pings may come first
    assert events and events[0].startswith(b"event: agent_started")
    assert log == ["cancelled"]
    assert _other_tasks() - before == set()
    # The reservation was released: the exclusive system accepts a new run.
    release.set()
    async with client_for(app) as client:
        again = await client.post("/v1/systems/hold/runs", json={"input": "x"})
    assert again.status_code == 200


def test_lifespan_runs_around_the_server() -> None:
    phases: list[str] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        phases.append("open")
        yield
        phases.append("close")

    with TestClient(create_app(echo_agent(), lifespan=lifespan)) as client:
        assert client.get("/v1/health").status_code == 200
        assert phases == ["open"]
    assert phases == ["open", "close"]


async def test_server_checkpointer_does_not_apply_to_agents_without_one() -> None:
    async with client_for(create_app(echo_agent(), checkpointer=InMemorySaver())) as client:
        description = (await client.get("/v1/systems/echo")).json()
        interrupts = await client.get("/v1/systems/echo/threads/t-1/interrupts")
    assert description["threads"] is False
    assert interrupts.status_code == 400


# ---------------------------------------------------------------------- audit regressions


class AuthenticationError(Exception):
    """Named like the authentication errors of provider SDKs (not an awa error)."""


async def test_stream_redacts_provider_errors_named_like_server_errors() -> None:
    def explode(call: ModelCall) -> Any:
        raise AuthenticationError("Incorrect API key provided: sk-proj-a4leak")

    flow = Sequential("flow", [echo_agent("one"), Agent("two", model=FunctionChatModel(explode))])
    async with client_for(create_app(flow)) as client:
        response = await client.post("/v1/systems/flow/runs/stream", json={"input": "x"})
    events = frames(response.text)
    assert "sk-proj-a4leak" not in response.text
    nested = [data for name, data in events if name == "error" and data["depth"] > 0]
    assert nested and nested[0]["error_type"] == "AuthenticationError"
    assert events[-1][0] == "error" and events[-1][1]["status"] == 500


async def test_concurrent_resumes_of_one_thread_resume_it_once() -> None:
    executed: list[float] = []
    waiting, go = asyncio.Event(), asyncio.Event()

    async def deps_factory(sent: dict[str, Any]) -> Any:
        if sent.get("who") == "slow":
            waiting.set()
            await go.wait()
        return sent

    served = ServedSystem(refunds_agent(executed), allowed_deps={"who"}, deps_factory=deps_factory)
    decision = {"action": "approve"}
    async with client_for(create_app(served)) as client:
        paused = (await client.post("/v1/systems/refunds/runs", json={"input": "refund"})).json()
        path = f"/v1/systems/refunds/threads/{paused['thread_id']}/resume"
        slow = asyncio.create_task(
            client.post(path, json={"decision": decision, "deps": {"who": "slow"}})
        )
        await asyncio.wait_for(waiting.wait(), timeout=5)
        fast = await client.post(path, json={"decision": decision, "deps": {"who": "fast"}})
        go.set()
        late = await slow
    assert executed == [480.0]
    assert sorted([fast.status_code, late.status_code]) == [200, 409]


async def test_request_bodies_over_the_limit_are_refused_before_auth() -> None:
    app = create_app(echo_agent(), api_key="k", max_body_bytes=1000)
    auth = {"Authorization": "Bearer k"}
    async with client_for(app) as client:
        small = await client.post("/v1/systems/echo/runs", json={"input": "hi"}, headers=auth)
        big = await client.post("/v1/systems/echo/runs", json={"input": "x" * 2000})

        async def chunked() -> AsyncIterator[bytes]:
            yield b'{"input": "'
            for _ in range(20):
                yield b"x" * 100
            yield b'"}'

        streamed = await client.post(
            "/v1/systems/echo/runs",
            content=chunked(),
            headers={**auth, "Content-Type": "application/json"},
        )
    assert small.status_code == 200
    for response in (big, streamed):
        assert response.status_code == 413
        assert response.json()["error"]["type"] == "PayloadTooLarge"
    with pytest.raises(ValueError, match="max_body_bytes"):
        create_app(echo_agent(), max_body_bytes=0)


async def test_stream_redacts_messages_of_tools_that_raised_unexpected_errors() -> None:
    @tool
    def query_db() -> str:
        """Query the database."""
        raise RuntimeError("connection to db failed for user svc password=hunter2")

    @tool
    def check(value: str) -> str:
        """Validate a value."""
        raise ValueError(f"{value!r} is not a valid order id")

    dba = Agent("dba", model=FakeChatModel([tool_call("query_db")]), tools=[query_db])
    clerk = Agent(
        "clerk", model=FakeChatModel([tool_call("check", {"value": "X-1"})]), tools=[check]
    )
    async with client_for(create_app([dba, clerk])) as client:
        leaked = await client.post("/v1/systems/dba/runs/stream", json={"input": "x"})
        kept = await client.post("/v1/systems/clerk/runs/stream", json={"input": "x"})
    assert "hunter2" not in leaked.text
    [redacted] = [data for name, data in frames(leaked.text) if name == "tool_result"]
    assert redacted["is_error"] and redacted["content"].startswith("RuntimeError: ")
    # Deliberate errors keep their message, as in error events.
    [shown] = [data for name, data in frames(kept.text) if name == "tool_result"]
    assert shown["content"] == "ValueError: 'X-1' is not a valid order id"


def delta(text: str) -> TextDeltaEvent:
    return TextDeltaEvent(run_id="r", root_run_id="r", agent="a", delta=text)


def failure() -> ErrorEvent:
    return ErrorEvent(run_id="r", root_run_id="r", agent="a", error_type="X", message="m")


def test_bad_server_options_are_refused() -> None:
    with pytest.raises(ValueError, match="sse_ping_interval must be positive"):
        create_app(echo_agent(), sse_ping_interval=0)
    with pytest.raises(ValueError, match="thread_scope"):
        create_app(echo_agent(), thread_scope="everyone")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sse_overflow"):
        create_app(echo_agent(), sse_overflow="block")  # type: ignore[arg-type]


async def test_a_full_buffer_drops_text_deltas_and_disconnects_when_nothing_can_be_shed() -> None:
    buffer = _EventBuffer(2, "drop")
    assert buffer.offer(delta("a")) and buffer.offer(delta("b"))
    assert buffer.offer(delta("c")) and buffer.dropped == 1  # the newest delta is shed

    other = _EventBuffer(1, "drop")
    assert other.offer(failure()) and other.offer(failure()) is False  # no delta to shed

    strict = _EventBuffer(1, "disconnect")
    first = delta("a")
    assert strict.offer(first) and strict.offer(delta("b")) is False
    assert await strict.get() == ("event", first)


async def test_an_audit_log_that_fails_does_not_block_the_approval() -> None:
    class BrokenAudit:
        def __call__(self, event: Any) -> None:
            pass

        def record(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("audit down")

    executed: list[float] = []
    app = create_app(refunds_agent(executed), audit_log=BrokenAudit())
    async with client_for(app) as client:
        paused = (await client.post("/v1/systems/refunds/runs", json={"input": "refund"})).json()
        resumed = await client.post(
            f"/v1/systems/refunds/threads/{paused['thread_id']}/resume",
            json={"decision": {"action": "approve"}},
        )

    assert resumed.status_code == 200 and executed == [480.0]


def test_reviewer_identity_is_added_to_one_or_several_decisions() -> None:
    reviewer = Principal(id="rita", tenant="acme")
    pending = ["i1", "i2"]

    assert stamp_reviewer({"action": "approve"}, None, pending, reviewer) == {"action": "approve"}
    assert stamp_reviewer("yes", "reviewer", pending, reviewer) == "yes"
    single = stamp_reviewer({"action": "approve"}, "reviewer", pending, reviewer)
    assert single["action"] == "approve" and single["reviewer"] == reviewer.describe()
    several = stamp_reviewer(
        {"i1": {"action": "approve"}, "i2": {"action": "reject"}}, "reviewer", pending, reviewer
    )
    assert set(several) == {"i1", "i2"} and several["i2"]["reviewer"] == reviewer.describe()


def test_decision_summaries_keep_only_the_actions() -> None:
    assert _decision_summary({"action": "approve", "note": "private"}) == "approve"
    assert _decision_summary({"i1": {"action": "reject"}, "i2": "yes"}) == {
        "i1": "reject",
        "i2": None,
    }
    assert _decision_summary("yes") is None


def _refunds() -> Agent:
    @tool
    def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order."""
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


def _no_python(body: Any) -> str:
    text = json.dumps(body)
    assert "resume(" not in text and "agent." not in text and "team." not in text, text
    return text


async def test_paused_run_and_interrupt_list_name_the_http_request() -> None:
    transport = httpx.ASGITransport(app=create_app(_refunds()))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        paused = (await client.post("/v1/systems/refunds/runs", json={"input": "refund"})).json()
        thread = paused["thread_id"]
        listed = (await client.get(f"/v1/systems/refunds/threads/{thread}/interrupts")).json()
        resumed = await client.post(
            f"/v1/systems/refunds/threads/{thread}/resume", json={"decision": {"action": "approve"}}
        )

    assert paused["status"] == "interrupted"
    for body in (paused["interrupts"], listed["interrupts"]):
        text = _no_python(body)
        assert f".../threads/{thread}/resume" in text
        assert '{"decision": {"action": "approve"}}' in body[0]["value"]["instructions"]
        assert body[0]["value"]["tool"] == "issue_refund"
    # the request the text names is the one that works
    assert resumed.status_code == 200 and resumed.json()["status"] == "completed"


def test_other_interrupt_values_stay_as_they_are() -> None:
    items = [{"id": "1", "value": "Approve the plan?"}, {"id": "2", "value": {"question": "x"}}]
    assert http_interrupts(items, "t-1") == items


def _agent(name: str = "echo") -> Agent[None, str]:
    return Agent(name, description="Echoes.", model=FakeChatModel(["echo"], repeat_last=True))


def _started(events: list[RunEvent]) -> list[str]:
    return [e.agent for e in events if isinstance(e, AgentStartedEvent)]


async def test_app_subscribers_see_every_run_while_the_app_runs() -> None:
    events: list[RunEvent] = []
    app = create_app(_agent(), subscribers=[events.append])
    outside = _agent("outside")

    assert events.append not in global_subscribers()
    with TestClient(app) as client:
        assert global_subscribers().count(events.append) == 1
        response = client.post("/v1/systems/echo/runs", json={"input": "hi"})
        assert response.status_code == 200
        await outside.run("not served, still seen")
    assert events.append not in global_subscribers()

    assert _started(events) == ["echo", "outside"]


def test_router_subscribers_are_registered_once_with_both_lifespans() -> None:
    events: list[RunEvent] = []
    router = create_router(_agent(), subscribers=[events.append])
    app = FastAPI(lifespan=router_lifespan(router))
    app.include_router(router, prefix="/ai")

    with TestClient(app) as client:
        assert global_subscribers().count(events.append) == 1
        client.post("/ai/agents/systems/echo/runs", json={"input": "hi"})
    assert events.append not in global_subscribers()

    assert _started(events) == ["echo"]


def test_subscribers_must_be_callable() -> None:
    with pytest.raises(TypeError, match="subscribers"):
        create_app(_agent(), subscribers=["audit"])  # type: ignore[list-item]
