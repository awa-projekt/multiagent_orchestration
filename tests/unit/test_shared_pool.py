"""One PostgreSQL pool per process: shared by every store, safe behind PgBouncer, and units of
work keep their per-transaction settings (tests marked `postgres` use AWA_TEST_POSTGRES_DSN)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from awa.core.settings import Settings
from awa.persistence.runlog import Scope
from awa.persistence.sql import fetch_one, shared_pool

pytestmark = pytest.mark.postgres


@pytest.fixture
async def schema(postgres_dsn: str) -> AsyncIterator[str]:
    import psycopg

    name = f"awa_test_p{uuid4().hex[:10]}"
    yield name
    async with await psycopg.AsyncConnection.connect(postgres_dsn, autocommit=True) as conn:
        await conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")


async def _prepared_after_repeats(dsn: str, pooler: Any) -> int:
    pool = shared_pool(dsn, max_size=1, pooler=pooler)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            for _ in range(8):
                await fetch_one(conn, "SELECT %s::int + 1", (1,))
            row = await fetch_one(conn, "SELECT count(*) FROM pg_prepared_statements")
    finally:
        await pool.close()
    return int(row[0])


async def test_behind_pgbouncer_no_statement_is_prepared(postgres_dsn: str) -> None:
    assert await _prepared_after_repeats(postgres_dsn, "none") > 0  # the check can see them
    assert await _prepared_after_repeats(postgres_dsn, "pgbouncer") == 0


async def test_a_unit_of_work_keeps_its_transaction_settings(postgres_dsn: str) -> None:
    pool = shared_pool(postgres_dsn, max_size=1)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = '4321ms'")
                await conn.execute("SELECT 1")
                inside = await fetch_one(conn, "SHOW statement_timeout")
            after = await fetch_one(conn, "SHOW statement_timeout")
    finally:
        await pool.close()

    assert inside == ("4321ms",)
    assert after != ("4321ms",)


async def test_every_store_of_the_server_uses_the_one_pool(postgres_dsn: str, schema: str) -> None:
    from awa.automation.models import ProcessRun
    from awa.server._data_store import PostgresData

    data = PostgresData(
        postgres_dsn, schema, settings=Settings(_env_file=None, postgres_pool_max=3)
    )
    await data.open()
    try:
        for store in (data.runs, data.log, data.threads, data.idempotency):
            assert store._pools._shared is data.pool  # type: ignore[attr-defined]
        assert data.checkpointer.conn is data.pool
        run = ProcessRun(automation="invoices")
        await data.runs.create(run)
        await data.log.open_run(Scope("acme", "invoices"), run.id, now=datetime.now(UTC))
        assert await data.runs.get(run.id) is not None
        async with data.pool.connection() as conn:
            row = await fetch_one(
                conn,
                "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'awa'"
                " AND datname = current_database() AND pid <> pg_backend_pid()",
            )
        assert data.pool.get_stats()["pool_max"] == 3
        assert int(row[0]) <= 3
    finally:
        await data.close()


async def test_a_second_lifespan_moves_every_store_onto_a_fresh_pool(
    postgres_dsn: str, schema: str
) -> None:
    from awa.server._data_store import PostgresData

    data = PostgresData(postgres_dsn, schema, settings=Settings(_env_file=None))
    await data.open()
    first = data.pool
    await data.close()
    await data.open()
    try:
        assert data.pool is not first
        for store in (data.runs, data.log, data.threads, data.idempotency):
            assert store._pools._shared is data.pool  # type: ignore[attr-defined]
        assert data.checkpointer.conn is data.pool
        await data.threads.ping()
    finally:
        await data.close()


def test_the_served_app_hands_the_pooled_stores_to_the_server(
    postgres_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Building the app opens no connection (the pool opens in the lifespan), so no schema is made.
    from awa.persistence.threads import PostgresIdempotencyCache, PostgresThreadIndex
    from awa.server import _app, build_blueprint_app

    seen: dict[str, Any] = {}

    def create_app(systems: Any, **options: Any) -> Any:
        seen.update(options)
        from fastapi import FastAPI

        return FastAPI()

    monkeypatch.setattr(_app, "create_app", create_app)
    blueprint = tmp_path / "desk.yaml"
    blueprint.write_text(
        "name: desk\nagents:\n  - name: clerk\n    description: Answers.\n    model: fake:x\n"
        "triggers:\n  - name: hook\n    webhook: {allow_unsigned: true}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "awa.models.ModelRegistry.create",
        lambda self, spec, **kw: __import__(
            "awa.testing", fromlist=["FakeChatModel"]
        ).FakeChatModel(["ok"]),
    )

    build_blueprint_app(blueprint, dsn=postgres_dsn, schema="awa_test_unused")

    threads, cache = seen["thread_index"], seen["idempotency_cache"]
    assert isinstance(threads, PostgresThreadIndex)
    assert isinstance(cache, PostgresIdempotencyCache)
    assert threads._pools._shared is cache._pools._shared  # type: ignore[attr-defined]
    assert seen["checkpointer"].conn is threads._pools._shared  # type: ignore[attr-defined]
