"""Typed events describing what happens during a run.

`RunEvent` is a discriminated union (on `type`) of everything a run reports while it executes;
the same events drive streaming, lifecycle hooks and tracing. `AuditEvent` is the separate
per-graph-node record written by `awa.observability.audit.audit_node`.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from awa.contracts.communication import AgentMessage, _utcnow
from awa.contracts.usage import Usage

if TYPE_CHECKING:
    from awa.contracts.results import RunResult


class AuditEvent(BaseModel):
    """One audit record for a graph node execution.

    Attributes:
        event_type: `"node_completed"` or `"node_failed"`.
        root_run_id: Id of the outermost run of that run tree, when known.
        input_artifact_refs: References to the artifacts the node read.
        output_artifact_refs: References to the artifacts the node wrote.
    """

    event_id: str
    event_type: str
    node_name: str
    run_id: str | None = None
    root_run_id: str | None = None
    agent_name: str | None = None
    model_profile: str | None = None
    input_artifact_refs: list[str] = Field(default_factory=list)
    output_artifact_refs: list[str] = Field(default_factory=list)
    latency_ms: int | None = None
    created_at: datetime


class _BaseRunEvent(BaseModel):
    """Fields every run event carries; `root_run_id` is the outermost run, `depth` 0 at the top."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    root_run_id: str
    agent: str
    depth: int = 0
    timestamp: datetime = Field(default_factory=_utcnow)


RunKind = Literal["agent", "team", "network", "workflow"]
"""What kind of runnable a run belongs to."""


class AgentStartedEvent(_BaseRunEvent):
    """An agent, team, network or workflow run started.

    Attributes:
        kind: What runs; tracing names every kind but `"agent"` `invoke_workflow`.
        session_id: The session the run belongs to (`RunIds.session_id`).
        agent_call_id: Id of the call that started a nested run (`RunIds.agent_call_id`).
        tool_call_id: The provider's id of the tool call that started it, if any.
        branch: The chain of agent calls down to this run, for display (`RunIds.branch`).
    """

    type: Literal["agent_started"] = "agent_started"
    parent_run_id: str | None = None
    input_preview: str = ""
    kind: RunKind = "agent"
    session_id: str | None = None
    agent_call_id: str | None = None
    tool_call_id: str | None = None
    branch: str | None = None


class TextDeltaEvent(_BaseRunEvent):
    """A chunk of streamed model text."""

    type: Literal["text_delta"] = "text_delta"
    delta: str


class ModelResponseEvent(_BaseRunEvent):
    """A complete model response (one model call) was received.

    Attributes:
        input_tokens: Input tokens the provider reported for the call (cache reads and writes
            included).
        provider: The provider's name as the chat model integration reports it, when known.
        finish_reason: The raw reason the provider gave for ending the response, when known.
        prefix_hash: Hash of what a provider caches (system message, tools, output schema); a
            change between calls of one agent misses the cache.
        replayed: The response came from the replay cache of `awa eval --replay` (tokens 0).
        duration_ms: Milliseconds the call took, retries inside it included.
        cleared_tool_results: Older tool results context editing replaced by a placeholder
            (see `awa.memory.context_editing`).
    """

    type: Literal["model_response"] = "model_response"
    content: str = ""
    tool_call_names: tuple[str, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    model: str | None = None
    provider: str | None = None
    finish_reason: str | None = None
    prefix_hash: str | None = None
    replayed: bool = False
    duration_ms: int | None = None
    cleared_tool_results: int = 0


class ToolCallEvent(_BaseRunEvent):
    """The model requested a tool call; its `ToolResultEvent` carries the same `call_id`."""

    type: Literal["tool_call"] = "tool_call"
    tool_name: str
    call_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultEvent(_BaseRunEvent):
    """A tool call finished.

    Attributes:
        content: The tool's result text, or `"<ErrorType>: <message>"` when the tool raised.
        cache: `"hit"` when the result came from the read-only tool cache, `"miss"` when the
            tool ran and its result was stored; `None` when the tool is not cached.
        arguments: The call's arguments when an earlier run requested it (it waited for approval).
    """

    type: Literal["tool_result"] = "tool_result"
    tool_name: str
    call_id: str
    content: str = ""
    is_error: bool = False
    cache: Literal["hit", "miss"] | None = None
    arguments: dict[str, Any] | None = None


class DelegationEvent(_BaseRunEvent):
    """An agent delegated a task (`mode="delegate"`) or consulted a peer (`"consult"`).

    `correlation_id` links it to its `DelegationResultEvent`.
    """

    type: Literal["delegation"] = "delegation"
    target: str
    task: str
    mode: Literal["delegate", "consult"] = "delegate"
    correlation_id: str


class DelegationResultEvent(_BaseRunEvent):
    """A delegated task (or peer consultation) returned; `status` is `"error"` when it failed."""

    type: Literal["delegation_result"] = "delegation_result"
    target: str
    correlation_id: str
    status: Literal["ok", "error"] = "ok"
    content_preview: str = ""


class HandoffEvent(_BaseRunEvent):
    """Control of the conversation passed from `source` to `target`, for the source's `reason`."""

    type: Literal["handoff"] = "handoff"
    source: str
    target: str
    reason: str = ""


class AgentMessageEvent(_BaseRunEvent):
    """An inter-agent message was sent through a message bus."""

    type: Literal["agent_message"] = "agent_message"
    message: AgentMessage


class MessageUndeliveredEvent(_BaseRunEvent):
    """An inter-agent message never reached its recipient.

    Emitted when a team or network run ends with unread messages (dead-lettered with reason
    `"run_ended"`), and for messages the bus dead-lettered earlier (expired or evicted).
    `reason` is an `awa.messaging.bus.DeadLetterReason`.
    """

    type: Literal["message_undelivered"] = "message_undelivered"
    message: AgentMessage
    recipient: str | None = None
    reason: str


class AgentFinishedEvent(_BaseRunEvent):
    """A run ended without an error.

    Also emitted when the run passes control to an enclosing graph (an interrupt such as an
    approval request, or a handoff); `output_preview` is empty then.
    """

    type: Literal["agent_finished"] = "agent_finished"
    output_preview: str = ""
    usage: Usage = Field(default_factory=Usage)
    duration_ms: int = 0


class ErrorEvent(_BaseRunEvent):
    """A run failed.

    Attributes:
        stop: When a bound stopped the run (a usage ceiling, the time, `max_steps`, no
            progress): `StopDiagnosis.to_dict()` with the bound, where, the last steps and a
            suggested fix. `None` otherwise.
    """

    type: Literal["error"] = "error"
    error_type: str
    message: str
    stop: dict[str, Any] | None = None


class ModelRetryEvent(_BaseRunEvent):
    """A model request failed with a transient error and is retried after `delay` seconds.

    Emitted by the retry policy of `ModelSettings.retry`. `attempt` is the 1-based attempt that
    failed; `kind` is the error category of `ModelProviderError.kind`.
    """

    type: Literal["model_retry"] = "model_retry"
    model: str
    attempt: int
    delay: float
    error_type: str
    kind: str = "error"
    status: int | None = None


class OutputRetryEvent(_BaseRunEvent):
    """A rejected answer (schema, validator or retry guardrail) goes back to the model.

    Attributes:
        check: `"schema"` or the name of the validator(s) that rejected the answer.
        attempt: 1-based number of this retry.
        max_retries: The retries the agent allows per turn.
    """

    type: Literal["output_retry"] = "output_retry"
    check: str
    attempt: int
    max_retries: int
    kind: Literal["schema", "validator"]


class ModelFallbackEvent(_BaseRunEvent):
    """A model request moved on from `from_model` to the fallback `to_model`.

    `reason` is the `ModelProviderError.kind` that made `from_model` give up, or `"circuit_open"`.
    """

    type: Literal["model_fallback"] = "model_fallback"
    from_model: str
    to_model: str
    reason: str
    error_type: str | None = None


class RunCancelledEvent(_BaseRunEvent):
    """A run was stopped before it finished: cancelled, or out of time.

    Emitted once by the stopped run, right before its `ErrorEvent`; nested runs it stops end with
    their own `ErrorEvent`.

    Attributes:
        reason: `"timeout"` when a `timeout` or `UsageLimits.max_duration` expired, otherwise
            the reason given to `cancel` (default `"cancelled"`).
        cancelled_by: Who cancelled the run (a principal id), when given.
    """

    type: Literal["run_cancelled"] = "run_cancelled"
    reason: str = "cancelled"
    cancelled_by: str | None = None
    elapsed_ms: int = 0


class ApprovalRequestedEvent(_BaseRunEvent):
    """A tool call paused the run to wait for a human decision.

    Attributes:
        interrupt_id: Id of the interrupt; a resume answers it.
        args_preview: The call arguments as truncated JSON, with secrets redacted.
    """

    type: Literal["approval_requested"] = "approval_requested"
    tool: str
    interrupt_id: str
    args_preview: str = ""
    reason: str = "tool_call_requires_approval"
    thread_id: str | None = None
    expires_at: datetime | None = None


class ApprovalDecidedEvent(_BaseRunEvent):
    """A human decision on an approval-gated tool call was applied.

    Attributes:
        decision: `"approve"`, `"reject"` or `"edit"` (approved with edited arguments). An
            expired or malformed decision is a `"reject"`.
        interrupt_id: Id of the answered interrupt; `None` for a remembered decision.
        reviewer: The `reviewer` (or `actor`) of the resume payload as given; the library does
            not authenticate it.
        note: The reviewer's note, or why the decision was turned into a rejection.
        remembered: The decision was reused from the decision store.
        expired: The request had expired when the decision arrived.
    """

    type: Literal["approval_decided"] = "approval_decided"
    tool: str
    decision: Literal["approve", "reject", "edit"]
    interrupt_id: str | None = None
    reviewer: str | None = None
    note: str = ""
    latency_ms: int | None = None
    remembered: bool = False
    expired: bool = False
    thread_id: str | None = None


class GuardrailBlockedEvent(_BaseRunEvent):
    """A guardrail blocked an input, an output or a tool call inside a run.

    Carries no candidate content: `reason` is the guardrail's explanation, redacted and truncated.
    `stage` `"tool"` covers a tool's arguments or result and sets `tool`.
    """

    type: Literal["guardrail_blocked"] = "guardrail_blocked"
    guardrail: str
    stage: Literal["input", "output", "tool"]
    reason: str = ""
    tool: str | None = None


class LimitExceededEvent(_BaseRunEvent):
    """A run tree hit one of its limits and was stopped or refused work.

    `limit` names it (for example `"max_requests"`); `value` is the observed value and `maximum`
    the configured limit, when known.
    """

    type: Literal["limit_exceeded"] = "limit_exceeded"
    limit: str
    value: float | None = None
    maximum: float | None = None
    message: str = ""


StepStatus = Literal[
    "started", "checking", "needs_approval", "checked", "failed", "retrying", "skipped", "rejected"
]
"""Where a process step is: `started` (its agent works), `checking` (its checks run),
`needs_approval` (a person must decide), `checked` (all checks passed), `failed`, `retrying`
(a failed attempt runs again), `skipped` (left out by its `when` or jumped over by a `go`) or
`rejected` (a special case or a person turned the item down)."""


class StepEvent(_BaseRunEvent):
    """A step of a process workflow changed status.

    Emitted by `awa.workflows.ProcessWorkflow` (a `process:` blueprint). `agent` is the agent
    that does the step; `run_id` is the workflow run.

    Attributes:
        index: Position of the step, from 0.
        checks: The checks that have run so far, each `{"id", "kind", "passed", "message"}`;
            set with `checked`, `needs_approval` and `failed`.
        message: Why the step failed, or what is waited for.
        attempt: Which attempt of the step this is (1 for the first), when it can be retried.
        case: The id of the special case or eval case the event belongs to, if any.
    """

    type: Literal["step"] = "step"
    step: str
    index: int
    total: int
    status: StepStatus
    title: str = ""
    checks: tuple[dict[str, Any], ...] = ()
    message: str = ""
    attempt: int | None = Field(default=None, ge=1)
    case: str | None = None


class RunCompletedEvent(_BaseRunEvent):
    """Final event of a stream; `result` is the complete `RunResult`."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    type: Literal["run_completed"] = "run_completed"
    result: RunResult[Any]


RunEvent = Annotated[
    AgentStartedEvent
    | TextDeltaEvent
    | ModelResponseEvent
    | ToolCallEvent
    | ToolResultEvent
    | DelegationEvent
    | DelegationResultEvent
    | HandoffEvent
    | AgentMessageEvent
    | MessageUndeliveredEvent
    | AgentFinishedEvent
    | ErrorEvent
    | ModelRetryEvent
    | OutputRetryEvent
    | ModelFallbackEvent
    | RunCancelledEvent
    | ApprovalRequestedEvent
    | ApprovalDecidedEvent
    | GuardrailBlockedEvent
    | LimitExceededEvent
    | StepEvent
    | RunCompletedEvent,
    Field(discriminator="type"),
]
"""Any event a run can emit, discriminated on `type`."""


def _rebuild() -> None:
    from awa.contracts.results import RunResult

    RunCompletedEvent.model_rebuild(_types_namespace={"RunResult": RunResult})


_rebuild()

RUN_EVENT_ADAPTER: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)
"""Validates or serializes any `RunEvent` (e.g. `RUN_EVENT_ADAPTER.dump_json(event)`)."""


_REJECTION = re.compile(
    r"^Tool call to '[^']*' was rejected by a human reviewer and was not executed\."
    r"(?: Reason: (?P<note>.*?))? Do not retry this call unless the user asks for it\.$",
    re.DOTALL,
)


def rejection_message(tool: str, note: str = "") -> str:
    """The tool result a model receives when a reviewer rejected its call.

    It says that nothing ran, gives the reviewer's note and tells the model not to retry.
    """

    note = note.strip()
    if note and note[-1] not in ".!?":
        note += "."
    reason = f" Reason: {note}" if note else ""
    return (
        f"Tool call to {tool!r} was rejected by a human reviewer and was not executed."
        f"{reason} Do not retry this call unless the user asks for it."
    )


def rejection_note(content: str) -> str | None:
    """The reviewer's note of a `rejection_message`, `""` without one, `None` for other text."""

    match = _REJECTION.match(content)
    if match is None:
        return None
    return match.group("note") or ""


__all__ = [
    "RUN_EVENT_ADAPTER",
    "AgentFinishedEvent",
    "AgentMessageEvent",
    "AgentStartedEvent",
    "ApprovalDecidedEvent",
    "ApprovalRequestedEvent",
    "AuditEvent",
    "DelegationEvent",
    "DelegationResultEvent",
    "ErrorEvent",
    "GuardrailBlockedEvent",
    "HandoffEvent",
    "LimitExceededEvent",
    "MessageUndeliveredEvent",
    "ModelFallbackEvent",
    "ModelResponseEvent",
    "ModelRetryEvent",
    "OutputRetryEvent",
    "RunCancelledEvent",
    "RunCompletedEvent",
    "RunEvent",
    "RunKind",
    "StepEvent",
    "StepStatus",
    "TextDeltaEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "rejection_message",
    "rejection_note",
]
