"""Deterministic token benchmark: a triage-like run before and after the token cuts.

A scripted fake model lists a 60-message Outlook folder twelve times (the Outlook connector's
own item transform on Graph-shaped messages) and then answers. The benchmark counts the
characters sent to the model and a simulated prefix-cache hit rate (the part of each request
that equals the start of the previous request, which is what OpenAI/Azure and Claude cache).

Before: every list item with all its Graph fields and no context editing.
After: compact items (the default) and context editing on (the default).
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from awa import Agent
from awa._internal.upstream.tools import tool
from awa.connectors._saas_microsoft_outlook import _brief
from awa.core.settings import get_settings
from awa.testing.models import FakeChatModel, ModelCall, tool_call

PAGES = 12  # the traced collector listed 19 times
PER_PAGE = 60  # one list page


def _graph_message(n: int) -> dict[str, Any]:
    return {
        "id": f"AAMkAGI2TG93AAA{n:04d}" + "x" * 120,
        "subject": f"Order {n} needs a delivery date update",
        "from": {"emailAddress": {"address": f"buyer{n}@fabrikam.test", "name": f"Buyer {n}"}},
        "receivedDateTime": f"2026-10-0{1 + n % 7}T09:{n % 60:02d}:00Z",
        "bodyPreview": f"Hello team, regarding order {n}: " + "please confirm the date. " * 9,
        "isRead": n % 3 == 0,
        "hasAttachments": n % 4 == 0,
        "categories": ["Blue category"],
        "parentFolderId": "AQMkADAwATM0MDAAMS1hZmQ1LTk2" + "y" * 90,
        "internetMessageId": f"<CAB{n:04d}.{n * 7}@mail.fabrikam.test>",
        "conversationId": "AAQkAGI2TG93" + "z" * 100,
        "webLink": "https://outlook.office365.com/owa/?ItemID=AAMkAGI2" + "w" * 160,
    }


MAILBOX = [_graph_message(n) for n in range(PER_PAGE)]


def _run(*, detail: bool, editing: bool) -> tuple[int, int]:
    os.environ["AWA_CONTEXT_EDITING"] = "true" if editing else "false"
    get_settings.cache_clear()

    @tool
    def list_messages(page: int) -> str:
        """List the inbox (the scripted model re-lists it, as the traced run did)."""
        items = MAILBOX if detail else [_brief(m) for m in MAILBOX]
        listed = {"items": items, "page": page}
        return json.dumps(listed)

    script: list[Any] = [tool_call("list_messages", {"page": p}) for p in range(PAGES)]
    model = FakeChatModel([*script, "Triage done."])
    agent = Agent("collector", model=model, tools=[list_messages], instructions="Triage mail.")
    import asyncio

    assert asyncio.run(agent.run("Triage the inbox.")).output == "Triage done."

    def text(call: ModelCall) -> str:
        return "".join(f"{m.type}:{m.content}|" for m in call.messages)

    sent = [text(c) for c in model.calls]
    cached = sum(
        len(os.path.commonprefix([previous, current]))
        for previous, current in zip(sent, sent[1:], strict=False)
    )
    return sum(map(len, sent)), cached


@pytest.fixture(autouse=True)
def _clean_env() -> Any:
    before = os.environ.get("AWA_CONTEXT_EDITING")
    yield
    if before is None:
        os.environ.pop("AWA_CONTEXT_EDITING", None)
    else:
        os.environ["AWA_CONTEXT_EDITING"] = before
    get_settings.cache_clear()


def test_compact_lists_and_context_editing_cut_the_input_by_more_than_half(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A long run: clearing starts at 12,000 tokens here (the default 40,000 is for real runs).
    monkeypatch.setenv("AWA_CONTEXT_EDITING_TRIGGER", "12000")
    monkeypatch.setenv("AWA_CONTEXT_EDITING_KEEP", "2")
    # Measures list shape and clearing alone, not the default cut of long tool results.
    monkeypatch.setenv("AWA_TOOL_MAX_OUTPUT_CHARS", "10000000")
    # The run before compaction sends about 1.2 million tokens, above the default ceiling.
    monkeypatch.setenv("AWA_RUN_MAX_TOKENS", "10000000")
    runs = {
        "before": _run(detail=True, editing=False),
        "compact": _run(detail=False, editing=False),
        "after": _run(detail=False, editing=True),
    }
    for name, (sent, cached) in runs.items():
        print(f"\n{name}: sent={sent} cached={cached} ({cached / sent:.0%}) unc={sent - cached}")
    (before, before_cached), (compact, _), (after, after_cached) = runs.values()
    assert compact < before * 0.6
    assert after < before * 0.5

    # Clearing rewrites part of the prefix once per step, so the cached share drops, but the
    # price falls: cached input costs about a quarter (Azure OpenAI) of uncached input.
    def price(sent: int, cached: int) -> float:
        return sent - cached + 0.25 * cached

    assert price(after, after_cached) < price(before, before_cached) * 0.5
    assert after - after_cached < before - before_cached
