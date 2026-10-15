"""Budgets across a resume, and the output cap of the tool layer."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from awa import Agent
from awa._internal.upstream.messages import ToolMessage
from awa.agents.middleware import _cap_output, budget_record
from awa.contracts.usage import UsageLimits
from awa.core.settings import get_settings
from awa.runtime.context import RunContext
from awa.testing import FakeChatModel, tool_call
from awa.tools import create_tool_approval_wrapper, tool
from awa.tools.hardening import (
    MAX_BLOCK_CHARS,
    MAX_BLOCKS,
    OUTPUT_LIMIT_METADATA_KEY,
    frame_tool_content,
    unframe_tool_output,
)


def _agent(calls: list[int]) -> tuple[Agent[Any, Any], FakeChatModel]:
    @tool
    def count(n: int) -> str:
        """Count a call."""

        calls.append(n)
        return f"call {n}"

    @tool
    def gated(n: int) -> str:
        """Act after approval."""

        return "done acting"

    model = FakeChatModel(
        [
            [tool_call("count", {"n": 1}), tool_call("gated", {"n": 1})],
            tool_call("count", {"n": 2}),
            tool_call("count", {"n": 3}),
            "finished",
        ]
    )
    agent = Agent(
        "worker",
        model=model,
        tools=[count, create_tool_approval_wrapper(gated)],
        checkpointer=InMemorySaver(),
    )
    return agent, model


async def test_resume_continues_usage_and_tool_counts() -> None:
    calls: list[int] = []
    agent, model = _agent(calls)

    paused = await agent.run("go", usage_limits=UsageLimits(per_tool_limits={"count": 2}))
    assert paused.thread_id is not None and paused.is_interrupted
    done = await agent.resume(paused.thread_id, {"action": "approve"})

    assert done.output == "finished"
    assert calls == [1, 2]  # the third call is refused, not run
    last = [str(m.content) for m in model.calls[-1].messages if isinstance(m, ToolMessage)]
    assert "per_tool_limits" in last[-1]


async def test_resume_with_explicit_limits_uses_them() -> None:
    calls: list[int] = []
    agent, model = _agent(calls)

    paused = await agent.run("go", usage_limits=UsageLimits(per_tool_limits={"count": 2}))
    assert paused.thread_id is not None
    done = await agent.resume(
        paused.thread_id,
        {"action": "approve"},
        usage_limits=UsageLimits(per_tool_limits={"count": 5}),
    )

    assert done.output == "finished" and calls == [1, 2, 3]
    last = [str(m.content) for m in model.calls[-1].messages if isinstance(m, ToolMessage)]
    assert not any("per_tool_limits" in text for text in last)


def test_budget_record_counts_requested_calls() -> None:
    ctx: RunContext[Any] = RunContext(run_id="r1", agent="worker", deps=None)
    ctx.loop.count_tool("count", "c1")
    requested = [
        tool_call(name, {}, id=f"c{i}") for i, name in enumerate(["count"] * 3 + ["other"])
    ]
    record = budget_record(ctx, requested)
    assert record["loop"]["tool_calls"] == {"count": 3, "other": 1}
    assert budget_record(ctx)["loop"]["tool_calls"] == {"count": 1}


async def test_a_call_paused_for_approval_counts_once_against_its_limit() -> None:
    ran: list[int] = []

    @tool
    def pay(n: int) -> str:
        """Pay once."""

        ran.append(n)
        return "paid"

    model = FakeChatModel([tool_call("pay", {"n": 1}), "finished"])
    agent = Agent(
        "payer",
        model=model,
        tools=[create_tool_approval_wrapper(pay)],
        checkpointer=InMemorySaver(),
    )
    paused = await agent.run("pay", usage_limits=UsageLimits(per_tool_limits={"pay": 1}))
    assert paused.thread_id is not None

    done = await agent.resume(paused.thread_id, {"action": "approve"})

    assert done.output == "finished" and ran == [1]


def _result(content: Any) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id="c1", name="lookup")


def _request(metadata: dict[str, Any] | None = None) -> Any:
    described = SimpleNamespace(metadata=metadata or {})
    return SimpleNamespace(tool_call={"name": "lookup", "id": "c1", "args": {}}, tool=described)


def _profile(monkeypatch: pytest.MonkeyPatch, name: str, limit: str) -> None:
    monkeypatch.setenv("AWA_TOOL_PROFILE", name)
    monkeypatch.setenv("AWA_TOOL_MAX_OUTPUT_CHARS", limit)
    get_settings.cache_clear()


def test_a_cap_output_through_the_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    long_text = "row of data\n" * 300
    _profile(monkeypatch, "quality", "500")
    cut = _cap_output(_request(), _result(long_text))
    assert "[cut " in cut.content and len(cut.content) < len(long_text)
    own = _cap_output(_request({OUTPUT_LIMIT_METADATA_KEY: 100_000}), _result(long_text))
    assert own.content == long_text  # a tool with its own output limit is left alone

    _profile(monkeypatch, "cost", "800")
    saved = _cap_output(_request(), _result(long_text))
    assert "the full result is saved as R-" in saved.content and len(saved.content) <= 800

    short = _result("short")
    assert _cap_output(_request(), short) is short


def test_block_content_is_capped_like_text(monkeypatch: pytest.MonkeyPatch) -> None:
    long_text = "row of data\n" * 300
    image = {"type": "image", "url": "https://x.example/a.png"}
    framed = frame_tool_content(
        [{"type": "text", "text": long_text}, image], tool="lookup", source="mcp:crm"
    )
    _profile(monkeypatch, "quality", "500")
    [text, kept] = _cap_output(_request(), _result(framed)).content
    assert kept == image
    unframed = unframe_tool_output(text["text"])
    assert unframed is not None and unframed[1] == "mcp:crm"
    assert "[cut " in unframed[2] and len(unframed[2]) < 600

    _profile(monkeypatch, "cost", "800")
    [saved, kept] = _cap_output(_request(), _result([long_text, image])).content
    assert kept == image and "the full result is saved as R-" in saved["text"]
    short = _result([{"type": "text", "text": "short"}])
    assert _cap_output(_request(), short) is short


def test_non_text_blocks_are_bounded_in_number_and_size() -> None:
    image = {"type": "image", "base64": "A" * 10, "mime_type": "image/png"}
    huge = {"type": "file", "base64": "A" * (MAX_BLOCK_CHARS + 1), "mime_type": "application/pdf"}
    content = [{"type": "text", "text": "rows"}, huge, *[image] * (MAX_BLOCKS + 3)]
    bounded = _cap_output(_request(), _result(content)).content
    assert huge not in bounded and bounded.count(image) == MAX_BLOCKS
    note = bounded[-1]["text"]
    assert note.startswith("[dropped 4 block(s) (file, image)")

    only_images = _result([image, image])
    assert _cap_output(_request(), only_images) is only_images
