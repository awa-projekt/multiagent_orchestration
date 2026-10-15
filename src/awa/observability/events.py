"""Ready-made subscribers for run events (`EventLogger`, `EventRecorder`), and LangSmith trace tags."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any, Literal, TypeVar, overload

from awa._internal.logging import get_logger
from awa._internal.util import preview
from awa.contracts.events import (
    AgentMessageEvent,
    DelegationEvent,
    DelegationResultEvent,
    ErrorEvent,
    HandoffEvent,
    RunEvent,
    TextDeltaEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from awa.core.settings import Settings
from awa.errors import ConfigurationError
from awa.kernel.redaction import Redactor, redact_secrets
from awa.observability._trace_encode import EVENT_FIELDS
from awa.observability.otel import PII_REDACTOR, resolve_redactor

EventT = TypeVar("EventT")

_CHAT_PREVIEW_CHARS = 100
_COORDINATION_TOOLS = frozenset(
    {
        "delegate_task",
        "consult_peer",
        "check_delegations",
        "wait_for_delegations",
        "cancel_delegation",
    }
)  # their calls are shown as delegation or handoff lines instead
_FRAME_START = '<tool_output note="'
_FRAME_END = "</tool_output>"


def tool_output_text(content: str) -> str:
    """A tool result without its `<tool_output>` data frame; other text is returned unchanged."""

    text = content.strip()
    if not (text.startswith(_FRAME_START) and text.endswith(_FRAME_END)):
        return content
    payload = text[text.find(">") + 1 : -len(_FRAME_END)]
    try:
        body = json.loads(payload)
    except ValueError:
        return content
    inner = body.get("content") if isinstance(body, dict) else None
    return inner if isinstance(inner, str) else content


@overload
def describe_event(event: RunEvent) -> str: ...


@overload
def describe_event(event: RunEvent, *, style: Literal["chat"]) -> str | None: ...


def describe_event(event: RunEvent, *, style: Literal["log", "chat"] = "log") -> str | None:
    """Return a compact, single-line description of `event`.

    `style="log"` describes every event with its ids (`"tool_call agent=researcher tool=search"`);
    `"chat"` describes only what a person follows in a terminal (`"researcher -> search({...})"`,
    credentials redacted) and returns `None` for the rest.
    """

    if style == "chat":
        return _chat_line(event)
    head = f"{event.type} agent={event.agent} depth={event.depth} run={event.run_id}"
    facts = EVENT_FIELDS.get(type(event))
    detail = facts.log(event) if facts is not None and facts.log is not None else ""
    return f"{head} {detail}" if detail else head


def _chat_line(event: RunEvent) -> str | None:
    if isinstance(event, ToolCallEvent):
        if event.tool_name in _COORDINATION_TOOLS or event.tool_name.startswith("transfer_to_"):
            return None
        return f"{event.agent} -> {event.tool_name}({_cut(event.arguments)})"
    if isinstance(event, ToolResultEvent):
        if not event.is_error:
            return None
        return (
            f"{event.agent} <- {event.tool_name} [error]: {_cut(tool_output_text(event.content))}"
        )
    if isinstance(event, DelegationEvent):
        verb = "asks" if event.mode == "consult" else "=>"
        return f"{event.agent} {verb} {event.target}: {_cut(event.task)}"
    if isinstance(event, DelegationResultEvent):
        return f"{event.agent} <= {event.target} [{event.status}]"
    if isinstance(event, HandoffEvent):
        reason = f": {_cut(event.reason)}" if event.reason else ""
        return f"{event.source} >> {event.target}{reason}"
    if isinstance(event, AgentMessageEvent):
        message = event.message
        recipient = message.recipient or message.topic or "all"
        return f"{message.sender} -> {recipient} [{message.kind}]: {_cut(message.subject)}"
    if isinstance(event, ErrorEvent):
        return f"! {event.agent} failed: {event.error_type}: {_cut(event.message)}"
    return None


def _cut(value: Any) -> str:
    """`value` on one line (JSON unless it is text), credentials redacted, cut for a chat line."""

    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return preview(redact_secrets(text), _CHAT_PREVIEW_CHARS, flatten=True)


class EventLogger:
    """Subscriber that logs every run event, redacted, on an `awa` logger.

    Args:
        level: Log level, a number or a name such as `"DEBUG"`.
        logger: Defaults to `awa.events`.
        include_text_deltas: Also log `TextDeltaEvent`s (one line per streamed chunk).
        redactor: The default masks credentials and personal data (credentials only with
            `guard.redaction` off); `None` logs lines unchanged.

    Example:
        ```python
        recorder = EventRecorder()
        agent = Agent("helper", hooks=[EventLogger(), recorder])
        ```
    """

    def __init__(
        self,
        *,
        level: int | str = logging.INFO,
        logger: logging.Logger | None = None,
        include_text_deltas: bool = False,
        redactor: Redactor | None = PII_REDACTOR,
    ) -> None:
        self._level = logging.getLevelName(level.upper()) if isinstance(level, str) else level
        if not isinstance(self._level, int):
            raise ConfigurationError(
                f"Unknown logging level: {level!r}; use a number or one of "
                "DEBUG, INFO, WARNING, ERROR, CRITICAL"
            )
        self._logger = logger or get_logger("events")
        self._include_text_deltas = include_text_deltas
        self._redactor = resolve_redactor(redactor)

    def __call__(self, event: RunEvent) -> None:
        """Log `event`."""

        if isinstance(event, TextDeltaEvent) and not self._include_text_deltas:
            return
        if self._logger.isEnabledFor(self._level):
            line = describe_event(event)
            if self._redactor is not None:
                line = self._redactor.redact(line)
            self._logger.log(self._level, "%s", line, extra={"unredacted": self._redactor is None})


class EventRecorder:
    """Subscriber that stores every run event in memory, in arrival order (`events`)."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def __call__(self, event: RunEvent) -> None:
        """Record `event`."""

        self.events.append(event)

    def of_type(self, event_type: type[EventT]) -> list[EventT]:
        """Return the recorded events that are instances of `event_type`, in order."""

        return [event for event in self.events if isinstance(event, event_type)]

    def types(self) -> list[str]:
        """Return the `type` discriminator of every recorded event, in order."""

        return [event.type for event in self.events]

    def clear(self) -> None:
        """Remove all recorded events."""

        self.events.clear()

    def __len__(self) -> int:
        return len(self.events)

    def __iter__(self) -> Iterator[RunEvent]:
        return iter(list(self.events))


def build_trace_tags(settings: Settings, *, workflow: str) -> list[str]:
    """LangSmith trace tags for a run of `workflow`: `workflow:<name>`, `env:<env>`, and `langsmith` when on.

    LangChain sends traces to LangSmith itself with `LANGSMITH_TRACING` set; attach these tags via
    `RunnableConfig(tags=...)`. No credential is read.
    """

    tags = [f"workflow:{workflow}", f"env:{settings.env}"]
    if settings.langsmith_tracing:
        tags.append("langsmith")
    return tags


__all__ = ["EventLogger", "EventRecorder", "build_trace_tags", "describe_event"]
