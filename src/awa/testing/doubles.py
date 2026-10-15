"""Test doubles for the library's own ports: time (`FakeClock`) and identifiers (`FixedIds`)."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

_DEFAULT_START = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


class FakeClock:
    """A clock that moves only when told to; a `Clock` and a function returning the time.

    `sleep` advances the clock instead of waiting; `clock()` is `clock.now()`.

    Args:
        start: The first reading; an aware datetime (default: Monday 2026-03-02 09:00 UTC).

    Example:
        ```python
        clock = FakeClock()
        queue = InMemoryEventQueue(clock=clock)
        await queue.send({"id": 1}, delay="10m")
        clock.advance(minutes=10)          # the message is now visible
        ```
    """

    def __init__(self, start: datetime | None = None) -> None:
        start = _DEFAULT_START if start is None else start
        if start.tzinfo is None:
            raise ValueError("a FakeClock starts at an aware datetime (give it a time zone)")
        self._now = start.astimezone(UTC)
        self._elapsed = 0.0

    def __repr__(self) -> str:
        return f"FakeClock({self._now.isoformat()})"

    def now(self) -> datetime:
        """The current reading."""

        return self._now

    def __call__(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        """Seconds advanced since the clock was made."""

        return self._elapsed

    def advance(self, delta: timedelta | None = None, **parts: float) -> datetime:
        """Move forward by `delta`, or by the parts of a `timedelta` (`advance(minutes=5)`); never back."""

        step = (delta or timedelta()) + timedelta(**parts)
        if step < timedelta():
            raise ValueError("a clock does not go backwards")
        self._now += step
        self._elapsed += step.total_seconds()
        return self._now

    async def sleep(self, seconds: float) -> None:
        """Advance by `seconds` (at least a millisecond, so a loop that sleeps for 0 still moves)."""

        self.advance(seconds=max(seconds, 0.001))


class FixedIds:
    """Predictable identifiers counted per prefix (`evt_000000000001`, ...), shaped like the real ones."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()

    def new_id(self, prefix: str) -> str:
        """The next identifier for `prefix`."""

        self._counts[prefix] += 1
        return f"{prefix}_{self._counts[prefix]:012x}"


__all__ = ["FakeClock", "FixedIds"]
