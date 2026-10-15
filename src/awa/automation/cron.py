"""Cron expressions: a dependency-free, time-zone-aware parser."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import UTC, date, datetime, timedelta, tzinfo

from awa.automation._time import as_utc, zone
from awa.errors import InvalidCronError

_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
_MONTHS = {
    name: index
    for index, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"),
        start=1,
    )
}
_WEEKDAYS = {
    name: index for index, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))
}
_FIELDS: tuple[tuple[str, int, int, dict[str, int]], ...] = (
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, _MONTHS),
    ("day of week", 0, 7, _WEEKDAYS),
)
# Enough for any valid expression (Feb 29 on a given weekday recurs within 28 years).
_MAX_SEARCH_DAYS = 366 * 29


class CronExpression:
    """A parsed cron expression bound to a time zone.

    Five fields (minute 0-59, hour 0-23, day of month 1-31, month 1-12 or `JAN`-`DEC`, day of week
    0-7 or `SUN`-`SAT`, with lists, ranges and steps such as `*/15`) or the macros `@hourly`,
    `@daily`/`@midnight`, `@weekly`, `@monthly`, `@yearly`/`@annually`. When both day fields are
    restricted, either matches. Wall-clock times that are skipped by a daylight-saving jump fire at
    the same distance after it; repeated times fire once, at their first occurrence.

    Args:
        timezone: IANA time zone name or `tzinfo`; default UTC.

    Raises:
        InvalidCronError: If the expression or the time zone is invalid.

    Example:
        ```python
        cron = CronExpression("0 6 * * MON-FRI", timezone="Europe/Berlin")
        cron.next_after(datetime(2026, 3, 27, 12, tzinfo=UTC))  # Monday 30 March, 06:00 CEST
        ```
    """

    def __init__(self, expression: str, *, timezone: str | tzinfo | None = None) -> None:
        if not isinstance(expression, str) or not expression.strip():
            raise InvalidCronError("A cron expression must be a non-empty string")
        text = " ".join(expression.split())
        fields = _MACROS.get(text.lower(), text).split(" ")
        if len(fields) != 5:
            raise InvalidCronError(
                f"Cron expression {expression!r} must have 5 fields (minute hour day-of-month "
                f"month day-of-week) or be a macro such as @daily; got {len(fields)}"
            )
        parsed = [
            _parse_field(value, name, low, high, names)
            for value, (name, low, high, names) in zip(fields, _FIELDS, strict=True)
        ]
        self.expression = text
        self.timezone = zone(timezone)
        self._minutes = sorted(parsed[0])
        self._hours = sorted(parsed[1])
        self._days = frozenset(parsed[2])
        self._months = frozenset(parsed[3])
        self._weekdays = frozenset(0 if day == 7 else day for day in parsed[4])
        # As in classic cron, a field starting with `*` (also `*/2`) does not restrict.
        self._dom_restricted = not fields[2].startswith("*")
        self._dow_restricted = not fields[4].startswith("*")

    def __repr__(self) -> str:
        return f"CronExpression({self.expression!r}, timezone={str(self.timezone)!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CronExpression):
            return NotImplemented
        return self.expression == other.expression and str(self.timezone) == str(other.timezone)

    def __hash__(self) -> int:
        return hash((self.expression, str(self.timezone)))

    def matches_day(self, day: date) -> bool:
        """Whether the expression fires on `day` (a local calendar date)."""

        if day.month not in self._months:
            return False
        weekday = (day.weekday() + 1) % 7  # cron counts from Sunday = 0
        dom = day.day in self._days
        dow = weekday in self._weekdays
        if self._dom_restricted and self._dow_restricted:
            return dom or dow
        return dom and dow

    def next_after(self, after: datetime) -> datetime:
        """The first fire time strictly after `after` (naive is UTC), as an aware UTC datetime.

        Raises:
            InvalidCronError: If the expression never fires (for example `0 0 31 2 *`).
        """

        after_utc = as_utc(after)
        local_day = after_utc.astimezone(self.timezone).date() - timedelta(days=1)
        for offset in range(_MAX_SEARCH_DAYS):
            day = local_day + timedelta(days=offset)
            if not self.matches_day(day):
                continue
            instant = self._first_instant(day, after_utc)
            if instant is not None:
                return instant
        raise InvalidCronError(f"Cron expression {self.expression!r} never fires")

    def _first_instant(self, day: date, after: datetime) -> datetime | None:
        """The earliest fire time on the local `day` strictly after `after` (aware UTC).

        A wall time `w` fires at `w - offset`, with the offset between the day's lowest and
        highest, so only wall times above `after + lowest` can qualify, and the search ends
        once `w - highest` cannot beat the best instant found.
        """

        start = datetime(day.year, day.month, day.day)
        offsets = [
            wall - _to_utc(wall, self.timezone).replace(tzinfo=None)
            for wall in (start, start + timedelta(hours=23, minutes=59))
        ]
        lowest, highest = min(offsets), max(offsets)
        floor = after.replace(tzinfo=None) + lowest
        if floor.date() > day:
            return None
        first_hour, first_minute = (floor.hour, floor.minute) if floor.date() == day else (-1, -1)
        best: datetime | None = None
        for hour in self._hours[bisect_left(self._hours, first_hour) :]:
            skip = bisect_right(self._minutes, first_minute) if hour == first_hour else 0
            for minute in self._minutes[skip:]:
                wall = start.replace(hour=hour, minute=minute)
                if best is not None and wall - highest >= best.replace(tzinfo=None):
                    return best
                instant = _to_utc(wall, self.timezone)
                if instant > after and (best is None or instant < best):
                    best = instant
        return best


def _to_utc(wall: datetime, tz: tzinfo) -> datetime:
    """The instant of a wall-clock time: the first one when ambiguous; after the gap when missing."""

    if tz is UTC:
        return wall.replace(tzinfo=UTC)
    # fold=0 picks the first of two ambiguous instants and, for a time inside a
    # forward jump, applies the offset from before the jump (02:30 -> 03:30).
    return wall.replace(tzinfo=tz, fold=0).astimezone(UTC)


def _parse_field(text: str, name: str, low: int, high: int, names: dict[str, int]) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise InvalidCronError(f"Empty list item in the {name} field {text!r}")
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not _is_number(step_text) or int(step_text) < 1:
                raise InvalidCronError(f"Invalid step {step_text!r} in the {name} field")
            step = int(step_text)
        if base == "*":
            start, stop = low, high
            if name == "day of week" and not step_text:
                stop = 6
        elif "-" in base:
            left, _, right = base.partition("-")
            start, stop = (
                _value(left, name, low, high, names),
                _value(right, name, low, high, names),
            )
            if start > stop:
                raise InvalidCronError(
                    f"Range {base!r} in the {name} field runs backwards; write it as two ranges"
                )
        else:
            start = _value(base, name, low, high, names)
            stop = high if step_text else start
        values.update(range(start, stop + 1, step))
    return values


def _value(text: str, name: str, low: int, high: int, names: dict[str, int]) -> int:
    key = text.upper()
    if key in names:
        return names[key]
    if not _is_number(text):
        raise InvalidCronError(f"Invalid value {text!r} in the {name} field")
    value = int(text)
    if not low <= value <= high:
        raise InvalidCronError(f"Value {value} in the {name} field is outside {low}-{high}")
    return value


def _is_number(text: str) -> bool:
    # `str.isdigit` also accepts digits `int` cannot read, such as "²".
    return text.isascii() and text.isdigit()


__all__ = ["CronExpression"]
