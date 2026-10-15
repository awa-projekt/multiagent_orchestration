"""The job-queue worker of `tests/_helpers/job_worker.py` on PostgreSQL (set AWA_TEST_POSTGRES_DSN).

The queue is a `PostgresEventQueue`, the idempotency cache a `PostgresIdempotencyCache`
(two connections stand for two workers), and approvals pause and resume through a PostgreSQL
checkpointer. Everything lives in its own schema, dropped afterwards. The scenarios are the
ones of the offline variant in `tests/unit/test_jobs_queue_recipe.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import pytest

from _helpers import job_worker
from _helpers.jobs_recipe import HOOK_SECRET, SCENARIOS, Env, checker, job
from awa.automation import PostgresEventQueue
from awa.core.settings import Settings
from awa.persistence import open_checkpointer
from awa.server import PostgresIdempotencyCache

pytestmark = pytest.mark.postgres


@pytest.fixture
async def schema(postgres_dsn: str) -> AsyncIterator[str]:
    """A fresh schema `awa_test_jobs_<hex>` for the tables of one test, dropped afterwards."""

    import psycopg

    name = f"awa_test_jobs_{uuid4().hex[:10]}"
    async with await psycopg.AsyncConnection.connect(postgres_dsn, autocommit=True) as conn:
        await conn.execute(f"CREATE SCHEMA {name}")
    yield name
    async with await psycopg.AsyncConnection.connect(postgres_dsn, autocommit=True) as conn:
        await conn.execute(f"DROP SCHEMA {name} CASCADE")


@pytest.fixture
async def env(
    postgres_dsn: str, schema: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Env]:
    monkeypatch.setenv(job_worker.SECRET_ENV, HOOK_SECRET)
    separator = "&" if "?" in postgres_dsn else "?"
    schema_dsn = f"{postgres_dsn}{separator}options={quote(f'-csearch_path={schema}')}"
    queue = PostgresEventQueue(postgres_dsn, name="jobs", schema=schema, max_attempts=2)
    cache = PostgresIdempotencyCache(postgres_dsn, schema=schema)
    other_cache = PostgresIdempotencyCache(postgres_dsn, schema=schema)  # another worker

    @asynccontextmanager
    async def open_saver() -> AsyncIterator[Any]:
        settings = Settings(checkpointer_backend="postgres", postgres_dsn=schema_dsn)
        async with open_checkpointer(settings) as saver:
            yield saver

    try:
        yield Env(queue, cache, other_cache, open_saver)
    finally:
        for resource in (queue, cache, other_cache):
            await resource.close()


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_recipe_scenario_on_postgres(name: str, env: Env) -> None:
    await SCENARIOS[name](env)


async def test_the_cache_and_the_queue_live_in_postgres(
    env: Env, postgres_dsn: str, schema: str
) -> None:
    """The idempotency record and the queue message are rows in the schema, not process memory."""

    import psycopg

    registry = job_worker.build_registry(checker(env))
    await env.send(job("INV-1", None))
    await env.worker(registry)()
    async with await psycopg.AsyncConnection.connect(postgres_dsn, autocommit=True) as conn:
        cursor = await conn.execute(
            f"SELECT scope, key, status, body->>'status' FROM {schema}.server_idempotency"
        )
        assert await cursor.fetchall() == [
            ("awa.jobs\x1f", "invoice-check:INV-1", 200, "completed")
        ]
        cursor = await conn.execute(f"SELECT count(*) FROM {schema}.event_queue")
        assert await cursor.fetchone() == (0,)  # acknowledged
