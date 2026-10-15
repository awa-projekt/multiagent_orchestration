"""PostgreSQL-backed objects work in consecutive event loops (`asyncio.run` twice)."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest

from awa._internal import aio as asyncio_compat
from awa.persistence.sql import LoopLocalPool


class _FakePool:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _fake_pools(monkeypatch: pytest.MonkeyPatch) -> list[_FakePool]:
    opened: list[_FakePool] = []

    async def open_pool(self: Any) -> _FakePool:
        opened.append(_FakePool())
        return opened[-1]

    monkeypatch.setattr(LoopLocalPool, "_open", open_pool)
    return opened


def test_a_loop_local_pool_opens_one_pool_per_loop_and_closes_it_at_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = _fake_pools(monkeypatch)
    prepared: list[Any] = []

    async def prepare(pool: Any) -> None:
        prepared.append(pool)

    pools = LoopLocalPool("postgresql://db", max_size=2, prepare=prepare)

    async def use() -> Any:
        first = await pools.get()
        assert await pools.get() is first
        return first

    used = [asyncio.run(use()), asyncio.run(use())]

    assert used == opened == prepared and used[0] is not used[1]
    assert all(pool.closed for pool in opened)


def test_closing_a_loop_local_pool_closes_the_running_loops_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened = _fake_pools(monkeypatch)
    pools = LoopLocalPool("postgresql://db", max_size=2)

    async def use() -> None:
        await pools.get()
        await pools.close()
        assert opened[-1].closed
        await pools.get()  # a later use opens a new pool

    asyncio.run(use())

    assert len(opened) == 2 and all(pool.closed for pool in opened)


def test_a_loop_local_pool_leaves_a_passed_pool_open_and_retries_a_failed_prepare() -> None:
    shared = _FakePool()
    attempts: list[int] = []

    async def prepare(pool: Any) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("database starting")

    pools = LoopLocalPool(shared, max_size=2, prepare=prepare)

    async def use() -> Any:
        with pytest.raises(OSError):
            await pools.get()
        pool = await pools.get()
        await pools.close()
        return pool

    assert asyncio.run(use()) is shared
    assert not shared.closed and len(attempts) == 2


def _postgres_objects(dsn: str) -> list[Any]:
    from awa.automation import PostgresEventQueue, PostgresRunStore
    from awa.server import PostgresIdempotencyCache, PostgresThreadIndex

    return [
        PostgresRunStore(dsn, schema="awa_test", pool_size=4),
        PostgresEventQueue(dsn, name="loops", schema="awa_test", pool_size=4),
        PostgresThreadIndex(dsn, schema="awa_test", pool_size=4),
        PostgresIdempotencyCache(dsn, schema="awa_test", pool_size=4),
    ]


@pytest.mark.postgres
@pytest.mark.parametrize("index", range(4))
def test_postgres_stores_work_in_consecutive_event_loops(postgres_dsn: str, index: int) -> None:
    store = _postgres_objects(postgres_dsn)[index]
    pools: list[Any] = []

    async def query(concurrency: int) -> None:
        async def one() -> None:
            async with await store._connection() as conn:
                await conn.execute("SELECT pg_sleep(0.05)")

        # The pool must grow to serve these: its workers belong to this loop.
        await asyncio.wait_for(asyncio.gather(*(one() for _ in range(concurrency))), 10)
        pools.append(await store._pools.get())

    asyncio_compat.run(query(1), selector_loop=True)
    asyncio_compat.run(query(4), selector_loop=True)
    asyncio_compat.run(store.close(), selector_loop=True)  # a later loop can close it

    assert pools[0] is not pools[1]
    assert all(pool.closed for pool in pools)


@pytest.mark.skipif(sys.platform != "win32", reason="the ProactorEventLoop exists on Windows only")
def test_the_proactor_loop_is_refused_at_once_instead_of_a_30_second_pool_timeout() -> None:
    # Opening a PostgreSQL pool under plain asyncio.run on Windows must not wait 30 s for a
    # psycopg_pool.PoolTimeout that does not say why.
    from awa.errors import ConfigurationError

    dsn = "postgresql://nobody@127.0.0.1:1/none"

    async def open_it() -> None:
        await LoopLocalPool(dsn, max_size=1).get()

    proactor = asyncio.ProactorEventLoop  # type: ignore[attr-defined]
    with (
        asyncio.Runner(loop_factory=proactor) as runner,
        pytest.raises(ConfigurationError, match="selector_loop=True"),
    ):
        runner.run(asyncio.wait_for(open_it(), timeout=5))


def test_a_selector_loop_passes_the_event_loop_check() -> None:
    from awa.persistence.sql import check_event_loop

    async def check() -> None:
        check_event_loop()

    asyncio_compat.run(check(), selector_loop=True)
