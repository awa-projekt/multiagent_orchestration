"""The run log backend protocol: the steps, attempts, results and approvals of process runs, in order.

Prompts, answers and tool bodies are kept only for a run whose `Scope.capture` was on when it
started (`RunLog.capture` is the one door); otherwise only their length. Every query is limited
to the `Scope`'s tenant and project, and people are linked only through an HMAC
(`PseudonymKey`). Backends pass `awa.testing.contracts.RunLogContract`.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from awa.errors import ConfigurationError

AUDITOR_ROLE = "auditor"
"""The role that may read captured prompts, answers and tool bodies (`RunLog.content`)."""

_MAX_SCOPE_CHARS = 200

StepState = Literal[
    "pending",
    "started",
    "checking",
    "needs_approval",
    "checked",
    "retrying",
    "failed",
    "skipped",
    "rejected",
    "cancelled",
]
"""Where a step is: the `StepEvent` statuses, plus `pending` (not reached) and `cancelled`."""

AttemptStatus = Literal["running", "succeeded", "failed", "cancelled"]
"""How one attempt of a step ended (`running` while it works)."""

ContentKind = Literal[
    "system", "input_messages", "output_messages", "tool_arguments", "tool_result"
]
"""What a captured body is (OpenTelemetry GenAI vocabulary)."""

ResultKind = Literal["intermediate", "final"]
"""Whether a step result is one of an attempt that did not pass or the one the step went on with."""

ApprovalDecision = Literal["approved", "rejected", "changes_requested"]
"""How a person decided on an approval."""


def _check_scope_part(value: object, what: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(
            f"{what} must be a non-empty string (use 'local' for a single tenant)"
        )
    if len(value) > _MAX_SCOPE_CHARS or "\x00" in value:
        raise ConfigurationError(
            f"{what} must be at most {_MAX_SCOPE_CHARS} characters without NUL"
        )


@dataclass(frozen=True)
class Scope:
    """Who asks, and for which tenant and project: the first argument of every `RunLog` call.

    Built only by the parts that know the caller (server, CLI, the owning automation).

    Attributes:
        tenant: The tenant; never empty (`"local"` for a single tenant).
        project: The project (the automation's name); never empty.
        roles: The caller's roles; `auditor` may read captured content.
        capture: Whether content is kept for a run opened with this scope (frozen at opening).

    Raises:
        ConfigurationError: If `tenant` or `project` is empty, too long or holds a NUL.
    """

    tenant: str
    project: str
    principal: str | None = None
    roles: frozenset[str] = field(default_factory=frozenset)
    capture: bool = False

    def __post_init__(self) -> None:
        _check_scope_part(self.tenant, "tenant")
        _check_scope_part(self.project, "project")
        object.__setattr__(self, "roles", frozenset(self.roles))

    @property
    def is_auditor(self) -> bool:
        """Whether the caller may read captured content."""

        return AUDITOR_ROLE in self.roles


@dataclass(frozen=True)
class SystemScope:
    """An installation-wide job (the retention sweep), accepted only by `RunLog.expired`."""

    reason: str


@dataclass(frozen=True)
class PseudonymKey:
    """The secret that links people to their records without storing who they are.

    The run log keeps `HMAC-SHA256(key, principal id)`, so a database copy alone does not say whose
    runs they are.

    Attributes:
        key: The secret (from `AWA_PSEUDONYM_KEY`).
        key_id: Which version of the key (from `AWA_PSEUDONYM_KEY_ID`), kept with each link.

    Raises:
        ConfigurationError: If the key is shorter than 16 bytes.
    """

    key: bytes = field(repr=False)
    key_id: str = "1"

    def __post_init__(self) -> None:
        if len(self.key) < 16:
            raise ConfigurationError("The pseudonym key must be at least 16 bytes long")

    def subject(self, principal: str) -> str:
        """The HMAC-SHA256 of `principal` under this key, as 64 hex digits."""

        return hmac.new(self.key, principal.encode("utf-8"), hashlib.sha256).hexdigest()


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunHead(_Record):
    """A run as its log knows it: scope, whether content is captured (frozen), when it was opened."""

    run_id: str
    tenant: str
    project: str
    capture: bool
    opened_at: datetime


class CheckRecord(_Record):
    """One check of a step and how it came out (as in `StepEvent.checks`)."""

    id: str
    kind: str
    passed: bool
    message: str = ""


class ToolCallRecord(_Record):
    """One tool call of an attempt: what was called and how it ended.

    Attributes:
        status: `ok`, `error`, or `pending` while it runs.
        arguments_ref: Captured arguments (`RunLog.content`), with capture on.
        result_ref: Captured result text, with capture on.
    """

    call_id: str
    name: str
    status: Literal["ok", "error", "pending"] = "pending"
    arguments_length: int = 0
    result_length: int = 0
    arguments_ref: str | None = None
    result_ref: str | None = None


class StepRecord(_Record):
    """One step of a run, with the totals of its attempts.

    Attributes:
        ordinal: The step's position in the process, from 0 (steps are listed in this order).
        status: See `StepState`.
        model: The model its last attempt used, when known.
        rule_id: The special case, or the failing rule or check, that decided the step.
        message: Why it failed or what it waits for.
        cost_micros: Cost in millionths of a US dollar; `None` when no price was known.
        revision: Bumped on every save (optimistic concurrency).
    """

    run_id: str
    step_key: str
    ordinal: int = Field(ge=0)
    status: StepState = "pending"
    title: str = ""
    agent: str | None = None
    model: str | None = None
    rule_id: str | None = None
    message: str = ""
    attempts: int = Field(default=0, ge=0)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_micros: int | None = None
    error_type: str | None = None
    revision: int = 0


class AttemptRecord(_Record):
    """One attempt of a step, numbered from 1 by the run log; `agent_run_id` links it to its trace."""

    run_id: str
    step_key: str
    attempt: int = Field(ge=1)
    status: AttemptStatus = "running"
    started_at: datetime
    ended_at: datetime | None = None
    model: str | None = None
    agent_run_id: str | None = None
    retry_reason: str | None = None
    error_type: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_micros: int | None = None


class StepResult(_Record):
    """What one attempt of a step produced.

    `intermediate` results belong to attempts that did not pass; the `final` result is the one the
    step went on with (at most one per step).

    Attributes:
        result_id: Time-ordered id (UUIDv7 from the run log's id source).
        output: The structured output (a business record, kept whatever the capture setting).
        answer_ref: The captured answer text (`RunLog.content`), with capture on.
    """

    result_id: str = ""
    run_id: str
    step_key: str
    attempt: int = Field(ge=1)
    kind: ResultKind
    output: Any = None
    answer_length: int = 0
    answer_ref: str | None = None
    tool_calls: tuple[ToolCallRecord, ...] = ()
    checks: tuple[CheckRecord, ...] = ()
    created_at: datetime


class Approval(_Record):
    """A person's decision on a step: asked, then approved or rejected with a reason.

    Attributes:
        approval_id: Set by the run log when the approval is requested (empty before).
        result_id: The step result under review, when there is one.
        reasons: Why a person must decide (the rules that asked), set when requested.
        decision: The decision; `None` while pending.
        reason: The reviewer's reason (not the same as `reasons`).
    """

    approval_id: str = ""
    run_id: str
    step_key: str | None = None
    result_id: str | None = None
    requested_at: datetime
    reasons: tuple[str, ...] = ()
    decision: ApprovalDecision | None = None
    reviewer: str | None = None
    reason: str | None = None
    decided_at: datetime | None = None


class CapturedContent(_Record):
    """A prompt, answer or tool body kept because the run was opened with capture on."""

    content_ref: str
    run_id: str
    kind: ContentKind
    body: Any
    created_at: datetime


class RunRef(_Record):
    """A run in a listing across tenants (the retention sweep)."""

    run_id: str
    tenant: str
    project: str
    opened_at: datetime


@runtime_checkable
class RunLog(Protocol):
    """Where the steps, attempts, results and approvals of process runs are kept, in order.

    Every method is limited to `scope.tenant` and `scope.project`: a run of another scope does
    not exist for it. Times are passed in by the caller; the run log never reads a clock.
    """

    async def open_run(
        self, scope: Scope, run_id: str, *, now: datetime, initiator: str | None = None
    ) -> RunHead:
        """Start the log of a run (idempotent; the first call freezes `scope.capture`) and return its head.

        `initiator`, the principal who started the run, is linked as a person (HMAC).
        """
        ...

    async def head(self, scope: Scope, run_id: str) -> RunHead | None:
        """The run's head, or `None` when the scope has no such run."""
        ...

    async def save_step(
        self, scope: Scope, step: StepRecord, *, expected_revision: int | None
    ) -> StepRecord:
        """Insert or replace a step; the stored revision is one higher.

        Raises:
            StoreConflictError: If `expected_revision` is not the stored revision (`None` expects no
                stored step).
            RunNotFoundError: If the run was never opened in this scope.
        """
        ...

    async def steps(self, scope: Scope, run_id: str) -> list[StepRecord]:
        """The run's steps, by position."""
        ...

    async def start_attempt(
        self, scope: Scope, run_id: str, step_key: str, *, now: datetime
    ) -> AttemptRecord:
        """Open the next attempt of a stored step (numbered n + 1) and count it on the step.

        Raises:
            RunNotFoundError: If the step is not stored in this scope.
        """
        ...

    async def finish_attempt(self, scope: Scope, attempt: AttemptRecord) -> None:
        """Store how an attempt ended (status, end, model, tokens, cost)."""
        ...

    async def attempts(
        self, scope: Scope, run_id: str, *, step_key: str | None = None
    ) -> list[AttemptRecord]:
        """The attempts of the run (or of one step), by step position and number."""
        ...

    async def put_result(self, scope: Scope, result: StepResult) -> StepResult:
        """Store a result (a `final` one replaces the step's earlier final); returns it with
        its `result_id`."""
        ...

    async def results(
        self,
        scope: Scope,
        run_id: str,
        *,
        step_key: str | None = None,
        kind: ResultKind | None = None,
    ) -> list[StepResult]:
        """The run's results, by step position, attempt and creation."""
        ...

    async def capture(
        self, scope: Scope, run_id: str, kind: ContentKind, body: Any, *, now: datetime
    ) -> str | None:
        """Keep a body when the run was opened with capture on; the reference for `content`, or `None`."""
        ...

    async def content(self, scope: Scope, content_ref: str) -> CapturedContent | None:
        """Read a captured body (every read is recorded with the reader's HMAC).

        Raises:
            PermissionDeniedError: If the scope lacks the `auditor` role.
        """
        ...

    async def request_approval(self, scope: Scope, approval: Approval) -> Approval:
        """Record that a person must decide; returns it with its `approval_id`."""
        ...

    async def decide_approval(
        self,
        scope: Scope,
        approval_id: str,
        *,
        decision: ApprovalDecision,
        reviewer: str,
        reason: str | None,
        now: datetime,
    ) -> Approval:
        """Record the decision on a pending approval and link the reviewer (HMAC).

        Raises:
            StoreConflictError: If it was decided before.
            RunNotFoundError: If there is no such approval in this scope.
            ConfigurationError: If `reviewer` is empty.
        """
        ...

    async def approvals(self, scope: Scope, run_id: str) -> list[Approval]:
        """The run's approvals, oldest first."""
        ...

    async def subject_runs(self, scope: Scope, principal: str) -> list[str]:
        """The runs a person started or decided on in this scope (through the HMAC link).

        Raises:
            ConfigurationError: If the run log has no pseudonym key.
        """
        ...

    async def project_runs(self, scope: Scope) -> list[str]:
        """Every run of the scope's project."""
        ...

    async def expired(
        self, system: SystemScope, *, before: datetime, limit: int = 1000
    ) -> list[RunRef]:
        """Runs of every tenant opened before `before`, oldest first (the retention sweep)."""
        ...

    async def delete_runs(self, scope: Scope, run_ids: Sequence[str]) -> int:
        """Delete the runs' logs physically (steps, attempts, results, approvals, captured
        content, person links) and compact the storage; returns how many runs were deleted."""
        ...

    async def close(self) -> None:
        """Release the run log's resources."""
        ...


__all__ = [
    "AUDITOR_ROLE",
    "Approval",
    "ApprovalDecision",
    "AttemptRecord",
    "AttemptStatus",
    "CapturedContent",
    "CheckRecord",
    "ContentKind",
    "PseudonymKey",
    "ResultKind",
    "RunHead",
    "RunLog",
    "RunRef",
    "Scope",
    "StepRecord",
    "StepResult",
    "StepState",
    "SystemScope",
    "ToolCallRecord",
]
