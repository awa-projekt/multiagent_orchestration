"""What `Automation` builds on: run changes in the store, system probes, waits and traces."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any

from awa._internal.aio import LoopLocal
from awa.automation._sqlite import SqliteRunStore
from awa.automation._time import iso
from awa.automation.models import ProcessEventType, ProcessRun, WaitState, with_history
from awa.automation.store import InMemoryRunStore, RunStore
from awa.automation.waits import wait_timeout
from awa.contracts.results import RunResult
from awa.contracts.usage import PriceTable, Usage
from awa.core.config import env_value
from awa.errors import (
    CheckpointLostError,
    ConfigurationError,
    ProcessRunStateError,
    RunCancelledError,
    RunNotFoundError,
    WaitTimeoutError,
)
from awa.runtime.ambient import subscribe_globally

if TYPE_CHECKING:
    from awa.observability.trace_store import TraceRecorder, TraceStore

TRACE_LEVEL_ENV = "AWA_TRACE_LEVEL"
"""Environment variable that sets the trace level of automations (`off`, `summary`, `full`)."""

DEFAULT_TRACE_MAX_AGE_DAYS = 30
DEFAULT_TRACE_MAX_FILES = 2000
DueEvents = list[tuple[ProcessEventType, dict[str, Any]]]

_EXCLUSIVE: LoopLocal[dict[int, asyncio.Lock]] = LoopLocal(dict)
_LEASE_LOST = "lease of "


async def retry_run(
    store: RunStore,
    run_id: str,
    *,
    now: datetime,
    actor: str | None = None,
    restart: bool | None = None,
) -> ProcessRun:
    """Queue an unsuccessfully finished run again in `store`, without events (`awa runs retry`).

    Args:
        actor: Who retries it (recorded).
        restart: Start again from the input instead of resuming the paused thread; default: only
            when the run failed with `CheckpointLostError`.

    Raises:
        RunNotFoundError: If there is no such run.
        ProcessRunStateError: If the run is not finished unsuccessfully.
    """

    def change(current: ProcessRun) -> ProcessRun:
        if current.status not in ("failed", "dead_lettered", "cancelled"):
            raise ProcessRunStateError(
                f"Run {run_id!r} is {current.status}; only failed, dead-lettered or "
                "cancelled runs can be retried"
            )
        fresh = restart if restart is not None else current.error_code == CheckpointLostError.code
        if fresh:
            current = current.model_copy(update={"resume_value": None, "waits": []})
        return with_history(
            current.model_copy(
                update={
                    "status": "queued",
                    "attempts": 0,
                    "next_attempt_at": None,
                    "error": None,
                    "error_code": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                    "resuming": current.resume_value is not None,
                }
            ),
            now,
            "queued",
            f"retried by {actor or 'operator'}" + (", from the input" if fresh else ""),
        )

    return await store.modify(run_id, change, now=now)


async def decide_run(
    store: RunStore,
    run_id: str,
    decision: Any,
    *,
    now: datetime,
    interrupt_id: str | None = None,
    actor: str | None = None,
) -> ProcessRun:
    """Answer a run's pending approval in `store`, without events (`awa runs decide`).

    The worker that resumes the run needs its checkpoints: a shared PostgreSQL checkpointer, or the
    process that paused it.

    Returns:
        The run as stored, queued to resume once every wait is answered.

    Raises:
        RunNotFoundError: If there is no such run.
        ProcessRunStateError: If the run waits for no such approval.
    """

    run = await store.get(run_id)
    if run is None:
        raise RunNotFoundError(f"No process run {run_id!r}")
    wait = approval_wait(run, interrupt_id)
    answer = with_reviewer(decision, actor)
    stored = await store.modify(
        run_id,
        lambda current: with_answer(current, wait.key, answer, now=now, kind="approval"),
        now=now,
    )
    if stored.revision == run.revision:
        raise ProcessRunStateError(f"Run {run_id!r} does not wait on {wait.key!r}")
    return stored


def approval_wait(run: ProcessRun, interrupt_id: str | None) -> WaitState:
    """The open approval of `run` named `interrupt_id`, or else its oldest open one."""

    open_waits = [w for w in run.waits if not w.answered and w.kind == "approval"]
    if interrupt_id is not None:
        open_waits = [w for w in open_waits if w.interrupt_id == interrupt_id]
    if run.status != "waiting" or not open_waits:
        raise ProcessRunStateError(f"Run {run.id!r} waits for no approval to decide")
    return open_waits[0]


def with_reviewer(decision: Any, actor: str | None) -> Any:
    """The decision with `actor` as its reviewer, replacing what the client sent."""

    if isinstance(decision, dict) and actor:
        kept = {key: value for key, value in decision.items() if key not in ("reviewer", "actor")}
        return {**kept, "reviewer": actor}
    return decision


def with_answer(
    current: ProcessRun,
    key: str,
    answer: Any,
    *,
    now: datetime,
    kind: str,
    migrate: Callable[[ProcessRun], ProcessRun] | None = None,
) -> ProcessRun | None:
    """`current` with its wait on `key` answered (queued to resume once all are), or `None`."""

    if current.status != "waiting":
        return None
    updated = current.model_copy(deep=True)
    hit = False
    for wait in updated.waits:
        if wait.key == key and not wait.answered:
            wait.answered = True
            wait.answer = answer
            hit = True
    if not hit:
        return None
    if all(wait.answered for wait in updated.waits):
        if migrate is not None:
            updated = migrate(updated)
        updated = _queued_for_resume(updated)
    return with_history(updated, now, updated.status, f"{kind} {key} answered")


async def cancel_run(
    store: RunStore,
    run_id: str,
    *,
    now: datetime,
    reason: str = "cancelled",
    actor: str | None = None,
) -> ProcessRun:
    """Mark an unfinished run cancelled in `store`, without events (`awa runs cancel`).

    Raises:
        RunNotFoundError: If there is no such run.
        ProcessRunStateError: If the run already finished.
    """

    def change(current: ProcessRun) -> ProcessRun:
        if current.is_finished:
            raise ProcessRunStateError(f"Run {run_id!r} already {current.status}")
        return with_history(
            current.model_copy(
                update={
                    "status": "cancelled",
                    "error": f"RunCancelledError: {reason}",
                    "error_code": RunCancelledError.code,
                    "lease_owner": None,
                    "lease_expires_at": None,
                }
            ),
            now,
            "cancelled",
            f"{reason} ({actor or 'operator'})",
        )

    return await store.modify(run_id, change, now=now)


def names_timeout(method: Callable[..., Any]) -> bool:
    """Whether `method` declares a `timeout` parameter of its own (not just `**kwargs`)."""

    try:
        return "timeout" in inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False


def checkpoint_lost(run: ProcessRun) -> CheckpointLostError:
    """The error of a run whose paused thread state is gone, with how to start it again."""

    return CheckpointLostError(
        f"checkpoint lost: the paused state of thread {run.thread_id!r} is gone (it was kept "
        "in the memory of a process that stopped, or deleted); retry the run "
        f"(`awa runs retry {run.id}`) to start it again from its input"
    )


@contextlib.asynccontextmanager
async def exclusive(system: Any) -> AsyncIterator[None]:
    """Hold a system that works on one run at a time (an `AgentNetwork`) while it runs:
    further runs of this process wait for it instead of failing as busy."""

    from awa.team.network import AgentNetwork

    # A network refuses a second run while one shares its board or bus (`_scope.shared`).
    if not isinstance(system, AgentNetwork) or not getattr(
        getattr(system, "_scope", None), "shared", True
    ):
        yield
        return
    async with _EXCLUSIVE.get().setdefault(id(system), asyncio.Lock()):
        yield


def restores_usage(system: Any) -> bool:
    """Whether a resumed `system` (a team or network) reports the usage of its whole paused run."""

    from awa.team.network import AgentNetwork
    from awa.team.team import Team

    return isinstance(system, Team | AgentNetwork)


def usage_without(usage: Usage, base: Usage | None) -> Usage:
    """`usage` less `base` (what a system restored from its paused run), with the breakdowns."""

    net = usage.snapshot()
    if base is None:
        return net
    for field in (
        "requests",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "tool_calls",
    ):
        setattr(net, field, max(0, getattr(usage, field) - getattr(base, field)))
    for breakdown in ("by_agent", "by_model"):
        kept = getattr(net, breakdown)
        for key, part in getattr(usage, breakdown).items():
            kept[key] = usage_without(part, getattr(base, breakdown).get(key))
    return net


def keeps_threads(system: Any) -> bool:
    """Whether `system` has a checkpointer and can resume a paused thread."""

    return _checkpointer_of(system) is not None and hasattr(system, "resume")


async def no_checkpoint(system: Any, thread_id: str | None) -> bool:
    """Whether `system` keeps checkpoints but has none for `thread_id` (the state is gone)."""

    return (
        thread_id is not None
        and _checkpointer_of(system) is not None
        and await latest_checkpoint(system, thread_id) is None
    )


async def latest_checkpoint(system: Any, thread_id: str | None) -> str | None:
    """The id of the newest checkpoint of `thread_id`, or `None` when there is none."""

    checkpointer = _checkpointer_of(system)
    if checkpointer is None or thread_id is None:
        return None
    found = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
    if found is None:
        return None
    config = getattr(found, "config", {}) or {}
    value = config.get("configurable", {}).get("checkpoint_id")
    return str(value) if value is not None else "present"


def act_on_waits(updated: ProcessRun, now: datetime, events: DueEvents) -> ProcessRun:
    """Time out, escalate and remind the open waits of `updated` (changed in place, and
    returned queued to resume when every wait is answered); what happened goes to `events`."""

    for wait in updated.waits:
        if wait.answered:
            continue
        info = {"key": wait.key, "kind": wait.kind, "waiting_since": iso(wait.requested_at)}
        if wait.deadline is not None and wait.deadline <= now:
            events.append(("process.wait_timeout", {**info, "on_timeout": wait.on_timeout}))
            if wait.on_timeout == "escalate":
                wait.deadline = None
                wait.escalated = True
                events.append(("process.escalated", info))
            elif wait.on_timeout == "continue":
                wait.answered = True
                wait.answer = wait_timeout()
                continue  # answered: no escalation and no reminder
            else:
                updated.status = "failed"
                updated.error = f"{WaitTimeoutError.__name__}: waited for {wait.key!r} since {iso(wait.requested_at)}"
                updated.error_code = WaitTimeoutError.code
                break
        if wait.escalate_at is not None and not wait.escalated and wait.escalate_at <= now:
            wait.escalated = True
            events.append(("process.escalated", info))
        if wait.next_reminder_at is not None and wait.next_reminder_at <= now:
            wait.reminders_sent += 1
            assert wait.remind_every is not None
            # One reminder for the intervals missed while no worker ran.
            missed = (now - wait.next_reminder_at) // wait.remind_every + 1
            wait.next_reminder_at += wait.remind_every * missed
            events.append(("process.reminder", {**info, "reminder": wait.reminders_sent}))
    if updated.status == "waiting" and updated.waits and all(w.answered for w in updated.waits):
        return _queued_for_resume(updated)
    return updated


def lease_losses(run: ProcessRun) -> int:
    """How often in a row the run lost its worker: requeues after an expired lease since its
    last other status change (each followed by the claim of the next worker)."""

    lost = 0
    for entry in reversed(run.history):
        note = str(entry.get("note", ""))
        if entry.get("status") == "running" and note.startswith("claimed by "):
            continue
        if entry.get("status") == "queued" and note.startswith(_LEASE_LOST):
            lost += 1
            continue
        break
    return lost


def resolve_traces(
    trace: TraceStore | str | None, store: RunStore, prices: PriceTable | None
) -> TraceStore | None:
    """The trace store of an automation, from its `trace` argument.

    A level (or `AWA_TRACE_LEVEL`) makes `traces/` beside the SQLite data file (`.awa/traces`
    otherwise), kept 30 days and 2000 files. Without either, in-memory runs are not traced and other
    stores trace at `summary`.
    """

    if trace is not None and not isinstance(trace, str):
        return trace
    named = trace if trace is not None else (env_value(TRACE_LEVEL_ENV) or "").strip() or None
    if named is None and type(store) is InMemoryRunStore:
        return None
    from awa.observability.trace_store import DEFAULT_TRACE_DIR, TraceStore, resolve_level

    try:
        level = resolve_level(named, default="summary")
    except ValueError as exc:
        where = "trace" if trace is not None else TRACE_LEVEL_ENV
        raise ConfigurationError(f"{where}: {exc}") from None
    if level == "off":
        return None
    folder = Path(DEFAULT_TRACE_DIR)
    if isinstance(store, SqliteRunStore):
        folder = Path(store.path).parent / "traces"
    return TraceStore(
        folder,
        level=level,
        max_age=timedelta(days=DEFAULT_TRACE_MAX_AGE_DAYS),
        max_files=DEFAULT_TRACE_MAX_FILES,
        prices=prices,
    )


class Recording:
    """Feeds an attempt's events to its recorder; closes the trace off the event loop at the end."""

    def __init__(self, recorder: TraceRecorder) -> None:
        self.recorder = recorder
        self.outcome = "ok"
        self._unsubscribe = subscribe_globally(recorder)

    def finished(self, result: RunResult[Any]) -> None:
        self.outcome = "paused" if result.is_interrupted else "ok"

    async def __aenter__(self) -> Recording:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._unsubscribe()
        if exc is None:
            await asyncio.to_thread(self.recorder.close, self.outcome, timeout=2.0)
        else:
            cancelled = isinstance(exc, asyncio.CancelledError | RunCancelledError)
            outcome = "cancelled" if cancelled else "failed"
            await asyncio.to_thread(self.recorder.close, outcome, exc, timeout=2.0)


def _queued_for_resume(run: ProcessRun) -> ProcessRun:
    answers = [wait for wait in run.waits if wait.answered]
    value: Any = (
        answers[0].answer
        if len(answers) == 1
        else {wait.interrupt_id: wait.answer for wait in answers}
    )
    return run.model_copy(
        update={
            "status": "queued",
            "resuming": True,
            "resume_value": value,
            "next_attempt_at": None,
        }
    )


def _checkpointer_of(system: Any) -> Any:
    for attribute in ("checkpointer", "_checkpointer"):
        value = getattr(system, attribute, None)
        if value is not None and hasattr(value, "aget_tuple"):
            return value
    return None
