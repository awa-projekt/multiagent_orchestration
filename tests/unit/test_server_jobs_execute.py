"""`awa.server.jobs`: JSON jobs through a fake queue, and signed completion callbacks."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from awa import Agent
from awa.automation import Webhook
from awa.errors import (
    IdempotencyConflictError,
    IdempotencyMismatchError,
    InvalidRequestError,
    JobFailedError,
    WebhookDeliveryError,
    WebhookVerificationError,
)
from awa.runtime import current_run_context
from awa.server import InMemoryIdempotencyCache, ServedSystem
from awa.server.jobs import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    JOB_ID_METADATA_KEY,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    JobRegistry,
    JobResult,
    JobSpec,
    WebhookCallback,
    execute,
    execute_sync,
    sign_webhook,
    verify_webhook,
)
from awa.testing import FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper


def echo_agent(name: str = "echo", calls: list[str] | None = None) -> Agent:
    def respond(call: ModelCall) -> str:
        if calls is not None:
            calls.append(str(call.last_message.content))
        return f"echo: {call.last_message.content}"

    return Agent(name, description="Echoes.", model=FunctionChatModel(respond))


def context_agent(seen: list[Any]) -> Agent:
    def respond(call: ModelCall) -> str:
        ctx = current_run_context()
        assert ctx is not None
        seen.append((ctx.deps, dict(ctx.metadata)))
        return "ok"

    return Agent("ctx", description="Records its context.", model=FunctionChatModel(respond))


def failing_agent() -> Agent:
    def boom(call: ModelCall) -> Any:
        raise RuntimeError("db password=hunter2")

    return Agent("boom", description="Fails.", model=FunctionChatModel(boom))


def slow_agent(release: asyncio.Event) -> Agent:
    @tool
    async def wait() -> str:
        """Wait."""
        await release.wait()
        return "done"

    def respond(call: ModelCall) -> Any:
        if isinstance(call.last_message, ToolMessage):
            return "finished"
        return tool_call("wait")

    return Agent("slow", description="Slow.", model=FunctionChatModel(respond), tools=[wait])


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


class FakeQueue:
    """A broker stand-in: messages are JSON text, as on Redis, SQS or RabbitMQ."""

    def __init__(self) -> None:
        self.messages: deque[str] = deque()
        self.results: list[str] = []

    def enqueue(self, payload: dict[str, Any]) -> None:
        self.messages.append(json.dumps(payload))

    async def work(self, registry: JobRegistry, **options: Any) -> None:
        while self.messages:
            payload = json.loads(self.messages.popleft())
            result = await execute(payload, registry, **options)
            self.results.append(json.dumps(result.model_dump(mode="json")))


# ---------------------------------------------------------------------- JSON through a queue


async def test_jobs_round_trip_through_a_json_queue() -> None:
    queue = FakeQueue()
    registry = JobRegistry({"echo": echo_agent()})
    queue.enqueue(
        JobSpec(system="echo", input="hello", metadata={"ticket": 7}).model_dump(mode="json")
    )
    queue.enqueue(
        JobSpec(system="echo", input=[{"role": "user", "content": "as messages"}]).model_dump(
            mode="json"
        )
    )
    await queue.work(registry)
    results = [JobResult.model_validate_json(text) for text in queue.results]
    assert [r.status for r in results] == ["completed", "completed"]
    assert [r.text for r in results] == ["echo: hello", "echo: as messages"]
    first = results[0]
    assert first.ok and first.metadata == {"ticket": 7} and first.system == "echo"
    assert first.result is not None and first.result["status"] == "completed"
    assert first.result["usage"]["requests"] == 1 and first.error is None
    assert first.finished_at >= first.started_at and not first.replayed


def test_execute_sync_runs_from_synchronous_workers() -> None:
    registry = JobRegistry({"echo": echo_agent()})
    result = execute_sync({"system": "echo", "input": "sync"}, registry)
    assert result.text == "echo: sync"
    as_text = execute_sync(json.dumps({"system": "echo", "input": "text"}), registry)
    assert as_text.text == "echo: text"
    assert execute_sync(b'{"system": "echo", "input": "bytes"}', registry).text == "echo: bytes"


async def test_invalid_jobs_are_refused() -> None:
    registry = JobRegistry({"echo": echo_agent()})
    with pytest.raises(InvalidRequestError, match="Invalid job"):
        await execute({"system": "echo"}, registry)
    with pytest.raises(InvalidRequestError, match="Invalid job"):
        await execute({"system": "echo", "input": "x", "unknown": 1}, registry)
    with pytest.raises(TypeError):
        await execute(42, registry)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="thread_id"):
        JobSpec(system="echo", decision={"action": "approve"})
    with pytest.raises(ValueError, match="not both"):
        JobSpec(system="echo", input="x", thread_id="t-1", decision={"action": "approve"})
    with pytest.raises(ValueError, match="empty"):
        JobSpec(system="echo", input="")


# ---------------------------------------------------------------------- systems


async def test_registry_builds_factories_once_sync_and_async() -> None:
    built: list[str] = []

    def build_echo() -> Agent:
        built.append("echo")
        return echo_agent()

    async def build_other() -> ServedSystem:
        built.append("other")
        return ServedSystem(echo_agent("other"))

    registry = JobRegistry({"echo": build_echo, "other": build_other})
    for _ in range(2):
        assert (await execute({"system": "echo", "input": "a"}, registry)).ok
        assert (await execute({"system": "other", "input": "b"}, registry)).ok
    assert built == ["echo", "other"]
    assert registry.names == ["echo", "other"]


async def test_unknown_systems_and_bad_factories_fail_the_job() -> None:
    registry = JobRegistry({"echo": echo_agent(), "broken": lambda: "not a system"})
    missing = await execute({"system": "nope", "input": "x"}, registry)
    assert missing.status == "failed" and missing.error is not None
    assert missing.error["code"] == "awa.system_not_found" and not missing.retryable
    broken = await execute({"system": "broken", "input": "x"}, registry)
    assert broken.status == "failed" and broken.error is not None
    assert broken.error["code"] == "awa.internal"


def test_registry_validates_names() -> None:
    registry = JobRegistry()
    with pytest.raises(ValueError, match="Invalid system name"):
        registry.register("bad name", echo_agent())
    registry.register("echo", echo_agent())
    with pytest.raises(ValueError, match="already registered"):
        registry.register("echo", echo_agent())
    with pytest.raises(TypeError):
        registry.register("x", 42)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="prefixes"):
        JobRegistry(allow_imports="myco")  # type: ignore[arg-type]


@pytest.fixture
def systems_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    name = "awa_jobs_fixture_systems"
    (tmp_path / f"{name}.py").write_text(
        "from awa import Agent\n"
        "from awa.testing import FunctionChatModel\n"
        "def build():\n"
        "    return Agent('imported', description='Imported.',\n"
        "                 model=FunctionChatModel(lambda call: 'from an import path'))\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    yield name
    sys.modules.pop(name, None)


async def test_import_paths_need_permission(systems_module: str) -> None:
    ref = f"{systems_module}:build"
    anywhere = JobRegistry(allow_imports=True)
    unrestricted = await execute({"system": ref, "input": "x"}, anywhere)
    assert unrestricted.text == "from an import path"
    for refused in (
        await execute({"system": ref, "input": "x"}, JobRegistry()),
        await execute({"system": ref, "input": "x"}),  # no registry: nothing may be imported
    ):
        assert refused.status == "failed" and refused.error is not None
        assert "not allowed" in refused.error["message"]
    prefixed = JobRegistry(allow_imports=[systems_module])
    assert (await execute({"system": ref, "input": "x"}, prefixed)).ok
    other = JobRegistry(allow_imports=["myco"])
    assert (await execute({"system": ref, "input": "x"}, other)).status == "failed"
    missing = await execute({"system": "no_such_module_xyz:thing", "input": "x"}, anywhere)
    assert missing.error is not None and missing.error["code"] == "awa.system_not_found"


async def test_deps_metadata_tenant_and_actor_reach_the_run() -> None:
    seen: list[Any] = []
    bare = context_agent(seen)
    served = ServedSystem(
        context_agent(seen),
        allowed_deps={"region"},
        principal_deps=lambda caller: {"user_id": caller.id},
        deps_factory=lambda sent: ("deps", sent["region"], sent["user_id"]),
    )
    registry = JobRegistry({"bare": bare, "served": served})
    job = {
        "input": "x",
        "deps": {"region": "eu"},
        "metadata": {"trace": "t-1"},
        "tenant": "acme",
        "actor": "billing-svc",
    }
    assert (await execute({**job, "system": "bare"}, registry)).ok
    result = await execute({**job, "system": "served", "job_id": "job-7"}, registry)
    assert result.ok and result.job_id == "job-7"
    (bare_deps, bare_meta), (served_deps, served_meta) = seen
    assert bare_deps == {"region": "eu"}
    assert served_deps == ("deps", "eu", "billing-svc")
    assert served_meta["trace"] == "t-1" and served_meta[JOB_ID_METADATA_KEY] == "job-7"
    assert served_meta["awa.tenant"] == "acme" and served_meta["awa.actor"] == "billing-svc"
    refused = await execute({"system": "served", "input": "x", "deps": {"db": 1}}, registry)
    assert refused.status == "failed" and refused.error is not None
    assert refused.error["code"] == "awa.invalid_request"


async def test_a_job_without_actor_is_refused_when_the_system_derives_deps_from_the_caller() -> (
    None
):
    seen: list[Any] = []
    served = ServedSystem(
        context_agent(seen),
        principal_deps=lambda caller: {"user_id": caller.id},
        deps_factory=lambda sent: ("deps", sent.get("user_id")),
    )
    registry = JobRegistry({"served": served})
    refused = await execute({"system": "served", "input": "x"}, registry)
    assert refused.status == "failed" and refused.error is not None
    assert refused.error["code"] == "awa.invalid_request"
    assert "set actor" in refused.error["message"] and "'served'" in refused.error["message"]
    assert seen == []  # the run never started
    ok = await execute({"system": "served", "input": "x", "actor": "ann"}, registry)
    assert ok.ok and seen[0][0] == ("deps", "ann")


# ---------------------------------------------------------------------- outcomes


async def test_failures_are_results_with_hidden_internals() -> None:
    result = await execute({"system": "boom", "input": "x"}, JobRegistry({"boom": failing_agent()}))
    assert result.status == "failed" and result.result is None and not result.ok
    assert result.error is not None and result.error["code"] == "awa.internal"
    assert "hunter2" not in json.dumps(result.model_dump(mode="json"))
    with pytest.raises(JobFailedError, match="awa.internal") as raised:
        result.raise_for_status()
    assert raised.value.result is result
    assert (
        (await execute({"system": "echo", "input": "x"}, JobRegistry({"echo": echo_agent()})))
        .raise_for_status()
        .ok
    )


async def test_timeouts_fail_the_job_as_retryable() -> None:
    release = asyncio.Event()
    registry = JobRegistry({"slow": ServedSystem(slow_agent(release), run_timeout=5)})
    result = await execute({"system": "slow", "input": "x", "timeout": 0.05}, registry)
    assert result.status == "failed" and result.error is not None
    assert result.error["code"] == "awa.run_timeout" and result.retryable


async def test_approvals_pause_the_job_and_a_decision_job_resumes_it() -> None:
    executed: list[float] = []
    registry = JobRegistry({"refunds": refunds_agent(executed)})
    paused = await execute({"system": "refunds", "input": "refund A-1"}, registry)
    assert paused.status == "interrupted" and paused.ok and len(paused.interrupts) == 1
    assert paused.thread_id is not None and executed == []
    resumed = await execute(
        {"system": "refunds", "thread_id": paused.thread_id, "decision": {"action": "approve"}},
        registry,
    )
    assert resumed.status == "completed" and "refunded 480" in resumed.text
    assert executed == [480]
    not_resumable = await execute(
        {"system": "echo", "thread_id": "t-1", "decision": {"action": "approve"}},
        JobRegistry({"echo": _NoResume(echo_agent())}),
    )
    assert not_resumable.error is not None and "resuming" in not_resumable.error["message"]


class _NoResume:
    """A system without `resume`."""

    def __init__(self, inner: Agent) -> None:
        self._inner = inner
        self.name = inner.name
        self.card = inner.card

    async def run(self, input: Any, **options: Any) -> Any:
        return await self._inner.run(input, **options)


# ---------------------------------------------------------------------- idempotency and hooks


async def test_idempotency_keys_run_a_job_once() -> None:
    calls: list[str] = []
    registry = JobRegistry({"echo": echo_agent(calls=calls)})
    cache = InMemoryIdempotencyCache()
    job = JobSpec(system="echo", input="once", idempotency_key="order-1")
    first = await execute(job, registry, idempotency_cache=cache)
    # The producer rebuilds the job (new job id): still the same job.
    again = await execute(
        JobSpec(system="echo", input="once", idempotency_key="order-1"),
        registry,
        idempotency_cache=cache,
    )
    assert calls == ["once"]
    assert again.replayed and not first.replayed and again.text == first.text
    assert again.job_id == first.job_id
    with pytest.raises(IdempotencyMismatchError):
        await execute(
            {"system": "echo", "input": "other", "idempotency_key": "order-1"},
            registry,
            idempotency_cache=cache,
        )
    # Without a cache, keys are not enforced.
    await execute(job, registry)
    assert calls == ["once", "once"]


async def test_running_keys_conflict_and_retryable_failures_run_again() -> None:
    cache = InMemoryIdempotencyCache()
    job = JobSpec(system="slow", input="x", idempotency_key="k-1", timeout=0.05)
    busy = JobSpec(system="echo", input="x", idempotency_key="k-busy")
    # Another worker holds the key.
    # The persisted scope of job keys is "awa.jobs".
    await cache.begin("awa.jobs\x1f", "k-busy", busy.fingerprint(), 60)
    with pytest.raises(IdempotencyConflictError):
        await execute(busy, JobRegistry({"echo": echo_agent()}), idempotency_cache=cache)
    release = asyncio.Event()
    registry = JobRegistry({"slow": slow_agent(release)})
    first = await execute(job, registry, idempotency_cache=cache)
    second = await execute(job, registry, idempotency_cache=cache)
    assert first.retryable and second.retryable and not second.replayed


async def test_on_complete_hooks_see_every_result() -> None:
    registry = JobRegistry({"echo": echo_agent()})
    seen: list[str] = []

    async def store(result: JobResult) -> None:
        seen.append(f"async:{result.text}:{result.replayed}")

    cache = InMemoryIdempotencyCache()
    job = {"system": "echo", "input": "x", "idempotency_key": "h-1"}
    await execute(job, registry, idempotency_cache=cache, on_complete=store)
    await execute(job, registry, idempotency_cache=cache, on_complete=store)
    await execute(
        {"system": "echo", "input": "y"}, registry, on_complete=lambda r: seen.append(r.text)
    )
    assert seen == ["async:echo: x:False", "async:echo: x:True", "echo: y"]
    with pytest.raises(ValueError, match="idempotency_ttl"):
        await execute(job, registry, idempotency_ttl=0)


SECRET = "whsec-test"


class Receiver:
    """A local HTTP endpoint answering with scripted statuses and recording requests."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = list(statuses)
        self.requests: list[tuple[dict[str, str], bytes]] = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                receiver.requests.append((dict(self.headers.items()), body))
                status = receiver.statuses.pop(0) if receiver.statuses else 200
                self.send_response(status)
                if status == 429:
                    self.send_header("Retry-After", "0")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hooks/awa?token=abc"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver_factory() -> Iterator[Any]:
    created: list[Receiver] = []

    def make(*statuses: int) -> Receiver:
        receiver = Receiver(list(statuses))
        created.append(receiver)
        return receiver

    yield make
    for receiver in created:
        receiver.close()


def echo_registry() -> JobRegistry:
    agent = Agent(
        "echo",
        description="Echoes.",
        model=FunctionChatModel(lambda call: f"echo: {call.last_message.content}"),
    )
    return JobRegistry({"echo": agent})


def callback(url: str, **options: Any) -> WebhookCallback:
    return WebhookCallback(url=url, secret=SECRET, allow_http=True, backoff=0.01, **options)


# ---------------------------------------------------------------------- delivery


async def test_result_is_posted_signed_and_retried_after_a_server_error(
    receiver_factory: Any,
) -> None:
    receiver = receiver_factory(500, 429, 200)
    job = JobSpec(system="echo", input="hi", callback=callback(receiver.url))
    result = await execute(job, echo_registry())
    assert result.callback_delivered is True and result.callback_error is None
    assert len(receiver.requests) == 3
    deliveries = {headers[DELIVERY_HEADER] for headers, _ in receiver.requests}
    assert len(deliveries) == 1 and next(iter(deliveries)).startswith(job.job_id)
    headers, body = receiver.requests[-1]
    assert headers[EVENT_HEADER] == "job.completed"
    verify_webhook(body, headers, SECRET)
    # The body is the JobResult JSON (before the callback status was known).
    posted = JobResult.model_validate_json(body)
    assert posted.job_id == job.job_id and posted.text == "echo: hi"
    assert posted.result is not None and posted.result["status"] == "completed"


async def test_the_automation_webhook_trigger_accepts_job_callbacks(receiver_factory: Any) -> None:
    receiver = receiver_factory(200)
    await execute(
        JobSpec(system="echo", input="x", callback=callback(receiver.url)), echo_registry()
    )
    headers, body = receiver.requests[0]
    event = Webhook("jobs", secret=SECRET).verify(body, headers)
    assert event.payload["status"] == "completed"
    assert event.dedup_key == f"jobs:{headers[DELIVERY_HEADER]}"


async def test_refused_or_failing_deliveries_are_reported_not_raised(receiver_factory: Any) -> None:
    refused = receiver_factory(400)
    result = await execute(
        JobSpec(system="echo", input="x", callback=callback(refused.url)), echo_registry()
    )
    assert result.ok and result.callback_delivered is False
    assert "HTTP 400" in (result.callback_error or "") and len(refused.requests) == 1
    assert "token=abc" not in (result.callback_error or "")
    failing = receiver_factory(503, 503)
    with pytest.raises(WebhookDeliveryError, match="2 attempt"):
        await callback(failing.url, retries=1).deliver(result)
    assert len(failing.requests) == 2


async def test_unreachable_receivers_are_reported() -> None:
    unreachable = callback("http://127.0.0.1:9/nothing", retries=0, timeout=1)
    result = await execute(JobSpec(system="echo", input="x", callback=unreachable), echo_registry())
    assert result.callback_delivered is False and "1 attempt" in (result.callback_error or "")


def test_a_malformed_callback_url_is_refused_when_the_job_is_given() -> None:
    with pytest.raises(ValueError, match="https:// URL"):
        callback("https://hooks.example/a b")


async def test_an_unexpected_delivery_error_keeps_the_finished_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(self: Any, result: Any) -> None:
        raise UnicodeError("unexpected")

    monkeypatch.setattr(WebhookCallback, "deliver", broken)
    job = JobSpec(system="echo", input="x", callback=callback("https://hooks.example/h"))

    result = await execute(job, echo_registry())

    assert result.ok and result.callback_delivered is False
    assert "unexpected" in (result.callback_error or "")


async def test_replays_redeliver_callbacks_that_were_not_delivered(receiver_factory: Any) -> None:
    receiver = receiver_factory(400)
    cache = InMemoryIdempotencyCache()
    job = JobSpec(system="echo", input="x", idempotency_key="cb-1", callback=callback(receiver.url))
    first = await execute(job, echo_registry(), idempotency_cache=cache)
    assert first.callback_delivered is False
    again = await execute(job, echo_registry(), idempotency_cache=cache)
    assert again.replayed and again.callback_delivered is True
    third = await execute(job, echo_registry(), idempotency_cache=cache)
    assert third.replayed and third.callback_delivered is True
    assert len(receiver.requests) == 2


# ---------------------------------------------------------------------- secrets and serialisation


async def test_secret_env_travels_with_the_job_and_literal_secrets_do_not(
    receiver_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    receiver = receiver_factory(200)
    with pytest.raises(PydanticSerializationError, match="secret_env"):
        JobSpec(system="echo", input="x", callback=callback(receiver.url)).model_dump(mode="json")
    queued = JobSpec(
        system="echo",
        input="x",
        callback=WebhookCallback(url=receiver.url, secret_env="AWA_TEST_HOOK", allow_http=True),
    ).model_dump(mode="json")
    assert queued["callback"]["secret"] is None
    monkeypatch.delenv("AWA_TEST_HOOK", raising=False)
    missing = await execute(json.dumps(queued), echo_registry())
    assert missing.callback_delivered is False
    assert "AWA_TEST_HOOK" in (missing.callback_error or "")
    monkeypatch.setenv("AWA_TEST_HOOK", "from-env")
    delivered = await execute(json.dumps(queued), echo_registry())
    assert delivered.callback_delivered is True
    headers, body = receiver.requests[-1]
    verify_webhook(body, headers, "from-env")


def test_callbacks_need_https_and_a_non_empty_secret() -> None:
    with pytest.raises(ValidationError, match="https"):
        WebhookCallback(url="http://example.com/hook")
    with pytest.raises(ValidationError, match="empty"):
        WebhookCallback(url="https://example.com/hook", secret="")
    unsigned = WebhookCallback(url="https://example.com/hook")
    result = JobResult(
        job_id="job-1",
        system="echo",
        status="failed",
        error={"type": "X", "code": "awa.x", "message": "m", "retryable": False},
        started_at="2026-01-01T00:00:00Z",  # type: ignore[arg-type]
        finished_at="2026-01-01T00:00:01Z",  # type: ignore[arg-type]
    )
    _, headers = unsigned.request(result)
    assert SIGNATURE_HEADER not in headers and headers[EVENT_HEADER] == "job.failed"


# ---------------------------------------------------------------------- verification


def test_verify_webhook_checks_signature_window_and_rotation() -> None:
    body = b'{"job_id": "job-1"}'
    now = 1_800_000_000
    headers = sign_webhook(SECRET, body, timestamp=now)
    expected = hmac.new(SECRET.encode(), f"{now}.".encode() + body, hashlib.sha256).hexdigest()
    assert headers == {TIMESTAMP_HEADER: str(now), SIGNATURE_HEADER: f"sha256={expected}"}
    lower = {key.lower(): value for key, value in headers.items()}
    verify_webhook(body, lower, SECRET, now=now + 10)
    verify_webhook(body, headers, ["old-secret", SECRET], now=now)
    with pytest.raises(WebhookVerificationError, match="does not match"):
        verify_webhook(body, headers, "other", now=now)
    with pytest.raises(WebhookVerificationError, match="does not match"):
        verify_webhook(body + b" ", headers, SECRET, now=now)
    with pytest.raises(WebhookVerificationError, match="replay window"):
        verify_webhook(body, headers, SECRET, now=now + 301)
    with pytest.raises(WebhookVerificationError, match=TIMESTAMP_HEADER):
        verify_webhook(body, {SIGNATURE_HEADER: headers[SIGNATURE_HEADER]}, SECRET, now=now)
    with pytest.raises(WebhookVerificationError, match=SIGNATURE_HEADER):
        verify_webhook(body, {TIMESTAMP_HEADER: str(now)}, SECRET, now=now)
    with pytest.raises(ValueError, match="empty"):
        sign_webhook("", body)
    assert abs(int(sign_webhook(SECRET, body)[TIMESTAMP_HEADER]) - time.time()) < 5


async def test_a_timeout_inside_the_system_is_no_job_time_limit() -> None:
    from awa.contracts.cards import AgentCard

    class Upstream:
        name = "upstream"
        card = AgentCard(name="upstream", description="Calls a slow API.")

        async def run(  # takes no `timeout=`: the job's limit cancels the task instead
            self, input: Any, *, thread_id: Any = None, deps: Any = None, metadata: Any = None
        ) -> Any:
            raise TimeoutError("the supplier API did not answer")

    result = await execute(
        {"system": "upstream", "input": "x", "timeout": 5}, JobRegistry({"upstream": Upstream()})
    )

    assert result.status == "failed" and result.error is not None
    assert result.error["code"] != "awa.run_timeout"
