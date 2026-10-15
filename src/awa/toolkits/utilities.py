"""Utility tools that make exact what models get wrong: arithmetic and dates; none goes outside."""

from __future__ import annotations

import calendar
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field

from awa._internal.upstream.tools import BaseTool, ToolException
from awa.errors import ConfigurationError
from awa.kernel.conditions import calculate
from awa.toolkits._common import ToolArgs, ToolSpecs, selected, tool_json, toolkit_tools

UTILITY_TOOLS = ("calculator", "datetime_now", "date_math")
"""Names of the tools `utility_toolkit` builds, in order."""

_OFFSET = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_OUT_OF_RANGE = "the resulting date is outside the supported date range (years 1 to 9999); use a smaller duration"


class CalculatorArgs(ToolArgs):
    expression: str = Field(
        description="Arithmetic such as '(1200 * 0.19) + 15', '2 ** 10', 'sqrt(2) * pi' or "
        "'round(1234.5678, 2)'. Operators: + - * / // % **; functions: abs, round, min, max, "
        "sqrt, log, log10, log2, exp, sin, cos, tan, asin, acos, atan, floor, ceil, hypot; "
        "constants: pi, e, tau."
    )


def _calculator(args: CalculatorArgs) -> str:
    result = calculate(args.expression)
    if isinstance(result, float):
        return repr(int(result)) if result.is_integer() and abs(result) < 1e15 else repr(result)
    return str(result)


def resolve_timezone(name: str) -> tzinfo:
    """Return the time zone `name`: an IANA name, `UTC`, or an offset like `+02:00`.

    Raises:
        ToolException: If the zone is unknown.
    """

    text = name.strip()
    if text.upper() in ("UTC", "Z", "GMT"):
        return UTC
    match = _OFFSET.match(text)
    if match:
        sign, hours, minutes = match.group(1), int(match.group(2)), int(match.group(3) or 0)
        if hours > 23 or minutes > 59:
            raise ToolException(f"invalid UTC offset {name!r}")
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(-delta if sign == "-" else delta)
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        raise ToolException(
            f"unknown time zone {name!r}; use an IANA name such as 'Europe/Berlin' "
            "(on Windows install the 'tzdata' package), 'UTC' or an offset like '+02:00'"
        ) from None


class DatetimeNowArgs(ToolArgs):
    timezone: str | None = Field(
        default=None, description="IANA time zone ('Europe/Berlin'), 'UTC' or '+02:00'."
    )


class DateMathArgs(ToolArgs):
    operation: Literal[
        "add", "subtract", "difference", "add_business_days", "business_days_between", "info"
    ] = Field(
        description="add/subtract a duration; difference between two dates; "
        "add_business_days; business_days_between two dates (start included, end excluded); "
        "info about one date."
    )
    date: str = Field(
        description="ISO date or date-time ('2026-03-31', '2026-03-31T14:00'), or 'today'."
    )
    other_date: str | None = Field(
        default=None, description="Second date for difference and business_days_between."
    )
    years: int = Field(default=0, description="Years to add or subtract.")
    months: int = Field(default=0, description="Months (the day is clamped to the month's end).")
    weeks: int = Field(default=0, description="Weeks.")
    days: int = Field(default=0, description="Days (business days for add_business_days).")
    hours: int = Field(default=0, description="Hours.")
    minutes: int = Field(default=0, description="Minutes.")
    holidays: list[str] | None = Field(
        default=None, description="Extra non-working ISO dates for the business-day operations."
    )


class _Dates:
    def __init__(self, zone: str, holidays: Sequence[str], weekend: Sequence[int]) -> None:
        self.zone = zone
        self.holidays = frozenset(self._day(text) for text in holidays)
        self.weekend = frozenset(weekend)

    @staticmethod
    def _day(text: str) -> date:
        try:
            return date.fromisoformat(text.strip()[:10])
        except ValueError:
            raise ToolException(f"invalid date {text!r}; use YYYY-MM-DD") from None

    def _parse(self, text: str) -> datetime:
        value = text.strip()
        zone = resolve_timezone(self.zone)
        if value.lower() in ("today", "now"):
            now = datetime.now(zone)
            return (
                now
                if value.lower() == "now"
                else now.replace(hour=0, minute=0, second=0, microsecond=0)
            )
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ToolException(
                f"invalid date {text!r}; use ISO format such as 2026-03-31"
            ) from None
        return parsed

    def now(self, args: DatetimeNowArgs) -> str:
        zone = resolve_timezone(args.timezone or self.zone)
        moment = datetime.now(zone)
        iso = moment.isocalendar()
        return tool_json(
            {
                "datetime": moment.isoformat(timespec="seconds"),
                "date": moment.date().isoformat(),
                "time": moment.strftime("%H:%M:%S"),
                "timezone": args.timezone or self.zone,
                "utc_offset": moment.strftime("%z"),
                "weekday": calendar.day_name[moment.weekday()],
                "iso_week": iso.week,
                "unix_timestamp": int(moment.timestamp()),
            }
        )

    def _business_day(self, day: date, extra: frozenset[date]) -> bool:
        return day.weekday() not in self.weekend and day not in self.holidays and day not in extra

    def math(self, args: DateMathArgs) -> str:
        start = self._parse(args.date)
        extra = frozenset(self._day(text) for text in args.holidays or ())
        # A clock time follows the date after `T` or a space (`today` has none).
        has_time = args.date.strip().lower() == "now" or bool(_CLOCK.search(args.date))
        if args.operation in ("add", "subtract"):
            sign = 1 if args.operation == "add" else -1
            try:
                moved = _add_months(start, sign * (args.years * 12 + args.months))
                moved += sign * timedelta(
                    weeks=args.weeks, days=args.days, hours=args.hours, minutes=args.minutes
                )
            except OverflowError:
                raise ToolException(_OUT_OF_RANGE) from None
            shows_time = has_time or args.hours or args.minutes
            return tool_json(self._describe(moved, bool(shows_time), extra))
        if args.operation == "add_business_days":
            day = start.date()
            step = 1 if args.days >= 0 else -1
            remaining = abs(args.days)
            if remaining > 100_000:
                raise ToolException("too many business days")
            while remaining:
                day += timedelta(days=step)
                if self._business_day(day, extra):
                    remaining -= 1
            return tool_json(self._describe(datetime.combine(day, start.timetz()), has_time, extra))
        if args.operation == "info":
            return tool_json(self._describe(start, has_time, extra))
        if args.other_date is None:
            raise ToolException(f"{args.operation} needs other_date")
        end = self._parse(args.other_date)
        if args.operation == "difference":
            if (start.tzinfo is None) != (end.tzinfo is None):
                start, end = start.replace(tzinfo=None), end.replace(tzinfo=None)
            delta = end - start
            return tool_json(
                {
                    "days": delta.days,
                    "total_hours": round(delta.total_seconds() / 3600, 4),
                    "weeks": round(delta.days / 7, 4),
                    "months": _months_between(start.date(), end.date()),
                }
            )
        first, last = sorted((start.date(), end.date()))
        if (last - first).days > 400_000:
            raise ToolException("the dates are too far apart")
        count = sum(
            1
            for offset in range((last - first).days)
            if self._business_day(first + timedelta(days=offset), extra)
        )
        return tool_json({"business_days": count if start.date() <= end.date() else -count})

    def _describe(
        self, moment: datetime, with_time: bool, extra: frozenset[date]
    ) -> dict[str, Any]:
        day = moment.date()
        iso = day.isocalendar()
        return {
            "result": moment.isoformat(timespec="minutes") if with_time else day.isoformat(),
            "weekday": calendar.day_name[day.weekday()],
            "iso_week": iso.week,
            "quarter": (day.month - 1) // 3 + 1,
            "day_of_year": day.timetuple().tm_yday,
            "days_in_month": calendar.monthrange(day.year, day.month)[1],
            "is_business_day": self._business_day(day, extra),
        }


_CLOCK = re.compile(r"\d[T ]\d", re.IGNORECASE)


def _add_months(moment: datetime, months: int) -> datetime:
    if not months:
        return moment
    index = moment.year * 12 + moment.month - 1 + months
    year, month = divmod(index, 12)
    if not 1 <= year <= 9999:
        raise ToolException(_OUT_OF_RANGE)
    day = min(moment.day, calendar.monthrange(year, month + 1)[1])
    return moment.replace(year=year, month=month + 1, day=day)


def _months_between(start: date, end: date) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    if months > 0 and end.day < start.day:
        months -= 1
    elif months < 0 and end.day > start.day:
        months += 1
    return months


def utility_toolkit(
    *,
    include: Sequence[str] | None = None,
    default_timezone: str = "UTC",
    holidays: Sequence[str] = (),
    weekend: Sequence[int] = (5, 6),
) -> list[BaseTool]:
    """Build the utility tools (none needs approval or touches the outside world).

    Args:
        include: Build only these tools (names from `UTILITY_TOOLS`).
        default_timezone: Time zone of `datetime_now`, `today` and `now`.
        holidays: Non-working ISO dates for the business-day operations.
        weekend: Weekday numbers that are not business days (Monday is 0).

    Raises:
        ConfigurationError: If the time zone, a holiday or a weekday is invalid, or `include`
            names an unknown tool.

    Example:
        ```python
        [calc] = utility_toolkit(include=["calculator"])
        ```
    """

    names = selected(UTILITY_TOOLS, include, "utilities")
    try:
        resolve_timezone(default_timezone)
        dates = _Dates(default_timezone, holidays, weekend)
    except ToolException as exc:
        raise ConfigurationError(str(exc)) from None
    if any(not 0 <= day <= 6 for day in weekend):
        raise ConfigurationError("weekend days are numbers from 0 (Monday) to 6 (Sunday)")
    specs: ToolSpecs = {
        "calculator": (
            _calculator,
            CalculatorArgs,
            "Evaluate an arithmetic expression exactly. Use it for every calculation "
            "(totals, percentages, taxes, conversions of amounts) instead of computing in "
            "your head.",
        ),
        "datetime_now": (
            dates.now,
            DatetimeNowArgs,
            f"Return the current date and time (default time zone {default_timezone}), "
            "weekday and ISO week. Use it whenever an answer depends on today's date.",
        ),
        "date_math": (
            dates.math,
            DateMathArgs,
            "Calendar arithmetic: add or subtract years/months/weeks/days/hours, the "
            "difference between two dates, adding business days, counting business days "
            "between dates (weekends and holidays excluded), and facts about a date "
            "(weekday, ISO week, quarter). Use it for due dates, deadlines and periods "
            "instead of computing dates in your head. Returns the result as JSON.",
        ),
    }
    return toolkit_tools("utilities", names, specs)


__all__ = ["UTILITY_TOOLS", "resolve_timezone", "utility_toolkit"]
