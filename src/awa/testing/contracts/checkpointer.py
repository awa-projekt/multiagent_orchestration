"""The contract of a checkpointer: what awa relies on from every LangGraph `BaseCheckpointSaver`."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypedDict

import pytest

from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa._internal.upstream.graph import END, START, Command, StateGraph, interrupt
from awa.testing.contracts._base import AdapterContract, Place


@dataclass(frozen=True)
class CheckpointerCapabilities:
    """What a saver promises beyond the base contract (`survives_restart`: a reopened saver sees the threads)."""

    survives_restart: bool = False


class _State(TypedDict, total=False):
    text: str
    answer: str


class CheckpointerContract(AdapterContract):
    """Run this against a checkpointer: implement `opened` and set `capabilities`."""

    capabilities = CheckpointerCapabilities()

    def opened(self, place: Place) -> AbstractAsyncContextManager[BaseCheckpointSaver[Any]]:
        """An async context manager yielding a saver on `place`; opening twice is a restart."""

        raise NotImplementedError

    @staticmethod
    def config(place: Place, thread: str = "t", checkpoint_id: str | None = None) -> Any:
        """The config of `thread` in this test's own thread space (savers may share a database)."""

        configurable = {"thread_id": f"{place.name}-{thread}", "checkpoint_ns": ""}
        if checkpoint_id is not None:
            configurable["checkpoint_id"] = checkpoint_id
        return {"configurable": configurable}

    async def step(
        self, saver: BaseCheckpointSaver[Any], place: Place, thread: str, number: int
    ) -> Any:
        """Save checkpoint `number` of `thread` on top of the thread's latest one."""

        checkpoint: Any = {
            "v": 4,
            "id": f"{number:06}",
            "ts": datetime.now(UTC).isoformat(),
            "channel_values": {"a": number},
            "channel_versions": {"a": f"{number:06}"},
            "versions_seen": {},
            "updated_channels": None,
        }
        latest = await saver.aget_tuple(self.config(place, thread))
        parent = latest.config["configurable"]["checkpoint_id"] if latest else None
        return await saver.aput(
            self.config(place, thread, parent),
            checkpoint,
            {"source": "loop" if number > 1 else "input", "step": number},
            {"a": f"{number:06}"},
        )

    async def test_a_stored_checkpoint_reads_back(self, place: Place) -> None:
        async with self.opened(place) as saver:
            saved = await self.step(saver, place, "t", 1)

            found = await saver.aget_tuple(self.config(place, "t", "000001"))

            assert saved["configurable"]["checkpoint_id"] == "000001"
            assert found is not None and found.checkpoint["id"] == "000001"
            assert found.checkpoint["channel_values"] == {"a": 1}
            assert found.metadata["step"] == 1 and found.metadata["source"] == "input"
            assert found.parent_config is None
            assert await saver.aget_tuple(self.config(place, "t", "999999")) is None
            assert await saver.aget_tuple(self.config(place, "other")) is None

    async def test_without_an_id_the_latest_checkpoint_is_returned(self, place: Place) -> None:
        async with self.opened(place) as saver:
            for number in (1, 2, 3):
                await self.step(saver, place, "t", number)

            latest = await saver.aget_tuple(self.config(place, "t"))

            assert latest is not None and latest.checkpoint["id"] == "000003"
            assert latest.parent_config is not None
            assert latest.parent_config["configurable"]["checkpoint_id"] == "000002"

    async def test_a_listing_is_newest_first_limited_and_filtered_by_metadata(
        self, place: Place
    ) -> None:
        async with self.opened(place) as saver:
            for number in (1, 2, 3, 4):
                await self.step(saver, place, "t", number)
            await self.step(saver, place, "other", 1)

            everything = [c async for c in saver.alist(self.config(place, "t"))]
            limited = [c async for c in saver.alist(self.config(place, "t"), limit=2)]
            before = [
                c
                async for c in saver.alist(
                    self.config(place, "t"), before=self.config(place, "t", "000003")
                )
            ]
            inputs = [
                c async for c in saver.alist(self.config(place, "t"), filter={"source": "input"})
            ]

            assert [c.checkpoint["id"] for c in everything] == [
                "000004",
                "000003",
                "000002",
                "000001",
            ]
            assert [c.checkpoint["id"] for c in limited] == ["000004", "000003"]
            assert [c.checkpoint["id"] for c in before] == ["000002", "000001"]
            assert [c.checkpoint["id"] for c in inputs] == ["000001"]

    async def test_the_pending_writes_of_a_task_come_back_in_order(self, place: Place) -> None:
        async with self.opened(place) as saver:
            saved = await self.step(saver, place, "t", 1)
            await saver.aput_writes(saved, [("a", 1), ("b", 2), ("c", 3)], "task-1")
            await saver.aput_writes(saved, [("a", 10)], "task-2")

            found = await saver.aget_tuple(saved)

            assert found is not None
            writes = found.pending_writes or []
            assert [w[1:] for w in writes if w[0] == "task-1"] == [("a", 1), ("b", 2), ("c", 3)]
            assert [w[1:] for w in writes if w[0] == "task-2"] == [("a", 10)]

    async def test_delete_thread_removes_that_thread_and_nothing_else(self, place: Place) -> None:
        async with self.opened(place) as saver:
            for number in (1, 2):
                saved = await self.step(saver, place, "gone", number)
                await saver.aput_writes(saved, [("a", number)], "task")
            await self.step(saver, place, "kept", 1)

            await saver.adelete_thread(f"{place.name}-gone")
            await saver.adelete_thread(f"{place.name}-missing")

            assert await saver.aget_tuple(self.config(place, "gone")) is None
            assert [c async for c in saver.alist(self.config(place, "gone"))] == []
            assert await saver.aget_tuple(self.config(place, "kept")) is not None

    async def test_a_graph_that_pauses_on_an_interrupt_resumes_with_a_command(
        self, place: Place
    ) -> None:
        def ask(state: _State) -> _State:
            return {"answer": interrupt("Send it?")}

        builder = StateGraph(_State)
        builder.add_node("ask", ask)
        builder.add_edge(START, "ask")
        builder.add_edge("ask", END)

        async with self.opened(place) as saver:
            graph = builder.compile(checkpointer=saver)
            config = self.config(place, "graph")

            paused = await graph.ainvoke({"text": "hello"}, config)
            state = await graph.aget_state(config)
            done = await graph.ainvoke(Command(resume="yes"), config)

            assert "__interrupt__" in paused and state.next == ("ask",)
            assert done["answer"] == "yes" and done["text"] == "hello"
            assert (await graph.aget_state(config)).next == ()

    async def test_threads_survive_a_restart(self, place: Place) -> None:
        if not self.capabilities.survives_restart:
            pytest.skip("a single-process saver: a restart starts empty")
        async with self.opened(place) as first:
            saved = await self.step(first, place, "t", 1)
            await first.aput_writes(saved, [("a", 1)], "task")
        async with self.opened(place) as second:
            found = await second.aget_tuple(self.config(place, "t"))

            assert found is not None and found.checkpoint["id"] == "000001"
            assert [w[1:] for w in found.pending_writes or []] == [("a", 1)]

    async def test_a_single_process_saver_starts_empty_after_a_restart(self, place: Place) -> None:
        if self.capabilities.survives_restart:
            pytest.skip("a durable saver keeps its threads")
        async with self.opened(place) as first:
            await self.step(first, place, "t", 1)
        async with self.opened(place) as second:
            assert await second.aget_tuple(self.config(place, "t")) is None


__all__ = ["CheckpointerCapabilities", "CheckpointerContract"]
