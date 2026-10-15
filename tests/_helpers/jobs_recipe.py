"""The scenarios of the job-queue worker in `job_worker.py`.

One set of scenarios runs twice: against the in-memory queue and idempotency
cache in the unit suite (`tests/unit/test_jobs_queue_recipe.py`), and against
`PostgresEventQueue`, `PostgresIdempotencyCache` and a PostgreSQL checkpointer
in `tests/integration/test_jobs_queue_postgres.py`. Models are scripted; the
webhook receiver is a real local HTTP server on a free port.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from _helpers import job_worker
from _helpers.polling import until
from awa import Agent
from awa.automation import EventQueue
from awa.server import BaseIdempotencyCache
from awa.server.jobs import (
    DELIVERY_HEADER,
    JobRegistry,
    JobSpec,
    WebhookCallback,
    execute,
    sign_webhook,
)
from awa.testing import FunctionChatModel, ModelCall, tool_call
from awa.tools import create_tool_approval_wrapper

QUEUE_SECRET = "queue-secret-for-tests"
HOOK_SECRET = "hook-secret-for-tests"
SYSTEM = "invoice-check"


@dataclass
class Env:
    """What a scenario runs against.

    Attributes:
        queue: The queue (`max_attempts` 2).
        cache: The idempotency cache of the first worker.
        other_cache: A second connection to the same store: another worker.
        saver: Opens a checkpointer; every call sees the same checkpoints.
    """

    queue: EventQueue
    cache: BaseIdempotencyCache
    other_cache: BaseIdempotencyCache
    saver: Callable[[], AbstractAsyncContextManager[Any]]
    runs: list[str] = field(default_factory=list)
    executed: list[float] = field(default_factory=list)

    def worker(self, registry: JobRegistry, cache: BaseIdempotencyCache | None = None) -> Any:
        """`work_once` for `registry`, with short retry delays."""

        async def work(**options: Any) -> list[Any]:
            options.setdefault("retry_delay", 0.05)
            return await job_worker.work_once(
                self.queue, registry, cache or self.cache, QUEUE_SECRET, **options
            )

        return work

    async def send(self, job: dict[str, Any], **options: Any) -> str:
        return await self.queue.send(job_worker.sign_job(job, QUEUE_SECRET, **options))

    async def dead(self) -> list[Any]:
        dead = getattr(self.queue, "dead_letters")  # noqa: B009 - a method on PostgreSQL, a list in memory
        return list(await dead() if callable(dead) else dead)


def checker(env: Env, mode: str = "ok") -> Agent:
    """The invoice checker on a scripted model.

    Modes: `ok` answers; `flaky` hangs in its first run (a timeout, which
    is retryable); `slow` always hangs; `broken` fails with an error no retry fixes.
    """

    attempts = 0

    @tool
    async def get_invoice(invoice_id: str) -> str:
        """Return an invoice with its purchase order amount."""
        if mode == "slow" or (mode == "flaky" and attempts == 1):
            await asyncio.sleep(30)
        return json.dumps(job_worker.INVOICES.get(invoice_id, {}))

    def respond(call: ModelCall) -> Any:
        nonlocal attempts
        if isinstance(call.last_message, ToolMessage):
            return f"checked: {call.last_message.content}"
        attempts += 1
        match = re.search(r"INV-\d+", str(call.last_message.content))
        env.runs.append(match.group(0) if match else "?")
        if mode == "broken":
            raise RuntimeError("database password=hunter2")
        return tool_call("get_invoice", {"invoice_id": env.runs[-1]})

    return Agent(
        "invoice_checker",
        description="Checks invoices.",
        model=FunctionChatModel(respond),
        tools=[get_invoice],
    )


def refunds(env: Env, saver: Any) -> Agent:
    """An agent whose refund tool needs a human approval."""

    @tool
    def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order."""
        env.executed.append(amount)
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
        checkpointer=saver,
    )


def job(invoice_id: str, url: str | None, **changes: Any) -> dict[str, Any]:
    return {**job_worker.invoice_job(invoice_id, url), **changes}


def post(url: str, body: bytes, headers: dict[str, str]) -> int:
    """POST to the receiver; the HTTP status (errors included)."""

    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json", **headers}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code


# ---------------------------------------------------------------------- scenarios


async def signed_message_is_accepted_and_the_callback_is_verified(env: Env) -> None:
    registry = job_worker.build_registry(checker(env))
    with job_worker.receiver(HOOK_SECRET) as (url, received):
        await env.send(job("INV-2", url))
        (handled,) = await env.worker(registry)()
        assert handled.action == "done" and handled.result is not None
        result = handled.result
        assert result.status == "completed" and not result.replayed
        assert "Steel AG" in result.text
        assert result.callback_delivered is True and result.callback_error is None
        # The receiver verified signature and window over the raw body before recording.
        assert [r.job_id for r in received] == [result.job_id]
        assert received[0].text == result.text and received[0].metadata == {"invoice_id": "INV-2"}
    assert env.runs == ["INV-2"] and await env.worker(registry)() == []  # acknowledged


async def tampered_wrong_key_unsigned_and_keyless_messages_are_refused(env: Env) -> None:
    registry = job_worker.build_registry(checker(env))
    with job_worker.receiver(HOOK_SECRET) as (url, received):
        signed = job_worker.sign_job(job("INV-1", url), QUEUE_SECRET)
        tampered = {**signed, "body": signed["body"].replace("INV-1", "INV-2")}
        await env.queue.send(tampered)
        await env.queue.send(job_worker.sign_job(job("INV-1", url), "another-secret"))
        await env.queue.send(job("INV-1", url))  # a plain job, no signature at all
        await env.queue.send("not even a message")
        await env.queue.send(
            job_worker.sign_job(job("INV-1", url, idempotency_key=None), QUEUE_SECRET)
        )
        handled = await env.worker(registry)(limit=10)
    assert [h.action for h in handled] == ["refused"] * 5 and all(h.result is None for h in handled)
    reasons = " | ".join(h.reason for h in handled)
    assert "does not match" in reasons and "not a signed job" in reasons
    assert "idempotency_key" in reasons
    assert env.runs == [] and received == []  # nothing ran, nothing was posted
    assert await env.worker(registry)() == [] and await env.dead() == []  # dropped, not looping


async def expired_messages_are_refused(env: Env) -> None:
    registry = job_worker.build_registry(checker(env))
    old = int(time.time()) - 7200  # signed correctly, two hours ago
    await env.send(job("INV-1", None), timestamp=old)
    (handled,) = await env.worker(registry)()
    assert handled.action == "refused" and "replay window" in handled.reason
    assert env.runs == []
    # The window is the worker's choice: the same message passes a longer one.
    await env.send(job("INV-1", None), timestamp=old)
    (handled,) = await env.worker(registry)(tolerance=86_400.0)
    assert handled.action == "done" and env.runs == ["INV-1"]
    # A future timestamp is outside the window too.
    await env.send(job("INV-2", None), timestamp=int(time.time()) + 7200)
    (handled,) = await env.worker(registry)()
    assert handled.action == "refused"


async def a_replayed_message_returns_the_cached_result_without_a_second_run(env: Env) -> None:
    registry = job_worker.build_registry(checker(env))
    with job_worker.receiver(HOOK_SECRET) as (url, received):
        message = job_worker.sign_job(job("INV-1", url), QUEUE_SECRET)
        await env.queue.send(message)
        (first,) = await env.worker(registry)()
        await env.queue.send(message)  # a captured message, injected again: identical bytes
        # The producer signs the same work again (new timestamp, new job id).
        await env.send(job("INV-1", url))
        # Another worker, another connection to the same cache.
        again = await env.worker(registry, env.other_cache)()
    assert first.result is not None and not first.result.replayed
    assert [h.action for h in again] == ["done", "done"]
    assert all(h.result is not None and h.result.replayed for h in again)
    assert all(h.result is not None and h.result.job_id == first.result.job_id for h in again)
    assert all(h.result is not None and h.result.text == first.result.text for h in again)
    assert env.runs == ["INV-1"]  # the system ran once
    assert len(received) == 1  # and the result was posted once
    # The same key for a different job is refused, not served the old result.
    await env.send({**job("INV-1", None), "input": "Check invoice INV-2."})
    (mismatch,) = await env.worker(registry)()
    assert mismatch.action == "refused" and "different job" in mismatch.reason
    assert env.runs == ["INV-1"]


async def a_worker_that_crashed_after_the_run_does_not_run_the_job_again(env: Env) -> None:
    registry = job_worker.build_registry(checker(env))
    await env.send(job("INV-1", None))
    (message,) = await env.queue.receive(visibility=0.05)
    spec = job_worker.open_job(message.payload, QUEUE_SECRET)
    await execute(spec, registry, idempotency_cache=env.cache)  # ran, then the worker died
    handled: list[Any] = []

    async def redelivered() -> bool:
        handled.extend(await env.worker(registry)())
        return bool(handled)

    await until(redelivered)
    assert handled[0].action == "done" and handled[0].result.replayed
    assert env.runs == ["INV-1"]


async def a_callback_the_receiver_refuses_is_sent_again_on_replay(env: Env) -> None:
    registry = job_worker.build_registry(checker(env))
    with job_worker.receiver("the-receivers-other-secret") as (url, received):
        message = job_worker.sign_job(job("INV-1", url), QUEUE_SECRET)
        await env.queue.send(message)
        (first,) = await env.worker(registry)()
        assert first.result is not None and first.result.status == "completed"
        assert first.result.callback_delivered is False  # 401: the signature did not verify
        assert "401" in str(first.result.callback_error) and received == []
    with job_worker.receiver(HOOK_SECRET) as (url2, received2):
        # The receiver moved to a new URL: the message names it, so send a corrected message.
        await env.queue.send(job_worker.sign_job(job("INV-1", url2), QUEUE_SECRET))
        (second,) = await env.worker(registry)()
        assert second.result is not None and second.result.replayed
        assert second.result.callback_delivered is True and len(received2) == 1
    assert env.runs == ["INV-1"]


async def the_receiver_checks_signature_window_and_delivery_ids(env: Env) -> None:
    with job_worker.receiver(HOOK_SECRET) as (url, received):
        body = json.dumps(
            {
                "job_id": "job_x",
                "system": SYSTEM,
                "status": "completed",
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:00:01Z",
            }
        ).encode()
        headers = {**sign_webhook(HOOK_SECRET, body), DELIVERY_HEADER: "d-1"}
        assert post(url, body, headers) == 204
        assert post(url, body, headers) == 204  # a retry of the same delivery
        assert len(received) == 1
        assert post(url, body + b" ", headers) == 401  # tampered body
        assert post(url, body, sign_webhook("wrong", body)) == 401
        stale = sign_webhook(HOOK_SECRET, body, timestamp=int(time.time()) - 3600)
        assert post(url, body, {**stale, DELIVERY_HEADER: "d-2"}) == 401
        assert post(url, body, {}) == 401
        assert len(received) == 1


async def retryable_failures_go_back_on_the_queue_and_run_again(env: Env) -> None:
    registry = job_worker.build_registry(checker(env, "flaky"))
    with job_worker.receiver(HOOK_SECRET) as (url, received):
        await env.send(job("INV-1", url, timeout=0.3))
        (first,) = await env.worker(registry)()
        assert first.action == "retry" and first.result is not None
        assert first.result.status == "failed" and first.result.retryable
        assert first.result.error is not None and first.result.error["code"] == "awa.run_timeout"
        second: list[Any] = []

        async def due() -> bool:
            second.extend(await env.worker(registry)())
            return bool(second)

        await until(due)
        assert second[0].action == "done" and second[0].result.status == "completed"
        assert not second[0].result.replayed  # a retryable failure is not cached
        assert env.runs == ["INV-1", "INV-1"]
        assert [r.status for r in received] == ["failed", "completed"]
    assert await env.dead() == []


async def failures_that_never_succeed_end_in_the_dead_letters(env: Env) -> None:
    registry = job_worker.build_registry(checker(env, "slow"))
    await env.send(job("INV-1", None, timeout=0.2))
    seen: list[Any] = []

    async def dead_lettered() -> bool:
        seen.extend(await env.worker(registry)())
        return bool(await env.dead())

    await until(dead_lettered, timeout=20)
    assert [h.action for h in seen] == ["retry", "retry"]  # max_attempts of the queue
    (dead,) = await env.dead()
    assert dead.attempts == 2 and job_worker.open_job(dead.payload, QUEUE_SECRET).input is not None
    assert await env.worker(registry)() == []


async def failures_no_retry_can_fix_are_final_and_cached(env: Env) -> None:
    registry = job_worker.build_registry(checker(env, "broken"))
    with job_worker.receiver(HOOK_SECRET) as (url, received):
        await env.send(job("INV-1", url))
        (handled,) = await env.worker(registry)()
        assert handled.action == "done" and handled.result is not None
        assert handled.result.status == "failed" and not handled.result.retryable
        assert "hunter2" not in json.dumps(handled.result.model_dump(mode="json"))
        assert handled.result.callback_delivered is True
        # Redelivered: the stored failure comes back, the system does not run again.
        await env.send(job("INV-1", url))
        (again,) = await env.worker(registry)()
        assert (
            again.result is not None and again.result.replayed and again.result.status == "failed"
        )
        assert [r.status for r in received] == ["failed"]
    assert env.runs == ["INV-1"] and await env.dead() == []


async def approvals_pause_the_job_and_another_worker_resumes_it(env: Env) -> None:
    async with env.saver() as saver_a, env.saver() as saver_b:
        worker_a = JobRegistry({SYSTEM: refunds(env, saver_a)})
        worker_b = JobRegistry({SYSTEM: refunds(env, saver_b)})
        with job_worker.receiver(HOOK_SECRET) as (url, received):
            callback = WebhookCallback(url=url, secret_env=job_worker.SECRET_ENV, allow_http=True)
            request = JobSpec(
                system=SYSTEM,
                input="Refund order A-1.",
                idempotency_key="refund:A-1",
                callback=callback,
            )
            await env.send(request.model_dump(mode="json"))
            (paused,) = await env.worker(worker_a)()
            assert paused.action == "done" and paused.result is not None
            assert paused.result.status == "interrupted" and len(paused.result.interrupts) == 1
            assert env.executed == []  # nothing is refunded before the decision
            thread = paused.result.thread_id
            assert thread is not None
            # The decision arrives as a signed message and another worker takes it.
            decision = job_worker.decision_job(thread, system=SYSTEM)
            assert decision["decision"] == {"action": "approve"}
            await env.send(decision)
            (resumed,) = await env.worker(worker_b, env.other_cache)()
            assert resumed.result is not None and resumed.result.status == "completed"
            assert "refunded 480" in resumed.result.text and env.executed == [480]
            # A replayed decision message does not refund twice.
            await env.send(decision)
            (again,) = await env.worker(worker_a)()
            assert again.result is not None and again.result.replayed and env.executed == [480]
        assert [r.status for r in received] == ["interrupted"]  # the first job named a callback
        assert received[0].thread_id == thread


SCENARIOS: dict[str, Callable[[Env], Awaitable[None]]] = {
    f.__name__: f
    for f in (
        signed_message_is_accepted_and_the_callback_is_verified,
        tampered_wrong_key_unsigned_and_keyless_messages_are_refused,
        expired_messages_are_refused,
        a_replayed_message_returns_the_cached_result_without_a_second_run,
        a_worker_that_crashed_after_the_run_does_not_run_the_job_again,
        a_callback_the_receiver_refuses_is_sent_again_on_replay,
        the_receiver_checks_signature_window_and_delivery_ids,
        retryable_failures_go_back_on_the_queue_and_run_again,
        failures_that_never_succeed_end_in_the_dead_letters,
        failures_no_retry_can_fix_are_final_and_cached,
        approvals_pause_the_job_and_another_worker_resumes_it,
    )
}

__all__ = ["SCENARIOS", "Env"]
