"""Offline test for `examples/06_memory.py`."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import SystemMessage, ToolMessage

from _helpers.examples import load_example
from awa.memory import RECALLED_MEMORIES_ID_PREFIX, MemoryScope
from awa.testing import FunctionChatModel, ModelCall, tool_call


def recalled(call: ModelCall) -> list[str]:
    system = call.messages[0]
    assert isinstance(system, SystemMessage)
    assert "<memories" not in str(system.text)  # auto_recall never touches the system prompt
    context = [m for m in call.messages if (m.id or "").startswith(RECALLED_MEMORIES_ID_PREFIX)]
    if not context:
        return []
    match = re.search(r"<memories[^>]*>\n(.*)\n</memories>", str(context[0].text), re.DOTALL)
    assert match is not None
    return [m["content"] for m in json.loads(match.group(1))]


async def test_assistant_remembers_per_customer_across_conversations() -> None:
    assistant = load_example("06_memory").build_account_assistant()

    def respond(call: ModelCall) -> Any:
        question = str(call.messages[-1].content)
        if isinstance(call.messages[-1], ToolMessage):
            return "Noted: invoices in EUR."
        if "send my invoices in EUR" in question:
            return tool_call("remember", {"content": "Invoices in EUR", "tags": ["billing"]})
        memories = recalled(call)
        return f"Your invoices use {memories[0]}." if memories else "I have no preference on file."

    model = FunctionChatModel(respond)
    with assistant.override(model=model):
        first = await assistant.run(
            "Please send my invoices in EUR and address them to our finance team.",
            deps={"user_id": "customer-17"},
        )
        later = await assistant.run(
            "Which currency do you use for my invoices?", deps={"user_id": "customer-17"}
        )
        other = await assistant.run(
            "Which currency do you use for my invoices?", deps={"user_id": "customer-99"}
        )

    assert first.text == "Noted: invoices in EUR."
    assert later.text == "Your invoices use Invoices in EUR."
    assert other.text == "I have no preference on file."
    assert assistant.memory is not None
    stored = await assistant.memory.list_items(
        owner=MemoryScope(agent="account_assistant", user_id="customer-17")
    )
    assert [item.content for item in stored] == ["Invoices in EUR"]
    assert assistant.compaction is not None
    assert {"remember", "recall", "forget"} <= set(assistant.card.tools)
