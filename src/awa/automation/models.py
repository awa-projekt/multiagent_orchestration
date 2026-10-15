"""The data of process automation: trigger events, process runs, policies and process events."""

from __future__ import annotations

import random
from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from awa._internal.ids import new_id
from awa._internal.retry import backoff_delay
from awa._internal.util import preview, to_timedelta
from awa.automation._time import as_utc, utcnow
from awa.contracts.usage import Usage
from awa.errors import AwaError

RunStatus: TypeAlias = Literal[
    "queued", "running", "waiting", "succeeded", "failed", "dead_lettered", "cancelled", "rejected"
]
"""Where a process run stands.

- `queued`: waiting for a worker (also while a retry's backoff runs, see
  `ProcessRun.next_attempt_at`, and after a wait was answered).
- `running`: a worker holds its lease and executes it.
- `waiting`: paused on `wait_for_event` or an approval.
- `succeeded`, `failed` (a non-retryable error), `dead_lettered` (retries
  exhausted), `cancelled`, `rejected` (a special case or a person turned the item
  down; `StepRejectedError`, never retried): finished.
"""

TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "failed", "dead_lettered", "cancelled", "rejected"}
)
"""Statuses of finished runs."""

ACTIVE_STATUSES: frozenset[str] = frozenset({"queued", "running", "waiting"})
"""Statuses of runs that are not finished."""

MigrationPolicy: TypeAlias = Literal["pin", "at_wait"]
"""How runs started on an older version continue: `pin` keeps them on their version;
`at_wait` moves a waiting run to the current version when it resumes."""

DEFAULT_RETRY_ON: tuple[str, ...] = ("awa.model_rate_limit", "awa.run_timeout", "awa.tool_timeout")
"""Error codes `RetryPolicy` retries by default (besides any `retryable` library error)."""

OUTPUT_CHARS = 4000
"""How much of a run's output a `ProcessRun` keeps."""
_MAX_HISTORY = 50


class TriggerEvent(BaseModel):
    """Something happened that may start a process run.

    Attributes:
        source: Name of the trigger that produced it.
        payload: The event's data (JSON-compatible).
        dedup_key: Identifies the business event; two events with the same key start one run.
            `None` disables deduplication unless the automation's `dedup` template sets one.
        metadata: Extra details of the source (headers, file path), not visible as `payload`.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id("evt"))
    source: str
    payload: Any = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=utcnow)
    dedup_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("received_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return as_utc(value)


class WaitState(BaseModel):
    """One thing a waiting run waits for: an external event or a human approval.

    Attributes:
        kind: `"event"` (`wait_for_event`) or `"approval"` (any other interrupt).
        key: Correlation key an event is delivered to (`"approval:<interrupt id>"` for approvals).
        on_timeout: At the deadline: `"fail"`, `"continue"` (resume with `None`) or `"escalate"`
            (notify, keep waiting).
        escalate_at: When an unanswered wait is escalated (approval SLA).
        value: The interrupt's payload (what the run asked for).
    """

    interrupt_id: str
    kind: Literal["event", "approval"] = "event"
    key: str
    requested_at: datetime
    deadline: datetime | None = None
    on_timeout: Literal["fail", "continue", "escalate"] = "fail"
    remind_every: timedelta | None = None
    next_reminder_at: datetime | None = None
    reminders_sent: int = 0
    escalate_at: datetime | None = None
    escalated: bool = False
    answered: bool = False
    answer: Any = None
    value: Any = None


class ProcessRun(BaseModel):
    """The durable record of one process run: what started it, where it stands, how it ended.

    Attributes:
        id: Unique id of the run (also the awa run id of its attempts).
        thread_checkpoint: The thread's latest checkpoint before the first attempt; a retry
            resumes the thread when it moved past it.
        output: The start of the final output, once it succeeded.
        error_code: `AwaError.code` of the last error (`awa.usage_limit_exceeded` for a run limit).
        usage: What all attempts and resumptions used, failed ones included.
        cost: What `usage` cost under the automation's prices; `None` without prices.
        usage_paused: What a team or network had used when it last paused (it restores that).
        trace: File name of the latest attempt's trace; `None` when not traced.
        next_attempt_at: A queued run is not started before this time (retry backoff).
        version: Version (digest) of the system the run is pinned to.
        resume_value: The answer a queued run resumes with, once all waits are answered.
        lease_epoch: Incremented on every claim (a fencing token): writes of an attempt whose
            epoch is no longer current are refused.
        revision: Incremented on every change (optimistic concurrency).
        history: Short log of status changes (`at`, `status`, `note`).
    """

    id: str = Field(default_factory=lambda: new_id("proc"))
    automation: str
    status: RunStatus = "queued"
    attempts: int = 0
    max_attempts: int = 1
    thread_id: str | None = None
    thread_checkpoint: str | None = None
    input: Any = ""
    trigger: str | None = None
    event_id: str | None = None
    dedup_key: str | None = None
    payload: Any = None
    output: str | None = None
    error: str | None = None
    error_code: str | None = None
    usage: Usage = Field(default_factory=Usage)
    cost: float | None = None
    usage_paused: Usage | None = None
    trace: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    next_attempt_at: datetime | None = None
    sla_deadline: datetime | None = None
    sla_warn_at: datetime | None = None
    sla_warned: bool = False
    sla_breached: bool = False
    version: str | None = None
    waits: list[WaitState] = Field(default_factory=list)
    resume_value: Any = None
    resuming: bool = False
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    lease_epoch: int = 0
    tenant: str | None = None
    actor: str | None = None
    revision: int = 0
    history: list[dict[str, Any]] = Field(default_factory=list)
    # What the system of the running attempt restored (see `usage_paused`); not stored.
    _restored: Usage | None = PrivateAttr(default=None)

    def held_by(self, attempt: ProcessRun) -> bool:
        """Whether this run is still leased to the claim `attempt` came from (worker and epoch)."""

        return (
            self.status == "running"
            and self.lease_owner == attempt.lease_owner
            and self.lease_epoch == attempt.lease_epoch
        )

    @property
    def is_finished(self) -> bool:
        """Whether the run reached a final status."""

        return self.status in TERMINAL_STATUSES

    @property
    def wait_keys(self) -> list[str]:
        """Correlation keys of the unanswered waits."""

        return [wait.key for wait in self.waits if not wait.answered]

    def due_at(self) -> datetime | None:
        """The next time the run needs attention without a worker's help (SLA, wait deadlines)."""

        if self.is_finished:
            return None
        times: list[datetime] = []
        if self.sla_warn_at is not None and not self.sla_warned:
            times.append(self.sla_warn_at)
        if self.sla_deadline is not None and not self.sla_breached:
            times.append(self.sla_deadline)
        if self.status == "waiting":
            for wait in self.waits:
                if wait.answered:
                    continue
                times.extend(
                    moment
                    for moment in (
                        wait.deadline,
                        wait.next_reminder_at,
                        None if wait.escalated else wait.escalate_at,
                    )
                    if moment is not None
                )
        return min(times) if times else None

    def summary(self) -> dict[str, Any]:
        """A compact JSON view (for listings)."""

        return {
            "id": self.id,
            "automation": self.automation,
            "status": self.status,
            "attempts": self.attempts,
            "trigger": self.trigger,
            "thread_id": self.thread_id,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "error": self.error,
            "error_code": self.error_code,
            "requests": self.usage.requests,
            "tokens": self.usage.total_tokens,
            "cost": self.cost,
            "version": self.version,
            "waiting_for": self.wait_keys,
        }


def output_summary(value: Any) -> str:
    """The start of a run output as text (kept on the `ProcessRun`)."""

    if isinstance(value, BaseModel):
        text = value.model_dump_json()
    else:
        text = "" if value is None else str(value)
    return preview(text, OUTPUT_CHARS)


def with_history(run: ProcessRun, now: datetime, status: str, note: str) -> ProcessRun:
    """`run` with a history entry appended; the last 50 entries are kept."""

    entry = {"at": now.isoformat(), "status": status, "note": note}
    return run.model_copy(update={"history": [*run.history, entry][-_MAX_HISTORY:]})


class RetryPolicy(BaseModel):
    """How failed attempts of a process run are retried.

    Retried are `AwaError`s flagged `retryable` (rate limits, outages, timeouts), codes in
    `retry_on` and instances of `retry_on_types`; other errors fail the run, and after
    `max_attempts` it is dead-lettered. The delay before attempt n+1 is `initial * backoff**(n-1)`,
    capped at `max_delay`, plus up to `jitter` of it; a longer provider `retry_after` wins.

    Attributes:
        max_attempts: Attempts in total, the first included.
        retry_on_types: Exception types to retry (not serializable; code only).

    Example:
        ```python
        RetryPolicy(max_attempts=5, initial="30s", backoff=2.0, max_delay="1h")
        ```
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    max_attempts: int = Field(default=3, ge=1)
    initial: timedelta = timedelta(seconds=30)
    backoff: float = Field(default=2.0, ge=1.0)
    max_delay: timedelta = timedelta(hours=1)
    jitter: float = Field(default=0.0, ge=0.0, le=1.0)
    retry_on: tuple[str, ...] = DEFAULT_RETRY_ON
    retry_on_types: tuple[type[BaseException], ...] = Field(default=(), exclude=True)

    @field_validator("initial", "max_delay", mode="before")
    @classmethod
    def _duration(cls, value: Any) -> Any:
        return to_timedelta(value, what="retry delay") if value is not None else value

    def is_retryable(self, error: BaseException) -> bool:
        """Whether `error` should be retried (attempts left are not checked)."""

        if self.retry_on_types and isinstance(error, self.retry_on_types):
            return True
        if isinstance(error, AwaError):
            return bool(error.retryable) or error.code in self.retry_on
        return isinstance(error, TimeoutError)

    def delay(
        self,
        attempt: int,
        error: BaseException | None = None,
        *,
        rand: Callable[[], float] = random.random,
    ) -> timedelta:
        """The delay after attempt `attempt` (1-based); `rand` supplies the jitter in `[0, 1)`."""

        seconds = backoff_delay(
            attempt - 1,
            initial=self.initial.total_seconds(),
            factor=self.backoff,
            cap=self.max_delay.total_seconds(),
        )
        if self.jitter:  # added on top: jitter never shortens the delay
            seconds += seconds * self.jitter * rand()
        hint = getattr(error, "retry_after", None)
        if isinstance(hint, int | float) and hint > seconds:
            seconds = min(float(hint), self.max_delay.total_seconds())
        return timedelta(seconds=seconds)


class SLA(BaseModel):
    """Service levels of a process: how long a run and its human steps may take.

    Attributes:
        complete_within: A run must finish within this time of being queued.
        warn_at: Fraction of `complete_within` that sends `process.sla_warning`; `None` disables it.
        on_breach: `"notify"` (send `process.sla_breached`, keep running) or `"cancel"`.
        approval_within: A human wait unanswered this long is escalated (`process.escalated`).
        remind_every: Interval of `process.reminder` for an unanswered approval.

    Example:
        ```python
        SLA(complete_within="2d", warn_at=0.8, approval_within="4h", remind_every="1h")
        ```
    """

    model_config = ConfigDict(frozen=True)

    complete_within: timedelta | None = None
    warn_at: float | None = Field(default=0.8, gt=0.0, lt=1.0)
    on_breach: Literal["notify", "cancel"] = "notify"
    approval_within: timedelta | None = None
    remind_every: timedelta | None = None

    @field_validator("complete_within", "approval_within", "remind_every", mode="before")
    @classmethod
    def _duration(cls, value: Any) -> Any:
        return to_timedelta(value, what="SLA duration") if value is not None else value

    @model_validator(mode="after")
    def _positive(self) -> SLA:
        for name in ("complete_within", "approval_within", "remind_every"):
            value = getattr(self, name)
            if value is not None and value <= timedelta(0):
                raise ValueError(f"{name} must be positive")
        return self


ProcessEventType: TypeAlias = Literal[
    "trigger.duplicate",
    "trigger.failed",
    "process.queued",
    "process.started",
    "process.succeeded",
    "process.failed",
    "process.rejected",
    "process.retry_scheduled",
    "process.dead_lettered",
    "process.waiting",
    "process.delivered",
    "process.resumed",
    "process.reminder",
    "process.escalated",
    "process.wait_timeout",
    "process.sla_warning",
    "process.sla_breached",
    "process.cancelled",
    "process.recovered",
    "process.requeued",
    "process.migrated",
    "process.lease_lost",
]
"""Every process event type."""

NOTIFY_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "process.dead_lettered",
        "process.reminder",
        "process.escalated",
        "process.wait_timeout",
        "process.sla_warning",
        "process.sla_breached",
    }
)
"""Event types an automation sends to its notifiers by default (people must act)."""


class ProcessEvent(BaseModel):
    """Something happened to a process run (or a trigger): queued, retried, waiting, escalated, ...

    Goes to the automation's subscribers, notifiers and audit log; `data` holds the details.
    """

    model_config = ConfigDict(frozen=True)

    type: ProcessEventType
    automation: str
    run_id: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)
    status: RunStatus | None = None
    attempt: int = 0
    data: dict[str, Any] = Field(default_factory=dict)

    def describe(self) -> str:
        """One line for logs and notifications."""

        target = f" run {self.run_id}" if self.run_id else ""
        details = ", ".join(f"{key}={value}" for key, value in self.data.items() if value)
        return f"[{self.automation}]{target}: {self.type}" + (f" ({details})" if details else "")


def statuses(values: str | Collection[str] | None) -> tuple[str, ...] | None:
    """Normalize a status filter (a status, several, or `None` for all)."""

    if values is None:
        return None
    selected = (values,) if isinstance(values, str) else tuple(values)
    valid = TERMINAL_STATUSES | ACTIVE_STATUSES
    unknown = [value for value in selected if value not in valid]
    if unknown:
        raise ValueError(f"Unknown run status {unknown[0]!r}; use one of {sorted(valid)}")
    return selected


__all__ = [
    "ACTIVE_STATUSES",
    "DEFAULT_RETRY_ON",
    "NOTIFY_EVENT_TYPES",
    "SLA",
    "TERMINAL_STATUSES",
    "MigrationPolicy",
    "ProcessEvent",
    "ProcessEventType",
    "ProcessRun",
    "RetryPolicy",
    "RunStatus",
    "TriggerEvent",
    "WaitState",
    "output_summary",
    "with_history",
]
