"""The result of an agent or team run."""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from awa._internal.upstream.messages import BaseMessage
from awa.contracts.usage import Usage

OutputT = TypeVar("OutputT")


class RunResult(BaseModel, Generic[OutputT]):
    """Everything a finished run produced.

    Attributes:
        agent: Name of the agent that produced the final output (after a handoff, the agent
            that received control last).
        output: An instance of the agent's `output_type` when one is declared, else the text.
        text: The text of the final assistant message. With an `output_type` it is whatever
            that message carried (empty for tool-call output, raw JSON for native output);
            `output` holds the validated object either way.
        messages: The complete message thread of the final agent, input history included.
        new_messages_start: Index into `messages` where this run's own messages begin.
        usage: Resource usage of the whole run tree.
        handoff_path: Names of the agents that held control, in order.
        interrupts: Pending human-in-the-loop interrupts (`id` and `value`), in tool-call
            order; non-empty means the run is paused and must be resumed.
        metadata: Additional run information set by the producer (for example why a loop
            workflow stopped).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    agent: str
    output: OutputT
    text: str = ""
    messages: list[BaseMessage] = Field(default_factory=list)
    new_messages_start: int = 0
    usage: Usage = Field(default_factory=Usage)
    duration_ms: int = 0
    handoff_path: tuple[str, ...] = ()
    thread_id: str | None = None
    interrupts: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_interrupted(self) -> bool:
        """Whether the run is paused waiting for a human decision."""

        return bool(self.interrupts)

    def new_messages(self) -> list[BaseMessage]:
        """Return only the messages this run produced (excluding input history)."""

        return self.messages[self.new_messages_start :]


__all__ = ["OutputT", "RunResult"]
