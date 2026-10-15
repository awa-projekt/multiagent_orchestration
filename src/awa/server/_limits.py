"""Request rate limits per principal: `RateLimit`, `BaseRateLimiter`, `TokenBucketRateLimiter`."""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from awa._internal.aio import LoopLocal


@dataclass(frozen=True)
class RateLimit:
    """At most `requests` requests per `per` seconds, bursts up to `burst` (default `requests`).

    Example:
        ```python
        app = create_app(systems, authenticator=auth, rate_limit=RateLimit(60, per=60))
        ```
    """

    requests: int
    per: float = 60.0
    burst: int | None = None

    def __post_init__(self) -> None:
        if self.requests < 1:
            raise ValueError("RateLimit.requests must be at least 1")
        if self.per <= 0:
            raise ValueError("RateLimit.per must be positive")
        if self.burst is not None and self.burst < 1:
            raise ValueError("RateLimit.burst must be at least 1")

    @property
    def capacity(self) -> int:
        """The bucket size: `burst`, or `requests`."""

        return self.burst if self.burst is not None else self.requests

    @property
    def rate(self) -> float:
        """Requests regained per second."""

        return self.requests / self.per


class BaseRateLimiter(ABC):
    """Decides whether a caller may send another request now.

    Implement it over a shared store (such as Redis) to limit across several
    workers; `TokenBucketRateLimiter` limits within one process.
    """

    @abstractmethod
    async def acquire(self, key: str) -> float | None:
        """Take one request from `key`'s allowance: `None`, or the seconds until it is allowed."""


class TokenBucketRateLimiter(BaseRateLimiter):
    """A token bucket per key, kept in this process.

    Beyond `max_keys` the least recently used buckets are dropped (and start full again).
    """

    def __init__(
        self,
        limit: RateLimit,
        *,
        max_keys: int = 100_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_keys < 1:
            raise ValueError("max_keys must be at least 1")
        self._limit = limit
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock: LoopLocal[asyncio.Lock] = LoopLocal(asyncio.Lock)  # one per event loop

    @property
    def limit(self) -> RateLimit:
        """The allowance per key."""

        return self._limit

    async def acquire(self, key: str) -> float | None:
        """Take a token from `key`'s bucket; return the wait time when it is empty."""

        async with self._lock.get():
            now = self._clock()
            capacity = float(self._limit.capacity)
            tokens, stamp = self._buckets.get(key, (capacity, now))
            tokens = min(capacity, tokens + (now - stamp) * self._limit.rate)
            wait: float | None = None
            if tokens >= 1.0:
                tokens -= 1.0
            else:
                wait = (1.0 - tokens) / self._limit.rate
            self._buckets[key] = (tokens, now)
            self._buckets.move_to_end(key)
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
            return wait


def as_rate_limiter(value: RateLimit | BaseRateLimiter | None) -> BaseRateLimiter | None:
    """Turn a `RateLimit` into a `TokenBucketRateLimiter`; pass limiters through."""

    if value is None or isinstance(value, BaseRateLimiter):
        return value
    if isinstance(value, RateLimit):
        return TokenBucketRateLimiter(value)
    raise TypeError(f"Expected a RateLimit or BaseRateLimiter, got {type(value).__name__}")
