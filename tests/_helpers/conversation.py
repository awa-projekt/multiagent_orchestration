"""Reading a scripted model's conversation: the system prompt, the request, tool results and
the calls and delegations a run made."""

from __future__ import annotations

import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from awa.contracts.events import DelegationEvent, ToolCallEvent
from awa.testing import ModelCall


def system_text(call: ModelCall) -> str:
    first = call.messages[0]
    return str(first.content) if isinstance(first, SystemMessage) else ""


def request(call: ModelCall) -> str:
    """The first user message: the task the agent works on."""

    return next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))


def results(call: ModelCall, tool: str) -> list[str]:
    return [str(m.content) for m in call.messages if isinstance(m, ToolMessage) and m.name == tool]


def called(call: ModelCall, tool: str) -> list[dict[str, Any]]:
    """Arguments of every call of `tool` the agent made so far in this conversation."""

    return [
        dict(item["args"])
        for m in call.messages
        if isinstance(m, AIMessage)
        for item in m.tool_calls
        if item["name"] == tool
    ]


def delegated_results(call: ModelCall) -> dict[str, str]:
    """Results of `delegate_task` calls by member name (the latest one wins)."""

    found: dict[str, str] = {}
    for content in results(call, "delegate_task"):
        match = re.search(r'<result agent="([\w-]+)"[^>]*>\s*(.*?)\s*</result>', content, re.S)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def tool_names(events: list[Any]) -> list[str]:
    return [event.tool_name for event in events if isinstance(event, ToolCallEvent)]


def delegation_targets(events: list[Any]) -> list[str]:
    return [event.target for event in events if isinstance(event, DelegationEvent)]
