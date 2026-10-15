"""Answer a tool call that never got a result before the next model call.

A cancelled or crashed run can leave a tool call without a `ToolMessage`, which providers refuse.
A resumed paused thread skips `before_agent`, so a call waiting for an approval is never
repaired away.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, AnyMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

_MESSAGE = (
    "Tool call {name} with id {call_id} did not complete - no result was recorded. "
    "It may have been cancelled or interrupted."
)


def _unanswered(message: AIMessage, answered: set[str]) -> list[Any]:
    return [call for call in message.tool_calls if call.get("id") and call["id"] not in answered]


def unanswered_tool_calls(messages: Sequence[AnyMessage]) -> int:
    """How many tool calls in `messages` have no `ToolMessage` with their id."""

    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return sum(len(_unanswered(m, answered)) for m in messages if isinstance(m, AIMessage))


def _patched(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    patched: list[AnyMessage] = []
    for message in messages:
        patched.append(message)
        if not isinstance(message, AIMessage):
            continue
        for call in _unanswered(message, answered):
            answered.add(call["id"])
            patched.append(
                ToolMessage(
                    content=_MESSAGE.format(name=call["name"], call_id=call["id"]),
                    name=call["name"],
                    tool_call_id=call["id"],
                    status="error",
                )
            )
    return patched


class RepairToolCallsMiddleware(AgentMiddleware):
    """Answers each tool call without a result with a synthetic error message when a turn starts.

    Nothing is removed or reordered; `invalid_tool_calls` are left to LangChain.
    """

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Repair the thread when it holds an unanswered tool call."""

        messages = state.get("messages", [])
        if not unanswered_tool_calls(messages):
            return None
        return {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *_patched(messages)]}


__all__ = ["RepairToolCallsMiddleware", "unanswered_tool_calls"]
