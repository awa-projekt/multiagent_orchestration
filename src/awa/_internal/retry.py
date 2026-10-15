"""Retry arithmetic shared by every package: backoff delays, `Retry-After`, transient errors.

Nothing here sleeps or loops: callers keep their own retry loops.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from datetime import UTC
from functools import cache
from typing import Any

from awa.errors import AwaError


def backoff_delay(
    attempt: int,
    *,
    initial: float,
    factor: float = 2.0,
    cap: float | None = None,
    jitter: float = 0.0,
    rand: Callable[[], float] | None = None,
) -> float:
    """Seconds to wait before retry number `attempt + 1`: `initial * factor**attempt`, never negative.

    Args:
        attempt: Retries made so far (0 before the first retry); negative counts as 0.
        cap: Largest delay returned, jitter included; `None` for no cap.
        jitter: Spread as a fraction: the delay is multiplied by a uniform value in
            `[1 - jitter, 1 + jitter)` (0 for none, at most 1).
        rand: Source of uniform values in `[0, 1)` for the jitter.

    Example:
        ```python
        backoff_delay(3, initial=0.5, cap=2.0)  # 2.0
        ```
    """

    if initial < 0 or factor < 0:
        raise ValueError("initial and factor must be >= 0")
    if not 0.0 <= jitter <= 1.0:
        raise ValueError("jitter must be between 0 and 1")
    if cap is not None and cap < 0:
        raise ValueError("cap must be >= 0")
    exponent = max(0, attempt)
    try:
        delay = float(initial * factor**exponent)
    except OverflowError:
        delay = math.inf
    if jitter and delay > 0 and math.isfinite(delay):
        draw = (random.random if rand is None else rand)()  # noqa: S311 - not security relevant
        delay *= 1.0 - jitter + 2.0 * jitter * draw
    if cap is not None:
        delay = min(delay, cap)
    return max(0.0, delay)


def parse_retry_after(value: Any, now: float | None = None) -> float | None:
    """Seconds a `Retry-After` value asks to wait, or `None` if it is missing or invalid.

    Accepts seconds (`"120"`, `1.5`) or an HTTP date measured from `now`; the past gives 0.
    """

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return max(0.0, float(value)) if math.isfinite(value) else None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if math.isfinite(seconds) else None
    from email.utils import parsedate_to_datetime

    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if moment.tzinfo is None:  # "-0000": the date is UTC
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, moment.timestamp() - (time.time() if now is None else now))


def retry_after_from_headers(headers: Any, *, now: float | None = None) -> float | None:
    """The wait the response headers ask for: `retry-after-ms`, else `Retry-After`.

    `headers` is any mapping-like object with `get`; names are tried as given and title-cased.
    """

    if headers is None or not hasattr(headers, "get"):
        return None
    milliseconds = parse_retry_after(_header(headers, "retry-after-ms"))
    if milliseconds is not None:
        return milliseconds / 1000
    return parse_retry_after(_header(headers, "retry-after"), now)


def _header(headers: Any, name: str) -> Any:
    value = headers.get(name)
    return headers.get(name.title()) if value is None else value


@cache
def _langgraph_retry_on() -> Any:
    from awa._internal.upstream.graph import RetryPolicy

    return RetryPolicy().retry_on


def retry_on_transient(exc: Exception) -> bool:
    """Whether `exc` looks transient and is worth retrying.

    awa's deliberate errors are never retried except timeouts; LangGraph's `NodeTimeoutError` and a
    tool's own `ToolException` are not retried either, every other timeout is. Everything else follows
    LangGraph's default rule, which skips programming errors.
    """

    if isinstance(exc, AwaError):
        return isinstance(exc, TimeoutError)
    from awa._internal.upstream.graph import NodeTimeoutError
    from awa._internal.upstream.tools import ToolException

    if isinstance(exc, NodeTimeoutError | ToolException):
        return False
    if isinstance(exc, TimeoutError):
        return True
    default = _langgraph_retry_on()
    if callable(default):
        return bool(default(exc))
    return isinstance(exc, tuple(default))
