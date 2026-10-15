"""Every exception `awa` raises on purpose, importable from one place.

All of them derive from `AwaError`. Errors that belong to one feature live next to the code that
raises them and are imported on first access.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any, ClassVar

_WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def _error_code(class_name: str) -> str:
    stem = class_name.removesuffix("Error") or class_name
    return "awa." + _WORD_BOUNDARY.sub("_", stem).lower()


def _present(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


class AwaError(Exception):
    """Base class for all library errors.

    Attributes:
        code: Stable id derived from the class name (`UsageLimitExceededError` has
            `"awa.usage_limit_exceeded"`); a subclass may set its own.
        retryable: Whether repeating the same operation unchanged may succeed. Conflicts
            that need a fresh read first are not retryable in this sense.
        details: Structured context of this occurrence; never contains secrets.
    """

    code: ClassVar[str] = "awa.error"
    retryable: ClassVar[bool] = False
    details: Mapping[str, Any] = MappingProxyType({})

    def __init__(self, *args: object, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(*args)
        if details:
            self.details = dict(details)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "code" not in cls.__dict__:
            cls.code = _error_code(cls.__name__)

    def __reduce__(self) -> tuple[Any, ...]:
        # Subclasses take keyword-only arguments that `args` does not hold, so a
        # copy is rebuilt from the instance state instead of calling `__init__`.
        return (_restore, (type(self), self.args, self.__dict__.copy()))


def _restore(cls: type[AwaError], args: tuple[Any, ...], state: dict[str, Any]) -> AwaError:
    error = cls.__new__(cls, *args)
    error.args = args
    error.__dict__.update(state)
    return error


class ConfigurationError(AwaError, ValueError):
    """Raised when configuration is invalid where it is used (also a `ValueError`)."""


class CallCycleError(AwaError, ValueError):
    """An agent call would repeat a call further up the chain; the text says why.

    The `as_tool` tools turn it into their tool result, so the calling model can adapt.
    """


class StructuredOutputError(AwaError):
    """Raised when an agent fails to produce the expected structured response."""


class SkillNotFoundError(AwaError):
    """Raised when a requested skill is not registered."""


class SkillReferenceNotFoundError(AwaError):
    """Raised when a skill resource path is unsafe, not bundled with the skill, or unreadable."""


class SkillAlreadyRegisteredError(AwaError):
    """Raised when adding a skill whose name is taken and replacement was not requested."""


class AgentNotFoundError(AwaError):
    """Raised when a requested agent is not registered."""


class ToolNotFoundError(AwaError):
    """Raised when a requested tool is not registered."""


class OptionalDependencyError(AwaError, ImportError):
    """Raised when a feature needs an optional extra that is not installed."""


class UpstreamCompatibilityError(AwaError, ImportError):
    """Raised when the installed LangChain or LangGraph lacks an API awa depends on.

    The message names the missing API, the awa module that needs it and the
    locations tried; `awa doctor` shows the supported version ranges.
    """


class MissingConfigurationError(ConfigurationError):
    """Raised when a selected backend lacks required configuration, such as `postgres_dsn`."""


class SkillManifestError(AwaError):
    """Raised when a `SKILL.md` file exists but cannot be parsed or validated."""


class AgentRunError(AwaError):
    """Raised when an agent run fails for a reason no more specific `AwaError` covers."""


class MaxStepsExceededError(AgentRunError):
    """Raised when an agent run needs more model/tool steps than its `max_steps` allows."""


class OutputRejectedError(AgentRunError):
    """Raised when output validators still reject the answer after every retry.

    `problems` holds the reasons given for the last answer, `attempts` the retries used.
    """

    def __init__(self, message: str, *, problems: Sequence[str], attempts: int) -> None:
        super().__init__(message, details={"attempts": attempts})
        self.problems = list(problems)
        self.attempts = attempts


class StepCheckFailedError(AgentRunError):
    """Raised when a check of a process step fails or a person rejects the step's result.

    The step after it never starts.

    Attributes:
        step: Name of the step that failed.
        checks: What the step's checks found, each `{"id", "kind", "passed", "message"}`.
        steps: Status of every step when the run stopped, each
            `{"name", "status", "agent"}` (`status` is `checked`, `failed` or `waiting`).
    """

    def __init__(
        self,
        *args: object,
        step: str = "",
        checks: list[dict[str, Any]] | None = None,
        steps: list[dict[str, Any]] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        info = _present(step=step or None, checks=checks or None, steps=steps or None)
        super().__init__(*args, details={**info, **(details or {})})
        self.step = step
        self.checks = list(checks or [])
        self.steps = list(steps or [])


class StepRejectedError(StepCheckFailedError):
    """Raised when a special case or a person rejects the item a process step works on.

    The run ends with status `rejected`, not `failed`; `details["rejected"]` is `True`.
    """


class UsageLimitExceededError(AwaError):
    """Raised after the first model response that crosses a `UsageLimits` bound.

    Also raised when a run tree reaches a built-in ceiling (`awa.runtime.termination`).
    `diagnosis`, when set, is a `StopDiagnosis` naming the bound, the last steps and a fix.
    """

    diagnosis: Any = None


class NoProgressError(UsageLimitExceededError):
    """Raised when a run repeats the same tool call or tool failure without progress.

    `details["limit"]` names the rule (`identical_tool_calls`, `repeated_tool_errors`).
    """


class MemoryScopeError(AwaError):
    """Raised when the run does not identify the owner a memory scope needs (no silent sharing)."""


class EvalError(AwaError):
    """Raised when an evaluation dataset, a scorer specification or a saved report is invalid."""


class MissingPriceError(AwaError):
    """Raised when the `PriceTable` has no price for a model whose cost is computed."""


class ToolTimeoutError(AwaError, TimeoutError):
    """Raised when a hardened tool call exceeds its timeout (also a `TimeoutError`)."""

    retryable = True


class ModelResolutionError(AwaError):
    """Raised when a model reference (profile name or `provider:model`) cannot be resolved."""


class ModelProviderError(AwaError):
    """Raised when a provider request fails and no resilience policy could recover.

    The provider SDK exception stays available as `__cause__`; nothing here contains
    credentials. `provider`, `model`, `status` (HTTP) and `retry_after` (seconds) are set
    when known.

    Attributes:
        kind: `"rate_limit"`, `"server_error"`, `"timeout"`, `"connection"`,
            `"authentication"`, `"permission_denied"`, `"invalid_request"`,
            `"not_found"`, `"context_overflow"`, `"circuit_open"` or `"error"`.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "error",
        provider: str | None = None,
        model: str | None = None,
        status: int | None = None,
        retry_after: float | None = None,
        retryable: bool = False,
    ) -> None:
        details = _present(
            kind=kind, provider=provider, model=model, status=status, retry_after=retry_after
        )
        super().__init__(message, details=details)
        self.kind = kind
        self.provider = provider
        self.model = model
        self.status = status
        self.retry_after = retry_after
        # Retryability depends on the occurrence, not only on the class.
        self.__dict__["retryable"] = retryable


class ModelRateLimitError(ModelProviderError):
    """Raised when a provider keeps answering with a rate limit (HTTP 429)."""


class TopologyError(AwaError):
    """Raised when a team topology has duplicate names or unknown or disallowed edges."""


class AgentAlreadyRegisteredError(AwaError):
    """Raised when registering an agent under a taken name without requesting replacement."""


class HandoffError(AwaError):
    """Raised when a handoff target is not allowed or the run's handoff limit was reached."""


class MessagingError(AwaError):
    """Base class for inter-agent messaging failures."""


class RecipientNotFoundError(MessagingError):
    """Raised when a message is addressed to a mailbox that is not registered."""


class CommunicationNotAllowedError(MessagingError):
    """Raised when the `CommunicationPolicy` forbids reaching the recipient or topic."""


class MailboxFullError(MessagingError):
    """Raised when a bounded mailbox with overflow policy `"reject"` is full."""

    retryable = True


class MessageRequestTimeoutError(MessagingError):
    """Raised when a request/reply exchange receives no reply in time."""

    retryable = True


class BlackboardConflictError(MessagingError):
    """Raised when a compare-and-set blackboard write targets a stale version."""


class TaskError(AwaError):
    """Base class for task board failures."""


class TaskNotFoundError(TaskError):
    """Raised when a task id is not on the board."""


class TaskStateError(TaskError):
    """Raised when a task's current status does not allow the requested change."""


class TaskClaimError(TaskStateError):
    """Raised when a task is owned elsewhere, not pending, blocked, or needs other capabilities."""


class TaskPermissionError(TaskError):
    """Raised when an agent changes a task it neither owns nor created."""


class TaskDependencyError(TaskError):
    """Raised when a task depends on an unknown task, or dependencies form a cycle."""


class TaskConflictError(TaskError):
    """Raised when an update expected a task version that is no longer current."""


class TaskTimeoutError(TaskError, TimeoutError):
    """Raised when waiting for tasks to settle exceeds its timeout."""

    retryable = True


class MCPError(AwaError):
    """Raised when an MCP server is misconfigured, unreachable, or cannot list its tools.

    Failures while a tool runs reach the model as a failed tool result instead.
    """


class MCPToolDriftError(MCPError):
    """Raised when an MCP server's tools no longer match its pin (`MCPServer.pin` / `lock`).

    Attributes:
        changed: MCP names of tools whose description or input schema changed.
        added: MCP names of selected tools that are not pinned.
        removed: MCP names of pinned tools the server no longer offers.
    """

    def __init__(
        self,
        message: str,
        *,
        server: str,
        changed: tuple[str, ...] = (),
        added: tuple[str, ...] = (),
        removed: tuple[str, ...] = (),
    ) -> None:
        super().__init__(
            message,
            details={
                "server": server,
                "changed": list(changed),
                "added": list(added),
                "removed": list(removed),
            },
        )
        self.server = server
        self.changed = changed
        self.added = added
        self.removed = removed


class AdapterError(AwaError):
    """Raised when an existing tool, graph or skill cannot be imported into awa."""


class AuthenticationError(AwaError):
    """Raised when a request lacks the server's API key or carries a wrong one."""


class SystemNotFoundError(AwaError):
    """Raised when a request names a system the server does not serve."""


class ThreadNotFoundError(AwaError):
    """Raised when a thread id has no saved state in the system's checkpointer."""


class SystemBusyError(AwaError):
    """Raised when a run cannot start because the system or the thread is already running."""

    retryable = True


class NoPendingInterruptError(AwaError, ValueError):
    """Raised when resuming a thread that is not waiting for a human decision."""


class InvalidRequestError(AwaError, ValueError):
    """Raised when a request asks for something the served system does not allow."""


class PermissionDeniedError(AwaError):
    """Raised when an authenticated caller lacks the role an operation needs."""


class RunNotFoundError(AwaError):
    """Raised when a run id names no run the caller can see on this server."""


class RunCancelledError(AwaError):
    """Raised when a run was cancelled before it finished.

    `run_id`, `reason` and `cancelled_by` (a principal id) are set when known.
    """

    def __init__(
        self,
        *args: object,
        run_id: str | None = None,
        reason: str | None = None,
        cancelled_by: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        info = _present(run_id=run_id, reason=reason, cancelled_by=cancelled_by)
        super().__init__(*args, details={**info, **(details or {})})
        self.run_id = run_id
        self.reason = reason
        self.cancelled_by = cancelled_by


class RunTimeoutError(AwaError, TimeoutError):
    """Raised when a run exceeded its `timeout` or `UsageLimits.max_duration`; its whole tree stops.

    `elapsed` and `limit` are seconds; `limit_name` is `"timeout"` or `"max_duration"`.
    """

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        run_id: str,
        elapsed: float,
        limit: float,
        limit_name: str = "timeout",
    ) -> None:
        super().__init__(
            message,
            details={
                "run_id": run_id,
                "elapsed": round(elapsed, 3),
                "limit": limit,
                "limit_name": limit_name,
            },
        )
        self.run_id = run_id
        self.elapsed = elapsed
        self.limit = limit
        self.limit_name = limit_name


class IdempotencyConflictError(AwaError):
    """Raised when a request reuses an `Idempotency-Key` whose first request is still running."""

    retryable = True


class IdempotencyMismatchError(AwaError):
    """Raised when an `Idempotency-Key` is reused with a different body, system or mode."""


class RateLimitExceededError(AwaError):
    """Raised when a caller exceeds its rate limit; `retry_after` is in seconds."""

    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, details=_present(retry_after=retry_after))
        self.retry_after = retry_after


class ServiceUnavailableError(AwaError):
    """Raised when the server shuts down or hits its concurrency limit; `retry_after` is seconds."""

    retryable = True

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message, details=_present(retry_after=retry_after))
        self.retry_after = retry_after


class ScaffoldError(AwaError):
    """Raised when a project cannot be created: bad name, existing files, or a failed draft."""


class PostgresSetupTimeoutError(AwaError, TimeoutError):
    """Raised when another session holds the PostgreSQL setup lock for longer than the timeout."""

    retryable = True


class CacheStoreError(AwaError):
    """Raised by a `CacheStore` that cannot read or write (its database or file failed).

    awa's caches catch it, log it once and carry on without the cache.
    """


class DataStoreError(AwaError):
    """Raised when awa's data store (runs, steps, results) cannot be opened or used.

    The message names the fix.
    """


class SchemaVersionError(DataStoreError):
    """Raised when the database schema does not match this version of awa.

    The ledger is behind (run `awa store upgrade`), a newer awa applied a breaking change, or an
    applied migration file changed since (its checksum differs).
    """


class StoreConflictError(DataStoreError):
    """Raised when a write to the data store lost a race or repeats a final decision."""


class AutomationError(AwaError):
    """Base class for process-automation failures (`awa.automation`)."""


class InvalidCronError(AutomationError, ValueError):
    """Raised when a cron expression or schedule cannot be parsed or names an unknown time zone."""


class TemplateError(AutomationError, ValueError):
    """Raised when an automation template is malformed or names a field the event lacks."""


class WebhookVerificationError(AuthenticationError):
    """Raised when a webhook's HMAC signature is missing or wrong, or its timestamp is stale."""


class NoWaitingRunError(RunNotFoundError):
    """Raised when an event is delivered to a correlation key no process run is waiting on."""


class ProcessRunStateError(AutomationError):
    """Raised when a process run's status forbids the change, or another worker took its lease."""


class CheckpointLostError(ProcessRunStateError):
    """Raised when a paused run cannot resume because its saved thread state is gone.

    Retrying the run (`awa runs retry`) starts it again from its input.
    """


class WaitTimeoutError(AutomationError, TimeoutError):
    """Raised when `wait_for_event` or an approval timed out and was configured to fail."""


class JobFailedError(AwaError):
    """Raised by `JobResult.raise_for_status` when a background job failed or was cancelled.

    `result` is the `JobResult`; its `retryable` tells a queue whether a rerun may succeed.
    """

    def __init__(
        self, *args: object, result: Any = None, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(*args, details=details)
        self.result = result


class WebhookDeliveryError(AwaError):
    """Raised when a webhook receiver refused a callback (4xx) or every retry failed."""

    retryable = True


_DEFINED = [
    "AdapterError",
    "AgentAlreadyRegisteredError",
    "AgentNotFoundError",
    "AgentRunError",
    "AuthenticationError",
    "AutomationError",
    "AwaError",
    "BlackboardConflictError",
    "CacheStoreError",
    "CallCycleError",
    "CheckpointLostError",
    "CommunicationNotAllowedError",
    "ConfigurationError",
    "DataStoreError",
    "EvalError",
    "HandoffError",
    "IdempotencyConflictError",
    "IdempotencyMismatchError",
    "InvalidCronError",
    "InvalidRequestError",
    "JobFailedError",
    "MCPError",
    "MCPToolDriftError",
    "MailboxFullError",
    "MaxStepsExceededError",
    "MemoryScopeError",
    "MessageRequestTimeoutError",
    "MessagingError",
    "MissingConfigurationError",
    "MissingPriceError",
    "ModelProviderError",
    "ModelRateLimitError",
    "ModelResolutionError",
    "NoPendingInterruptError",
    "NoProgressError",
    "NoWaitingRunError",
    "OptionalDependencyError",
    "OutputRejectedError",
    "PermissionDeniedError",
    "PostgresSetupTimeoutError",
    "ProcessRunStateError",
    "RateLimitExceededError",
    "RecipientNotFoundError",
    "RunCancelledError",
    "RunNotFoundError",
    "RunTimeoutError",
    "ScaffoldError",
    "SchemaVersionError",
    "ServiceUnavailableError",
    "SkillAlreadyRegisteredError",
    "SkillManifestError",
    "SkillNotFoundError",
    "SkillReferenceNotFoundError",
    "StepCheckFailedError",
    "StepRejectedError",
    "StoreConflictError",
    "StructuredOutputError",
    "SystemBusyError",
    "SystemNotFoundError",
    "TaskClaimError",
    "TaskConflictError",
    "TaskDependencyError",
    "TaskError",
    "TaskNotFoundError",
    "TaskPermissionError",
    "TaskStateError",
    "TaskTimeoutError",
    "TemplateError",
    "ThreadNotFoundError",
    "ToolNotFoundError",
    "ToolTimeoutError",
    "TopologyError",
    "UpstreamCompatibilityError",
    "UsageLimitExceededError",
    "WaitTimeoutError",
    "WebhookDeliveryError",
    "WebhookVerificationError",
]

# Errors defined in feature packages, imported on first access so that `import awa.errors`
# stays light.
_LAZY: dict[str, str] = {
    "AuditChainError": "awa.observability.audit",
    "BlueprintError": "awa.blueprints.errors",
    "GuardrailTripwireError": "awa.guardrails",
    "InvalidGateActorError": "awa.tools.approval",
    "IdempotencyKeyAlreadyClaimedError": "awa.tools.idempotency",
    "PeopleError": "awa.people._ref",
    **dict.fromkeys(
        (
            "DataError",
            "PathNotAllowedError",
            "RowLimitError",
            "TooLargeError",
            "TooManyFilesError",
            "TypeMismatchError",
            "UnsafeArchiveError",
            "UnsupportedTypeError",
        ),
        "awa.data._errors",
    ),
    **dict.fromkeys(
        (
            "BuildConflictError",
            "BuildDesignInvalidError",
            "BuildNotFoundError",
            "BuildUsageLimitError",
            "QuotaExceededError",
            "UploadNotFoundError",
            "UploadTimeoutError",
        ),
        "awa.server.errors",
    ),
    "ComponentError": "awa.plugins.switches",
    **dict.fromkeys(("ExportError", "ExportUnsupportedError"), "awa.exporter._report"),
    "PatternError": "awa.blueprints._patterns",
    "PluginError": "awa.plugins._kinds",
    "TraceDeleteError": "awa.observability.trace_store",
}

__all__ = sorted({*_DEFINED, *_LAZY})


def __getattr__(name: str) -> Any:
    if name not in _LAZY:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(_LAZY[name]), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
