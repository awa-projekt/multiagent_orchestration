"""Progress records of paused runs, kept in the run's checkpointer (`RecordStore`)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict

from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa._internal.upstream.graph import (
    END,
    START,
    CompiledStateGraph,
    GraphInterrupt,
    Interrupt,
    StateGraph,
)
from awa._internal.upstream.runnables import RunnableConfig


class _PauseState(TypedDict, total=False):
    record: dict[str, Any] | None


class RecordStore:
    """Saves and loads small progress records of paused runs on their own threads.

    A record that lists `interrupts` shows them as pending on its thread.
    """

    def __init__(self, name: str, checkpointer: BaseCheckpointSaver[Any] | None) -> None:
        self._name = name
        self._checkpointer = checkpointer
        self._graph: CompiledStateGraph[Any, Any, Any, Any] | None = None

    def _compiled(self) -> CompiledStateGraph[Any, Any, Any, Any]:
        if self._graph is None:
            builder: StateGraph[Any, Any, Any, Any] = StateGraph(_PauseState)
            builder.add_node("pause", _pause_node)
            builder.add_edge(START, "pause")
            builder.add_edge("pause", END)
            self._graph = builder.compile(checkpointer=self._checkpointer, name=self._name)
        return self._graph

    async def load(self, thread: str) -> dict[str, Any] | None:
        """The progress record saved on `thread`, or `None`."""

        config: RunnableConfig = {"configurable": {"thread_id": thread}}
        snapshot = await self._compiled().aget_state(config)
        record = snapshot.values.get("record") if snapshot.values else None
        return dict(record) if isinstance(record, Mapping) else None

    async def save(self, thread: str, record: dict[str, Any]) -> None:
        """Write a run's progress record (and its pending interrupts) to its thread."""

        config: RunnableConfig = {"configurable": {"thread_id": thread}, "run_name": self._name}
        await self._compiled().ainvoke({"record": record}, config=config)


def _pause_node(state: _PauseState) -> dict[str, Any]:
    """Raise the pending interrupts of a saved record, so its thread shows them."""

    pending = (state.get("record") or {}).get("interrupts") or []
    if pending:
        raise GraphInterrupt(
            tuple(Interrupt(value=item.get("value"), id=str(item["id"])) for item in pending)
        )
    return {}


__all__ = ["RecordStore"]
