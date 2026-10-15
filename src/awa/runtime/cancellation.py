"""Run timeouts and cancellation: the deadline of a run tree and its stop button.

`cancel(run_id)` stops a run tree from any task or thread; the run emits `RunCancelledEvent` and
its caller gets `RunCancelledError`. An expired `timeout=` or `UsageLimits.max_duration` stops
it the same way with `RunTimeoutError`. The run's task is cancelled, interrupting whatever it
awaits.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable, Coroutine, Generator, Mapping
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from awa.errors import ConfigurationError, RunCancelledError, RunTimeoutError
from awa.runtime.context import RunContext, current_run_context, new_run_id, use_run_id
from awa.runtime.termination import raise_if_stopped

ResultT = TypeVar("ResultT")

_CANCELLED_MESSAGE = "The run was cancelled."

_lock = threading.Lock()
# Run id (top-level or nested) -> the scope of its top-level run.
_active: dict[str, RunScope] = {}


@dataclass(frozen=True)
class _Stop:
    """Why a scope was stopped."""

    reason: str
    cancelled_by: str | None = None
    timed_out: bool = False


class RunScope:
    """Deadline and stop state of the part of a run tree that one run owns.

    The root scope is bounded by the top-level `timeout` and `UsageLimits.max_duration`. A nested
    `timeout` ending before the enclosing deadline gets its own scope; a longer one is ignored.
    Created when a run starts executing.

    Attributes:
        deadline: Event loop time (`loop.time()`) at which the scope expires, or `None`.
    """

    def __init__(
        self,
        *,
        run_id: str,
        agent: str,
        task: asyncio.Task[Any],
        parent: RunScope | None,
        limit: float | None,
        limit_name: str,
    ) -> None:
        loop = task.get_loop()
        self.run_id = run_id
        self.agent = agent
        self.parent = parent
        self._task = task
        self._loop = loop
        self._started = loop.time()
        self._limit = limit
        self._limit_name = limit_name
        self.deadline: float | None = self._started + limit if limit is not None else None
        self._stop: _Stop | None = None
        self._cancels = 0  # task.cancel() calls made for the stop
        self._posted = False  # a cancel callback is scheduled
        self._closed = False  # the owning run is reporting its end, or has ended
        self._timer = (
            loop.call_at(self.deadline, self._expire) if self.deadline is not None else None
        )

    @property
    def root(self) -> RunScope:
        """The scope of the top-level run."""

        scope = self
        while scope.parent is not None:
            scope = scope.parent
        return scope

    @property
    def stopping(self) -> bool:
        """Whether a stop (cancel or timeout) was requested."""

        return self._stop is not None

    def elapsed(self) -> float:
        """Seconds since the owning run started."""

        return self._loop.time() - self._started

    def remaining(self) -> float | None:
        """Seconds left before `deadline` (never negative), or `None`; thread-safe."""

        if self.deadline is None:
            return None
        return max(0.0, self.deadline - self._loop.time())

    def request_stop(self, reason: str, cancelled_by: str | None = None) -> bool:
        """Stop the owning run and everything nested in it; thread-safe. `False` if already ended or stopping."""

        with _lock:
            if self._stop is not None or self._closed:
                return False
            self._stop = _Stop(reason, cancelled_by)
        self._post_cancel()
        return True

    def _post_cancel(self) -> None:
        # The task is always cancelled from a loop callback, never while it
        # runs, so the cancellation is delivered at its very next step.
        with _lock:
            if self._posted or self._closed:
                return
            self._posted = True
        try:
            self._loop.call_soon_threadsafe(self._cancel_task)
        except RuntimeError:  # the loop is closed: the run is gone
            self._posted = False

    def _cancel_task(self) -> None:
        self._posted = False
        if self._closed or self._task.done():
            return
        self._task.cancel(_CANCELLED_MESSAGE)
        self._cancels += 1

    def _expire(self) -> None:
        self._timer = None
        with _lock:
            if self._stop is not None or self._closed:
                return
            self._stop = _Stop("timeout", timed_out=True)
        self._cancel_task()

    def _expired(self) -> bool:
        return self.deadline is not None and self._loop.time() >= self.deadline

    def _close(self) -> _Stop | None:
        """End the scope; return its stop if a cancellation it caused ended the run."""

        with _lock:
            self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        remaining = self._task.cancelling()
        for _ in range(self._cancels):
            remaining = self._task.uncancel()
        self._cancels = 0
        # Also cancelled from outside (a closed stream, the caller): not ours to report.
        return self._stop if remaining == 0 else None

    def _error(self, stop: _Stop) -> RunCancelledError | RunTimeoutError:
        elapsed = self.elapsed()
        if stop.timed_out:
            limit = self._limit or 0.0
            return RunTimeoutError(
                f"Run {self.run_id!r} of {self.agent!r} exceeded {self._limit_name}={limit:g}s "
                f"and was stopped after {elapsed:.2f}s",
                run_id=self.run_id,
                elapsed=elapsed,
                limit=limit,
                limit_name=self._limit_name,
            )
        message = f"Run {self.run_id!r} of {self.agent!r} was cancelled"
        if stop.cancelled_by:
            message += f" by {stop.cancelled_by!r}"
        if stop.reason != "cancelled":
            message += f": {stop.reason}"
        return RunCancelledError(
            message, run_id=self.run_id, reason=stop.reason, cancelled_by=stop.cancelled_by
        )


class RunGuard:
    """A run's registration while it executes; see `enter_run`."""

    def __init__(self, ctx: RunContext[Any], scope: RunScope | None) -> None:
        self._ctx = ctx
        self._scope = scope

    async def cancelled(
        self, fields: Mapping[str, Any]
    ) -> RunCancelledError | RunTimeoutError | None:
        """Report that the run ended by cancellation; call it from `except CancelledError`.

        When the run's own scope was stopped, emits `RunCancelledEvent` and an `ErrorEvent` and returns
        the error to raise; otherwise emits the plain `ErrorEvent` and returns `None` (re-raise).
        """

        from awa.contracts.events import ErrorEvent, RunCancelledEvent
        from awa.runtime.termination import diagnose_stop

        ctx = self._ctx
        scope = self._scope
        stop = scope._close() if scope is not None else None
        if scope is None or stop is None:
            await ctx.emit(
                ErrorEvent(error_type="CancelledError", message=_CANCELLED_MESSAGE, **fields)
            )
            return None
        error = scope._error(stop)
        diagnosis = await diagnose_stop(ctx, error)
        await ctx.emit(
            RunCancelledEvent(
                reason=stop.reason,
                cancelled_by=stop.cancelled_by,
                elapsed_ms=int(scope.elapsed() * 1000),
                **fields,
            )
        )
        await ctx.emit(
            ErrorEvent(
                error_type=type(error).__name__, message=str(error), stop=diagnosis, **fields
            )
        )
        return error

    def exit(self) -> None:
        """Unregister the run; call it in `finally`."""

        run_id = self._ctx.run_id
        with _lock:
            _active.pop(run_id, None)
        if self._scope is not None:
            self._scope._close()


def enter_run(ctx: RunContext[Any], timeout: float | None) -> RunGuard:
    """Register the run of `ctx` as it starts executing in the current task.

    Raises:
        ConfigurationError: If `timeout` is not positive, or a top-level run with the same id is
            active (see `use_run_id`).
    """

    if timeout is not None and not timeout > 0:
        raise ConfigurationError(f"timeout must be a positive number of seconds, got {timeout!r}")
    task = asyncio.current_task()
    own = _own_scope(ctx, task, timeout) if task is not None else None
    scope = own or ctx.scope
    with _lock:
        duplicate = own is not None and own.parent is None and ctx.run_id in _active
        if not duplicate and scope is not None:
            _active[ctx.run_id] = scope.root
    if duplicate:
        assert own is not None
        own._close()
        raise ConfigurationError(f"A run with id {ctx.run_id!r} is already active")
    ctx.scope = scope
    return RunGuard(ctx, own)


def _own_scope(
    ctx: RunContext[Any], task: asyncio.Task[Any], timeout: float | None
) -> RunScope | None:
    """A scope of its own for a top-level run, or for a nested one whose `timeout` ends first."""

    parent = ctx.scope
    if parent is not None and (
        timeout is None
        or (parent.deadline is not None and task.get_loop().time() + timeout >= parent.deadline)
    ):
        return None
    limit, limit_name = timeout, "timeout"
    max_duration = ctx.limits.max_duration if ctx.limits and parent is None else None
    if max_duration is not None and (timeout is None or max_duration < timeout):
        limit, limit_name = max_duration, "max_duration"
    return RunScope(
        run_id=ctx.run_id,
        agent=ctx.agent,
        task=task,
        parent=parent,
        limit=limit,
        limit_name=limit_name,
    )


async def checkpoint() -> None:
    """Let a pending cancel or an expired deadline of the current run take effect.

    awa calls this before every model and tool call; a long computing tool can call it between
    steps.

    Raises:
        NoProgressError: When the run tree was stopped for repeating itself.
    """

    ctx = current_run_context()
    # A run tree stopped for repeating itself ends at its next step, like a limit.
    raise_if_stopped(ctx)
    scope = ctx.scope if ctx is not None else None
    if scope is None:
        return
    current = asyncio.current_task()
    while scope is not None:
        if scope._stop is None and scope._expired():
            scope._expire()
        if scope._stop is not None and not scope._closed:
            if scope._task is current:
                # Cancels are delivered at the task's next step, so a pending one
                # here was swallowed by the code that received it: cancel again.
                scope._post_cancel()
                await asyncio.sleep(0)
                return
            if scope._cancels == 0:
                scope._post_cancel()
        scope = scope.parent


def cancel(run_id: str, reason: str | None = None, *, cancelled_by: str | None = None) -> bool:
    """Stop the active run `run_id` (or the tree any nested `run_id` belongs to); thread-safe.

    The top-level run emits `RunCancelledEvent(reason, cancelled_by)` and raises
    `RunCancelledError`. A run paused on an interrupt is not active, so its thread stays resumable.

    Args:
        run_id: `RunResult.run_id`, `RunHandle.run_id`, or the `root_run_id` of any of its events.
        cancelled_by: Who stops it, for example a principal id.

    Returns:
        Whether a stop was requested: `False` if no such run is active or it is already stopping.

    Example:
        ```python
        async def stop_button(run_id: str, user: str) -> bool:
            return awa.runtime.cancel(run_id, "stopped by operator", cancelled_by=user)
        ```
    """

    with _lock:
        scope = _active.get(run_id)
    if scope is None:
        return False
    return scope.request_stop(reason or "cancelled", cancelled_by)


def active_runs() -> list[str]:
    """Ids of the top-level runs executing in this process, oldest first."""

    with _lock:
        return [run_id for run_id, scope in _active.items() if scope.run_id == run_id]


class RunHandle(Generic[ResultT]):
    """A top-level run started in the background: its id, stop button and result.

    Returned by `start`; awaiting the handle is the same as awaiting `result()`.

    Example:
        ```python
        handle = team.start("Plan the migration", timeout=900)
        handle.cancel("stopped by user", cancelled_by=user.id)
        result = await handle.result()  # raises RunCancelledError after a cancel
        ```
    """

    def __init__(self, run_id: str, task: asyncio.Task[ResultT]) -> None:
        self._run_id = run_id
        self._task = task
        self._early_stop: _Stop | None = None

    @property
    def run_id(self) -> str:
        """Id of the top-level run (also `RunResult.run_id` and every event's `root_run_id`)."""

        return self._run_id

    def done(self) -> bool:
        """Whether the run has finished (with a result, an error or a cancellation)."""

        return self._task.done()

    def cancel(self, reason: str | None = None, *, cancelled_by: str | None = None) -> bool:
        """Stop the run; see `awa.runtime.cancel`. `False` once the run has finished."""

        if self._task.done():
            return False
        if cancel(self._run_id, reason, cancelled_by=cancelled_by):
            return True
        with _lock:
            started = self._run_id in _active
        if started:  # already being stopped
            return False
        # The task has not started executing the run yet.
        self._early_stop = _Stop(reason or "cancelled", cancelled_by)
        loop = self._task.get_loop()
        with contextlib.suppress(RuntimeError):
            if asyncio.get_running_loop() is loop:
                return self._task.cancel(_CANCELLED_MESSAGE)
        try:
            loop.call_soon_threadsafe(self._task.cancel, _CANCELLED_MESSAGE)
        except RuntimeError:  # the loop is closed
            return False
        return True

    async def result(self) -> ResultT:
        """Wait for the run and return its result.

        Raises:
            RunCancelledError: If the run was cancelled.
            RunTimeoutError: If it ran out of time.
        """

        try:
            return await self._task
        except asyncio.CancelledError:
            stop = self._early_stop
            current = asyncio.current_task()
            if stop is None or not self._task.cancelled() or (current and current.cancelling()):
                raise
            message = f"Run {self._run_id!r} was cancelled before it started"
            if stop.cancelled_by:
                message += f" by {stop.cancelled_by!r}"
            raise RunCancelledError(
                message, run_id=self._run_id, reason=stop.reason, cancelled_by=stop.cancelled_by
            ) from None

    def __await__(self) -> Generator[Any, None, ResultT]:
        return self.result().__await__()

    def __repr__(self) -> str:
        state = "done" if self._task.done() else "running"
        return f"RunHandle({self._run_id!r}, {state})"


def start_run(
    run: Callable[[], Coroutine[Any, Any, ResultT]], *, what: str = "start()"
) -> RunHandle[ResultT]:
    """Start the top-level run `run()` as a task and return its handle (used by `start`).

    Raises:
        ConfigurationError: If called inside an active run.
    """

    loop = asyncio.get_running_loop()
    if current_run_context() is not None:
        raise ConfigurationError(
            f"{what} starts a top-level run and cannot be used inside a run; await run() "
            "instead to run nested"
        )
    run_id = new_run_id()
    with use_run_id(run_id):
        # The task copies the context now, with the assigned id in it.
        task = loop.create_task(run(), name=f"awa-run:{run_id}")
    return RunHandle(run_id, task)


__all__ = [
    "RunGuard",
    "RunHandle",
    "RunScope",
    "active_runs",
    "cancel",
    "checkpoint",
    "enter_run",
    "start_run",
]
