"""The read-only tool result cache (awa.tools.result_cache): what is reused, and what never is."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from awa import Agent
from awa.contracts.events import RunEvent, ToolResultEvent
from awa.errors import CacheStoreError
from awa.persistence.cache import InMemoryCacheStore
from awa.runtime.cache import Cache, CacheSettings
from awa.testing import FakeChatModel, tool_call
from awa.toolkits import file_toolkit
from awa.tools import tool
from awa.tools.result_cache import is_cacheable, tool_fingerprint
from awa.workflows import Sequential


class Counter:
    def __init__(self) -> None:
        self.calls: list[str] = []


def lookup_tool(
    counter: Counter, *, risk: Any = None, name: str = "lookup_order", **options: Any
) -> Any:
    @tool(name=name, risk=risk, **options)
    def lookup(order_id: str) -> str:
        """Look up an order."""
        counter.calls.append(order_id)
        return f"order {order_id}: shipped"

    return lookup


READ_ONLY = {"external_action": False}


def cache(**settings: Any) -> Cache:
    return Cache(CacheSettings(**settings), InMemoryCacheStore(), project="shop")


def script(*calls: Any) -> FakeChatModel:
    return FakeChatModel([*calls, "done"])


class Events:
    def __init__(self) -> None:
        self.results: list[ToolResultEvent] = []

    async def __call__(self, event: RunEvent) -> None:
        if isinstance(event, ToolResultEvent):
            self.results.append(event)


async def test_the_same_read_only_call_in_one_run_runs_once_and_shows_as_a_hit() -> None:
    counter, events = Counter(), Events()
    agent = Agent(
        "clerk",
        model=script(
            tool_call("lookup_order", {"order_id": "7"}),
            tool_call("lookup_order", {"order_id": "7"}),
        ),
        tools=[lookup_tool(counter, risk=READ_ONLY)],
        cache=cache(),
        hooks=[events],
    )

    await agent.run("Where is order 7?")

    assert counter.calls == ["7"]
    assert [e.cache for e in events.results] == ["miss", "hit"]
    assert events.results[0].content == events.results[1].content == "order 7: shipped"


async def test_other_arguments_are_another_entry() -> None:
    counter = Counter()
    agent = Agent(
        "clerk",
        model=script(
            tool_call("lookup_order", {"order_id": "7"}),
            tool_call("lookup_order", {"order_id": "8"}),
        ),
        tools=[lookup_tool(counter, risk=READ_ONLY)],
        cache=cache(),
    )

    await agent.run("x")

    assert counter.calls == ["7", "8"]


async def test_by_default_a_result_lives_for_one_run_only() -> None:
    counter, shared = Counter(), cache()
    agent = Agent(
        "clerk",
        model=FakeChatModel([tool_call("lookup_order", {"order_id": "7"}), "done"] * 2),
        tools=[lookup_tool(counter, risk=READ_ONLY)],
        cache=shared,
    )

    await agent.run("first")
    await agent.run("second")

    assert counter.calls == ["7", "7"]


async def test_with_a_ttl_results_are_reused_across_runs_but_never_across_tenants() -> None:
    counter, shared = Counter(), cache(tools_ttl=600)
    agent = Agent(
        "clerk",
        model=FakeChatModel([tool_call("lookup_order", {"order_id": "7"}), "done"] * 4),
        tools=[lookup_tool(counter, risk=READ_ONLY)],
        cache=shared,
    )

    await agent.run("a", metadata={"awa.tenant": "acme"})
    await agent.run("b", metadata={"awa.tenant": "acme"})
    await agent.run("c", metadata={"awa.tenant": "globex"})
    await agent.run("d", metadata={"awa.tenant": "globex"})

    assert counter.calls == ["7", "7"]  # once per tenant
    tenants = sorted(line.tenant for line in shared.store.stats())  # type: ignore[union-attr]
    assert tenants == ["acme", "globex"]


async def test_tools_ttl_cache_isolated_per_principal() -> None:
    counter, shared = Counter(), cache(tools_ttl=600)
    agent = Agent(
        "clerk",
        model=FakeChatModel([tool_call("lookup_order", {"order_id": "7"}), "done"] * 4),
        tools=[lookup_tool(counter, risk=READ_ONLY)],
        cache=shared,
    )

    for actor in ("alice", "alice", "bob", "bob"):
        await agent.run("my order", metadata={"awa.tenant": "acme", "awa.actor": actor})

    assert counter.calls == ["7", "7"]  # once per person, never alice's result for bob


async def test_a_tool_with_a_persons_rights_is_not_shared_by_runs_without_a_principal() -> None:
    counter, shared = Counter(), cache(tools_ttl=600)
    mine = lookup_tool(counter, risk=READ_ONLY, metadata={"awa_identity": "user"})
    agent = Agent(
        "clerk",
        model=script(*[tool_call("lookup_order", {"order_id": "7"})] * 2),
        tools=[mine],
        cache=shared,
    )
    await agent.run("my mail")  # the second call of the run is answered from the cache
    other = Agent(
        "clerk",
        model=FakeChatModel([tool_call("lookup_order", {"order_id": "7"}), "done"]),
        tools=[mine],
        cache=shared,
    )
    await other.run("my mail")

    assert counter.calls == ["7", "7"]  # once per run, not across runs


@pytest.mark.parametrize(
    "risk",
    [None, {"external_action": True}, {"untrusted_input": True}],
    ids=["undeclared", "acting", "no-external-action-flag"],
)
async def test_tools_that_act_or_do_not_declare_read_only_are_never_cached(risk: Any) -> None:
    counter = Counter()
    probe = lookup_tool(counter, risk=risk, name="update_order")
    agent = Agent(
        "clerk",
        model=script(
            tool_call("update_order", {"order_id": "7"}),
            tool_call("update_order", {"order_id": "7"}),
        ),
        tools=[probe],
        cache=cache(),
    )

    await agent.run("x")

    assert not is_cacheable(probe)
    assert counter.calls == ["7", "7"]


async def test_a_tool_behind_an_approval_gate_is_never_cached() -> None:
    gated = lookup_tool(Counter(), risk=READ_ONLY, approval=True)

    assert not is_cacheable(gated)


async def test_an_acting_tool_sets_earlier_results_of_the_run_aside() -> None:
    counter = Counter()

    @tool(risk={"external_action": True})
    def cancel_order(order_id: str) -> str:
        """Cancel an order."""
        return "cancelled"

    agent = Agent(
        "clerk",
        model=script(
            tool_call("lookup_order", {"order_id": "7"}),
            tool_call("cancel_order", {"order_id": "7"}),
            tool_call("lookup_order", {"order_id": "7"}),
            tool_call("lookup_order", {"order_id": "7"}),
        ),
        tools=[lookup_tool(counter, risk=READ_ONLY), cancel_order],
        cache=cache(),
    )

    await agent.run("x")

    assert counter.calls == ["7", "7"]  # read again after the cancel, then reused


async def test_a_new_data_state_token_is_a_miss() -> None:
    counter, shared = Counter(), cache(tools_ttl=600)
    agent = Agent(
        "clerk",
        model=FakeChatModel([tool_call("lookup_order", {"order_id": "7"}), "done"] * 3),
        tools=[lookup_tool(counter, risk=READ_ONLY)],
        cache=shared,
    )

    await agent.run("a", metadata={"awa.data_state": "v1"})
    await agent.run("b", metadata={"awa.data_state": "v1"})
    await agent.run("c", metadata={"awa.data_state": "v2"})

    assert counter.calls == ["7", "7"]


async def test_a_changed_file_is_read_again(tmp_path: Path) -> None:
    (tmp_path / "invoice.txt").write_text("total 100", encoding="utf-8")
    events = Events()
    read_file = next(t for t in file_toolkit(tmp_path, read_only=True) if t.name == "read_file")
    agent = Agent(
        "clerk",
        model=FakeChatModel([tool_call("read_file", {"path": "invoice.txt"}), "done"] * 3),
        tools=[read_file],
        cache=cache(tools_ttl=600),
        hooks=[events],
    )

    await agent.run("a")
    await agent.run("b")
    (tmp_path / "invoice.txt").write_text("total 999", encoding="utf-8")
    await agent.run("c")

    assert [e.cache for e in events.results] == ["miss", "hit", "miss"]
    assert "999" in events.results[2].content and "100" in events.results[1].content


async def test_the_data_state_is_read_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    import awa.tools.result_cache as result_cache

    threads: list[int] = []
    data_state = result_cache._data_state

    def recorded(root: str, arguments: Any) -> str | None:
        threads.append(threading.get_ident())
        return data_state(root, arguments)

    monkeypatch.setattr(result_cache, "_data_state", recorded)
    (tmp_path / "invoice.txt").write_text("total 100", encoding="utf-8")
    read_file = next(t for t in file_toolkit(tmp_path, read_only=True) if t.name == "read_file")
    model = FakeChatModel([tool_call("read_file", {"path": "invoice.txt"}), "done"])
    await Agent("clerk", model=model, tools=[read_file], cache=cache()).run("a")

    assert threads and threading.get_ident() not in threads


async def test_two_agents_of_one_run_share_a_parsed_document(tmp_path: Path) -> None:
    (tmp_path / "invoice.txt").write_text("total 100", encoding="utf-8")
    events = Events()
    shared = cache()
    read_file = next(t for t in file_toolkit(tmp_path, read_only=True) if t.name == "read_file")

    def reader(name: str) -> Agent:
        return Agent(
            name,
            model=script(tool_call("read_file", {"path": "invoice.txt"})),
            tools=[read_file],
            cache=shared,
            hooks=[events],
        )

    flow = Sequential("intake", [reader("extractor"), reader("checker")])

    await flow.run("Check invoice.txt")

    assert [e.cache for e in events.results] == ["miss", "hit"]
    assert [e.agent for e in events.results] == ["extractor", "checker"]


async def test_errors_are_never_cached() -> None:
    calls: list[int] = []

    @tool(risk=READ_ONLY)
    def flaky(order_id: str) -> str:
        """Look up an order."""
        calls.append(1)
        raise ValueError("backend down")

    agent = Agent(
        "clerk",
        model=script(tool_call("flaky", {"order_id": "7"}), tool_call("flaky", {"order_id": "7"})),
        tools=[flaky],
        cache=cache(),
    )

    await agent.run("x")

    assert len(calls) == 2


async def test_the_tool_cache_switch_and_a_missing_store_turn_it_off() -> None:
    for off in (cache(tools=False), cache(enabled=False), Cache(CacheSettings(), None)):
        counter = Counter()
        agent = Agent(
            "clerk",
            model=script(
                tool_call("lookup_order", {"order_id": "7"}),
                tool_call("lookup_order", {"order_id": "7"}),
            ),
            tools=[lookup_tool(counter, risk=READ_ONLY)],
            cache=off,
        )
        await agent.run("x")
        assert counter.calls == ["7", "7"]


async def test_a_broken_store_never_breaks_the_run(caplog: pytest.LogCaptureFixture) -> None:
    class Broken(InMemoryCacheStore):
        def get(self, *args: Any, **kwargs: Any) -> Any:
            raise CacheStoreError("disk full")

        def put(self, *args: Any, **kwargs: Any) -> None:
            raise CacheStoreError("disk full")

    counter = Counter()
    agent = Agent(
        "clerk",
        model=script(
            tool_call("lookup_order", {"order_id": "7"}),
            tool_call("lookup_order", {"order_id": "7"}),
        ),
        tools=[lookup_tool(counter, risk=READ_ONLY)],
        cache=Cache(CacheSettings(), Broken()),
    )

    with caplog.at_level(logging.WARNING, logger="awa.cache"):
        result = await agent.run("x")

    assert result.output == "done" and counter.calls == ["7", "7"]
    assert sum("cache store failed" in r.message for r in caplog.records) == 1


def test_the_fingerprint_follows_code_and_settings_but_never_holds_a_secret() -> None:
    def make(api_key: str, region: str) -> Any:
        @tool(risk=READ_ONLY)
        def lookup(order_id: str) -> str:
            """Look up an order."""
            return f"{region}:{api_key[:0]}{order_id}"

        return lookup

    base = tool_fingerprint(make("sk-live-1", "eu"))

    assert tool_fingerprint(make("sk-live-1", "eu")) == base
    assert tool_fingerprint(make("sk-live-2", "eu")) != base  # another account: a keyed digest
    assert tool_fingerprint(make("sk-live-1", "us")) != base  # a setting is
    assert "sk-live" not in base


async def test_keys_and_logs_hold_no_secret(caplog: pytest.LogCaptureFixture) -> None:
    secret = "sk-very-secret-value"
    store = InMemoryCacheStore()

    @tool(risk=READ_ONLY)
    def lookup(order_id: str, api_token: str = secret) -> str:
        """Look up an order."""
        return "shipped"

    agent = Agent(
        "clerk",
        model=script(
            tool_call("lookup", {"order_id": "7"}), tool_call("lookup", {"order_id": "7"})
        ),
        tools=[lookup],
        cache=Cache(CacheSettings(), store),
    )

    with caplog.at_level(logging.DEBUG):
        await agent.run("x")

    [(key, value)] = store.entries(Cache(CacheSettings(), store).scope("tools", None))
    assert secret not in key and secret.encode() not in value
    assert secret not in caplog.text


async def test_hits_misses_and_the_prefix_hash_are_span_attributes() -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from awa.observability import OpenTelemetrySubscriber

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    agent = Agent(
        "clerk",
        model=script(
            tool_call("lookup_order", {"order_id": "7"}),
            tool_call("lookup_order", {"order_id": "7"}),
        ),
        tools=[lookup_tool(Counter(), risk=READ_ONLY)],
        cache=cache(),
        hooks=[OpenTelemetrySubscriber(provider.get_tracer("test"))],
    )

    await agent.run("x")
    provider.shutdown()

    spans = exporter.get_finished_spans()
    tools = sorted(
        (s for s in spans if s.name == "execute_tool lookup_order"), key=lambda s: s.start_time or 0
    )
    assert [dict(s.attributes or {})["awa.cache.hit"] for s in tools] == [False, True]
    chats = [dict(s.attributes or {}) for s in spans if s.name.startswith("chat")]
    assert len({c["awa.prompt.prefix_hash"] for c in chats}) == 1
    assert all(c["awa.model.duration_ms"] >= 0 for c in chats)


def test_two_sql_toolkits_for_different_databases_never_share_a_fingerprint(tmp_path: Path) -> None:
    import sqlite3

    from awa.toolkits import sql_toolkit

    def query_tool(name: str) -> Any:
        path = str(tmp_path / name)
        sqlite3.connect(path).close()
        tools = sql_toolkit(connect=lambda: sqlite3.connect(path), dialect="sqlite")
        return next(t for t in tools if t.name == "sql_query")

    first, second = query_tool("a.db"), query_tool("b.db")
    assert tool_fingerprint(first) != tool_fingerprint(second)
    assert str(tmp_path) not in tool_fingerprint(first)


async def test_an_action_in_one_agent_sets_results_of_another_agent_aside() -> None:
    counter = Counter()

    @tool(risk={"external_action": True})
    def cancel_order(order_id: str) -> str:
        """Cancel an order."""
        return "cancelled"

    shared = cache()
    reader = Agent(
        "reader",
        model=script(tool_call("lookup_order", {"order_id": "7"})),
        tools=[lookup_tool(counter, risk=READ_ONLY)],
        cache=shared,
    )
    actor = Agent(  # its own cache middleware counts the action for the whole run tree
        "actor",
        model=script(tool_call("cancel_order", {"order_id": "7"})),
        tools=[cancel_order, lookup_tool(Counter(), risk=READ_ONLY, name="lookup_other")],
        cache=shared,
    )
    again = Agent(
        "reader2",
        model=script(tool_call("lookup_order", {"order_id": "7"})),
        tools=[lookup_tool(counter, risk=READ_ONLY)],
        cache=shared,
    )

    await Sequential("flow", [reader, actor, again]).run("x")

    assert counter.calls == ["7", "7"]  # the cancel of the second agent counts for the third


def test_evicting_old_scopes_never_brings_back_an_old_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awa.tools import result_cache

    monkeypatch.setattr(result_cache, "_MAX_RUNS", 3)
    actions = result_cache._Actions()
    actions.acted("run:a")
    before = actions.epoch("run:a")
    for name in "bcde":
        actions.acted(f"run:{name}")
    assert actions.epoch("run:a") >= before and actions.epoch("run:never") >= before
    assert actions.epoch("run:e") > before


async def test_the_principal_is_the_frozen_actor_not_later_metadata() -> None:
    counter, shared = Counter(), cache(tools_ttl=600)

    @tool(risk=READ_ONLY)
    def lookup_order(order_id: str) -> str:
        """Look up an order."""
        from awa.runtime.context import current_run_context

        ctx = current_run_context()
        assert ctx is not None
        ctx.metadata["awa.actor"] = "mallory"  # a tool cannot change whose result it is
        counter.calls.append(order_id)
        return "shipped"

    agent = Agent(
        "clerk",
        model=FakeChatModel([tool_call("lookup_order", {"order_id": "7"}), "done"] * 2),
        tools=[lookup_order],
        cache=shared,
    )
    for actor in ("alice", "bob"):
        await agent.run("my order", metadata={"awa.tenant": "acme", "awa.actor": actor})
    assert counter.calls == ["7", "7"]


async def test_a_listing_with_its_default_folder_sees_a_new_file(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    events = Events()
    tools = {t.name: t for t in file_toolkit(tmp_path, read_only=True)}
    listing = next(name for name in ("list_directory", "list_files", "ls") if name in tools)
    agent = Agent(
        "clerk",
        model=FakeChatModel([tool_call(listing, {}), "done"] * 2),
        tools=[tools[listing]],
        cache=cache(tools_ttl=600),
        hooks=[events],
    )
    await agent.run("a")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    await agent.run("b")
    assert [e.cache for e in events.results] == ["miss", "miss"]
    assert "b.txt" in events.results[1].content


@pytest.fixture
def fresh_actions(monkeypatch: pytest.MonkeyPatch) -> Any:
    from awa.tools import result_cache

    actions = result_cache._Actions()
    monkeypatch.setattr(result_cache, "_ACTIONS", actions)
    return actions


async def test_an_acting_tool_that_raises_still_sets_results_aside(fresh_actions: Any) -> None:
    from types import SimpleNamespace

    from awa.runtime.context import RunContext, use_run_context
    from awa.tools.result_cache import ToolResultCacheMiddleware

    @tool(risk={"external_action": True})
    def cancel_order(order_id: str) -> str:
        """Cancel an order."""
        return "cancelled"

    middleware = ToolResultCacheMiddleware(cache(), [cancel_order])
    ctx: RunContext[Any] = RunContext(run_id="r1", agent="clerk", deps=None)
    request = SimpleNamespace(
        tool_call={"name": "cancel_order", "id": "c1", "args": {}}, tool=cancel_order
    )

    async def failing(_: Any) -> Any:
        raise RuntimeError("payment service down")

    before = fresh_actions.epoch(f"run:{ctx.root_run_id}")
    with use_run_context(ctx), pytest.raises(RuntimeError):
        await middleware.awrap_tool_call(request, failing)  # type: ignore[arg-type]
    assert fresh_actions.epoch(f"run:{ctx.root_run_id}") > before


def test_a_tool_with_injected_context_is_not_cached() -> None:
    from awa.runtime.context import RunContext

    @tool(risk=READ_ONLY)
    def lookup_mine(ctx: RunContext[Any], order_id: str) -> str:
        """Look up an order of the current user."""
        return order_id

    assert not is_cacheable(lookup_mine)
    assert is_cacheable(lookup_tool(Counter(), risk=READ_ONLY))


def test_run_sync_caches_through_the_async_hook_and_a_sync_graph_is_refused() -> None:
    from langchain.agents import create_agent

    from awa.tools.result_cache import ToolResultCacheMiddleware

    counter = Counter()
    lookup = lookup_tool(counter, risk=READ_ONLY)
    calls = (
        tool_call("lookup_order", {"order_id": "7"}),
        tool_call("lookup_order", {"order_id": "7"}),
    )
    agent = Agent("clerk", model=script(*calls), tools=[lookup], cache=cache())
    assert agent.run_sync("x").output == "done"
    assert counter.calls == ["7"]
    graph = create_agent(  # refused at its first tool call
        FakeChatModel([calls[0]]),
        tools=[lookup],
        middleware=[ToolResultCacheMiddleware(cache(), [lookup])],
    )
    with pytest.raises(NotImplementedError):
        graph.invoke({"messages": [("user", "x")]})


def test_a_new_empty_folder_changes_the_listing_token(tmp_path: Path) -> None:
    from awa.tools.result_cache import _data_state

    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    before = _data_state(str(tmp_path), {})
    (tmp_path / "empty").mkdir()
    assert _data_state(str(tmp_path), {}) != before


def test_an_unreadable_folder_makes_the_call_uncacheable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.tools import result_cache

    (tmp_path / "locked").mkdir()

    def denied(path: Any, *, onerror: Any = None, **_: Any) -> Any:
        onerror(PermissionError("denied"))
        return iter(())

    monkeypatch.setattr(result_cache.os, "walk", denied)
    assert result_cache._data_state(str(tmp_path), {}) is None


def test_a_non_serializable_result_is_not_cached() -> None:
    from awa._internal.upstream.messages import ToolMessage
    from awa.tools.result_cache import _store

    message = ToolMessage(
        content=[{"type": "text", "text": "x", "extra": {1, 2}}], tool_call_id="c1"
    )
    assert _store(message) is None


def test_a_corrupt_entry_is_ignored() -> None:
    from awa.tools.result_cache import _restore

    call = {"name": "lookup_order", "id": "c1"}
    assert _restore(b"not json", call) is None
    assert _restore(b'{"other": 1}', call) is None
    assert _restore(b"\xff\xfe", call) is None


async def test_artifact_results_are_not_cached() -> None:
    counter = Counter()

    @tool(risk=READ_ONLY, response_format="content_and_artifact")
    def lookup_order(order_id: str) -> tuple[str, dict[str, str]]:
        """Look up an order."""
        counter.calls.append(order_id)
        return "shipped", {"id": order_id}

    agent = Agent(
        "clerk",
        model=script(
            tool_call("lookup_order", {"order_id": "7"}),
            tool_call("lookup_order", {"order_id": "7"}),
        ),
        tools=[lookup_order],
        cache=cache(),
    )
    await agent.run("x")
    assert counter.calls == ["7", "7"]
