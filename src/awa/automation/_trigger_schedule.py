"""`Schedule`: cron expressions or fixed intervals, with a missed-run policy."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Literal

from awa._internal.logging import get_logger
from awa._internal.util import DurationLike
from awa.automation._time import as_utc, require_timedelta, zone
from awa.automation._trigger_base import BaseTrigger, TriggerContext
from awa.automation.cron import CronExpression
from awa.automation.models import TriggerEvent
from awa.automation.templates import TemplateLike
from awa.errors import InvalidCronError

_logger = get_logger("automation.triggers")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_POLL_SLEEP = 60.0


CatchUp = Literal["skip", "latest", "all"]
"""Which missed ticks a `Schedule` fires (see `Schedule`)."""


class Schedule(BaseTrigger):
    """Fire on a cron expression or at a fixed interval; each tick starts one run.

    The last tick is kept in the run store, so a restarted worker knows what it missed and several
    workers agree on who fires (compare-and-set). Missed ticks follow `catch_up`: `"latest"` fires
    the latest once, `"all"` each (at most `max_catch_up`), `"skip"` none later than `grace`. A new
    schedule starts from now. Events carry `payload` plus `scheduled_for`, `date` (local),
    `fired_at` and `missed` (the ticks not fired; counted up to `max_catch_up` + 1, so a long
    outage is not walked tick by tick).

    Args:
        cron: Cron expression (see `CronExpression`); or use `every`.
        every: Fixed interval (for example `"15m"`), aligned to `start` (default the Unix epoch).
        timezone: IANA zone for cron expressions and local dates.

    Example:
        ```python
        Schedule("0 6 * * MON-FRI", timezone="Europe/Berlin", name="morning-dunning")
        Schedule(every="15m", name="poll-crm", catch_up="skip")
        ```
    """

    def __init__(
        self,
        cron: str | None = None,
        *,
        every: DurationLike | None = None,
        timezone: str | tzinfo | None = None,
        name: str = "schedule",
        catch_up: CatchUp = "latest",
        grace: DurationLike = timedelta(minutes=1),
        max_catch_up: int = 100,
        start: datetime | None = None,
        payload: Mapping[str, Any] | None = None,
        input: TemplateLike | None = None,
        thread_id: TemplateLike | None = None,
        dedup: TemplateLike | None = None,
    ) -> None:
        super().__init__(name, input=input, thread_id=thread_id, dedup=dedup)
        if (cron is None) == (every is None):
            raise InvalidCronError("A Schedule needs exactly one of cron= or every=")
        if catch_up not in ("skip", "latest", "all"):
            raise InvalidCronError(f"catch_up must be 'skip', 'latest' or 'all', got {catch_up!r}")
        if max_catch_up < 1:
            raise InvalidCronError("max_catch_up must be >= 1")
        self.timezone = zone(timezone)
        self.cron = CronExpression(cron, timezone=self.timezone) if cron is not None else None
        self.every = require_timedelta(every, what="every") if every is not None else None
        if self.every is not None and self.every < timedelta(seconds=1):
            raise InvalidCronError("every must be at least one second")
        self.catch_up: CatchUp = catch_up
        self.grace = require_timedelta(grace, what="grace")
        self.max_catch_up = max_catch_up
        self.start = as_utc(start) if start is not None else _EPOCH
        self.payload = dict(payload or {})

    def describe(self) -> dict[str, Any]:
        """See `BaseTrigger.describe`."""

        return {
            **super().describe(),
            "cron": self.cron.expression if self.cron else None,
            "every": self.every.total_seconds() if self.every else None,
            "timezone": str(self.timezone),
            "catch_up": self.catch_up,
        }

    def next_after(self, moment: datetime) -> datetime:
        """The first tick strictly after `moment` (aware UTC)."""

        moment = as_utc(moment)
        if self.cron is not None:
            return self.cron.next_after(moment)
        assert self.every is not None
        if moment < self.start:
            return self.start
        steps = (moment - self.start) // self.every + 1
        return self.start + self.every * steps

    def ticks_between(
        self, start: datetime, end: datetime, *, limit: int | None = None
    ) -> list[datetime]:
        """Ticks in `(start, end]`; with `limit`, only the most recent `limit` of them."""

        start, end = as_utc(start), as_utc(end)
        if self.every is not None and limit is not None:
            first = self.next_after(start)
            if first > end:
                return []
            count = (end - first) // self.every + 1
            skip = max(0, count - limit)
            first += self.every * skip
            return [first + self.every * i for i in range(count - skip)]
        ticks: list[datetime] = []
        current = start
        while True:
            current = self.next_after(current)
            if current > end:
                return ticks
            ticks.append(current)
            if limit is not None and len(ticks) > limit:
                ticks.pop(0)

    def select(self, due: Sequence[datetime], now: datetime) -> list[datetime]:
        """The ticks to fire out of the `due` ones, by the `catch_up` policy."""

        if not due:
            return []
        if self.catch_up == "all":
            return list(due[-self.max_catch_up :])
        latest = due[-1]
        if self.catch_up == "skip" and as_utc(now) - latest > self.grace:
            return []
        return [latest]

    def event_for(self, tick: datetime, *, now: datetime, missed: int = 0) -> TriggerEvent:
        """The event of one tick."""

        key = f"{self.name}@{tick.isoformat()}"
        return TriggerEvent(
            id=key,
            source=self.name,
            payload={
                **self.payload,
                "scheduled_for": tick.isoformat(),
                "date": tick.astimezone(self.timezone).date().isoformat(),
                "fired_at": as_utc(now).isoformat(),
                "missed": missed,
            },
            received_at=now,
            dedup_key=key,
        )

    async def events(self, context: TriggerContext) -> AsyncIterator[TriggerEvent]:
        """Fire ticks as they come due (see the class documentation)."""

        key = f"schedule:{context.automation}:{self.name}"
        while not context.stopping.is_set():
            now = as_utc(context.clock.now())
            stored = await context.state.get_state(key)
            if stored is None:
                await context.state.compare_and_set_state(key, None, now.isoformat())
                continue
            last = as_utc(datetime.fromisoformat(stored))
            limit = self.max_catch_up + 1 if self.catch_up == "all" else 1
            due = self.ticks_between(last, now, limit=limit)
            if due:
                total = len(self.ticks_between(last, now, limit=self.max_catch_up + 1))
                fire = self.select(due, now)
                if await context.state.compare_and_set_state(key, stored, due[-1].isoformat()):
                    missed = max(0, total - len(fire))
                    if missed:
                        _logger.info(
                            "Schedule %s/%s: %d missed tick(s) not fired (catch_up=%s)",
                            context.automation,
                            self.name,
                            missed,
                            self.catch_up,
                        )
                    for tick in fire:
                        yield self.event_for(tick, now=now, missed=missed)
                continue
            upcoming = self.next_after(max(last, now))
            wait = (upcoming - now).total_seconds()
            if not await context.pause(min(max(wait, 0.0), _MAX_POLL_SLEEP)):
                return
