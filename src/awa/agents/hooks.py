"""Lifecycle hooks: typed callbacks for the events of an agent's runs."""

from __future__ import annotations

from typing import Any

from awa.contracts.events import (
    AgentFinishedEvent,
    AgentMessageEvent,
    AgentStartedEvent,
    ApprovalDecidedEvent,
    ApprovalRequestedEvent,
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
    RunEvent,
    StepEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
)


class AgentHooks:
    """Base class for lifecycle hooks; override the methods you need (each is a no-op by default).

    An instance is callable with any `RunEvent` and dispatches it, so it can be passed to
    `Agent(hooks=[...])` or subscribed to an `EventEmitter`. It receives the events of the agent's
    runs and the runs they start.

    Example:
        ```python
        class PrintTools(AgentHooks):
            async def on_tool_call(self, event: ToolCallEvent) -> None:
                print(f"{event.agent} -> {event.tool_name}({event.arguments})")

        agent = Agent("assistant", tools=[search], hooks=[PrintTools()])
        ```
    """

    async def on_agent_start(self, event: AgentStartedEvent) -> None:
        """Called when an agent run starts."""

    async def on_agent_end(self, event: AgentFinishedEvent) -> None:
        """Called when an agent run ends without an error (also when it pauses or hands off)."""

    async def on_text_delta(self, event: TextDeltaEvent) -> None:
        """Called for every chunk of streamed model text (only while streaming)."""

    async def on_model_response(self, event: ModelResponseEvent) -> None:
        """Called after every model response."""

    async def on_model_retry(self, event: ModelRetryEvent) -> None:
        """Called before a failed model call is retried."""

    async def on_output_retry(self, event: OutputRetryEvent) -> None:
        """Called before a rejected answer goes back to the model."""

    async def on_model_fallback(self, event: ModelFallbackEvent) -> None:
        """Called when a run switches to a fallback model."""

    async def on_tool_call(self, event: ToolCallEvent) -> None:
        """Called for every tool call a model requests, before it runs."""

    async def on_tool_result(self, event: ToolResultEvent) -> None:
        """Called when a tool call finishes, successfully or not."""

    async def on_delegation(self, event: DelegationEvent) -> None:
        """Called when an agent delegates a task to, or consults, another agent."""

    async def on_delegation_result(self, event: DelegationResultEvent) -> None:
        """Called when a delegated task or consultation returns."""

    async def on_handoff(self, event: HandoffEvent) -> None:
        """Called when control passes from one agent to another."""

    async def on_message(self, event: AgentMessageEvent) -> None:
        """Called when an inter-agent message is sent."""

    async def on_message_undelivered(self, event: MessageUndeliveredEvent) -> None:
        """Called when a team or network run ends with a message nobody read."""

    async def on_error(self, event: ErrorEvent) -> None:
        """Called when an agent run fails."""

    async def on_run_cancelled(self, event: RunCancelledEvent) -> None:
        """Called when a run is cancelled or times out, right before its `on_error`."""

    async def on_approval_requested(self, event: ApprovalRequestedEvent) -> None:
        """Called when a tool call pauses the run to wait for a human decision."""

    async def on_approval_decided(self, event: ApprovalDecidedEvent) -> None:
        """Called when a human decision on a gated tool call is applied."""

    async def on_guardrail_blocked(self, event: GuardrailBlockedEvent) -> None:
        """Called when a guardrail blocks an input, output or tool call in a run."""

    async def on_limit_exceeded(self, event: LimitExceededEvent) -> None:
        """Called when the run tree hits one of its limits."""

    async def on_step(self, event: StepEvent) -> None:
        """Called when a step of a process workflow changes status."""

    async def __call__(self, event: RunEvent) -> None:
        """Dispatch `event` to the matching `on_*` method."""

        method = _HANDLERS.get(type(event))
        if method is not None:
            await getattr(self, method)(event)


_HANDLERS: dict[type[Any], str] = {
    AgentStartedEvent: "on_agent_start",
    AgentFinishedEvent: "on_agent_end",
    TextDeltaEvent: "on_text_delta",
    ModelResponseEvent: "on_model_response",
    ModelRetryEvent: "on_model_retry",
    OutputRetryEvent: "on_output_retry",
    ModelFallbackEvent: "on_model_fallback",
    ToolCallEvent: "on_tool_call",
    ToolResultEvent: "on_tool_result",
    DelegationEvent: "on_delegation",
    DelegationResultEvent: "on_delegation_result",
    HandoffEvent: "on_handoff",
    AgentMessageEvent: "on_message",
    MessageUndeliveredEvent: "on_message_undelivered",
    ErrorEvent: "on_error",
    RunCancelledEvent: "on_run_cancelled",
    ApprovalRequestedEvent: "on_approval_requested",
    ApprovalDecidedEvent: "on_approval_decided",
    GuardrailBlockedEvent: "on_guardrail_blocked",
    LimitExceededEvent: "on_limit_exceeded",
    StepEvent: "on_step",
}
"""Event type -> the `AgentHooks` method it is dispatched to."""

__all__ = ["AgentHooks"]
