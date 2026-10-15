"""Context editing: replace older tool results with a short placeholder before a model call.

Above `AWA_CONTEXT_EDITING_TRIGGER` tokens, older tool results become a note naming the call; the
most recent `AWA_CONTEXT_EDITING_KEEP` stay. Only the request changes, never the agent's state.
Results are cleared in steps so the cached prompt prefix stays stable between steps.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from awa._internal.upstream.agents import ClearToolUsesEdit, ContextEditingMiddleware
from awa._internal.upstream.messages import AIMessage, AnyMessage, BaseMessage, ToolMessage

PLACEHOLDER = (
    "[An older tool result was removed from this view to save context; what you did with it "
    "stands. Do not start that work again: call the tool only for data you still lack.]"
)
"""The text that replaces a cleared tool result (followed by the call it answered)."""

PRE_CLEAR_NOTE = (
    "[Context note: older tool results will soon be removed from this view. Write down the "
    "facts you still need from them in your next reply or notes.]"
)
"""Added to the latest tool result once a request nears the trigger, before any clearing."""

_NOTE_AT = 0.8
_ARGS_CHARS = 200


@dataclass(frozen=True, slots=True)
class ContextEditing:
    """When older tool results are cleared from what the model sees.

    Attributes:
        trigger: Approximate request size in tokens above which results are cleared.
        keep: Most recent tool results always kept verbatim.
        step: Results cleared together, so the cached prompt prefix stays the same in between.
        exclude_tools: Tool names whose results are never cleared.
        clear_at_least: Fewest tokens a step must free; a smaller step clears nothing.
        pre_clear_note: At 80 % of the trigger, ask the model to note needed facts before clearing.
    """

    trigger: int = 40_000
    keep: int = 4
    step: int = 4
    exclude_tools: tuple[str, ...] = ()
    clear_at_least: int = 0
    pre_clear_note: bool = True

    def __post_init__(self) -> None:
        if self.trigger < 1 or self.keep < 0 or self.step < 1 or self.clear_at_least < 0:
            raise ValueError(
                "trigger and step must be at least 1, keep and clear_at_least at least 0"
            )

    def middleware(self) -> ContextEditingMiddleware:
        """The LangChain middleware applying this edit."""

        return ContextEditingMiddleware(edits=[_SteppedClear(self)])


def from_settings() -> ContextEditing | None:
    """The process settings' context editing, or `None` when it is switched off."""

    from awa.core.settings import get_settings
    from awa.tools.offload import current_profile

    settings = get_settings()
    if not settings.context_editing:
        return None
    profile = current_profile()
    return ContextEditing(
        trigger=profile.editing_trigger,
        keep=profile.editing_keep,
        step=settings.context_editing_step,
        clear_at_least=profile.clear_at_least,
    )


def cleared_tool_results(messages: Sequence[Any]) -> int:
    """How many tool results in `messages` were replaced by context editing."""

    return sum(
        1
        for m in messages
        if isinstance(m, ToolMessage)
        and (m.response_metadata or {}).get("context_editing", {}).get("cleared")
    )


class _SteppedClear:
    """`ClearToolUsesEdit` whose cleared part grows in steps (cache friendly)."""

    def __init__(self, config: ContextEditing) -> None:
        self._config = config

    def apply(
        self, messages: list[AnyMessage], *, count_tokens: Callable[[Sequence[BaseMessage]], int]
    ) -> None:
        config = self._config
        results = sum(isinstance(m, ToolMessage) for m in messages)
        clearable = results - config.keep
        cleared = clearable - clearable % config.step  # a multiple of step: a stable boundary
        if cleared < 1:
            self._note(messages, count_tokens)
            return
        before = count_tokens(messages) if config.clear_at_least else 0
        edited = list(messages)
        ClearToolUsesEdit(
            trigger=config.trigger,
            keep=results - cleared,
            exclude_tools=config.exclude_tools,
            placeholder=PLACEHOLDER,
        ).apply(edited, count_tokens=count_tokens)
        if config.clear_at_least and before - count_tokens(edited) < config.clear_at_least:
            self._note(messages, count_tokens)
            return
        messages[:] = _with_stubs(edited)

    def _note(
        self, messages: list[AnyMessage], count_tokens: Callable[[Sequence[BaseMessage]], int]
    ) -> None:
        """Append the pre-clear note to the latest tool result near the trigger."""

        config = self._config
        if not config.pre_clear_note or cleared_tool_results(messages):
            return
        last = len(messages) - 1
        if last < 0 or not isinstance(messages[last], ToolMessage):
            return
        if sum(isinstance(m, ToolMessage) for m in messages) <= config.keep:
            return
        if count_tokens(messages) < config.trigger * _NOTE_AT:
            return
        message = messages[last]
        if isinstance(message.content, str):
            messages[last] = message.model_copy(
                update={"content": message.content + "\n" + PRE_CLEAR_NOTE}
            )


def _with_stubs(messages: list[AnyMessage]) -> list[AnyMessage]:
    """`messages` with each cleared result naming the call it answered (tool and args)."""

    calls: dict[str, tuple[str, str]] = {}
    out: list[AnyMessage] = []
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                args = json.dumps(call.get("args", {}), ensure_ascii=False, sort_keys=True)
                if len(args) > _ARGS_CHARS:
                    args = args[:_ARGS_CHARS] + "…"
                calls[call.get("id") or ""] = (call["name"], args)
        elif (
            isinstance(message, ToolMessage)
            and message.content == PLACEHOLDER
            and message.tool_call_id in calls
        ):
            name, args = calls[message.tool_call_id]
            message = message.model_copy(update={"content": f"{PLACEHOLDER} Call: {name} {args}"})
        out.append(message)
    return out


__all__ = [
    "PLACEHOLDER",
    "PRE_CLEAR_NOTE",
    "ContextEditing",
    "cleared_tool_results",
    "from_settings",
]
