"""Response bodies of the HTTP API, as pydantic models for the OpenAPI document."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from awa.server._config import SystemKind
from awa.server._runs import RunMode, RunStatus


class ErrorDetail(BaseModel):
    """What went wrong."""

    type: str = Field(description="Error type, e.g. `ThreadNotFoundError` or `InternalError`.")
    message: str = Field(description="Human-readable message; generic for unexpected errors.")
    retryable: bool = Field(
        default=False, description="Whether the same request may succeed when sent again later."
    )
    code: str = Field(description="Stable machine-readable code, e.g. `awa.thread_not_found`.")
    request_id: str | None = Field(default=None, description="The request's `X-Request-ID`.")
    details: Any = Field(default=None, description="Validation errors of an invalid request.")


class ErrorResponse(BaseModel):
    """Body of every error response."""

    error: ErrorDetail


class HealthResponse(BaseModel):
    """Liveness: the process answers."""

    status: Literal["ok"] = "ok"
    preflight_skipped: bool | None = Field(
        default=None,
        description="`true` when the server was started with `awa --skip-preflight` (the "
        "configuration checks were skipped); absent otherwise.",
    )
    security_off: list[str] | None = Field(
        default=None,
        description="The security parts switched off or replaced for this project "
        "(the blueprint's `components:`), for example `guard.sql_guard`; shown only to a caller that "
        "authenticates with the admin role, absent otherwise and when none is.",
    )


class ReadyResponse(BaseModel):
    """Readiness: whether the instance should receive traffic, and each check's outcome."""

    status: Literal["ready", "unavailable"]
    checks: dict[str, str] = Field(
        description="Check name to `ok`, `failed`, `timeout` or `draining`."
    )


class SystemSummary(BaseModel):
    """A served system in the listing."""

    name: str
    kind: SystemKind
    card: dict[str, Any] = Field(description="The system's `AgentCard`.")


class SystemList(BaseModel):
    """Every served system."""

    systems: list[SystemSummary]


class SystemDescription(BaseModel):
    """A served system in detail."""

    name: str
    kind: SystemKind
    card: dict[str, Any]
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    topology: Any = None
    mermaid: str | None = None
    accepts_deps: list[str]
    exclusive: bool
    threads: bool
    streaming: bool
    resume: bool
    max_concurrency: int | None = None
    approval_requires_role: bool = Field(
        default=False, description="Whether resuming needs one of the system's approver roles."
    )


class RunResponse(BaseModel):
    """A finished or paused run."""

    run_id: str
    status: Literal["completed", "interrupted"]
    agent: str
    output: Any = None
    text: str | None = None
    thread_id: str | None = None
    handoff_path: list[str] = Field(default_factory=list)
    interrupts: list[Any] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class RunAccepted(BaseModel):
    """An async run that was started (`?mode=async`)."""

    run_id: str
    status: Literal["running"] = "running"
    system: str
    thread_id: str | None = None
    request_id: str | None = None
    status_url: str = Field(description="Poll this URL (`GET`) for the outcome.")


class RunState(BaseModel):
    """A run the server started, as `GET /v1/runs/{run_id}` reports it."""

    run_id: str
    system: str
    mode: RunMode
    status: RunStatus
    thread_id: str | None = None
    request_id: str | None = None
    created_at: str
    finished_at: str | None = None
    result: RunResponse | None = None
    error: ErrorDetail | None = None


class RunList(BaseModel):
    """Runs of the caller that are in flight or finished recently (async runs)."""

    runs: list[RunState]


class Interrupt(BaseModel):
    """A pending human decision."""

    id: str | None = None
    value: Any = None


class InterruptList(BaseModel):
    """The approvals a thread waits for."""

    thread_id: str
    interrupts: list[Interrupt]


class ThreadSummary(BaseModel):
    """A thread the caller can see."""

    thread_id: str
    principal: str | None = None
    created_at: str


class ThreadList(BaseModel):
    """Threads of a system, newest first."""

    threads: list[ThreadSummary]
