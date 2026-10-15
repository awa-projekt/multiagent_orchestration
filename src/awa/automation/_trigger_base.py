"""The `Trigger` protocol, `TriggerContext`, `BaseTrigger` and `PollingTrigger`."""

from __future__ import annotations

import asyncio
import logging
import random
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from awa._internal.logging import get_logger
from awa._internal.util import DurationLike
from awa.automation._time import Sleep, as_utc, real_sleep, require_timedelta
from awa.automation.models import TriggerEvent
from awa.automation.templates import TemplateLike
from awa.errors import ConfigurationError
from awa.runtime.clock import Clock, SystemClock

if TYPE_CHECKING:
    from awa.automation.models import ProcessRun

_logger = get_logger("automation.triggers")

MAX_BACKOFF_SECONDS = 15 * 60
"""Longest wait between two polls of a source that keeps failing."""

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


class RunEndedError(Exception):
    """The run an event started did not succeed: what `BaseTrigger.settle` passes to `ack`."""

    def __init__(self, run: ProcessRun) -> None:
        self.run = run
        detail = f": {run.error}" if run.error else ""
        super().__init__(f"run {run.id} ended {run.status}{detail}")


class StateStore(Protocol):
    """The part of a `RunStore` triggers use to keep small values (a schedule's last tick)."""

    async def get_state(self, key: str) -> str | None:
        """Return the value of `key`."""
        ...

    async def compare_and_set_state(self, key: str, expected: str | None, value: str) -> bool:
        """Set `key` to `value` if it holds `expected`."""
        ...


@dataclass
class TriggerContext:
    """What a trigger gets from the worker that runs it.

    Attributes:
        state: Where the trigger may keep small values, shared by every worker of the automation.
        stopping: Set when the worker shuts down; `events` should return.
    """

    automation: str
    state: StateStore
    clock: Clock = field(default_factory=SystemClock)
    sleep: Sleep = real_sleep
    stopping: asyncio.Event = field(default_factory=asyncio.Event)

    async def pause(self, seconds: float) -> bool:
        """Sleep up to `seconds`, returning early (and `False`) once the worker is stopping."""

        if self.stopping.is_set():
            return False
        sleeper = asyncio.ensure_future(self.sleep(max(0.0, seconds)))
        stopper = asyncio.ensure_future(self.stopping.wait())
        try:
            await asyncio.wait({sleeper, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleeper, stopper):
                task.cancel()
            await asyncio.gather(sleeper, stopper, return_exceptions=True)
        return not self.stopping.is_set()


@runtime_checkable
class Trigger(Protocol):
    """Anything with a `name` and an async `events(context)` iterator of `TriggerEvent`s.

    Optional: `input`, `thread_id` and `dedup` template overrides, an async `ack(event, error)`
    called once the event was recorded (`error` is `None`) or refused, so a file is moved or a mail
    marked read only then, and an async `ready(context)` for `Worker.run_once`.
    """

    @property
    def name(self) -> str:
        """Name of the trigger, unique within its automation."""
        ...

    def events(self, context: TriggerContext) -> AsyncIterator[TriggerEvent]:
        """Yield events until `context.stopping` is set."""
        ...


class BaseTrigger(ABC):
    """Base class of the built-in triggers: a name, template overrides and a no-op `ack`.

    Args:
        name: Name of the trigger (letters, digits, `_`, `.`, `-`).
    """

    def __init__(
        self,
        name: str,
        *,
        input: TemplateLike | None = None,
        thread_id: TemplateLike | None = None,
        dedup: TemplateLike | None = None,
    ) -> None:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ConfigurationError(
                f"Trigger name {name!r} must be 1-100 letters, digits, '_', '.' or '-'"
            )
        self._name = name
        self.input = input
        self.thread_id = thread_id
        self.dedup = dedup

    @property
    def name(self) -> str:
        """Name of the trigger."""

        return self._name

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._name!r})"

    @abstractmethod
    def events(self, context: TriggerContext) -> AsyncIterator[TriggerEvent]:
        """Yield events until `context.stopping` is set."""

    ack_on_end: ClassVar[bool] = True
    """`True`: a worker acknowledges an event when its run ends (`settle`), so the input of a
    failed run is not handled as done. `False`: acknowledged as soon as the run is queued."""

    async def ack(self, event: TriggerEvent, error: BaseException | None) -> None:  # noqa: B027
        """Called after the automation refused `event` (`error`), or with `error=None` once it
        is handled (see `ack_on_end` and `settle`)."""

    async def settle(self, event: TriggerEvent, run: ProcessRun) -> None:
        """Acknowledge `event` when its run ends: `None` on success, else `RunEndedError`."""

        if run.status == "waiting":
            return
        await self.ack(event, None if run.status == "succeeded" else RunEndedError(run))

    def required_env(self) -> tuple[str, ...]:
        """The environment variables this trigger reads (checked before a worker starts)."""

        return ()

    async def ready(self, context: TriggerContext) -> list[TriggerEvent]:
        """The events ready now: one pass of `events`, up to its first pause (`run_once`)."""

        passed = asyncio.Event()

        async def end_pass(_: float) -> None:
            passed.set()

        one_pass = replace(context, sleep=end_pass, stopping=passed)
        return [event async for event in self.events(one_pass)]

    def describe(self) -> dict[str, Any]:
        """A JSON description for listings (`awa trigger list`)."""

        return {"name": self._name, "type": type(self).__name__}


class PollingTrigger(BaseTrigger):
    """A trigger that looks for new work every `poll_interval` (`poll` returns the events)."""

    def __init__(
        self,
        name: str,
        *,
        poll_interval: DurationLike,
        input: TemplateLike | None = None,
        thread_id: TemplateLike | None = None,
        dedup: TemplateLike | None = None,
    ) -> None:
        super().__init__(name, input=input, thread_id=thread_id, dedup=dedup)
        self.poll_interval = require_timedelta(poll_interval, what="poll_interval")
        self.stopped: str | None = None
        """Why the trigger stopped polling (a missing setting), or `None` while it runs."""

    @abstractmethod
    async def poll(self, now: datetime) -> list[TriggerEvent]:
        """Return the events that are ready now."""

    async def events(self, context: TriggerContext) -> AsyncIterator[TriggerEvent]:
        """Poll, yield what is ready, sleep; until the worker stops."""

        failures = 0
        last = ""
        while not context.stopping.is_set():
            try:
                found = await self.poll(as_utc(context.clock.now()))
            except ConfigurationError as exc:
                # A missing setting does not fix itself: stop this trigger, keep the others.
                self.stopped = str(exc)
                _logger.error("Trigger %s/%s stopped: %s", context.automation, self.name, exc)
                return
            except Exception as exc:  # noqa: BLE001 - a failing source must not stop the worker
                failures += 1
                message = f"{type(exc).__name__}: {exc}"
                if message != last or failures % 10 == 0:
                    _logger.warning(
                        "Trigger %s/%s: poll failed (%d in a row): %s",
                        context.automation,
                        self.name,
                        failures,
                        message,
                        exc_info=_logger.isEnabledFor(logging.DEBUG),
                    )
                last = message
                if not await context.pause(self.backoff(failures)):
                    return
                continue
            if failures:
                _logger.warning(
                    "Trigger %s/%s recovered after %d failed poll(s)",
                    context.automation,
                    self.name,
                    failures,
                )
                failures, last = 0, ""
            for event in found:
                yield event
            if not await context.pause(self.poll_interval.total_seconds()):
                return

    def backoff(self, failures: int, *, jitter: float | None = None) -> float:
        """Seconds to wait after `failures` failed polls: doubling, capped and jittered.

        `jitter` is a number from -1 to 1 (default random); jitter applies from the second failure
        on.
        """

        base = self.poll_interval.total_seconds() * 2 ** min(failures - 1, 30)
        if failures <= 1:
            return float(min(base, MAX_BACKOFF_SECONDS))
        spread = random.uniform(-1.0, 1.0) if jitter is None else jitter  # noqa: S311 - not security
        return float(min(base, MAX_BACKOFF_SECONDS) * (1 + 0.2 * spread))
