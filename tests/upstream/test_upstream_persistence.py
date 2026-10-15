"""Contracts of checkpointers and stores awa relies on."""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.base import BaseStore, SearchItem
from langgraph.store.memory import InMemoryStore

from awa._internal.upstream import checkpoint as compat_checkpoint


def broken(awa_module: str, what: str) -> str:
    return f"Upstream contract broken: {what}. awa depends on it in {awa_module}."


class _State(TypedDict):
    count: int


def _graph(saver: InMemorySaver) -> Any:
    builder = StateGraph(_State)
    builder.add_node("inc", lambda state: {"count": state["count"] + 1})
    builder.add_edge(START, "inc")
    builder.add_edge("inc", END)
    return builder.compile(checkpointer=saver)


async def test_in_memory_saver_supports_the_async_api_awa_calls() -> None:
    assert compat_checkpoint.InMemorySaver is InMemorySaver
    saver = InMemorySaver()
    graph = _graph(saver)
    config = {"configurable": {"thread_id": "t1"}, "metadata": {"awa_subject": "s-1"}}
    await graph.ainvoke({"count": 0}, config)

    found = await saver.aget_tuple({"configurable": {"thread_id": "t1"}})
    assert found is not None and found.checkpoint["channel_values"]["count"] == 1, broken(
        "awa.agents.agent, awa.persistence.erasure",
        "aget_tuple(config) returns the latest checkpoint",
    )
    listed = [c async for c in saver.alist(None, filter={"awa_subject": "s-1"})]
    assert listed and all(c.config["configurable"]["thread_id"] == "t1" for c in listed), broken(
        "awa.persistence.erasure", "alist(None, filter=metadata) finds checkpoints by run metadata"
    )
    await saver.adelete_thread("t1")
    assert await saver.aget_tuple({"configurable": {"thread_id": "t1"}}) is None, broken(
        "awa.persistence (erasure, retention)",
        "adelete_thread removes every checkpoint of a thread",
    )


async def test_state_history_and_update_state() -> None:
    graph = _graph(InMemorySaver())
    config = {"configurable": {"thread_id": "t2"}}
    await graph.ainvoke({"count": 0}, config)
    history = [snapshot async for snapshot in graph.aget_state_history(config, limit=10)]
    assert len(history) >= 2 and history[0].values["count"] == 1, broken(
        "docs/develop/memory.md (history and forks)",
        "aget_state_history yields snapshots, newest first",
    )
    updated = await graph.aupdate_state(history[-1].config, {"count": 41}, as_node="inc")
    assert (await graph.aget_state(updated)).values["count"] == 41, broken(
        "docs/develop/memory.md (history and forks)",
        "aupdate_state(config, values, as_node=) forks a checkpoint",
    )


async def test_in_memory_store_supports_the_async_api_awa_calls() -> None:
    store = InMemoryStore()
    assert isinstance(store, BaseStore)
    await store.aput(("awa", "memory", "u1"), "k1", {"text": "likes tea"})
    await store.aput(("awa", "memory", "u1"), "k2", {"text": "likes coffee"})
    await store.aput(("awa", "memory", "u2"), "k1", {"text": "other"})

    item = await store.aget(("awa", "memory", "u1"), "k1")
    assert item is not None and item.value == {"text": "likes tea"}
    page = await store.asearch(("awa", "memory", "u1"), limit=1, offset=1)
    assert len(page) == 1 and isinstance(page[0], SearchItem), broken(
        "awa.persistence.namespaces, awa.memory.long_term",
        "asearch(prefix, limit=, offset=) pages SearchItems",
    )
    everything = await store.asearch(("awa", "memory"), limit=10)
    assert {tuple(i.namespace) for i in everything} == {
        ("awa", "memory", "u1"),
        ("awa", "memory", "u2"),
    }, broken("awa.persistence.erasure", "asearch matches every namespace under a prefix")
    namespaces = await store.alist_namespaces(prefix=("awa",), limit=10, offset=0)
    assert ("awa", "memory", "u1") in namespaces, broken(
        "awa.persistence.retention", "alist_namespaces(prefix=, limit=, offset=)"
    )
    await store.adelete(("awa", "memory", "u1"), "k1")
    assert await store.aget(("awa", "memory", "u1"), "k1") is None, broken(
        "awa.memory, awa.persistence.erasure", "adelete(namespace, key)"
    )
