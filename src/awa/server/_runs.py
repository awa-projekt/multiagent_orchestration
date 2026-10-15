"""The runs a server process is executing: lookup by run id, cancellation and graceful drain."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, TypeAlias

from awa.errors import (
    AwaError,
    RunCancelledError,
    RunTimeoutError,
    ServiceUnavailableError,
    StepRejectedError,
)
from awa.runtime import cancel as cancel_run_tree

_STARTING_POLL = 0.01
_STOP_GRACE = 1.0

RunMode: TypeAlias = Literal["sync", "stream", "async"]
RunStatus: TypeAlias = Literal[
    "running", "completed", "interrupted", "failed", "cancelled", "rejected"
]
"""Where a served run stands; `rejected`: a special case or a person turned the item down
(`StepRejectedError`)."""


@dataclass(eq=False)
class RunHandle:
    """A run started by the server: where it runs, who owns it, how it ended."""

    run_id: str
    system: str
    mode: RunMode
    tenant: str | None
    principal: str
    request_id: str | None = None
    thread_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: RunStatus = "running"
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    finished_at: datetime | None = None
    task: asyncio.Task[Any] | None = None
    cancel_error: AwaError | None = None
    ids: set[str] = field(default_factory=set)

    @property
    def done(self) -> bool:
        """Whether the run has finished (in any way)."""

        return self.status != "running"

    def cancel(
        self, error: AwaError, *, reason: str | None = None, cancelled_by: str | None = None
    ) -> bool:
        """Stop the run tree (`awa.runtime.cancel`, else its task); `True` if it was running."""

        if self.task is None or self.task.done():
            return False
        if self.cancel_error is None:
            self.cancel_error = error
        stopped = False
        for run_id in list(self.ids):
            stopped = cancel_run_tree(run_id, reason, cancelled_by=cancelled_by) or stopped
        if not stopped:
            self.task.cancel()
        return True

    def reported(self, exc: BaseException) -> BaseException:
        """The error to report for `exc`: the reason the server stopped the run, if it did."""

        stopped = isinstance(exc, RunCancelledError | asyncio.CancelledError)
        if self.cancel_error is not None and stopped:
            return self.cancel_error
        return exc

    def outcome(self, exc: BaseException) -> RunStatus:
        """The status of a run that ended with `exc`."""

        if isinstance(exc, RunTimeoutError):
            return "failed"
        if isinstance(exc, StepRejectedError):
            return "rejected"
        cancelled = isinstance(exc, RunCancelledError | asyncio.CancelledError)
        return "cancelled" if cancelled or self.cancel_error is not None else "failed"

    def status_json(self) -> dict[str, Any]:
        """The run as `GET /v1/runs/{run_id}` returns it."""

        return {
            "run_id": self.run_id,
            "system": self.system,
            "mode": self.mode,
            "status": self.status,
            "thread_id": self.thread_id,
            "request_id": self.request_id,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "result": self.result,
            "error": self.error,
        }


class RunRegistry:
    """In-flight runs of this process, and finished async runs for `retention` seconds."""

    def __init__(
        self,
        *,
        retention: float = 3600.0,
        max_retained: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._retention = retention
        self._max_retained = max_retained
        self._clock = clock
        self._by_id: dict[str, RunHandle] = {}
        self._active: set[RunHandle] = set()
        self._finished: OrderedDict[RunHandle, float] = OrderedDict()
        self.draining = False

    def start(self, handle: RunHandle, *, addressable: bool = True) -> RunHandle:
        """Register a run about to start; `addressable` lets clients look it up and cancel it by id.

        Raises:
            ServiceUnavailableError: If the server is draining.
        """

        if self.draining:
            raise ServiceUnavailableError("The server is shutting down; retry on another instance")
        self._active.add(handle)
        if addressable:
            self.add_id(handle, handle.run_id)
        return handle

    def add_id(self, handle: RunHandle, run_id: str) -> None:
        """Make `handle` reachable under `run_id` too (the id the run itself reports)."""

        handle.ids.add(run_id)
        self._by_id[run_id] = handle

    def finish(
        self,
        handle: RunHandle,
        status: RunStatus,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """Record how a run ended; async runs stay readable for `retention` seconds."""

        if handle.status == "running":
            handle.status = status
            handle.result = result
            handle.error = error
            handle.finished_at = datetime.now(UTC)
        self._active.discard(handle)
        if handle.mode == "async":
            self._finished[handle] = self._clock() + self._retention
            self._purge()
        else:
            self._forget(handle)

    def get(self, run_id: str) -> RunHandle | None:
        """The run with `run_id`, if it is running or a retained async run."""

        self._purge()
        return self._by_id.get(run_id)

    def handles(self) -> list[RunHandle]:
        """Every addressable run: in flight (async and streamed) or retained, newest first."""

        self._purge()
        unique = dict.fromkeys(self._by_id.values())  # a run may have several ids
        return sorted(unique, key=lambda handle: handle.created_at, reverse=True)

    @property
    def active(self) -> list[RunHandle]:
        """Runs in flight, in any mode."""

        return list(self._active)

    def _forget(self, handle: RunHandle) -> None:
        for run_id in handle.ids:
            if self._by_id.get(run_id) is handle:
                del self._by_id[run_id]

    def _purge(self) -> None:
        now = self._clock()
        while self._finished:
            oldest, expires = next(iter(self._finished.items()))
            if expires > now and len(self._finished) <= self._max_retained:
                break
            del self._finished[oldest]
            self._forget(oldest)

    async def drain(self, timeout: float | None) -> int:
        """Refuse new runs, wait up to `timeout` seconds, cancel the rest; return how many."""

        self.draining = True
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            pending = {h.task: h for h in self._active if h.task is not None and not h.task.done()}
            # A streamed run admitted a moment ago starts its task with the response.
            starting = any(h.task is None for h in self._active)
            if not pending and not starting:
                return 0
            remaining = None if deadline is None else max(0.0, deadline - self._clock())
            if remaining == 0.0:
                break
            if pending:
                await asyncio.wait(pending, timeout=remaining)
            else:
                await asyncio.sleep(min(_STARTING_POLL, remaining or _STARTING_POLL))
        if not pending:  # only runs that never started their task
            return 0
        error = ServiceUnavailableError("The server shut down before the run finished")
        cancelled = sum(
            handle.cancel(error, reason="server shutting down", cancelled_by="server")
            for handle in pending.values()
        )
        _, stuck = await asyncio.wait(pending, timeout=_STOP_GRACE)
        for task in stuck:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return cancelled
