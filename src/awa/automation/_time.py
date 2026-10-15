"""Clocks, durations and time zones shared by the automation package."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TypeAlias

from awa._internal.util import DurationLike, to_timedelta
from awa.errors import InvalidCronError

Sleep: TypeAlias = Callable[[float], Awaitable[None]]
"""Waits the given number of seconds (`asyncio.sleep` outside tests)."""


def utcnow() -> datetime:
    """The current time, aware, in UTC."""

    return datetime.now(UTC)


async def real_sleep(seconds: float) -> None:
    """`asyncio.sleep` for a non-negative number of seconds (the default `Sleep`)."""

    await asyncio.sleep(max(0.0, seconds))


def require_timedelta(value: DurationLike, *, what: str = "duration") -> timedelta:
    """Like `to_timedelta`, for a value that must be given."""

    result = to_timedelta(value, what=what)
    assert result is not None
    return result


def as_utc(value: datetime) -> datetime:
    """Return `value` as an aware UTC datetime; naive values are taken as UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def zone(name: str | tzinfo | None) -> tzinfo:
    """Resolve an IANA time zone name (or `tzinfo`); `None` and `"UTC"` are UTC.

    Raises:
        InvalidCronError: If the zone is unknown.
    """

    if name is None:
        return UTC
    if isinstance(name, tzinfo):
        return name
    if name.upper() in {"UTC", "Z", "ETC/UTC"}:
        return UTC
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidCronError(
            f"Unknown time zone {name!r}; on Windows install the time zone data with "
            "`pip install tzdata`"
        ) from exc


def iso(value: datetime | None) -> str | None:
    """ISO 8601 text of `value` (UTC), or `None`."""

    return as_utc(value).isoformat() if value is not None else None
