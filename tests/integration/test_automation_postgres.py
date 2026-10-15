"""`PostgresRunStore` and `PostgresEventQueue` against a real PostgreSQL (set AWA_TEST_POSTGRES_DSN)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.tools import tool

from _helpers.automation import ScriptedSystem
from _helpers.polling import until
from awa import Agent
from awa.automation import (
    Automation,
    PostgresEventQueue,
    PostgresRunStore,
    ProcessRun,
    QueueTrigger,
    RetryPolicy,
    wait_for_event,
)
from awa.core.settings import Settings
from awa.errors import RunNotFoundError
from awa.persistence import open_checkpointer
from awa.testing import FakeChatModel, FakeClock, tool_call

pytestmark = pytest.mark.postgres

SCHEMA = "awa_test"


@pytest.fixture
async def store(postgres_dsn: str) -> AsyncIterator[PostgresRunStore]:
    runs = PostgresRunStore(postgres_dsn, schema=SCHEMA)
    await runs.setup()
    try:
        yield runs
    finally:
        await runs.close()


def unique(prefix: str = "a") -> str:
    return f"{prefix}{uuid4().hex[:10]}"


NOW = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


async def test_store_create_dedup_modify_and_list(store: PostgresRunStore) -> None:
    name = unique()
    first, created = await store.create(ProcessRun(automation=name, dedup_key="k", created_at=NOW))
    again, created_again = await store.create(ProcessRun(automation=name, dedup_key="k"))
    assert created and not created_again and again.id == first.id
    later, fresh = await store.create(
        ProcessRun(automation=name, dedup_key="k", created_at=NOW + timedelta(hours=2)),
        dedup_since=NOW + timedelta(hours=1),
    )
    assert fresh and later.id != first.id

    changed = await store.modify(
        first.id, lambda run: run.model_copy(update={"status": "failed"}), now=NOW
    )
    assert changed.revision == first.revision + 1 and changed.status == "failed"
    assert (await store.modify(first.id, lambda run: None, now=NOW)).revision == changed.revision
    listed = await store.list(automation=name)
    assert [run.id for run in listed] == [later.id, first.id]
    assert [run.id for run in await store.list(automation=name, status="failed")] == [first.id]
    with pytest.raises(RunNotFoundError):
        await store.modify("proc_missing", lambda run: run, now=NOW)
    assert await store.get("proc_missing") is None


async def test_concurrent_claims_never_share_a_run(store: PostgresRunStore) -> None:
    name = unique()
    for n in range(6):
        await store.create(ProcessRun(automation=name, created_at=NOW + timedelta(seconds=n)))
    claims = await asyncio.gather(
        *[
            store.claim(
                name, worker=f"w{n}", lease=timedelta(minutes=1), now=NOW + timedelta(minutes=1)
            )
            for n in range(8)
        ]
    )
    ids = [run.id for run in claims if run is not None]
    assert len(ids) == 6 and len(set(ids)) == 6
    assert all(run.attempts == 1 for run in claims if run is not None)


async def test_max_running_versions_leases_and_due(store: PostgresRunStore) -> None:
    name = unique()
    await store.create(ProcessRun(automation=name, version="v1", created_at=NOW))
    await store.create(ProcessRun(automation=name, version="v2", created_at=NOW))
    later = NOW + timedelta(minutes=1)
    assert (
        await store.claim(name, worker="w", lease=timedelta(seconds=10), now=later, versions=["v3"])
        is None
    )
    run = await store.claim(name, worker="w", lease=timedelta(seconds=10), now=later, max_running=1)
    assert run is not None
    assert (
        await store.claim(name, worker="x", lease=timedelta(seconds=10), now=later, max_running=1)
        is None
    )
    assert await store.renew(run.id, worker="w", lease=timedelta(seconds=10), now=later)
    assert not await store.renew(run.id, worker="x", lease=timedelta(seconds=10), now=later)
    assert await store.expired(name, now=later + timedelta(seconds=5)) == []
    [stale] = await store.expired(name, now=later + timedelta(seconds=11))
    assert stale.id == run.id and stale.lease_owner == "w"

    waiting = ProcessRun(
        automation=name, status="waiting", sla_deadline=NOW + timedelta(hours=1), created_at=NOW
    )
    waiting.waits = []
    await store.create(waiting)
    assert [r.id for r in await store.due(name, now=NOW + timedelta(hours=2))] == [waiting.id]
    assert await store.due(name, now=NOW) == []


async def test_state_compare_and_set(store: PostgresRunStore) -> None:
    key = unique("state:")
    assert await store.get_state(key) is None
    assert await store.compare_and_set_state(key, None, "1")
    assert not await store.compare_and_set_state(key, None, "2")
    assert not await store.compare_and_set_state(key, "0", "2")
    assert await store.compare_and_set_state(key, "1", "2")
    assert await store.get_state(key) == "2"


@tool
def ask(invoice: str) -> str:
    """Wait for a reply."""

    reply = wait_for_event(f"reply:{invoice}", timeout="2d")
    return f"reply: {reply['text']}"


async def test_a_waiting_run_survives_a_restart(postgres_dsn: str, store: PostgresRunStore) -> None:
    name = unique("proc")
    invoice = unique("inv")
    settings = Settings(checkpointer_backend="postgres", postgres_dsn=postgres_dsn)
    clock = FakeClock()
    async with open_checkpointer(settings) as saver:
        agent = Agent(
            "clerk",
            model=FakeChatModel([tool_call("ask", {"invoice": invoice})]),
            tools=[ask],
            checkpointer=saver,
        )
        before = Automation(agent, name=name, store=store, version="v1", clock=clock)
        await before.submit({"invoice": invoice})
        [paused] = await before.run_pending()
        assert paused.status == "waiting"
        assert [r.id for r in await store.waiting_on(name, f"reply:{invoice}")] == [paused.id]

    # A new process: new store, checkpointer, system and automation objects.
    fresh_store = PostgresRunStore(postgres_dsn, schema=SCHEMA)
    try:
        async with open_checkpointer(settings) as saver:
            agent = Agent(
                "clerk", model=FakeChatModel(["All confirmed."]), tools=[ask], checkpointer=saver
            )
            after = Automation(agent, name=name, store=fresh_store, version="v1", clock=clock)
            [answered] = await after.deliver(f"reply:{invoice}", {"text": "ok"})
            assert answered.status == "queued"
            [done] = await after.run_pending()
            assert done.status == "succeeded" and done.output == "All confirmed."
    finally:
        await fresh_store.close()


async def test_retries_and_recovery_on_postgres(store: PostgresRunStore) -> None:
    clock = FakeClock()
    auto = Automation(
        ScriptedSystem(errors=[TimeoutError("down")]),
        name=unique(),
        store=store,
        clock=clock,
        retry=RetryPolicy(max_attempts=3, initial=5),
    )
    await auto.submit({"x": 1})
    [failed] = await auto.run_pending()
    assert failed.status == "queued" and failed.next_attempt_at is not None
    clock.advance(seconds=5)
    crashed = await auto.claim("w-crash", lease=timedelta(seconds=30))
    assert crashed is not None and crashed.attempts == 2
    clock.advance(seconds=31)
    assert await auto.tick() == 1
    [done] = await auto.run_pending()
    assert done.status == "succeeded" and done.attempts == 3


async def test_postgres_event_queue(postgres_dsn: str) -> None:
    queue = PostgresEventQueue(postgres_dsn, name=unique("q"), schema=SCHEMA, max_attempts=2)
    try:
        first = await queue.send({"n": 1}, dedup_key="one")
        await queue.send({"n": 2}, delay="1h")
        [message] = await queue.receive(visibility="1s")
        assert message.id == first and message.payload == {"n": 1} and message.attempts == 1
        assert await queue.receive() == []
        redelivered: list[Any] = []

        async def visible_again() -> bool:
            redelivered[:] = await queue.receive(visibility="1s")
            return bool(redelivered)

        await until(visible_again, timeout=5)
        [again] = redelivered
        assert again.attempts == 2
        await queue.nack(first)
        [dead] = await queue.dead_letters()
        assert dead.id == first

        third = await queue.send({"n": 3})
        trigger = QueueTrigger(queue, name="jobs")
        [event] = await trigger.poll()
        assert event.id == third
        await trigger.ack(event, None)
        assert await queue.receive() == []

        # Received max_attempts times and never acknowledged: dead-lettered, not offered again.
        crashed = await queue.send({"n": 4})
        for _ in range(2):
            assert [m.id for m in await queue.receive(visibility="0s")] == [crashed]
        assert await queue.receive() == []
        assert [m.id for m in await queue.dead_letters()] == [first, crashed]
    finally:
        await queue.close()


async def test_store_keeps_usage_and_cost_and_reads_rows_without_them(
    store: PostgresRunStore, postgres_dsn: str
) -> None:
    from awa.contracts import Usage

    name = unique()
    usage = Usage()
    usage.record_model_call("desk", model="m", input_tokens=1000, output_tokens=200)
    created, _ = await store.create(ProcessRun(automation=name, usage=usage, cost=0.0036))

    fetched = await store.get(created.id)
    assert fetched is not None and fetched.usage == usage and fetched.cost == 0.0036

    # A row of version 1.0 has neither field in its JSON document: both fall back to their defaults.
    import psycopg

    async with await psycopg.AsyncConnection.connect(postgres_dsn, autocommit=True) as conn:
        await conn.execute(
            f"UPDATE {SCHEMA}.process_runs SET data = data - 'usage' - 'cost' WHERE id = %s",
            (created.id,),
        )
    old = await store.get(created.id)
    assert old is not None and old.usage == Usage() and old.cost is None
    assert old.automation == name
