"""`Worker`: the long-running process that executes automations."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from awa._internal.ids import new_id
from awa._internal.logging import get_logger
from awa._internal.util import DurationLike, to_timedelta
from awa.automation._time import Sleep, real_sleep, require_timedelta
from awa.automation.automation import Automation
from awa.automation.models import TERMINAL_STATUSES, ProcessRun
from awa.automation.triggers import TriggerContext
from awa.errors import ConfigurationError
from awa.runtime.clock import Clock

_logger = get_logger("automation.worker")


@dataclass
class _Awaited:
    automation: Automation
    trigger: Any
    event: Any
    settled: str | None = None


class Worker:
    """Run automations: their triggers, their queued runs and their housekeeping.

    Leases make every attempt run on one of any number of workers sharing a store. `stop()` shuts
    down gracefully: triggers stop, nothing new is claimed, running attempts get `drain_timeout` and
    are then handed back to the queue.

    Args:
        concurrency: Most attempts executing at once in this worker.
        worker_id: Id recorded on leases (default: host, process id and a random suffix).
        lease: Lease length of a claimed run; renewed every third of it.
        drain_timeout: How long `stop()` waits for running attempts (`None`: until they finish).
        triggers: Run the automations' triggers (off for a worker that only executes runs).
        sleep: How triggers wait (tests pass a fake).

    Example:
        ```python
        worker = Worker([invoices, dunning], concurrency=8)
        await worker.run()          # until worker.stop() is called
        ```
    """

    def __init__(
        self,
        automations: Sequence[Automation],
        *,
        concurrency: int = 4,
        worker_id: str | None = None,
        lease: DurationLike = timedelta(minutes=5),
        poll_interval: DurationLike = timedelta(seconds=1),
        tick_interval: DurationLike = timedelta(seconds=15),
        drain_timeout: DurationLike | None = timedelta(seconds=30),
        triggers: bool = True,
        clock: Clock | None = None,
        sleep: Sleep = real_sleep,
    ) -> None:
        from awa.models.replay import refuse_replay

        refuse_replay("The worker")
        if not automations:
            raise ConfigurationError("A Worker needs at least one automation")
        names = [automation.name for automation in automations]
        if len(set(names)) != len(names):
            raise ConfigurationError(f"Automation names must be unique, got {names}")
        if concurrency < 1:
            raise ConfigurationError("concurrency must be >= 1")
        self.automations = list(automations)
        self.concurrency = concurrency
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{new_id('w')[-6:]}"
        self.lease = require_timedelta(lease, what="lease")
        self.poll_interval = require_timedelta(poll_interval, what="poll_interval")
        self.tick_interval = require_timedelta(tick_interval, what="tick_interval")
        self.drain_timeout = to_timedelta(drain_timeout, what="drain_timeout")
        self.run_triggers = triggers
        self._clock = clock
        self._sleep = sleep
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._stop_requested = False
        self._inflight: set[asyncio.Task[ProcessRun]] = set()
        self._settling: set[asyncio.Task[None]] = set()
        self._awaiting: dict[str, _Awaited] = {}
        self._running = False

    def __repr__(self) -> str:
        return f"Worker({self.worker_id!r}, automations={[a.name for a in self.automations]!r})"

    @property
    def active(self) -> int:
        """Attempts executing now."""

        return len(self._inflight)

    def stop(self) -> None:
        """Begin a graceful shutdown (signal-handler safe; before `run`, it stops at once)."""

        self._stop_requested = True
        self._stopping.set()
        self._wake.set()

    async def run(self) -> None:
        """Run until `stop()` is called, then shut down gracefully."""

        if self._running:
            raise ConfigurationError("This worker is already running")
        self._running = True
        # Fresh events for this run's event loop: a worker may run again in another loop.
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        if self._stop_requested:
            self._stopping.set()
        tasks: list[asyncio.Task[Any]] = []
        try:
            if self.run_triggers:
                for automation in self.automations:
                    for trigger in automation.triggers:
                        tasks.append(
                            asyncio.create_task(
                                self._pump(automation, trigger),
                                name=f"awa-trigger-{automation.name}-{trigger.name}",
                            )
                        )
            tasks.append(asyncio.create_task(self._housekeeping(), name="awa-worker-tick"))
            _logger.info(
                "Worker %s started: %s", self.worker_id, ", ".join(a.name for a in self.automations)
            )
            await self._claim_loop()
        finally:
            self._stopping.set()
            if self._inflight:
                _logger.info(
                    "Worker %s: waiting %s for %d running attempt(s)",
                    self.worker_id,
                    "until they finish"
                    if self.drain_timeout is None
                    else f"up to {self.drain_timeout.total_seconds():.0f}s",
                    len(self._inflight),
                )
            await self._finish(self._inflight)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Events the triggers had in hand still get submitted and acknowledged.
            await self._finish(self._settling)
            await self._close_triggers()
            self._running = False
            self._stop_requested = False
            _logger.info("Worker %s stopped", self.worker_id)

    async def run_once(self) -> list[ProcessRun]:
        """One pass, for cron jobs and tests: submit what the triggers have ready now (unless
        `triggers=False`), do the housekeeping, then execute every ready run."""

        if self.run_triggers:
            for automation in self.automations:
                for trigger in automation.triggers:
                    ready = getattr(trigger, "ready", None)
                    try:
                        events = await ready(self._context(automation)) if ready else []
                    except Exception as exc:  # noqa: BLE001 - a failing source must not stop the pass
                        _logger.warning(
                            "Trigger %s failed: %s: %s",
                            trigger.name,
                            type(exc).__name__,
                            exc,
                            exc_info=_logger.isEnabledFor(logging.DEBUG),
                        )
                        events = []
                    for event in events:
                        await self._submit_and_ack(automation, trigger, event)
            await self._close_triggers()
        for automation in self.automations:
            await automation.tick()
        done: list[ProcessRun] = []
        for automation in self.automations:
            done.extend(await automation.run_pending(worker=self.worker_id))
        latest = {run.id: run for run in done}  # a run resumed in the pass ends last
        for run in latest.values():
            await self._settle_run(run)
        return done

    def _context(self, automation: Automation) -> TriggerContext:
        return TriggerContext(
            automation=automation.name,
            state=automation.store,
            clock=self._clock or automation.clock,
            sleep=self._sleep,
            stopping=self._stopping,
        )

    async def _close_triggers(self) -> None:
        for automation in self.automations:
            for trigger in automation.triggers:
                closer = getattr(trigger, "close", None)
                if closer is not None:
                    try:
                        await closer()
                    except Exception:  # noqa: BLE001 - the others must still close
                        _logger.debug("Closing trigger %s failed", trigger.name, exc_info=True)

    async def _pump(self, automation: Automation, trigger: Any) -> None:
        context = self._context(automation)
        while not self._stopping.is_set():
            try:
                async for event in trigger.events(context):
                    # Shielded: a shutdown cancels the pump, but an event in
                    # hand is still submitted and acknowledged (`run` awaits it).
                    settle = asyncio.create_task(
                        self._submit_and_ack(automation, trigger, event),
                        name=f"awa-settle-{automation.name}-{trigger.name}",
                    )
                    self._settling.add(settle)
                    settle.add_done_callback(self._settling.discard)
                    await asyncio.shield(settle)
                    if self._stopping.is_set():
                        break
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - restart the trigger after a pause
                _logger.error(
                    "Trigger %s/%s failed; restarting: %s: %s",
                    automation.name,
                    trigger.name,
                    type(exc).__name__,
                    exc,
                    exc_info=_logger.isEnabledFor(logging.DEBUG),
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopping.wait(), self.poll_interval.total_seconds()
                    )

    async def _submit_and_ack(self, automation: Automation, trigger: Any, event: Any) -> None:
        error: BaseException | None = None
        try:
            submission = await automation.submit(event, trigger=trigger)
            self._wake.set()
        except Exception as exc:  # noqa: BLE001 - reported, the trigger goes on
            error = exc
            _logger.warning(
                "Automation %s refused an event of %s: %s", automation.name, trigger.name, exc
            )
        else:
            if getattr(trigger, "ack_on_end", False) and callable(getattr(trigger, "settle", None)):
                # Acknowledged when the run ends: a failed run's input is not handled as done.
                self._awaiting[submission.run.id] = _Awaited(automation, trigger, event)
                if submission.run.status in TERMINAL_STATUSES or submission.run.status == "waiting":
                    await self._settle_run(submission.run)
                return
        ack = getattr(trigger, "ack", None)
        if ack is not None:
            try:
                await ack(event, error)
            except Exception:  # noqa: BLE001 - see above
                _logger.warning("Trigger %s: ack failed", trigger.name, exc_info=True)

    async def _claim_loop(self) -> None:
        while not self._stopping.is_set():
            claimed = False
            for automation in self.automations:
                if len(self._inflight) >= self.concurrency or self._stopping.is_set():
                    break
                try:
                    run = await automation.claim(self.worker_id, lease=self.lease)
                except Exception:  # noqa: BLE001 - the store may be briefly unavailable
                    _logger.warning("Claiming from %s failed", automation.name, exc_info=True)
                    run = None
                if run is None:
                    continue
                claimed = True
                task = asyncio.create_task(
                    automation.execute(run, worker=self.worker_id, lease=self.lease),
                    name=f"awa-process-{run.id}",
                )
                self._inflight.add(task)
                task.add_done_callback(self._done)
            if claimed and len(self._inflight) < self.concurrency:
                continue
            self._wake.clear()
            waiters = [asyncio.ensure_future(self._wake.wait())]
            waiters.extend(asyncio.ensure_future(a._wake.wait()) for a in self.automations)
            try:
                await asyncio.wait(
                    waiters,
                    timeout=self.poll_interval.total_seconds(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for waiter in waiters:
                    waiter.cancel()
                for automation in self.automations:
                    automation._wake.clear()

    def _done(self, task: asyncio.Task[ProcessRun]) -> None:
        self._inflight.discard(task)
        self._wake.set()
        if task.cancelled():
            return
        if task.exception() is not None:
            _logger.error("Process attempt failed unexpectedly", exc_info=task.exception())
            return
        run = task.result()
        if run.id in self._awaiting:
            settle = asyncio.ensure_future(self._settle_run(run))
            self._settling.add(settle)
            settle.add_done_callback(self._settling.discard)

    async def _settle_run(self, run: ProcessRun) -> None:
        """Tell the trigger whose event started `run` how it ended (or that it waits)."""

        awaited = self._awaiting.get(run.id)
        if awaited is None or (run.status not in TERMINAL_STATUSES and run.status != "waiting"):
            return
        if run.status in TERMINAL_STATUSES:
            self._awaiting.pop(run.id, None)
        elif awaited.settled == "waiting":
            return  # told already; told again only when it ends
        else:
            awaited.settled = run.status
        try:
            await awaited.trigger.settle(awaited.event, run)
        except Exception as exc:  # noqa: BLE001 - the worker goes on
            _logger.warning(
                "Trigger %s: settling run %s failed: %s", awaited.trigger.name, run.id, exc
            )

    async def _housekeeping(self) -> None:
        while not self._stopping.is_set():
            for automation in self.automations:
                try:
                    await automation.tick()
                except Exception:  # noqa: BLE001 - try again next round
                    _logger.warning("Housekeeping of %s failed", automation.name, exc_info=True)
            await self._settle_from_store()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), self.tick_interval.total_seconds())

    async def _settle_from_store(self) -> None:
        """Settle the awaited runs another worker executed: their end is only in the store."""

        for run_id, awaited in list(self._awaiting.items()):
            try:
                run = await awaited.automation.store.get(run_id)
            except Exception:  # noqa: BLE001 - try again next round
                _logger.warning("Reading run %s failed", run_id, exc_info=True)
                continue
            if run is not None:
                await self._settle_run(run)

    async def _finish(self, tasks: Collection[asyncio.Task[Any]]) -> None:
        """Wait up to `drain_timeout` (or until done) for `tasks`, then cancel the rest."""

        if not tasks:
            return
        timeout = None if self.drain_timeout is None else self.drain_timeout.total_seconds()
        _, pending = await asyncio.wait(set(tasks), timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


__all__ = ["Worker"]
