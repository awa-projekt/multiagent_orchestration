"""Wait for a condition instead of sleeping a fixed time (importable from every test directory)."""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


def _factor() -> float:
    try:
        return max(1.0, float(os.environ.get("AWA_TEST_TIMEOUT_FACTOR", "1")))
    except ValueError:
        return 1.0


TIMEOUT_FACTOR = _factor()
"""`AWA_TEST_TIMEOUT_FACTOR` (at least 1): multiplies the bounds below on a slow or busy machine."""

SUBPROCESS_TIMEOUT = 120.0 * TIMEOUT_FACTOR
"""Upper bound (seconds) of a test subprocess that imports awa; a healthy run takes a few."""

SLOW_SUBPROCESS_TIMEOUT = 300.0 * TIMEOUT_FACTOR
"""Upper bound (seconds) of a test subprocess that builds a project or an environment."""


def poll(
    read: Callable[[], T],
    done: Callable[[T], Any] = bool,
    *,
    timeout: float = 15.0,
    interval: float = 0.02,
    what: str = "the condition",
) -> T:
    """Call `read()` until `done(value)` holds and return that value; fail after `timeout`."""

    deadline = time.monotonic() + timeout
    while True:
        value = read()
        if done(value):
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"{what} did not hold within {timeout}s; last value: {value!r}")
        time.sleep(interval)


async def until(
    condition: Callable[[], bool | Awaitable[bool]], timeout: float = 10.0, interval: float = 0.01
) -> None:
    """Await until `condition()` (sync or async) holds; `TimeoutError` after `timeout`."""

    async with asyncio.timeout(timeout):
        while True:
            held = condition()
            if await held if inspect.isawaitable(held) else held:
                return
            await asyncio.sleep(interval)
