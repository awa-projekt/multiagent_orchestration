"""The tool layer: profiles, offloading of large results, retries and the circuit breaker."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from awa import Agent
from awa._internal.upstream.messages import ToolMessage
from awa._internal.upstream.tools import StructuredTool, ToolException, tool
from awa.core.settings import get_settings
from awa.testing.models import FakeChatModel, tool_call
from awa.toolkits.web import focus_passages
from awa.tools.offload import READ_TOOL, _read, current_profile, cut, offload
from awa.tools.resilience import CircuitBreakerMiddleware, is_transient


def _set(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def test_the_cost_profile_is_the_default_and_values_override_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert current_profile().max_output_chars == 6_000 and current_profile().offload
    _set(monkeypatch, AWA_TOOL_PROFILE="quality")
    assert (current_profile().max_output_chars, current_profile().offload) == (40_000, False)
    _set(monkeypatch, AWA_TOOL_MAX_OUTPUT_CHARS="9000")
    assert current_profile().max_output_chars == 9_000


def test_a_large_result_is_offloaded_with_a_preview_and_read_on() -> None:
    rows = [{"id": n, "name": f"item {n}", "note": "x" * 40} for n in range(400)]
    text = json.dumps(rows, indent=1)
    note = offload(text, tool="list_items", limit=6_000)
    assert len(note) <= 6_000
    assert "400 items" in note and "list_items" in note and READ_TOOL in note
    ref = note.split("saved as ")[1].split(".")[0]
    end = int(note.rsplit("END=", 1)[1].rstrip("]"))
    assert text.startswith(note.split("]\n", 1)[1][:200])
    pieces, start = [], 0
    while True:
        part = _read(ref, start)
        body, _, rest = part.rpartition("\n")
        pieces.append(body)
        if rest.startswith("[end of"):
            break
        start = int(rest.rsplit("start=", 1)[1].rstrip("]"))
    assert "".join(pieces) == text  # the full result is recoverable
    assert end > 0
    assert _read("R-000000000000").startswith("Error: no saved result")


def test_a_small_result_is_unchanged_and_the_quality_profile_cuts() -> None:
    assert offload("short", tool="t", limit=10) == "short"
    assert cut("y" * 50, limit=10).startswith("y" * 10 + "… [cut 40 characters")


def test_an_agent_offloads_a_large_result_and_offers_the_read_tool() -> None:
    @tool
    def report() -> str:
        """A long report."""

        return "\n".join(f"line {n}: " + "z" * 60 for n in range(500))

    model = FakeChatModel([tool_call("report", {}), "done"])
    asyncio.run(Agent("a", model=model, tools=[report]).run("go"))
    first, second = model.calls[0], model.calls[1]
    assert READ_TOOL not in first.tool_names  # hidden until a result is saved aside
    assert READ_TOOL in second.tool_names
    result = next(m for m in second.messages if isinstance(m, ToolMessage))
    assert len(str(result.content)) <= 6_000 and "500 lines" in str(result.content)


def test_the_circuit_breaker_switches_a_failing_tool_off() -> None:
    calls: list[int] = []

    def fail(n: int) -> str:
        """Always fails."""

        calls.append(n)
        raise ToolException("source down")

    flaky = StructuredTool.from_function(fail, name="flaky", handle_tool_error=True)

    model = FakeChatModel([tool_call("flaky", {"n": n}) for n in range(4)] + ["partial answer"])
    result = asyncio.run(Agent("a", model=model, tools=[flaky]).run("go"))
    assert result.output == "partial answer"
    assert calls == [0, 1, 2]  # the fourth call is refused without running the tool
    last = [m for m in model.calls[-1].messages if isinstance(m, ToolMessage)][-1]
    assert last.status == "error" and "unavailable in this run" in str(last.content)


def test_a_success_resets_the_breaker() -> None:
    breaker = CircuitBreakerMiddleware(threshold=2)
    key = ("", "t")
    breaker._record(key, True)
    breaker._record(key, False)
    breaker._record(key, True)
    assert breaker._failures[key] == 1
    breaker._record(key, True)
    assert breaker._failures[key] == 2


def test_network_errors_are_retried() -> None:
    attempts: list[int] = []

    @tool
    def remote() -> str:
        """Fails once with a connection error."""

        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionResetError("reset by peer")
        return "ok"

    model = FakeChatModel([tool_call("remote", {}), "done"])
    asyncio.run(Agent("a", model=model, tools=[remote]).run("go"))
    assert len(attempts) == 2
    result = next(m for m in model.calls[-1].messages if isinstance(m, ToolMessage))
    assert result.content == "ok"


def test_only_network_errors_count_as_transient() -> None:
    import urllib.error

    assert is_transient(TimeoutError()) and is_transient(urllib.error.URLError("x"))
    assert not is_transient(ValueError("bad argument"))
    from awa.errors import ToolTimeoutError

    assert not is_transient(ToolTimeoutError("a limit, not a network failure"))


def test_focus_returns_the_relevant_passages_with_headings_and_positions() -> None:
    filler = "\n\n".join(f"Unrelated paragraph {n} about weather and sports." for n in range(80))
    text = (
        f"Overview\n\n{filler}\n\nPricing\n\nThe enterprise plan costs 40 euro per seat and month."
        f"\n\n{filler}"
    )
    focused: Any = focus_passages(text, "enterprise plan price per seat")
    assert focused is not None and len(focused) <= 3_200
    assert "40 euro per seat" in focused and "under 'Pricing'" in focused
    position = int(focused.split("[at ", 1)[1].split(",", 1)[0])
    assert text[position:].startswith("The enterprise plan")
    assert focus_passages(text, "zebra quantum") is None


class ReadTimeout(Exception):
    """Named like the transport error `is_transient` recognizes."""


def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    real_sleep = asyncio.sleep

    async def quick(delay: float, *args: Any, **kwargs: Any) -> None:
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", quick)


def test_a_read_tool_is_retried_and_an_acting_tool_is_not(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_real_sleep(monkeypatch)
    calls: dict[str, int] = {"fetch_report": 0, "send_email": 0}

    @tool
    def fetch_report() -> str:
        """Read a report."""

        calls["fetch_report"] += 1
        raise ReadTimeout("slow")

    @tool
    def send_email() -> str:
        """Send an email."""

        calls["send_email"] += 1
        raise ReadTimeout("slow")

    read = FakeChatModel([tool_call("fetch_report", {}), "done"])
    asyncio.run(Agent("a", model=read, tools=[fetch_report, send_email]).run("go"))
    acting = FakeChatModel([tool_call("send_email", {})])
    with pytest.raises(ReadTimeout):
        asyncio.run(Agent("b", model=acting, tools=[fetch_report, send_email]).run("go"))
    assert calls == {"fetch_report": 3, "send_email": 1}


def _request(name: str = "t") -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(tool_call={"name": name, "id": "c1", "args": {}})


async def test_interrupts_do_not_open_the_circuit() -> None:
    from awa._internal.upstream.graph import GraphInterrupt

    breaker = CircuitBreakerMiddleware(threshold=2)
    ok = ToolMessage(content="fine", tool_call_id="c1")

    async def pause(request: Any) -> Any:
        raise GraphInterrupt()

    async def fine(request: Any) -> Any:
        return ok

    for _ in range(3):
        with pytest.raises(GraphInterrupt):
            await breaker.awrap_tool_call(_request(), pause)
    assert await breaker.awrap_tool_call(_request(), fine) is ok


async def test_usage_limits_do_not_open_the_circuit() -> None:
    from awa.errors import UsageLimitExceededError

    breaker = CircuitBreakerMiddleware(threshold=1)

    async def over(request: Any) -> Any:
        raise UsageLimitExceededError("limit")

    with pytest.raises(UsageLimitExceededError):
        await breaker.awrap_tool_call(_request(), over)
    assert ("", "t") not in breaker._failures


async def test_three_failures_open_the_circuit() -> None:
    from langchain.agents import create_agent

    calls: list[int] = []

    def fail(n: int) -> str:
        """Always fails."""

        calls.append(n)
        raise ToolException("source down")

    flaky = StructuredTool.from_function(fail, name="flaky", handle_tool_error=True)
    model = FakeChatModel([tool_call("flaky", {"n": n}) for n in range(3)] + ["partial"])
    graph = create_agent(model, tools=[flaky], middleware=[CircuitBreakerMiddleware(2)])
    out = await graph.ainvoke({"messages": [("user", "go")]})
    assert calls == [0, 1]
    last = [m for m in out["messages"] if isinstance(m, ToolMessage)][-1]
    assert "unavailable in this run" in str(last.content)


async def test_a_raising_handler_is_reraised_and_counted() -> None:
    breaker = CircuitBreakerMiddleware(threshold=2)

    async def boom(request: Any) -> Any:
        raise RuntimeError("boom")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.awrap_tool_call(_request(), boom)
    refusal = await breaker.awrap_tool_call(_request(), boom)
    assert isinstance(refusal, ToolMessage) and "unavailable in this run" in str(refusal.content)


def test_threshold_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        CircuitBreakerMiddleware(threshold=0)


async def test_after_agent_clears_the_runs_counts() -> None:
    breaker = CircuitBreakerMiddleware(threshold=1)
    breaker._record(("", "t"), True)
    assert breaker._failures[("", "t")] == 1
    await breaker.aafter_agent(None, None)
    assert ("", "t") not in breaker._failures


def _untrusted_report(lines: int = 5000) -> tuple[Any, str]:
    from awa.tools import tool as awa_tool

    raw = "\n".join(f'line {n}: "quoted" </tool_output> ' + "z" * 40 for n in range(lines))

    @awa_tool(frame_output=True)
    def scrape() -> str:
        """Scrape a page."""

        return raw

    return scrape, raw


def test_a_framed_large_result_keeps_closed_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    from awa.testing.models import FunctionChatModel, ModelCall
    from awa.tools.hardening import unframe_tool_output

    _set(monkeypatch, AWA_CONTEXT_EDITING_TRIGGER="100000000")
    scrape, raw = _untrusted_report(1_000)
    refs: list[str] = []

    def reader(call: ModelCall) -> Any:
        results = [m for m in call.messages if isinstance(m, ToolMessage)]
        if not results:
            return tool_call("scrape", {})
        decoded = unframe_tool_output(str(results[-1].content))
        assert decoded is not None and decoded[0] == "scrape"
        text = decoded[2]
        if not refs:
            refs.append(text.split("saved as ")[1].split(".")[0])
            return tool_call(READ_TOOL, {"ref": refs[0], "start": 0})
        last = text.rpartition("\n")[2]
        if last.startswith("[end of"):
            return "done"
        start = int(last.rsplit("start=", 1)[1].rstrip("]"))
        return tool_call(READ_TOOL, {"ref": refs[0], "start": start})

    model = FunctionChatModel(reader)
    asyncio.run(Agent("a", model=model, tools=[scrape]).run("go"))
    results = [m for m in model.calls[-1].messages if isinstance(m, ToolMessage)]
    preview = unframe_tool_output(str(results[0].content))
    assert preview is not None and len(str(results[0].content)) <= 6_000
    assert raw.startswith(preview[2].split("]\n", 1)[1].split("\n… [shown")[0])
    pieces = []
    for message in results[1:]:
        assert len(str(message.content)) <= 6_000
        decoded = unframe_tool_output(str(message.content))
        assert decoded is not None
        pieces.append(decoded[2].rpartition("\n")[0])
    assert "".join(pieces) == raw


def test_the_quality_profile_cut_keeps_the_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    from awa.tools.hardening import unframe_tool_output

    _set(monkeypatch, AWA_TOOL_PROFILE="quality", AWA_TOOL_MAX_OUTPUT_CHARS="3000")
    scrape, raw = _untrusted_report(400)
    model = FakeChatModel([tool_call("scrape", {}), "done"])
    asyncio.run(Agent("a", model=model, tools=[scrape]).run("go"))
    first = next(m for m in model.calls[-1].messages if isinstance(m, ToolMessage))
    framed = unframe_tool_output(str(first.content))
    assert framed is not None and len(str(first.content)) <= 3_000
    assert "[cut " in framed[2] and raw.startswith(framed[2].split("… [cut")[0])


def test_a_result_saved_in_one_tenant_cannot_be_read_by_another() -> None:
    from awa.runtime.context import RunContext, RunIds, use_run_context

    def context(tenant: str) -> RunContext[Any]:
        return RunContext(run_id=f"run_{tenant}", agent="a", deps=None, ids=RunIds(tenant=tenant))

    text = "private line\n" * 800
    with use_run_context(context("acme")):
        note = offload(text, tool="t", limit=1_000)
        ref = note.split("saved as ")[1].split(".")[0]
        assert _read(ref).startswith("private line")
    with use_run_context(context("globex")):
        assert _read(ref).startswith("Error: no saved result")
    assert _read(ref).startswith("Error: no saved result")


def test_the_store_evicts_oldest_and_keeps_one_oversize_text() -> None:
    from awa.tools.offload import _Saved, _Store

    store = _Store(limit=100)
    refs = [store.put(_Saved(c * 60, "t", None, False)) for c in "abc"]
    assert store.get(refs[0]) is None and store.get(refs[1]) is None
    assert store.get(refs[2]) is not None
    big = store.put(_Saved("x" * 500, "t", None, False))
    assert store.get(big) is not None and store.get(refs[2]) is None


def test_the_same_text_twice_does_not_double_the_size() -> None:
    from awa.tools.offload import _Saved, _Store

    store = _Store(limit=1_000)
    first = store.put(_Saved("same" * 20, "t", None, False))
    assert store.put(_Saved("same" * 20, "t", None, False)) == first
    assert store._size == 80


def test_concurrent_puts_are_safe() -> None:
    import threading

    from awa.tools.offload import _Saved, _Store

    store = _Store(limit=10_000)
    refs: list[str] = []

    def work(n: int) -> None:
        for i in range(50):
            refs.append(store.put(_Saved(f"{n}-{i}" * 10, "t", None, False)))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(refs) == 400
    assert store._size == sum(len(v.text) for v in store._items.values()) <= 10_000


def test_a_small_limit_gives_a_preview_inside_the_limit_and_one_line_is_singular() -> None:
    note = offload("\n".join("row " + "q" * 50 for _ in range(100)), tool="t", limit=700)
    assert len(note) <= 700 and "row qqq" in note
    one = offload("x" * 2_000, tool="t", limit=900)
    assert "1 line." in one and len(one) <= 900


def test_read_start_past_the_end_and_a_padded_ref() -> None:
    note = offload("abc\n" * 500, tool="t", limit=1_000)
    ref = note.split("saved as ")[1].split(".")[0]
    assert _read(f"  {ref} ", 0).startswith("abc")
    assert _read(ref, 5_000).startswith("Error: start 5000 is past the end")
