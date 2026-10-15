"""The clock backend protocol: what the library needs of time, so that a test can drive it.

Code that reads time or waits takes a `Clock` and defaults to `SystemClock`; a test passes
`awa.testing.FakeClock`.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Time as the library uses it: the wall clock, a monotonic reading and waiting."""

    def now(self) -> datetime:
        """The current time as an aware `datetime`."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards; only differences mean something."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Wait the given number of seconds (a fake clock advances instead of waiting)."""
        ...


class SystemClock:
    """The real clock: UTC wall time, `time.monotonic` and `asyncio.sleep`."""

    def now(self) -> datetime:
        """The current time in UTC."""

        return datetime.now(UTC)

    def monotonic(self) -> float:
        """`time.monotonic()`."""

        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        """`asyncio.sleep` for a non-negative number of seconds."""

        await asyncio.sleep(max(0.0, seconds))


def as_clock(clock: Clock | None = None) -> Clock:
    """Return `clock`, or the `SystemClock` for `None`."""

    return clock if clock is not None else SystemClock()


__all__ = ["Clock", "SystemClock", "as_clock"]
