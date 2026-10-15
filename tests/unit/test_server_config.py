"""The framework-independent parts of `awa.server` and its state backends."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ValidationError

from awa import (
    Agent,
    AgentRunError,
    Loop,
    Parallel,
    Sequential,
    UsageLimitExceededError,
    UsageLimits,
)
from awa.blueprints import BlueprintError
from awa.contracts import ErrorEvent, ToolCallEvent
from awa.contracts.usage import ModelPrice, PriceTable, merge_limits
from awa.errors import (
    AgentNotFoundError,
    ConfigurationError,
    MissingConfigurationError,
    ModelProviderError,
    ModelRateLimitError,
    TopologyError,
)
from awa.guardrails import GuardrailResult, GuardrailTripwireError
from awa.server import (
    AuthenticationError,
    BaseIdempotencyCache,
    BaseThreadIndex,
    IdempotencyConflictError,
    IdempotencyMismatchError,
    IdempotencyRecord,
    InMemoryIdempotencyCache,
    InMemoryThreadIndex,
    InvalidRequestError,
    NoPendingInterruptError,
    PermissionDeniedError,
    RateLimit,
    RateLimitExceededError,
    RunCancelledError,
    RunNotFoundError,
    ServedSystem,
    ServiceUnavailableError,
    SystemBusyError,
    SystemNotFoundError,
    ThreadNotFoundError,
    ThreadOwner,
    TokenBucketRateLimiter,
)
from awa.server._config import system_mermaid, system_topology
from awa.server._runs import RunHandle, RunRegistry
from awa.server._serialize import event_json, jsonable, pending_interrupts
from awa.server._stores import IdempotencyClaim, fingerprint, scope
from awa.server.errors import describe_error, is_safe_error_type
from awa.testing import FakeChatModel


def agent(name: str) -> Agent:
    return Agent(name, model=FakeChatModel(["ok"]))


def test_merge_limits_never_loosens_the_ceiling() -> None:
    prices = PriceTable(prices={"m": ModelPrice(input_per_mtok=1, output_per_mtok=2)})
    ceiling = UsageLimits(max_requests=10, max_total_tokens=1000, max_cost=1.0, prices=prices)
    requested = UsageLimits(max_requests=50, max_tool_calls=3, max_total_tokens=200)

    merged = merge_limits(requested, ceiling)

    assert merged is not None
    assert (merged.max_requests, merged.max_total_tokens, merged.max_tool_calls) == (10, 200, 3)
    assert (merged.max_cost, merged.prices) == (1.0, prices)
    assert merge_limits(None, ceiling) is ceiling
    assert merge_limits(requested, None) is requested
    own_cost = UsageLimits(max_cost=0.5, prices=prices)
    assert merge_limits(own_cost, UsageLimits(max_requests=2)) == UsageLimits(
        max_requests=2, max_cost=0.5, prices=prices
    )


def test_merge_limits_keeps_the_ceiling_depth_and_per_tool_caps() -> None:
    ceiling = UsageLimits(max_depth=2, per_tool_limits={"search": 3, "send": 1})
    requested = UsageLimits(max_depth=5, per_tool_limits={"search": 10, "read": 4})

    merged = merge_limits(requested, ceiling)

    assert merged is not None
    assert merged.max_depth == 2
    assert merged.per_tool_limits == {"search": 3, "send": 1, "read": 4}
    assert merge_limits(UsageLimits(max_requests=1), ceiling) == UsageLimits(
        max_requests=1, max_depth=2, per_tool_limits={"search": 3, "send": 1}
    )


def test_merge_limits_takes_the_shorter_duration_and_the_smaller_cost_of_one_price_table() -> None:
    prices = PriceTable(prices={"m": ModelPrice(input_per_mtok=1, output_per_mtok=2)})
    ceiling = UsageLimits(max_duration=300, max_cost=1.0, prices=prices)

    tighter = merge_limits(UsageLimits(max_duration=60, max_cost=0.2, prices=prices), ceiling)
    looser = merge_limits(UsageLimits(max_duration=900, max_cost=5.0, prices=prices), ceiling)
    other_prices = PriceTable(prices={"m": ModelPrice(input_per_mtok=9, output_per_mtok=9)})
    unlike = merge_limits(UsageLimits(max_cost=0.2, prices=other_prices), ceiling)

    assert tighter is not None and (tighter.max_duration, tighter.max_cost) == (60, 0.2)
    assert looser is not None and (looser.max_duration, looser.max_cost) == (300, 1.0)
    # Prices of different tables cannot be compared: the ceiling's cost limit holds.
    assert unlike is not None and (unlike.max_cost, unlike.prices) == (1.0, prices)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (AuthenticationError("no"), 401),
        (SystemNotFoundError("x"), 404),
        (ThreadNotFoundError("x"), 404),
        (RunNotFoundError("x"), 404),
        (PermissionDeniedError("x"), 403),
        (SystemBusyError("x"), 409),
        (NoPendingInterruptError("x"), 409),
        (RunCancelledError("x"), 409),
        (IdempotencyConflictError("x"), 409),
        (IdempotencyMismatchError("x"), 422),
        (UsageLimitExceededError("x"), 429),
        (RateLimitExceededError("x"), 429),
        (ServiceUnavailableError("x"), 503),
        (InvalidRequestError("x"), 400),
        # Library errors raised inside a run are not the client's fault: 422.
        (AgentNotFoundError("x"), 422),
        (TopologyError("x"), 422),
        (BlueprintError("x"), 422),
        (AgentRunError("x"), 422),
        (GuardrailTripwireError(GuardrailResult(guardrail_name="g", passed=False)), 422),
    ],
)
def test_library_errors_map_to_client_errors_with_their_message(
    error: Exception, status: int
) -> None:
    info = describe_error(error)
    assert (info.status, info.type, info.message, info.internal) == (
        status,
        type(error).__name__,
        str(error),
        False,
    )


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("key sk-1"),
        KeyError("x"),
        MissingConfigurationError("dsn=postgres://u:p@h"),
        ConfigurationError("store_backend=redis://u:p@h is unknown"),
        # Raised inside a run (model output, a tool's own validation): not a client error.
        ValueError("model returned 'sk-1'"),
        json.JSONDecodeError("Expecting value", "sk-1 {", 0),
    ],
)
def test_unexpected_errors_hide_their_details(error: Exception) -> None:
    info = describe_error(error)
    assert (info.status, info.type, info.internal) == (500, "InternalError", True)
    assert str(error) not in info.message


def test_pydantic_validation_errors_inside_runs_are_internal() -> None:
    class Output(BaseModel):
        amount: int

    try:
        Output.model_validate({"amount": "sk-live-not-a-number"})
    except ValidationError as exc:
        info = describe_error(exc)
    assert (info.status, info.type, info.internal) == (500, "InternalError", True)
    assert "sk-live" not in info.message


def test_long_messages_are_clipped() -> None:
    assert len(describe_error(InvalidRequestError("x" * 5000)).message) == 2000


def test_error_info_carries_code_retryable_and_retry_after() -> None:
    limited = describe_error(RateLimitExceededError("slow down", retry_after=1.2))
    assert (limited.status, limited.code, limited.retryable) == (
        429,
        "awa.rate_limit_exceeded",
        True,
    )
    assert limited.headers() == {"Retry-After": "2"}
    assert describe_error(AuthenticationError("no")).headers() == {"WWW-Authenticate": "Bearer"}
    body = describe_error(SystemBusyError("busy")).body("req_1")
    assert body == {
        "type": "SystemBusyError",
        "code": "awa.system_busy",
        "message": "busy",
        "retryable": True,
        "request_id": "req_1",
    }


def test_model_provider_errors_hide_the_provider_message() -> None:
    overloaded = describe_error(
        ModelRateLimitError("429 from provider: key sk-1", provider="openai", retry_after=7)
    )
    assert (overloaded.status, overloaded.type, overloaded.retryable) == (
        503,
        "ModelRateLimitError",
        False,
    )
    assert overloaded.headers() == {"Retry-After": "7"} and "sk-1" not in overloaded.message
    flaky = describe_error(
        ModelProviderError("502 upstream sk-2", kind="server_error", retryable=True)
    )
    assert (flaky.status, flaky.retryable, flaky.headers()) == (503, True, {})
    refused = describe_error(ModelProviderError("400 bad request sk-3", kind="invalid_request"))
    assert (refused.status, refused.code, refused.internal) == (502, "awa.model_provider", True)
    assert "sk-3" not in refused.message and refused.headers() == {}
    assert not is_safe_error_type("ModelProviderError")
    assert not is_safe_error_type("ModelRateLimitError")
    assert not is_safe_error_type("ConfigurationError")


def test_error_events_of_unexpected_errors_are_redacted() -> None:
    fields: dict[str, Any] = {"run_id": "r", "root_run_id": "r", "agent": "a", "depth": 1}
    leaked = event_json(ErrorEvent(error_type="RuntimeError", message="sk-secret", **fields))
    kept = event_json(ErrorEvent(error_type="HandoffError", message="too many", **fields))
    assert leaked["message"] == "The server failed to process the request."
    assert kept["message"] == "too many"
    assert is_safe_error_type("GuardrailTripwireError") and not is_safe_error_type("OSError")
    assert not is_safe_error_type("MissingConfigurationError")
    call = event_json(ToolCallEvent(tool_name="t", call_id="c", arguments={"x": 1}, **fields))
    assert call["type"] == "tool_call" and call["arguments"] == {"x": 1}


@dataclass
class Opaque:
    value: int


def test_jsonable_handles_models_dataclasses_and_unknown_objects() -> None:
    class Thing:
        def __str__(self) -> str:
            return "thing"

    assert jsonable({"a": Opaque(1), "b": Thing(), "c": UsageLimits(max_requests=1)}) == {
        "a": {"value": 1},
        "b": "thing",
        "c": UsageLimits(max_requests=1).model_dump(mode="json"),
    }


def test_workflow_topology_and_mermaid() -> None:
    flow = Sequential(
        "flow",
        [
            agent("a"),
            Parallel("fan", [agent("b"), agent("c")]),
            Loop("polish", agent("d"), until=lambda r: True, max_iterations=3),
        ],
    )
    assert system_topology(flow) == {
        "sequential": [
            "a",
            {"parallel": ["b", "c"], "name": "fan"},
            {"loop": "d", "max_iterations": 3, "name": "polish"},
        ],
        "name": "flow",
    }
    diagram = system_mermaid(ServedSystem(flow))
    assert diagram is not None
    assert "a --> fan;" in diagram and "fan --> polish;" in diagram
    assert system_mermaid(ServedSystem(agent("solo"), diagram="flowchart TD")) == "flowchart TD"
    assert system_topology(agent("solo")) is None


def test_served_system_checks_its_system_and_exclusivity() -> None:
    with pytest.raises(TypeError, match="AgentLike"):
        ServedSystem(object())  # type: ignore[arg-type]
    served = ServedSystem(agent("a"), allowed_deps=["x", "x"])
    assert served.allowed_deps == frozenset({"x"}) and not served.is_exclusive
    assert ServedSystem(agent("b"), exclusive=True).is_exclusive


async def test_async_deps_factory() -> None:
    async def build(sent: dict[str, Any]) -> tuple[str, ...]:
        await asyncio.sleep(0)
        return tuple(sorted(sent))

    served = ServedSystem(agent("a"), allowed_deps={"x", "y"}, deps_factory=build)
    assert await served.resolve_deps({"y": 1, "x": 2}) == ("x", "y")
    assert await ServedSystem(agent("b")).resolve_deps(None) is None


class SyncOnlySaver(InMemorySaver):
    async def aget_tuple(self, config: Any) -> Any:
        raise NotImplementedError


async def test_pending_interrupts_falls_back_to_synchronous_checkpointers() -> None:
    assert await pending_interrupts(SyncOnlySaver(), "missing") is None


POSTGRES_DSN_VARIABLE = "AWA_TEST_POSTGRES_DSN"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------- backends under test


# `backend` (from conftest) runs each test once per backend, on an event loop
# the PostgreSQL driver supports.


@pytest.fixture
async def thread_index(backend: str) -> AsyncIterator[BaseThreadIndex]:
    if backend == "memory":
        yield InMemoryThreadIndex()
        return
    from awa.server import PostgresThreadIndex

    index = PostgresThreadIndex(os.environ[POSTGRES_DSN_VARIABLE], schema="awa_test")
    try:
        yield index
    finally:
        await index.close()


@pytest.fixture
async def idempotency(backend: str) -> AsyncIterator[BaseIdempotencyCache]:
    if backend == "memory":
        yield InMemoryIdempotencyCache()
        return
    from awa.server import PostgresIdempotencyCache

    cache = PostgresIdempotencyCache(os.environ[POSTGRES_DSN_VARIABLE], schema="awa_test")
    try:
        yield cache
    finally:
        await cache.close()


# ---------------------------------------------------------------------- thread index


async def test_thread_index_claims_once_and_lists_by_tenant(thread_index: BaseThreadIndex) -> None:
    system = f"sys-{uuid4().hex[:8]}"
    alice = ThreadOwner("acme", "alice")
    first, second = await asyncio.gather(
        thread_index.claim(system, "t-1", alice),
        thread_index.claim(system, "t-1", ThreadOwner("globex", "mallory")),
    )
    assert first.principal == second.principal  # exactly one claim won
    winner = await thread_index.owner(system, "t-1")
    assert winner is not None and (winner.tenant, winner.principal) == (
        first.tenant,
        first.principal,
    )
    await thread_index.claim(system, "t-2", ThreadOwner("acme", "bob"))
    await thread_index.claim(system, "t-3", ThreadOwner(None, "anonymous"))
    acme = await thread_index.threads(system, tenant="acme")
    assert {record.thread_id for record in acme} <= {"t-1", "t-2"}
    bobs = await thread_index.threads(system, tenant="acme", principal="bob")
    assert [record.thread_id for record in bobs] == ["t-2"]
    untenanted = await thread_index.threads(system, tenant=None)
    assert [record.thread_id for record in untenanted] == ["t-3"]
    assert await thread_index.owner(system, "missing") is None
    await thread_index.forget(system, "t-2")
    assert await thread_index.owner(system, "t-2") is None
    await thread_index.ping()


async def test_thread_index_concurrent_claims_agree_on_one_owner(
    thread_index: BaseThreadIndex,
) -> None:
    # Many truly concurrent claims of one thread: every caller sees the same winner,
    # also the ones whose statement started before the winner committed.
    for _ in range(20):
        system = f"sys-{uuid4().hex[:8]}"
        owners = await asyncio.gather(
            *(
                thread_index.claim(system, "t", ThreadOwner("acme", f"p{number}"))
                for number in range(4)
            )
        )
        assert len({owner.principal for owner in owners}) == 1


async def test_in_memory_thread_index_is_bounded() -> None:
    index = InMemoryThreadIndex(max_threads=2)
    for number in range(3):
        await index.claim("s", f"t-{number}", ThreadOwner(None, "p"))
    assert await index.owner("s", "t-0") is None
    assert [r.thread_id for r in await index.threads("s", tenant=None)] == ["t-2", "t-1"]
    with pytest.raises(ValueError):
        InMemoryThreadIndex(max_threads=0)


# ---------------------------------------------------------------------- idempotency cache


async def test_idempotency_cache_claims_completes_and_releases(
    idempotency: BaseIdempotencyCache,
) -> None:
    scope, key = f"acme/{uuid4().hex[:8]}", "key-1"
    assert await idempotency.begin(scope, key, "fp", ttl=60) is None
    pending = await idempotency.begin(scope, key, "fp", ttl=60)
    assert pending is not None and not pending.completed and pending.fingerprint == "fp"
    record = IdempotencyRecord("fp", 202, {"run_id": "run_1"}, {"Location": "/v1/runs/run_1"})
    await idempotency.complete(scope, key, record)
    stored = await idempotency.begin(scope, key, "other", ttl=60)
    assert stored is not None and stored.completed
    assert (stored.status, stored.body, dict(stored.headers)) == (
        202,
        {"run_id": "run_1"},
        {"Location": "/v1/runs/run_1"},
    )
    # A completed response is never released; a pending claim is.
    await idempotency.release(scope, key)
    assert (await idempotency.begin(scope, key, "fp", ttl=60)) is not None
    assert await idempotency.begin(scope, "key-2", "fp", ttl=60) is None
    await idempotency.release(scope, "key-2")
    assert await idempotency.begin(scope, "key-2", "fp", ttl=60) is None
    await idempotency.ping()


async def test_in_memory_idempotency_records_expire() -> None:
    clock = Clock()
    cache = InMemoryIdempotencyCache(clock=clock, max_entries=2)
    assert await cache.begin("s", "k", "fp", ttl=10) is None
    clock.now += 11
    assert await cache.begin("s", "k", "fp2", ttl=10) is None  # expired: claimed again
    await cache.begin("s", "k2", "fp", ttl=10)
    await cache.begin("s", "k3", "fp", ttl=10)
    assert await cache.begin("s", "k", "fp", ttl=10) is None  # evicted beyond max_entries


async def test_idempotency_claim_replays_refuses_and_releases_retryable_failures() -> None:
    cache = InMemoryIdempotencyCache()
    asked = fingerprint({"system": "echo", "body": {"input": "x"}})
    assert asked == fingerprint({"body": {"input": "x"}, "system": "echo"})  # key order ignored
    assert scope("awa.jobs", None) == "awa.jobs\x1f"
    claim = IdempotencyClaim(cache, scope("acme", "app"), "k-1", asked, "a test request")
    messages = {"mismatch": "other request", "conflict": "still running"}
    assert await claim.begin(60, **messages) is None
    with pytest.raises(IdempotencyConflictError, match="still running"):
        await claim.begin(60, **messages)
    other = IdempotencyClaim(cache, claim.scope, "k-1", fingerprint("other"), "another request")
    with pytest.raises(IdempotencyMismatchError, match="other request"):
        await other.begin(60, **messages)
    # A failure a retry may overcome releases the key: the retry claims it again.
    await claim.finish(503, {"error": "busy"}, retryable=True)
    assert await claim.begin(60, **messages) is None
    await claim.finish(202, {"run_id": "run_1"}, {"Location": "/v1/runs/run_1"})
    stored = await claim.begin(60, **messages)
    assert stored is not None and (stored.status, stored.body) == (202, {"run_id": "run_1"})
    assert dict(stored.headers) == {"Location": "/v1/runs/run_1"}


async def test_idempotency_claim_never_raises_when_the_cache_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Broken(InMemoryIdempotencyCache):
        async def complete(self, scope: str, key: str, record: IdempotencyRecord) -> None:
            raise ConnectionError("down")

        async def release(self, scope: str, key: str) -> None:
            raise ConnectionError("down")

    claim = IdempotencyClaim(Broken(), "s", "k", "fp", "job job_1")
    await claim.finish(200, {})
    await claim.release()
    assert "Could not store the response of job job_1" in caplog.text
    assert "Could not release the idempotency key of job job_1" in caplog.text


# ---------------------------------------------------------------------- rate limiter


async def test_token_bucket_allows_bursts_and_refills() -> None:
    clock = Clock()
    limiter = TokenBucketRateLimiter(RateLimit(2, per=10, burst=3), clock=clock, max_keys=2)
    assert [await limiter.acquire("a") for _ in range(3)] == [None, None, None]
    wait = await limiter.acquire("a")
    assert wait == pytest.approx(5.0)
    clock.now += 5
    assert await limiter.acquire("a") is None
    assert await limiter.acquire("b") is None
    await limiter.acquire("c")  # evicts the least recently used bucket ("a")
    assert await limiter.acquire("a") is None
    assert limiter.limit.capacity == 3 and RateLimit(5).capacity == 5
    for bad in ({"requests": 0}, {"requests": 1, "per": 0}, {"requests": 1, "burst": 0}):
        with pytest.raises(ValueError):
            RateLimit(**bad)


# ---------------------------------------------------------------------- run registry


async def test_run_registry_retains_finished_async_runs_for_a_while() -> None:
    clock = Clock()
    registry = RunRegistry(retention=60, max_retained=1, clock=clock)
    first = registry.start(RunHandle("run_a", "s", "async", "acme", "alice"))
    registry.add_id(first, "run_a2")
    registry.finish(first, "completed", result={"output": 1})
    assert registry.get("run_a") is first and registry.get("run_a2") is first
    sync = registry.start(RunHandle("run_s", "s", "sync", None, "p"), addressable=False)
    assert registry.get("run_s") is None and sync in registry.active
    registry.finish(sync, "completed")
    second = registry.start(RunHandle("run_b", "s", "async", None, "p"))
    registry.finish(second, "failed", error={"type": "InternalError"})
    assert registry.get("run_a") is None  # beyond max_retained
    clock.now += 61
    assert registry.get("run_b") is None
    assert first.status_json()["result"] == {"output": 1}


async def test_run_registry_drain_waits_then_cancels() -> None:
    registry = RunRegistry()
    release = asyncio.Event()

    async def work() -> Any:
        await release.wait()

    quick = registry.start(RunHandle("run_q", "s", "async", None, "p"))
    quick.task = asyncio.create_task(asyncio.sleep(0))
    stuck = registry.start(RunHandle("run_x", "s", "async", None, "p"))
    stuck.task = asyncio.create_task(work())
    assert await registry.drain(0.05) == 1
    assert quick.task.done() and stuck.task.cancelled()
    assert stuck.cancel_error is not None and registry.draining
    with pytest.raises(Exception, match="shutting down"):
        registry.start(RunHandle("run_n", "s", "async", None, "p"))


@pytest.mark.postgres
async def test_postgres_ownership_is_shared_by_workers(postgres_dsn: str) -> None:
    import httpx
    from langchain_core.messages import ToolMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver

    from awa import Agent
    from awa.server import ApiKeyAuthenticator, PostgresThreadIndex, Principal, create_app
    from awa.testing import FunctionChatModel, ModelCall, tool_call
    from awa.tools import create_tool_approval_wrapper

    @tool
    def pay(order_id: str) -> str:
        """Pay an order."""
        return "paid"

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "done"
        return tool_call("pay", {"order_id": "1"})

    # Both workers share the checkpointer (as they would share Postgres).
    agent = Agent(
        "payments",
        model=FunctionChatModel(respond),
        tools=[create_tool_approval_wrapper(pay)],
        checkpointer=InMemorySaver(),
    )
    keys = ApiKeyAuthenticator(
        {"a": Principal("alice", tenant="acme"), "m": Principal("mallory", tenant="globex")}
    )
    indexes = [PostgresThreadIndex(postgres_dsn, schema="awa_test") for _ in range(2)]
    workers = [create_app(agent, authenticator=keys, thread_index=index) for index in indexes]
    try:
        clients = [
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://w")
            for app in workers
        ]
        paused = await clients[0].post(
            "/v1/systems/payments/runs", json={"input": "x"}, headers={"Authorization": "Bearer a"}
        )
        path = f"/v1/systems/payments/threads/{paused.json()['thread_id']}/interrupts"
        own = await clients[1].get(path, headers={"Authorization": "Bearer a"})
        foreign = await clients[1].get(path, headers={"Authorization": "Bearer m"})
        for client in clients:
            await client.aclose()
    finally:
        for index in indexes:
            await index.close()
    assert own.status_code == 200 and foreign.status_code == 404


# ------------------------------------------------------------------- answered paused states


async def test_one_paused_state_is_claimed_once_across_workers(
    backend: str, thread_index: BaseThreadIndex
) -> None:
    from awa.server import PostgresThreadIndex
    from awa.server._stores import DecisionClaims

    # Two workers: on PostgreSQL two index objects on one table, in memory the shared object.
    second = (
        PostgresThreadIndex(os.environ[POSTGRES_DSN_VARIABLE], schema="awa_test")
        if backend == "postgres"
        else thread_index
    )
    first, other = thread_index, second
    assert isinstance(first, DecisionClaims) and isinstance(other, DecisionClaims)
    thread, version = f"t-{uuid4().hex}", "checkpoint-1:0"
    try:
        won = await asyncio.gather(
            *(worker.claim_decision("desk", thread, version) for worker in [first, other] * 4)
        )
        assert sorted(won) == [False] * 7 + [True]
        # Another paused state of the same thread is a separate claim.
        assert await other.claim_decision("desk", thread, "checkpoint-2:0")
        # A failed resume releases its claim; the state can then be answered again, once.
        await first.release_decision("desk", thread, version)
        assert await other.claim_decision("desk", thread, version)
        assert not await first.claim_decision("desk", thread, version)
        # Forgetting the thread drops its answered states.
        await thread_index.forget("desk", thread)
        assert await other.claim_decision("desk", thread, version)
    finally:
        await thread_index.forget("desk", thread)
        if second is not thread_index:
            await second.close()
