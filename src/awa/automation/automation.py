"""`Automation`: turn trigger events into durable, retried, auditable process runs of a system."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from awa._internal.aio import LoopLocal
from awa._internal.logging import get_logger
from awa._internal.util import DurationLike, accepts_keyword, to_timedelta
from awa.automation._support import (
    DueEvents,
    Recording,
    act_on_waits,
    approval_wait,
    cancel_run,
    checkpoint_lost,
    decide_run,
    exclusive,
    keeps_threads,
    latest_checkpoint,
    lease_losses,
    names_timeout,
    no_checkpoint,
    resolve_traces,
    restores_usage,
    retry_run,
    usage_without,
    with_answer,
    with_reviewer,
)
from awa.automation._time import as_utc, iso, require_timedelta
from awa.automation.models import (
    NOTIFY_EVENT_TYPES,
    SLA,
    MigrationPolicy,
    ProcessEvent,
    ProcessEventType,
    ProcessRun,
    RetryPolicy,
    TriggerEvent,
    WaitState,
    output_summary,
    with_history,
)
from awa.automation.notify import Notifier
from awa.automation.store import InMemoryRunStore, RunStore
from awa.automation.templates import Template, TemplateLike, render
from awa.automation.versioning import MigrationItem, plan_migration, system_digest
from awa.automation.waits import event_delivery, parse_wait_request
from awa.contracts.protocols import AgentLike
from awa.contracts.results import RunResult
from awa.contracts.usage import PriceTable, Usage, UsageLimits, cost_or_none
from awa.errors import (
    AwaError,
    ConfigurationError,
    NoPendingInterruptError,
    NoWaitingRunError,
    ProcessRunStateError,
    RunCancelledError,
    RunNotFoundError,
    RunTimeoutError,
    StepRejectedError,
    TemplateError,
    UsageLimitExceededError,
    WaitTimeoutError,
)
from awa.observability.spans import record_span
from awa.runtime._run import usage_of_failure
from awa.runtime.ambient import subscribe_globally
from awa.runtime.cancellation import cancel as cancel_active_run
from awa.runtime.clock import Clock, as_clock
from awa.runtime.context import use_run_id

if TYPE_CHECKING:
    from awa.observability.trace_store import TraceLevel, TraceRecorder, TraceStore
    from awa.persistence.runlog import RunLog

PROCESS_RUN_METADATA_KEY = "awa.process_run_id"
"""`RunContext.metadata` key holding the process run id inside a run started by an automation."""

AUTOMATION_METADATA_KEY = "awa.automation"
"""`RunContext.metadata` key holding the automation's name."""

ATTEMPT_METADATA_KEY = "awa.attempt"
"""`RunContext.metadata` key holding the attempt number (1-based)."""

DEFAULT_RUN_TIMEOUT = 1800.0
"""Seconds an attempt may take unless `run_timeout` says otherwise (30 minutes)."""

ProcessEventSubscriber = Callable[[ProcessEvent], Any]
"""Receives every process event of an automation. May be sync or async."""

_logger = get_logger("automation")

_EARLY_PREFIX = "early:"
_NOTHING = object()


@dataclass(frozen=True)
class Submission:
    """The outcome of `Automation.submit`: the run, and `created=False` for a duplicate event."""

    run: ProcessRun
    created: bool


class Automation:
    """Start a system from events and see every run through: retries, waits, SLAs, audit.

    Each event becomes a durable `ProcessRun`; workers execute queued runs with leases (a crashed
    worker's run is taken over), retry retryable failures with backoff and dead-letter runs that
    keep failing. A paused run is `waiting` until `deliver` or `decide` answers it, possibly days
    later on another worker. Every step is a `ProcessEvent` for subscribers, notifiers and the audit
    log.

    Args:
        system: Any `AgentLike`; it needs a checkpointer for waits, approvals and resumed retries.
        input: Template of the run input (default: the payload as JSON) or a function of the event.
        thread_id: Template of the thread id; runs with the same id continue one conversation.
        dedup: Template of the dedup key (default: the event's `dedup_key`).
        dedup_window: Only runs younger than this count as duplicates.
        deps: The run's `deps`, or a function of the `TriggerEvent` returning them.
        concurrency: Most runs of this automation running at once, across all workers.
        retry: Retry policy (`None`: one attempt).
        version: Version of `system` (default: its `system_digest`).
        previous_versions: Older systems by version, to finish runs pinned to them.
        run_timeout: Wall-clock seconds per attempt (default 30 minutes); a timeout is retryable.
        usage_limits: What one process run may use over all attempts and approval waits; reaching
            one fails the run with `awa.usage_limit_exceeded`, not retried unless `retry` names it.
            A system whose `run`/`resume` takes no `usage_limits` is refused.
        prices: Model prices for `ProcessRun.cost` (default: those of `usage_limits`).
        trace: A `TraceStore`, a level (`"off"`, `"summary"`, `"full"`) or `None` (from
            `AWA_TRACE_LEVEL`, else `summary` for every run kept in a file or database).
        tenant: Tenant recorded on runs that do not name one.
        threads: Whether the system keeps threads (default: detected from its checkpointer).
        run_log: Where every run's steps, attempts, results and approvals are kept, scoped to the
            tenant and the automation's name.
        capture: Keep prompts, answers and tool bodies in the run log (read only by the `auditor`
            role); frozen per run at its first start.

    Example:
        ```python
        automation = Automation(
            invoice_team,
            triggers=[Mailbox("imap.example.com", folder="Invoices"),
                      Schedule("0 6 * * MON-FRI", timezone="Europe/Berlin", name="dunning",
                               input="Run the dunning check for {payload.date}.")],
            input="Process this supplier invoice:\\n{payload.text}",
            thread_id="inv-{payload.subject|slug}",
            retry=RetryPolicy(max_attempts=4, initial="1m"),
            sla=SLA(complete_within="2d", approval_within="4h", remind_every="1h"),
            store=PostgresRunStore(dsn),
        )
        await Worker([automation]).run()
        ```
    """

    def __init__(
        self,
        system: AgentLike,
        *,
        name: str | None = None,
        triggers: Sequence[Any] = (),
        input: TemplateLike = "{payload}",
        thread_id: TemplateLike | None = None,
        dedup: TemplateLike | None = None,
        dedup_window: DurationLike | None = None,
        deps: Any = None,
        concurrency: int = 1,
        retry: RetryPolicy | None = None,
        sla: SLA | None = None,
        store: RunStore | None = None,
        version: str | None = None,
        previous_versions: Mapping[str, AgentLike] | None = None,
        migration: MigrationPolicy = "pin",
        notify: Notifier | Sequence[Notifier] = (),
        notify_events: frozenset[str] = NOTIFY_EVENT_TYPES,
        subscribers: Sequence[ProcessEventSubscriber] = (),
        audit_log: Any = None,
        run_timeout: float | None = DEFAULT_RUN_TIMEOUT,
        usage_limits: UsageLimits | None = None,
        prices: PriceTable | None = None,
        trace: TraceStore | TraceLevel | None = None,
        metadata: Mapping[str, Any] | None = None,
        tenant: str | None = None,
        threads: bool | None = None,
        run_log: RunLog | None = None,
        capture: bool = False,
        clock: Clock | None = None,
    ) -> None:
        self.system = system
        self.name = name or system.name
        if concurrency < 1:
            raise ConfigurationError("concurrency must be >= 1")
        if run_timeout is not None and run_timeout <= 0:
            raise ConfigurationError(
                "run_timeout must be positive (or None to use the run ceiling AWA_RUN_MAX_SECONDS)"
            )
        if migration not in ("pin", "at_wait"):
            raise ConfigurationError(f"migration must be 'pin' or 'at_wait', got {migration!r}")
        names = [trigger.name for trigger in triggers]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ConfigurationError(
                f"Automation {self.name!r}: duplicate trigger {duplicates[0]!r}"
            )
        self.triggers = list(triggers)
        self.input = _compiled(input)
        self.thread_id = _compiled(thread_id) if thread_id is not None else None
        self.dedup = _compiled(dedup) if dedup is not None else None
        self.dedup_window = to_timedelta(dedup_window, what="dedup_window")
        self.deps = deps
        self.concurrency = concurrency
        self.retry_policy = retry if retry is not None else RetryPolicy(max_attempts=1)
        self.sla = sla
        self.store: RunStore = store if store is not None else InMemoryRunStore()
        self.version = version or system_digest(system)
        self.systems: dict[str, AgentLike] = {**dict(previous_versions or {}), self.version: system}
        self.migration: MigrationPolicy = migration
        self.notifiers: list[Notifier] = [notify] if callable(notify) else list(notify)
        self.notify_events = frozenset(notify_events)
        self.subscribers = list(subscribers)
        self.audit_log = audit_log
        self.run_timeout = run_timeout
        self.usage_limits = usage_limits
        if usage_limits is not None:
            for each in self.systems.values():
                _require_usage_limits(each)
        if prices is None and usage_limits is not None:
            prices = usage_limits.prices
        self.prices = prices
        self.traces = resolve_traces(trace, self.store, self.prices)
        self.metadata = dict(metadata or {})
        self.tenant = tenant
        self.threads = keeps_threads(system) if threads is None else threads
        self.run_log = run_log
        self.capture = capture
        self.clock = as_clock(clock)
        # One event per event loop: an automation may outlive the `asyncio.run` it ran in.
        self._wakes: LoopLocal[asyncio.Event] = LoopLocal(asyncio.Event)

    def __repr__(self) -> str:
        return f"Automation({self.name!r}, triggers={[t.name for t in self.triggers]!r})"

    def trigger(self, name: str) -> Any:
        """The trigger called `name`; `ConfigurationError` if there is none."""

        for trigger in self.triggers:
            if trigger.name == name:
                return trigger
        raise ConfigurationError(
            f"Automation {self.name!r} has no trigger {name!r} "
            f"(it has {[t.name for t in self.triggers]})"
        )

    def render(self, event: TriggerEvent, *, trigger: Any = None) -> dict[str, Any]:
        """What `event` would start: the input, thread id and dedup key (for dry runs).

        Raises:
            TemplateError: If a template names a missing field.
        """

        source = trigger if trigger is not None else self._trigger_of(event)
        context = self._context(event)

        def rendered(attribute: str, default: TemplateLike | None, fallback: Any = None) -> Any:
            template = getattr(source, attribute, None) or default
            if template is None:
                return fallback
            value = render(_compiled(template), context, event)
            return value if attribute == "input" else _text(value, attribute)

        return {
            "input": rendered("input", self.input),
            "thread_id": rendered("thread_id", self.thread_id),
            "dedup_key": rendered("dedup", self.dedup, event.dedup_key),
        }

    async def submit(
        self,
        event: TriggerEvent | Mapping[str, Any],
        *,
        trigger: Any = None,
        actor: str | None = None,
        tenant: str | None = None,
    ) -> Submission:
        """Record an event as a queued process run, or recognize a duplicate.

        Args:
            event: The event, or a payload (an event of source `"api"`).
            trigger: The trigger whose template overrides apply; default: the one named by
                `event.source`.

        Raises:
            TemplateError: If a template names a missing field.
        """

        if not isinstance(event, TriggerEvent):
            event = TriggerEvent(source="api", payload=dict(event), received_at=self.clock.now())
        now = as_utc(self.clock.now())
        try:
            rendered = self.render(event, trigger=trigger)
        except TemplateError as exc:
            await self._emit(
                "trigger.failed", None, data={"source": event.source, "error": str(exc)}
            )
            raise
        run = self._new_run(event, rendered, now, actor=actor, tenant=tenant)
        since = now - self.dedup_window if self.dedup_window is not None else None
        stored, created = await self.store.create(run, dedup_since=since)
        if created:
            await self._emit(
                "process.queued",
                stored,
                data={"trigger": event.source, "event_id": event.id, "dedup_key": stored.dedup_key},
            )
            self._wake.set()
        else:
            await self._emit(
                "trigger.duplicate",
                stored,
                data={"trigger": event.source, "event_id": event.id, "dedup_key": run.dedup_key},
            )
        return Submission(stored, created)

    def _new_run(
        self,
        event: TriggerEvent,
        rendered: Mapping[str, Any],
        now: datetime,
        *,
        actor: str | None,
        tenant: str | None,
    ) -> ProcessRun:
        dedup_key, thread_id = rendered["dedup_key"], rendered["thread_id"]
        if tenant is not None:  # another tenant's identical key names another run or thread
            scope = f"{len(tenant)}:{tenant}:"
            dedup_key = dedup_key and scope + dedup_key
            thread_id = thread_id and scope + thread_id
        run = ProcessRun(
            automation=self.name,
            max_attempts=self.retry_policy.max_attempts,
            input=rendered["input"],
            trigger=event.source,
            event_id=event.id,
            dedup_key=dedup_key,
            payload=event.payload,
            created_at=now,
            updated_at=now,
            version=self.version,
            tenant=tenant or self.tenant,
            actor=actor,
            history=[{"at": now.isoformat(), "status": "queued", "note": f"from {event.source}"}],
        )
        run.thread_id = thread_id or (run.id if self.threads else None)
        if self.sla is not None and self.sla.complete_within is not None:
            run.sla_deadline = now + self.sla.complete_within
            if self.sla.warn_at is not None:
                run.sla_warn_at = now + self.sla.complete_within * self.sla.warn_at
        return run

    async def claim(
        self, worker: str, *, lease: DurationLike = timedelta(minutes=5)
    ) -> ProcessRun | None:
        """Claim the next ready run for `worker` (see `RunStore.claim`), or `None`."""

        return await self.store.claim(
            self.name,
            worker=worker,
            lease=require_timedelta(lease, what="lease"),
            now=as_utc(self.clock.now()),
            versions=tuple(self.systems),
            max_running=self.concurrency,
        )

    async def execute(
        self, run: ProcessRun, *, worker: str, lease: DurationLike = timedelta(minutes=5)
    ) -> ProcessRun:
        """Execute one claimed attempt of `run` and record its outcome."""

        lease_length = require_timedelta(lease, what="lease")
        system = self._system_of(run)
        kind: ProcessEventType = "process.resumed" if run.resuming else "process.started"
        await self._emit(kind, run, data={"worker": worker, "version": run.version})
        recorder = await self._begin_trace(run)
        invoke = asyncio.ensure_future(self._invoke(system, run, recorder))
        stopped: list[str] = []
        heartbeat = asyncio.ensure_future(
            self._heartbeat(run, worker, lease_length, invoke, stopped)
        )
        try:
            result = await invoke
        except asyncio.CancelledError as exc:
            heartbeat.cancel()
            current = asyncio.current_task()
            spent = usage_of_failure(exc)  # the calls it made still count
            if stopped and (current is None or not current.cancelling()):
                # Stopped by the lease check: cancelled elsewhere, or another worker took over.
                return await self._stopped(run, worker, spent)
            # The worker is shutting down: hand the run back without counting the attempt.
            await asyncio.shield(self._requeue_after_shutdown(run, worker, spent))
            raise
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the run
            heartbeat.cancel()
            return await self._failed(run, worker, exc)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat
            if recorder is not None:  # closed already, unless cancelled before it began
                recorder.close("cancelled", timeout=0)
        if result.is_interrupted:
            return await self._paused(run, worker, result)
        return await self._succeeded(run, worker, result)

    async def run_pending(
        self, *, worker: str = "local", limit: int | None = None
    ) -> list[ProcessRun]:
        """Execute ready runs one after another in this process until none is ready."""

        done: list[ProcessRun] = []
        while limit is None or len(done) < limit:
            run = await self.claim(worker)
            if run is None:
                break
            done.append(await self.execute(run, worker=worker))
        return done

    async def deliver(
        self,
        key: str,
        payload: Any = None,
        *,
        actor: str | None = None,
        require_waiting: bool = False,
        tenant: str | None = None,
    ) -> list[ProcessRun]:
        """Deliver an external event to the runs waiting on `key` (`wait_for_event`).

        A run resumes once all it waits for is answered. An event no run waits on yet is kept
        (unless `require_waiting`) and answers the first later wait on the key.

        Args:
            tenant: Answer (and keep the event for) only runs of this tenant; `None`: every run.

        Returns:
            The runs that received it (empty when it was kept for later).

        Raises:
            NoWaitingRunError: With `require_waiting`, when no run waits on `key`.
        """

        runs = await self.store.waiting_on(self.name, key)
        if tenant is not None:
            runs = [run for run in runs if run.tenant == tenant]
        if not runs:
            if require_waiting:
                raise NoWaitingRunError(f"No run of {self.name!r} waits on {key!r}")
            await self._keep_early(key, payload, tenant)
            return []
        answered = []
        for run in runs:
            answered.append(
                await self._answer(run.id, key, event_delivery(payload), actor=actor, kind="event")
            )
        return answered

    async def decide(
        self,
        run_id: str,
        decision: Any,
        *,
        interrupt_id: str | None = None,
        actor: str | None = None,
    ) -> ProcessRun:
        """Answer a run waiting on an approval (or any other human interrupt).

        Args:
            decision: For an approval-gated tool `{"action": "approve"}`,
                `{"action": "reject", "note": ...}` or `{"action": "edit", "args": {...}}`.
            interrupt_id: Which approval to answer (default: the oldest open one); the run resumes
                once all are answered.
            actor: The reviewer; added to a dict decision as `reviewer`.

        Raises:
            RunNotFoundError: If there is no such run.
            ProcessRunStateError: If the run waits for no such approval.
        """

        run = await self._require(run_id)
        wait = approval_wait(run, interrupt_id)
        answer = with_reviewer(decision, actor)
        return await self._answer(run.id, wait.key, answer, actor=actor, kind="approval")

    async def retry(self, run_id: str, *, actor: str | None = None) -> ProcessRun:
        """Queue a failed, dead-lettered or cancelled run again, with fresh attempts.

        The run continues from its last checkpoint, or from its input when that is gone.

        Raises:
            RunNotFoundError: If there is no such run.
            ProcessRunStateError: If the run is not finished unsuccessfully.
        """

        current = await self._require(run_id)
        lost = current.resume_value is not None and await no_checkpoint(
            self._system_of(current), current.thread_id
        )
        run = await retry_run(
            self.store, run_id, now=as_utc(self.clock.now()), actor=actor, restart=lost or None
        )
        await self._emit("process.queued", run, data={"retry": True, "actor": actor})
        self._wake.set()
        return run

    async def cancel(
        self, run_id: str, *, reason: str = "cancelled", actor: str | None = None
    ) -> ProcessRun:
        """Cancel an unfinished run; another worker stops it when it next renews its lease.

        Raises:
            RunNotFoundError: If there is no such run.
            ProcessRunStateError: If the run already finished.
        """

        await self._require(run_id)
        run = await cancel_run(
            self.store, run_id, now=as_utc(self.clock.now()), reason=reason, actor=actor
        )
        cancel_active_run(run_id, reason, cancelled_by=actor)
        await self._emit("process.cancelled", run, data={"reason": reason, "actor": actor})
        return run

    async def tick(self) -> int:
        """Requeue runs of crashed workers and act on due SLAs and wait deadlines; how many."""

        now = as_utc(self.clock.now())
        count = 0
        for run in await self.store.expired(self.name, now=now):
            if await self._recover(run.id, now):
                count += 1
        for run in await self.store.due(self.name, now=now):
            if await self._handle_due(run.id, now):
                count += 1
        return count

    async def dead_letters(self, *, limit: int = 100) -> list[ProcessRun]:
        """The dead-lettered runs, newest first."""

        return await self.store.list(automation=self.name, status="dead_lettered", limit=limit)

    async def migration_plan(self) -> list[MigrationItem]:
        """What would happen to unfinished runs of other versions (see `plan_migration`)."""

        runs = await self.store.list(
            automation=self.name, status=("queued", "running", "waiting"), limit=10_000
        )
        known = [version for version in self.systems if version != self.version]
        return plan_migration(runs, current=self.version, policy=self.migration, known=known)

    async def migrate(self) -> list[MigrationItem]:
        """Move the runs `migration_plan` marks `"migrate"` to the current version now."""

        plan = await self.migration_plan()
        now = as_utc(self.clock.now())
        for item in plan:
            if item.action != "migrate":
                continue
            moved: list[bool] = []

            def change(
                current: ProcessRun, item: MigrationItem = item, moved: list[bool] = moved
            ) -> ProcessRun | None:
                # The run may have finished or moved since the plan was made.
                still = current.status in ("queued", "running", "waiting")
                if not still or current.version != item.version:
                    return None
                moved.append(True)
                return self._migrated(current, now)

            run = await self.store.modify(item.run_id, change, now=now)
            if moved:
                await self._emit(
                    "process.migrated", run, data={"from": item.version, "to": self.version}
                )
        return plan

    def _trigger_of(self, event: TriggerEvent) -> Any:
        return next((t for t in self.triggers if t.name == event.source), None)

    def _context(self, event: TriggerEvent) -> dict[str, Any]:
        now = as_utc(self.clock.now())
        return {
            "payload": event.payload,
            "event": event.model_dump(mode="json"),
            "trigger": event.source,
            "automation": self.name,
            "now": now.isoformat(),
            "today": now.date().isoformat(),
        }

    def _event_of(self, run: ProcessRun) -> TriggerEvent:
        return TriggerEvent(
            id=run.event_id or run.id,
            source=run.trigger or "api",
            payload=run.payload if run.payload is not None else {},
            received_at=run.created_at,
            dedup_key=run.dedup_key,
        )

    async def _invoke(
        self, system: AgentLike, run: ProcessRun, recorder: TraceRecorder | None
    ) -> RunResult[Any]:
        """Run the attempt, with its steps, attempts, results and approvals in the run log."""

        if self.run_log is None:
            return await self._traced(system, run, recorder)
        from awa.automation._run_recording import RunRecorder
        from awa.persistence.runlog import Scope
        from awa.workflows.step_output import use_step_output_sink

        scope = Scope(run.tenant or "local", self.name, principal=run.actor, capture=self.capture)
        writer = RunRecorder(self.run_log, scope, run.id, clock=self.clock, prices=self.prices)
        if not await writer.start():
            return await self._traced(system, run, recorder)
        unsubscribe = subscribe_globally(writer)
        try:
            with use_step_output_sink(writer.step_output):
                return await self._traced(system, run, recorder)
        finally:
            unsubscribe()

    async def _traced(
        self, system: AgentLike, run: ProcessRun, recorder: TraceRecorder | None
    ) -> RunResult[Any]:
        if recorder is None:
            return await self._start(system, run)
        async with Recording(recorder) as recording:  # also records a failure before the start
            result = await self._start(system, run)
            recording.finished(result)
            return result

    async def _start(self, system: AgentLike, run: ProcessRun) -> RunResult[Any]:
        deps = self.deps(self._event_of(run)) if callable(self.deps) else self.deps
        if inspect.isawaitable(deps):
            deps = await deps
        options: dict[str, Any] = {} if deps is None else {"deps": deps}
        metadata = {
            **self.metadata,
            PROCESS_RUN_METADATA_KEY: run.id,
            AUTOMATION_METADATA_KEY: self.name,
            ATTEMPT_METADATA_KEY: run.attempts,
            "awa.request_id": run.id,
        }
        if run.tenant:
            metadata["awa.tenant"] = run.tenant
        if run.actor:
            metadata["awa.actor"] = run.actor
        thread = run.thread_id if self.threads else None
        async with exclusive(system):
            with use_run_id(run.id):
                return await self._attempt(system, run, thread, options, metadata)

    async def _attempt(
        self,
        system: AgentLike,
        run: ProcessRun,
        thread: str | None,
        options: dict[str, Any],
        metadata: dict[str, Any],
    ) -> RunResult[Any]:
        resume = getattr(system, "resume", None)
        started_before = sum(1 for entry in run.history if entry.get("status") == "running") > 1
        if thread is not None and resume is not None:
            if run.resuming:
                if await no_checkpoint(system, thread):
                    raise checkpoint_lost(run)
                self._limit(system, run, options, restores=True)
                try:
                    return await self._call(resume, (thread, run.resume_value), options, metadata)
                except NoPendingInterruptError as exc:
                    raise checkpoint_lost(run) from exc
            if started_before and await self._progressed(system, run):
                # A retry continues the thread from the last step the failed attempt finished.
                self._limit(system, run, options, restores=True)
                try:
                    return await self._call(resume, (thread, run.resume_value), options, metadata)
                except NoPendingInterruptError:
                    pass
            elif not started_before:
                await self._remember_checkpoint(system, run)
        if thread is not None:
            options["thread_id"] = thread
        self._limit(system, run, options, restores=False)
        return await self._call(system.run, (run.input,), options, metadata)

    def _limit(
        self, system: AgentLike, run: ProcessRun, options: dict[str, Any], *, restores: bool
    ) -> None:
        """Give the attempt the run's remaining budget, counting restored usage once."""

        run._restored = run.usage_paused if restores and restores_usage(system) else None
        if self.usage_limits is not None:
            options["usage_limits"] = _remaining(
                self.usage_limits, usage_without(run.usage, run._restored), self._cost_of
            )

    async def _call(
        self,
        method: Callable[..., Any],
        args: tuple[Any, ...],
        options: dict[str, Any],
        metadata: dict[str, Any],
    ) -> RunResult[Any]:
        kwargs = dict(options)
        if accepts_keyword(method, "metadata"):
            kwargs["metadata"] = metadata
        if self.run_timeout is not None and names_timeout(method):
            kwargs["timeout"] = self.run_timeout
            result = await method(*args, **kwargs)
        else:  # a system without a timeout of its own is cut off here
            async with asyncio.timeout(self.run_timeout):
                result = await method(*args, **kwargs)
        if not isinstance(result, RunResult):
            raise ProcessRunStateError(
                f"System of automation {self.name!r} returned {type(result).__name__}, "
                "not a RunResult"
            )
        return result

    async def _progressed(self, system: AgentLike, run: ProcessRun) -> bool:
        latest = await latest_checkpoint(system, run.thread_id)
        return latest is not None and latest != run.thread_checkpoint

    async def _remember_checkpoint(self, system: AgentLike, run: ProcessRun) -> None:
        latest = await latest_checkpoint(system, run.thread_id)
        if latest is None:
            return
        run.thread_checkpoint = latest
        now = as_utc(self.clock.now())

        def change(current: ProcessRun) -> ProcessRun | None:
            if not current.held_by(run):
                return None
            return current.model_copy(update={"thread_checkpoint": latest})

        await self.store.modify(run.id, change, now=now)

    async def _heartbeat(
        self,
        run: ProcessRun,
        worker: str,
        lease: timedelta,
        attempt: asyncio.Future[Any],
        stopped: list[str],
    ) -> None:
        """Renew the lease every third of it; stop the attempt once the lease is gone.

        When renewing keeps failing until the lease would expire before the next beat, the worker
        fences itself instead of running on while another worker may claim the run.
        """

        interval = max(0.05, lease.total_seconds() / 3)
        expires = run.lease_expires_at or as_utc(self.clock.now()) + lease
        fenced = accepts_keyword(self.store.renew, "epoch")
        while True:
            await asyncio.sleep(interval)
            now = as_utc(self.clock.now())
            try:
                if fenced:
                    kept = await self.store.renew(
                        run.id, worker=worker, lease=lease, now=now, epoch=run.lease_epoch
                    )
                else:
                    kept = await self.store.renew(run.id, worker=worker, lease=lease, now=now)
            except Exception as exc:  # noqa: BLE001 - retried on the next beat while the lease holds
                left = expires - as_utc(self.clock.now())
                if left.total_seconds() > interval:
                    _logger.warning("Could not renew the lease of %s: %r", run.id, exc)
                    continue
                _logger.error(
                    "Could not renew the lease of %s before it runs out (%r); stopping the "
                    "attempt so that no other worker runs it at the same time",
                    run.id,
                    exc,
                )
                self._stop_attempt(run, worker, attempt, stopped, "lease expired")
                return
            if kept:
                expires = now + lease
                continue
            current = await self.store.get(run.id)
            reason = (
                "cancelled"
                if current is not None and current.status == "cancelled"
                else "lease lost"
            )
            self._stop_attempt(run, worker, attempt, stopped, reason)
            return

    def _stop_attempt(
        self,
        run: ProcessRun,
        worker: str,
        attempt: asyncio.Future[Any],
        stopped: list[str],
        reason: str,
    ) -> None:
        """Stop the running attempt of `run` (its whole run tree for an awa system)."""

        stopped.append(reason)
        record_span(
            "awa.lease.stop",
            {
                "awa.process_run.id": run.id,
                "awa.process_run.attempt": run.attempts,
                "awa.lease.epoch": run.lease_epoch,
                "awa.lease.worker": worker,
                "awa.lease.reason": reason,
            },
        )
        if not cancel_active_run(run.id, reason, cancelled_by=worker):
            attempt.cancel()

    @property
    def _wake(self) -> asyncio.Event:
        """Set when a run became ready; workers in the running event loop wait on it."""

        return self._wakes.get()

    def _system_of(self, run: ProcessRun) -> AgentLike:
        """The system `run` is pinned to."""

        return self.systems.get(run.version or self.version, self.system)

    def _cost_of(self, usage: Usage) -> float | None:
        """What `usage` cost under the automation's prices; `None` without prices or a model's price."""

        cost = cost_or_none(usage, self.prices)
        if cost is None and self.prices is not None:
            _logger.warning(
                "Automation %s: a model used has no price; cost not recorded", self.name
            )
        return cost

    def _counted(self, run: ProcessRun, current: ProcessRun, spent: Usage) -> dict[str, Any]:
        """The usage and cost of `current` plus `spent`, without what the system restored."""

        total = current.usage.snapshot()
        total.merge(usage_without(spent, run._restored))
        return {"usage": total, "cost": self._cost_of(total)}

    async def _require(self, run_id: str) -> ProcessRun:
        run = await self.store.get(run_id)
        if run is None or run.automation != self.name:
            raise RunNotFoundError(f"No process run {run_id!r} in automation {self.name!r}")
        return run

    async def _emit(
        self,
        kind: ProcessEventType,
        run: ProcessRun | None,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        event = ProcessEvent(
            type=kind,
            automation=self.name,
            run_id=run.id if run else None,
            timestamp=as_utc(self.clock.now()),
            status=run.status if run else None,
            attempt=run.attempts if run else 0,
            data={key: value for key, value in (data or {}).items() if value is not None},
        )
        targets: list[Callable[[ProcessEvent], Any]] = list(self.subscribers)
        if kind in self.notify_events:
            targets.extend(self.notifiers)
        for target in targets:
            try:
                outcome = target(event)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:  # noqa: BLE001 - observers never fail the process
                _logger.exception("Process event subscriber failed on %s", kind)
        if self.audit_log is not None:
            try:
                self.audit_log.record(
                    kind,
                    {**event.data, "status": event.status, "attempt": event.attempt},
                    run_id=event.run_id,
                    root_run_id=event.run_id,
                    agent=self.name,
                    actor=run.actor if run else None,
                    tenant=run.tenant if run else None,
                    request_id=event.run_id,
                )
            except Exception:  # noqa: BLE001 - see above
                _logger.exception("Audit log failed on %s", kind)

    def _migrated(self, run: ProcessRun, now: datetime) -> ProcessRun:
        return with_history(
            run.model_copy(update={"version": self.version}),
            now,
            run.status,
            f"migrated from {run.version} to {self.version}",
        )

    def _early_key(self, key: str, tenant: str | None) -> str:
        scope = f"{self.name}:{key}" if tenant is None else f"{self.name}\x1f{tenant}:{key}"
        return f"{_EARLY_PREFIX}{scope}"

    async def _keep_early(self, key: str, payload: Any, tenant: str | None = None) -> None:
        state_key = self._early_key(key, tenant)
        value = json.dumps({"payload": payload}, default=str)
        current = await self.store.get_state(state_key)
        await self.store.compare_and_set_state(state_key, current, value)
        await self._emit("process.delivered", None, data={"key": key, "kept": True})

    async def _take_early(self, key: str, tenant: str | None) -> Any:
        """The event kept for `key`: one delivered for the run's tenant, else an untenanted one."""

        for scope in [tenant, None] if tenant is not None else [None]:
            state_key = self._early_key(key, scope)
            current = await self.store.get_state(state_key)
            if current and await self.store.compare_and_set_state(state_key, current, ""):
                return json.loads(current).get("payload")
        return _NOTHING

    async def _recover(self, run_id: str, now: datetime) -> bool:
        from awa.runtime.termination import ceilings

        limit = ceilings().max_lease_losses

        def change(current: ProcessRun) -> ProcessRun | None:
            if (
                current.status != "running"
                or current.lease_expires_at is None
                or current.lease_expires_at >= now
            ):
                return None
            update, note = _after_lease_expiry(current, limit)
            return with_history(
                current.model_copy(
                    update={"lease_owner": None, "lease_expires_at": None, **update}
                ),
                now,
                update["status"],
                note,
            )

        before = await self.store.get(run_id)
        stored = await self.store.modify(run_id, change, now=now)
        if before is None or stored.revision == before.revision:
            return False
        kind: ProcessEventType = (
            "process.dead_lettered" if stored.status == "dead_lettered" else "process.recovered"
        )
        await self._emit(kind, stored, data={"worker": before.lease_owner, "error": stored.error})
        self._wake.set()
        return True

    async def _handle_due(self, run_id: str, now: datetime) -> bool:
        events: DueEvents = []
        cancel_reason: list[str] = []

        def change(current: ProcessRun) -> ProcessRun | None:
            events.clear()
            cancel_reason.clear()
            if current.is_finished:
                return None
            updated = current.model_copy(deep=True)
            if updated.sla_warn_at and not updated.sla_warned and updated.sla_warn_at <= now:
                updated.sla_warned = True
                events.append(("process.sla_warning", {"deadline": iso(updated.sla_deadline)}))
            if updated.sla_deadline and not updated.sla_breached and updated.sla_deadline <= now:
                updated.sla_breached = True
                events.append(("process.sla_breached", {"deadline": iso(updated.sla_deadline)}))
                if self.sla is not None and self.sla.on_breach == "cancel":
                    cancel_reason.append("SLA breached")
            if updated.status == "waiting":
                updated = act_on_waits(updated, now, events)
            if not events:
                return None
            if updated.status == "failed":
                updated = with_history(updated, now, "failed", "wait timed out")
            return updated

        stored = await self.store.modify(run_id, change, now=now)
        for kind, data in events:
            await self._emit(kind, stored, data=data)
        if stored.status == "failed" and stored.error_code == WaitTimeoutError.code:
            await self._emit("process.failed", stored, data={"error": stored.error})
        if cancel_reason:
            with contextlib.suppress(ProcessRunStateError):
                await self.cancel(run_id, reason=cancel_reason[0], actor="sla")
        if stored.status == "queued":
            self._wake.set()
        return bool(events)

    async def _answer(
        self, run_id: str, key: str, answer: Any, *, actor: str | None, kind: str
    ) -> ProcessRun:
        now = as_utc(self.clock.now())
        migrated: list[str | None] = []

        def migrate(run: ProcessRun) -> ProcessRun:
            if self.migration == "at_wait" and run.version != self.version:
                migrated.append(run.version)
                return self._migrated(run, now)
            return run

        def change(current: ProcessRun) -> ProcessRun | None:
            migrated.clear()
            return with_answer(current, key, answer, now=now, kind=kind, migrate=migrate)

        before = await self._require(run_id)
        stored = await self.store.modify(run_id, change, now=now)
        if stored.revision == before.revision:
            raise ProcessRunStateError(f"Run {run_id!r} does not wait on {key!r}")
        await self._emit(
            "process.delivered", stored, data={"key": key, "kind": kind, "actor": actor}
        )
        if migrated:
            await self._emit(
                "process.migrated", stored, data={"from": migrated[0], "to": self.version}
            )
        if stored.status == "queued":
            self._wake.set()
        return stored

    async def _stopped(
        self, run: ProcessRun, worker: str, spent: Usage | None = None
    ) -> ProcessRun:
        """The attempt was stopped: cancelled, or its lease went to another worker."""

        current = await self._charge(run, spent) or await self.store.get(run.id)
        if current is None or current.status != "cancelled":
            await self._emit("process.lease_lost", current or run, data={"worker": worker})
        return current or run

    async def _charge(self, run: ProcessRun, spent: Usage | None) -> ProcessRun | None:
        """Add the usage of an attempt without an outcome to the stored run, whatever its status."""

        if spent is None or not spent.requests:
            return None

        def add(current: ProcessRun) -> ProcessRun:
            return current.model_copy(update=self._counted(run, current, spent))

        try:
            return await self.store.modify(run.id, add, now=as_utc(self.clock.now()))
        except RunNotFoundError:
            return None

    def _finish(
        self,
        run: ProcessRun,
        worker: str,
        update: dict[str, Any],
        note: str,
        spent: Usage | None = None,
    ) -> Callable[[ProcessRun], ProcessRun | None]:
        now = as_utc(self.clock.now())

        def change(current: ProcessRun) -> ProcessRun | None:
            if (
                current.status != "running"
                or current.lease_owner != worker
                or current.lease_epoch != run.lease_epoch
            ):
                return None  # cancelled meanwhile, or another claim (stale epoch) took over
            status = update.get("status", current.status)
            counted = (
                self._counted(run, current, spent) if spent is not None and spent.requests else {}
            )
            return with_history(
                current.model_copy(
                    update={
                        "lease_owner": None,
                        "lease_expires_at": None,
                        "resuming": False,
                        **counted,
                        **update,
                    }
                ),
                now,
                status,
                note,
            )

        return change

    async def _save(
        self,
        run: ProcessRun,
        worker: str,
        update: dict[str, Any],
        note: str,
        spent: Usage | None = None,
    ) -> tuple[ProcessRun, bool]:
        now = as_utc(self.clock.now())
        stored = await self.store.modify(
            run.id, self._finish(run, worker, update, note, spent), now=now
        )
        applied = stored.status == update.get("status") and stored.lease_owner is None
        if not applied:
            await self._emit(
                "process.lease_lost", stored, data={"worker": worker, "status": stored.status}
            )
        return stored, applied

    async def _succeeded(self, run: ProcessRun, worker: str, result: RunResult[Any]) -> ProcessRun:
        stored, applied = await self._save(
            run,
            worker,
            {
                "status": "succeeded",
                "output": output_summary(result.output),
                "error": None,
                "error_code": None,
                "waits": [],
                "resume_value": None,
            },
            "succeeded",
            result.usage,
        )
        if applied:
            await self._emit(
                "process.succeeded",
                stored,
                data={
                    "duration_ms": result.duration_ms,
                    "tokens": result.usage.total_tokens,
                    "cost": stored.cost,
                },
            )
        return stored

    async def _paused(self, run: ProcessRun, worker: str, result: RunResult[Any]) -> ProcessRun:
        now = as_utc(self.clock.now())
        waits = [self._wait_state(item, now) for item in result.interrupts]
        # A team or network restores what it had used when it resumes: keep that number.
        paused = result.usage.snapshot() if restores_usage(self._system_of(run)) else None
        stored, applied = await self._save(
            run,
            worker,
            {"status": "waiting", "waits": waits, "resume_value": None, "usage_paused": paused},
            "waiting for " + ", ".join(wait.key for wait in waits),
            result.usage,
        )
        if not applied:
            return stored
        await self._emit(
            "process.waiting",
            stored,
            data={
                "waits": [
                    {
                        "key": w.key,
                        "kind": w.kind,
                        "deadline": w.deadline.isoformat() if w.deadline else None,
                    }
                    for w in waits
                ]
            },
        )
        for wait in waits:
            if wait.kind == "event":
                early = await self._take_early(wait.key, stored.tenant)
                if early is not _NOTHING:
                    stored = await self._answer(
                        stored.id, wait.key, event_delivery(early), actor=None, kind="event"
                    )
        return stored

    def _wait_state(self, interrupt: Mapping[str, Any], now: datetime) -> WaitState:
        interrupt_id = str(interrupt.get("id", ""))
        value = interrupt.get("value")
        request = parse_wait_request(value)
        if request is not None:
            return WaitState(
                interrupt_id=interrupt_id,
                kind="event",
                key=request.key,
                requested_at=now,
                deadline=now + request.timeout if request.timeout is not None else None,
                on_timeout=request.on_timeout,
                remind_every=request.remind_every,
                next_reminder_at=(
                    now + request.remind_every if request.remind_every is not None else None
                ),
                value=request.model_dump(mode="json"),
            )
        sla = self.sla
        remind = sla.remind_every if sla is not None else None
        return WaitState(
            interrupt_id=interrupt_id,
            kind="approval",
            key=f"approval:{interrupt_id}",
            requested_at=now,
            remind_every=remind,
            next_reminder_at=now + remind if remind else None,
            escalate_at=now + sla.approval_within if sla and sla.approval_within else None,
            value=value,
        )

    async def _failed(self, run: ProcessRun, worker: str, error: Exception) -> ProcessRun:
        message = f"{type(error).__name__}: {error}"[:2000]
        spent = usage_of_failure(error)
        if isinstance(error, RunCancelledError):
            current = await self._charge(run, spent) or await self.store.get(run.id)
            if current is not None and current.status == "cancelled":
                return current
            if error.reason in ("lease lost", "lease expired"):
                await self._emit("process.lease_lost", run, data={"worker": worker})
                return current or run
        if not isinstance(error, StepRejectedError):
            return await self._retry_or_fail(run, worker, error, message, spent)
        # A special case or a person turned the item down: a decision, never retried.
        stored, applied = await self._save(
            run,
            worker,
            {"status": "rejected", "error": message, "error_code": error.code},
            message[:200],
            spent,
        )
        if applied:
            await self._emit(
                "process.rejected",
                stored,
                data={"error": message, "step": error.step, "case": error.details.get("case")},
            )
        return stored

    async def _retry_or_fail(
        self, run: ProcessRun, worker: str, error: Exception, message: str, spent: Usage | None
    ) -> ProcessRun:
        now = as_utc(self.clock.now())
        code = error.code if isinstance(error, AwaError) else None
        # A limit of the run was reached (also its `max_duration`): retrying would only
        # spend more, so it fails at once unless the retry policy names this code.
        limit_reached = isinstance(error, UsageLimitExceededError) or (
            isinstance(error, RunTimeoutError) and error.limit_name == "max_duration"
        )
        if limit_reached:
            code = UsageLimitExceededError.code
            retryable = code in self.retry_policy.retry_on
        else:
            retryable = self.retry_policy.is_retryable(error)
        if retryable and run.attempts < run.max_attempts:
            delay = self.retry_policy.delay(run.attempts, error)
            stored, applied = await self._save(
                run,
                worker,
                {
                    "status": "queued",
                    "error": message,
                    "error_code": code,
                    "next_attempt_at": now + delay,
                },
                f"attempt {run.attempts} failed; retry in {delay.total_seconds():.0f}s",
                spent,
            )
            if applied:
                await self._emit(
                    "process.retry_scheduled",
                    stored,
                    data={"error": message, "delay_seconds": delay.total_seconds()},
                )
                self._wake.set()
            return stored
        status = "dead_lettered" if retryable else "failed"
        stored, applied = await self._save(
            run,
            worker,
            {"status": status, "error": message, "error_code": code},
            message[:200],
            spent,
        )
        if applied:
            await self._emit(
                "process.dead_lettered" if retryable else "process.failed",
                stored,
                data={
                    "error": message,
                    "code": code,
                    "tokens": stored.usage.total_tokens,
                    "cost": stored.cost,
                },
            )
        return stored

    async def _requeue_after_shutdown(
        self, run: ProcessRun, worker: str, spent: Usage | None = None
    ) -> None:
        try:
            update = {
                "status": "queued",
                "attempts": max(0, run.attempts - (0 if run.resuming else 1)),
                "resuming": run.resuming,
            }
            now = as_utc(self.clock.now())
            stored = await self.store.modify(
                run.id, self._finish(run, worker, update, "worker stopped", spent), now=now
            )
            await self._emit(
                "process.requeued", stored, data={"worker": worker, "reason": "shutdown"}
            )
        except Exception:  # noqa: BLE001 - the lease expires and recovery requeues it
            _logger.warning("Could not hand back run %s on shutdown", run.id, exc_info=True)

    async def _begin_trace(self, run: ProcessRun) -> TraceRecorder | None:
        """Start the trace of this attempt and note its file on the run; `None` when off."""

        if self.traces is None:
            return None
        try:
            path = self.traces.attempt_path(run.id, resume=run.resuming)
            recorder = self.traces.recorder(
                path,
                root_run_id=run.id,
                automation=self.name,
                process_run=run.id,
                attempt=run.attempts,
                resumed=run.resuming or None,
                trigger=run.trigger,
                thread_id=run.thread_id,
                tenant=run.tenant,
                actor=run.actor,
                version=run.version,
            )
        except Exception:  # noqa: BLE001 - a trace never fails the run it describes
            _logger.warning("Could not start the trace of %s", run.id, exc_info=True)
            return None
        reference = path.name
        run.trace = reference

        def note(current: ProcessRun) -> ProcessRun | None:
            if not current.held_by(run) or current.trace == reference:
                return None
            return current.model_copy(update={"trace": reference})

        try:
            await self.store.modify(run.id, note, now=as_utc(self.clock.now()))
        except asyncio.CancelledError:
            recorder.close("cancelled", timeout=0)
            raise
        except Exception:  # noqa: BLE001
            _logger.warning("Could not note the trace on run %s", run.id, exc_info=True)
        return recorder


def _after_lease_expiry(run: ProcessRun, limit: int) -> tuple[dict[str, Any], str]:
    owner = run.lease_owner
    lost = lease_losses(run) + 1
    if run.attempts >= run.max_attempts and not run.resuming:
        exhausted = {
            "status": "dead_lettered",
            "error": f"WorkerLost: worker {owner} stopped renewing its lease",
            "error_code": "awa.worker_lost",
        }
        return exhausted, "lease expired; attempts exhausted"
    if lost >= limit:
        # A run that kills its worker (a resume is not counted as an attempt) would be
        # taken over and crash the next worker forever.
        looping = {
            "status": "dead_lettered",
            "error": f"WorkerLost: the run lost its worker {lost} times in a row (last "
            f"{owner}); it is not taken over again (AWA_MAX_LEASE_LOSSES={limit})",
            "error_code": "awa.resume_crash_loop",
        }
        return looping, f"lease expired {lost} times in a row; dead-lettered"
    return {"status": "queued", "next_attempt_at": None}, f"lease of {owner} expired; requeued"


def _require_usage_limits(system: AgentLike) -> None:
    """Refuse a system that cannot take `usage_limits`: its limits would silently not apply."""

    for name in ("run", "resume"):
        method = getattr(system, name, None)
        if method is not None and not accepts_keyword(method, "usage_limits"):
            raise ConfigurationError(
                f"{type(system).__name__}.{name} does not take `usage_limits`, so the limits of "
                "this automation could not be enforced; give it the keyword or remove the limits"
            )


def _remaining(
    limits: UsageLimits, spent: Usage, cost: Callable[[Usage], float | None]
) -> UsageLimits:
    """`limits` less what earlier attempts and resumptions used (one budget per run)."""

    if not spent.requests:
        return limits

    def left(limit: float | None, used: float) -> Any:
        return None if limit is None else max(0, limit - used)

    return limits.model_copy(
        update={
            "max_requests": left(limits.max_requests, spent.requests),
            "max_input_tokens": left(limits.max_input_tokens, spent.input_tokens),
            "max_output_tokens": left(limits.max_output_tokens, spent.output_tokens),
            "max_total_tokens": left(limits.max_total_tokens, spent.total_tokens),
            "max_tool_calls": left(limits.max_tool_calls, spent.tool_calls),
            "max_cost": left(limits.max_cost, cost(spent) or 0.0),
        }
    )


def _compiled(template: TemplateLike) -> TemplateLike:
    return Template(template) if isinstance(template, str) else template


def _text(value: Any, what: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise TemplateError(f"The {what} template rendered an empty value")
    if len(text) > 255:
        raise TemplateError(f"The {what} template rendered more than 255 characters")
    return text


__all__ = [
    "ATTEMPT_METADATA_KEY",
    "AUTOMATION_METADATA_KEY",
    "DEFAULT_RUN_TIMEOUT",
    "PROCESS_RUN_METADATA_KEY",
    "Automation",
    "ProcessEventSubscriber",
    "Submission",
    "cancel_run",
    "decide_run",
    "retry_run",
]
