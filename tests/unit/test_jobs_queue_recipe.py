"""The job-queue worker of `tests/_helpers/job_worker.py` without a database.

The same scenarios run against PostgreSQL in `tests/integration/test_jobs_queue_postgres.py`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from _helpers import job_worker
from _helpers.jobs_recipe import HOOK_SECRET, SCENARIOS, Env
from _helpers.polling import until
from awa.automation import InMemoryEventQueue
from awa.server import InMemoryIdempotencyCache
from awa.testing import FunctionChatModel, ModelCall, tool_call


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setenv(job_worker.SECRET_ENV, HOOK_SECRET)
    saver = InMemorySaver()

    @asynccontextmanager
    async def open_saver() -> AsyncIterator[Any]:
        yield saver

    cache = InMemoryIdempotencyCache()
    return Env(InMemoryEventQueue(max_attempts=2), cache, cache, open_saver)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_recipe_scenario(name: str, env: Env) -> None:
    await SCENARIOS[name](env)


def answer_with_the_invoice(call: ModelCall) -> Any:
    if isinstance(call.last_message, ToolMessage):
        return f"Found: {call.last_message.content}"
    return tool_call("get_invoice", {"invoice_id": "INV-2"})


async def test_a_running_worker_runs_each_job_once_and_posts_signed_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(job_worker.SECRET_ENV, "test-secret")
    checker = job_worker.build_checker()
    queue = InMemoryEventQueue()
    cache = InMemoryIdempotencyCache()
    stop = asyncio.Event()
    with job_worker.receiver("test-secret") as (url, received):
        for invoice_id in ("INV-1", "INV-2", "INV-1"):
            job = job_worker.invoice_job(invoice_id, url)
            await job_worker.enqueue_job(queue, job, "queue-secret")
        with checker.override(model=FunctionChatModel(answer_with_the_invoice)):
            registry = job_worker.build_registry(checker)
            worker = asyncio.create_task(
                job_worker.run_worker(
                    queue, registry, cache, "queue-secret", stop, poll_interval=0.01
                )
            )
            await until(lambda: len(queue) == 0 and len(received) == 2)
            stop.set()
            handled = await worker
    results = [item.result for item in handled if item.result is not None]
    assert len(results) == len(handled) == 3
    assert [r.status for r in results] == ["completed", "completed", "completed"]
    assert [r.replayed for r in results] == [False, False, True]
    assert [r.metadata["invoice_id"] for r in results] == ["INV-1", "INV-2", "INV-1"]
    assert "18400.0" in results[1].text
    assert [r.callback_delivered for r in results] == [True, True, True]
    assert {r.job_id for r in received} == {r.job_id for r in results}


def test_job_messages_are_plain_json() -> None:
    message = job_worker.invoice_job("INV-1", "https://erp.example.com/hooks/awa")
    assert json.loads(json.dumps(message)) == message
    assert message["callback"]["secret_env"] == job_worker.SECRET_ENV
    assert message["callback"]["secret"] is None
    assert message["idempotency_key"] == "invoice-check:INV-1"
    assert job_worker.invoice_job("INV-1", None)["callback"] is None
    signed = job_worker.sign_job(message, "queue-secret")
    assert json.loads(json.dumps(signed)) == signed and set(signed) == {"body", "headers"}
    assert job_worker.open_job(signed, "queue-secret").idempotency_key == "invoice-check:INV-1"
    assert job_worker.decision_job("t-1")["decision"] == {"action": "approve"}
