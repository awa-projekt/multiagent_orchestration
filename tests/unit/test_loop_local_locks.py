"""Long-lived in-memory objects keep working when their lock was contended in an earlier loop.

An `asyncio.Lock` binds to the loop of its first waiter, so each object keeps one lock per loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from awa.automation import InMemoryEventQueue
from awa.messaging import Blackboard
from awa.server import RateLimit, TokenBucketRateLimiter
from awa.tools import InMemoryIdempotencyStore

Operation = Callable[[Any, int], Awaitable[Any]]

CASES: dict[str, tuple[Callable[[], Any], Operation]] = {
    "event queue": (InMemoryEventQueue, lambda queue, n: queue.send({"n": n})),
    "blackboard": (Blackboard, lambda board, n: board.write("key", n, author="test")),
    "rate limiter": (
        lambda: TokenBucketRateLimiter(RateLimit(requests=100)),
        lambda limiter, n: limiter.acquire("key"),
    ),
    "idempotency store": (InMemoryIdempotencyStore, lambda store, n: store.try_claim(f"k-{n}")),
}


async def _contended(target: Any, operation: Operation, n: int) -> None:
    lock = target._lock.get()
    async with lock:
        waiting = asyncio.create_task(operation(target, n))
        await asyncio.sleep(0)  # the operation waits for the lock, binding it to this loop
        assert not waiting.done()
    await waiting


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_contended_lock_works_in_consecutive_event_loops(case: str) -> None:
    factory, operation = CASES[case]
    target = factory()

    asyncio.run(_contended(target, operation, 1))
    asyncio.run(_contended(target, operation, 2))
