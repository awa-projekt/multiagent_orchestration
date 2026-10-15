"""A worker for awa jobs on a queue: signed JSON messages, idempotency, retries and a signed callback.

A producer turns work into a `JobSpec`, signs its JSON text with `sign_webhook` and puts it on an
`EventQueue`; the worker checks the signature and the age of every message, runs it with
`execute` and an idempotency cache, puts a retryable failure back with a delay (the queue
dead-letters it after `max_attempts`) and posts the result to a webhook signed with HMAC-SHA256.
`receiver` is a local stand-in for that webhook. The scenarios in `jobs_recipe.py` run this worker
on the in-memory queue and on PostgreSQL.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal

from awa import Agent, tool
from awa.automation import EventQueue
from awa.errors import (
    IdempotencyConflictError,
    IdempotencyMismatchError,
    InvalidRequestError,
    WebhookVerificationError,
)
from awa.server import BaseIdempotencyCache
from awa.server.jobs import (
    DELIVERY_HEADER,
    JobRegistry,
    JobResult,
    JobSpec,
    WebhookCallback,
    execute,
    sign_webhook,
    verify_webhook,
)

SECRET_ENV = "INVOICE_HOOK_SECRET"
"""Variable holding the secret that signs callbacks (read by the worker and the receiver)."""

MESSAGE_TOLERANCE = 3600.0
"""Seconds a signed message may wait on the queue before the worker refuses it as expired.

It is also the replay window of captured messages: keep it below the worker's
`idempotency_ttl` (a day by default), so a key is still remembered while a
replayed message could pass the signature check.
"""

INVOICES: dict[str, dict[str, Any]] = {
    "INV-1": {"supplier": "Paper Co", "amount_eur": 240.0, "po": "PO-77", "po_amount_eur": 240.0},
    "INV-2": {
        "supplier": "Steel AG",
        "amount_eur": 18_400.0,
        "po": "PO-81",
        "po_amount_eur": 16_000.0,
    },
}


@tool
def get_invoice(invoice_id: str) -> str:
    """Return an invoice with its purchase order amount."""

    invoice = INVOICES.get(invoice_id)
    return json.dumps(invoice) if invoice else f"No invoice {invoice_id!r}."


def build_checker() -> Agent:
    """The invoice checker the worker runs."""

    return Agent(
        "invoice_checker",
        description="Checks supplier invoices against their purchase orders.",
        instructions=(
            "Look the invoice up with get_invoice. Answer 'OK' if the amount matches the "
            "purchase order, otherwise 'HOLD' with the difference in EUR."
        ),
        tools=[get_invoice],
    )


def build_registry(checker: Agent | None = None) -> JobRegistry:
    """The systems jobs may name. Factories run once, in the worker."""

    return JobRegistry({"invoice-check": lambda: checker or build_checker()})


# ---------------------------------------------------------------------- the signed message


def sign_job(job: dict[str, Any], secret: str, *, timestamp: int | None = None) -> dict[str, Any]:
    """The queue message for `job`: its JSON text and the signature headers over that text."""

    body = json.dumps(job, separators=(",", ":"), sort_keys=True)
    return {"body": body, "headers": sign_webhook(secret, body.encode(), timestamp=timestamp)}


def open_job(
    message: Any, secret: str, *, tolerance: float = MESSAGE_TOLERANCE, now: float | None = None
) -> JobSpec:
    """Check the signature and age of a queue message and return the job it carries.

    Raises:
        WebhookVerificationError: If the message is no signed job, the
            signature does not match (tampered, wrong secret) or it is expired.
        InvalidRequestError: If the signed body is no valid job, or has no
            `idempotency_key`.
    """

    body, headers = (
        (message.get("body"), message.get("headers")) if isinstance(message, dict) else (None, None)
    )
    if not isinstance(body, str) or not isinstance(headers, dict):
        raise WebhookVerificationError("The queue message is not a signed job")
    verify_webhook(body.encode(), headers, secret, tolerance=tolerance, now=now)
    try:
        spec = JobSpec.model_validate_json(body)
    except ValueError as exc:
        raise InvalidRequestError(f"Invalid job: {exc}") from None
    if spec.idempotency_key is None:
        raise InvalidRequestError("This worker runs only jobs with an idempotency_key")
    return spec


# ---------------------------------------------------------------------- producer


def invoice_job(invoice_id: str, callback_url: str | None) -> dict[str, Any]:
    """What the web request handler builds: JSON only."""

    callback = (
        WebhookCallback(url=callback_url, secret_env=SECRET_ENV, allow_http=True, backoff=0.2)
        if callback_url
        else None
    )
    spec = JobSpec(
        system="invoice-check",
        input=f"Check invoice {invoice_id}.",
        idempotency_key=f"invoice-check:{invoice_id}",
        metadata={"invoice_id": invoice_id},
        timeout=120,
        callback=callback,
    )
    return spec.model_dump(mode="json")


def decision_job(
    thread_id: str, action: str = "approve", system: str = "invoice-check"
) -> dict[str, Any]:
    """A job that resumes the paused run of `thread_id` with a human decision."""

    spec = JobSpec(
        system=system,
        thread_id=thread_id,
        decision={"action": action},
        idempotency_key=f"decision:{thread_id}:{action}",
    )
    return spec.model_dump(mode="json")


async def enqueue_job(queue: EventQueue, job: dict[str, Any], secret: str) -> str:
    """Sign `job` and put it on `queue`; returns the message id."""

    return await queue.send(sign_job(job, secret))


# ---------------------------------------------------------------------- worker


@dataclass(frozen=True)
class Handled:
    """What the worker did with one message."""

    message_id: str
    action: Literal["done", "retry", "refused"]
    """`done`: acknowledged. `retry`: back on the queue. `refused`: dropped, it can never run."""
    result: JobResult | None = None
    reason: str = ""


async def handle_message(
    queue: EventQueue,
    message_id: str,
    payload: Any,
    registry: JobRegistry,
    cache: BaseIdempotencyCache,
    secret: str,
    *,
    tolerance: float = MESSAGE_TOLERANCE,
    retry_delay: float = 30.0,
) -> Handled:
    """Verify, run and acknowledge one message."""

    try:
        spec = open_job(payload, secret, tolerance=tolerance)
    except (WebhookVerificationError, InvalidRequestError) as exc:
        await queue.ack(message_id)  # retrying cannot make it valid
        return Handled(message_id, "refused", reason=str(exc))
    try:
        result = await execute(spec, registry, idempotency_cache=cache)
    except IdempotencyConflictError as exc:  # another worker is running this key right now
        await queue.nack(message_id, delay=retry_delay)
        return Handled(message_id, "retry", reason=str(exc))
    except IdempotencyMismatchError as exc:  # the key belongs to a different job
        await queue.ack(message_id)
        return Handled(message_id, "refused", reason=str(exc))
    if result.status == "failed" and result.retryable:
        await queue.nack(message_id, delay=retry_delay)  # dead-lettered after max_attempts
        return Handled(message_id, "retry", result, result.error["message"] if result.error else "")
    await queue.ack(message_id)
    return Handled(message_id, "done", result)


async def work_once(
    queue: EventQueue,
    registry: JobRegistry,
    cache: BaseIdempotencyCache,
    secret: str,
    *,
    limit: int = 10,
    visibility: timedelta = timedelta(minutes=15),
    tolerance: float = MESSAGE_TOLERANCE,
    retry_delay: float = 30.0,
) -> list[Handled]:
    """Take up to `limit` messages from the queue and handle them one after the other.

    A message that is received but neither acknowledged nor put back (a crashed
    worker) becomes visible again after `visibility`: set it above the longest
    job.
    """

    messages = await queue.receive(limit=limit, visibility=visibility)
    return [
        await handle_message(
            queue,
            message.id,
            message.payload,
            registry,
            cache,
            secret,
            tolerance=tolerance,
            retry_delay=retry_delay,
        )
        for message in messages
    ]


async def run_worker(
    queue: EventQueue,
    registry: JobRegistry,
    cache: BaseIdempotencyCache,
    secret: str,
    stop: asyncio.Event,
    *,
    poll_interval: float = 1.0,
    **options: Any,
) -> list[Handled]:
    """Work the queue until `stop` is set; start several workers for more throughput."""

    handled: list[Handled] = []
    while not stop.is_set():
        batch = await work_once(queue, registry, cache, secret, **options)
        handled.extend(batch)
        if not batch:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), poll_interval)
    return handled


# ---------------------------------------------------------------------- receiver


@contextmanager
def receiver(secret: str) -> Iterator[tuple[str, list[JobResult]]]:
    """A local stand-in for the application's webhook endpoint.

    It verifies the signature and the replay window over the raw body, answers
    401 otherwise, and remembers the delivery ids it processed, so a retried
    delivery is recorded once.
    """

    received: list[JobResult] = []
    seen: set[str] = set()
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                verify_webhook(body, dict(self.headers.items()), secret)
            except WebhookVerificationError:
                self.send_response(401)
            else:
                with lock:
                    delivery = self.headers.get(DELIVERY_HEADER, "")
                    if delivery not in seen:
                        seen.add(delivery)
                        received.append(JobResult.model_validate_json(body))
                self.send_response(204)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/hooks/invoices", received
    finally:
        server.shutdown()
        server.server_close()
