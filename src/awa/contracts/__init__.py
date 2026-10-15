"""Foundation layer: the data contracts every package shares.

Entry points: `AgentCard`, `AgentMessage`, `RunEvent` (the typed run events), `RunResult`,
`Usage` / `UsageLimits`, and `AgentLike` (the interface of anything runnable as an agent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from awa._internal.util import homes, lazy_attribute
from awa.contracts.cards import AgentCard, AgentStatus, validate_agent_name
from awa.contracts.communication import AgentMessage, MessageKind, MessagePriority
from awa.contracts.usage import ModelPrice, PriceTable, Usage, UsageLimits

if TYPE_CHECKING:
    from awa.contracts.events import (
        RUN_EVENT_ADAPTER,
        AgentFinishedEvent,
        AgentMessageEvent,
        AgentStartedEvent,
        ApprovalDecidedEvent,
        ApprovalRequestedEvent,
        AuditEvent,
        DelegationEvent,
        DelegationResultEvent,
        ErrorEvent,
        GuardrailBlockedEvent,
        HandoffEvent,
        LimitExceededEvent,
        MessageUndeliveredEvent,
        ModelFallbackEvent,
        ModelResponseEvent,
        ModelRetryEvent,
        OutputRetryEvent,
        RunCancelledEvent,
        RunCompletedEvent,
        RunEvent,
        RunKind,
        StepEvent,
        TextDeltaEvent,
        ToolCallEvent,
        ToolResultEvent,
    )
    from awa.contracts.protocols import AgentInput, AgentLike, Redactor
    from awa.contracts.results import RunResult

# Events, results and protocols reference LangChain message types (a
# `RunCompletedEvent` carries a `RunResult`), so they load on first access and
# `import awa.contracts` stays free of upstream imports.
_LAZY = homes(
    {
        "awa.contracts.events": (
            "RUN_EVENT_ADAPTER AgentFinishedEvent AgentMessageEvent AgentStartedEvent"
            " ApprovalDecidedEvent ApprovalRequestedEvent AuditEvent DelegationEvent"
            " DelegationResultEvent ErrorEvent GuardrailBlockedEvent HandoffEvent LimitExceededEvent"
            " MessageUndeliveredEvent ModelFallbackEvent ModelResponseEvent ModelRetryEvent"
            " OutputRetryEvent RunCancelledEvent RunCompletedEvent RunEvent RunKind StepEvent"
            " TextDeltaEvent ToolCallEvent ToolResultEvent"
        ),
        "awa.contracts.protocols": "AgentInput AgentLike Redactor",
        "awa.contracts.results": "RunResult",
    }
)

__all__ = [
    "AgentCard",
    "AgentFinishedEvent",
    "AgentInput",
    "AgentLike",
    "AgentMessage",
    "AgentMessageEvent",
    "AgentStartedEvent",
    "AgentStatus",
    "ApprovalDecidedEvent",
    "ApprovalRequestedEvent",
    "AuditEvent",
    "DelegationEvent",
    "DelegationResultEvent",
    "ErrorEvent",
    "GuardrailBlockedEvent",
    "HandoffEvent",
    "LimitExceededEvent",
    "MessageKind",
    "MessageUndeliveredEvent",
    "MessagePriority",
    "ModelFallbackEvent",
    "ModelPrice",
    "ModelResponseEvent",
    "ModelRetryEvent",
    "OutputRetryEvent",
    "PriceTable",
    "Redactor",
    "RUN_EVENT_ADAPTER",
    "RunCancelledEvent",
    "RunCompletedEvent",
    "RunEvent",
    "RunKind",
    "RunResult",
    "StepEvent",
    "TextDeltaEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "Usage",
    "UsageLimits",
    "validate_agent_name",
]


def __getattr__(name: str) -> Any:
    return lazy_attribute(globals(), _LAZY, name)


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
