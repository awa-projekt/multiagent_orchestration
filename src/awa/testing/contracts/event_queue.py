"""The contract of an `EventQueue`: at-least-once delivery with visibility timeouts."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import timedelta

import pytest

from awa.automation.queues import EventQueue, QueueMessage
from awa.testing.contracts._base import AdapterContract, Place
from awa.testing.doubles import FakeClock

HIDDEN = timedelta(hours=1)
NOW = timedelta(0)


@dataclass(frozen=True)
class EventQueueCapabilities:
    """What a queue promises beyond the base contract.

    Attributes:
        survives_restart: A second queue of the same name on the same `Place` sees the messages.
        controllable_clock: The queue measures time with the `clock` that `opened` passes.
        lists_dead_letters: `dead_letters` returns the messages that failed `max_attempts` times.
        normalizes_payload: A payload comes back as JSON (a tuple as a list, a datetime as text).
    """

    survives_restart: bool = False
    controllable_clock: bool = False
    lists_dead_letters: bool = False
    normalizes_payload: bool = False


class EventQueueContract(AdapterContract):
    """Run this against an `EventQueue`: implement `opened` (and `dead_letters`)."""

    capabilities = EventQueueCapabilities()

    def opened(
        self,
        place: Place,
        *,
        name: str = "q",
        max_attempts: int = 5,
        clock: FakeClock | None = None,
    ) -> AbstractAsyncContextManager[EventQueue]:
        """An async context manager yielding a queue `name` on `place` (`ValueError` for `max_attempts` < 1)."""

        raise NotImplementedError

    async def dead_letters(self, queue: EventQueue) -> list[QueueMessage]:
        """The messages `queue` gave up on (only called when `lists_dead_letters` is set)."""

        raise NotImplementedError

    async def test_a_sent_message_is_received_with_its_payload_and_key(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with self.opened(place, clock=clock) as queue:
            sent = await queue.send({"id": 7, "text": "Müller", "tags": [1, None]}, dedup_key="k")
            [message] = await queue.receive()

            assert message.id == sent and message.attempts == 1 and message.dedup_key == "k"
            assert message.payload == {"id": 7, "text": "Müller", "tags": [1, None]}

    async def test_a_received_payload_is_a_copy(self, place: Place, clock: FakeClock) -> None:
        async with self.opened(place, clock=clock) as queue:
            await queue.send({"items": [1]})
            [first] = await queue.receive(visibility=NOW)
            first.payload["items"].append(2)
            [again] = await queue.receive(visibility=NOW)

            assert again.payload == {"items": [1]}

    async def test_a_payload_is_stored_as_json(self, place: Place, clock: FakeClock) -> None:
        if not self.capabilities.normalizes_payload:
            pytest.skip("keeps the payload object as given: a tuple stays a tuple")
        async with self.opened(place, clock=clock) as queue:
            await queue.send({"pair": (1, 2), "at": clock.now()})
            [message] = await queue.receive()

            assert message.payload["pair"] == [1, 2]
            assert isinstance(message.payload["at"], str)

    async def test_a_negative_limit_receives_nothing_and_a_negative_time_is_refused(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with self.opened(place, clock=clock) as queue:
            await queue.send("work")

            assert await queue.receive(limit=-1) == []
            with pytest.raises(ValueError):
                await queue.send("later", delay=-1)
            with pytest.raises(ValueError):
                await queue.receive(visibility=-1)

    async def test_messages_arrive_in_the_order_they_were_sent_up_to_the_limit(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with self.opened(place, clock=clock) as queue:
            sent = [await queue.send(n) for n in range(5)]

            assert [m.id for m in await queue.receive(limit=3)] == sent[:3]
            assert [m.id for m in await queue.receive(limit=10)] == sent[3:]
            assert await queue.receive(limit=0) == []

    async def test_a_received_message_is_invisible_until_its_visibility_ends(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with self.opened(place, clock=clock) as queue:
            await queue.send("work")

            assert len(await queue.receive(visibility=HIDDEN)) == 1
            assert await queue.receive() == []

    async def test_a_message_whose_visibility_ended_is_offered_again_and_counts_the_attempt(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with self.opened(place, clock=clock) as queue:
            sent = await queue.send("work")
            [first] = await queue.receive(visibility=NOW)
            [second] = await queue.receive(visibility="30s")

            assert (first.id, first.attempts) == (sent, 1)
            assert (second.id, second.attempts) == (sent, 2)

    async def test_ack_removes_the_message_and_an_unknown_ack_is_ignored(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with self.opened(place, clock=clock) as queue:
            await queue.send("work")
            [message] = await queue.receive(visibility=NOW)
            await queue.ack(message.id)
            await queue.ack(message.id)
            await queue.ack("msg_unknown")

            assert await queue.receive(visibility=NOW) == []

    async def test_nack_offers_the_message_again_at_once_and_an_unknown_nack_is_ignored(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with self.opened(place, clock=clock) as queue:
            await queue.send("work")
            [message] = await queue.receive(visibility=HIDDEN)
            await queue.nack(message.id)
            await queue.nack("msg_unknown")
            [again] = await queue.receive(visibility=HIDDEN)

            assert again.id == message.id and again.attempts == 2

    async def test_nack_with_a_delay_keeps_the_message_hidden_for_that_time(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with self.opened(place, clock=clock) as queue:
            await queue.send("work")
            [message] = await queue.receive(visibility=HIDDEN)
            await queue.nack(message.id, delay="10m")

            assert await queue.receive() == []
            if self.capabilities.controllable_clock:
                clock.advance(minutes=10)
                assert [m.id for m in await queue.receive()] == [message.id]

    async def test_a_delayed_message_is_hidden_until_its_delay_has_passed(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with self.opened(place, clock=clock) as queue:
            sent = await queue.send("later", delay="10m")

            assert await queue.receive() == []
            if self.capabilities.controllable_clock:
                clock.advance(minutes=10)
                assert [m.id for m in await queue.receive()] == [sent]

    async def test_the_queue_does_not_deduplicate_the_automation_does(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with self.opened(place, clock=clock) as queue:
            await queue.send("a", dedup_key="same")
            await queue.send("b", dedup_key="same")

            assert [m.payload for m in await queue.receive()] == ["a", "b"]

    async def test_concurrent_receivers_never_get_the_same_message(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with self.opened(place, clock=clock) as queue:
            for n in range(5):
                await queue.send(n)
            batches = await asyncio.gather(
                *[queue.receive(limit=10, visibility=HIDDEN) for _ in range(4)]
            )
            ids = [m.id for batch in batches for m in batch]

            assert len(ids) == 5 and len(set(ids)) == 5

    async def test_queues_with_different_names_do_not_share_messages(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with (
            self.opened(place, name="a", clock=clock) as first,
            self.opened(place, name="b", clock=clock) as second,
        ):
            await first.send("for a")

            assert await second.receive() == []
            assert [m.payload for m in await first.receive()] == ["for a"]

    async def test_a_message_delivered_max_attempts_times_without_an_ack_is_dead_lettered(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with self.opened(place, max_attempts=2, clock=clock) as queue:
            sent = await queue.send("poison")
            assert len(await queue.receive(visibility=NOW)) == 1
            assert len(await queue.receive(visibility=NOW)) == 1

            assert await queue.receive(visibility=NOW) == []
            if self.capabilities.lists_dead_letters:
                assert [m.id for m in await self.dead_letters(queue)] == [sent]

    async def test_a_message_that_is_nacked_after_max_attempts_is_dead_lettered(
        self, place: Place, clock: FakeClock
    ) -> None:
        async with self.opened(place, max_attempts=1, clock=clock) as queue:
            sent = await queue.send("poison")
            [message] = await queue.receive(visibility=HIDDEN)
            await queue.nack(message.id)

            if self.capabilities.lists_dead_letters:  # at the nack, not at the next receive
                assert [m.id for m in await self.dead_letters(queue)] == [sent]
            assert await queue.receive(visibility=NOW) == []

    async def test_max_attempts_below_one_is_refused(self, place: Place) -> None:
        with pytest.raises(ValueError):
            async with self.opened(place, max_attempts=0):
                pass

    async def test_messages_survive_a_restart(self, place: Place, clock: FakeClock) -> None:
        if not self.capabilities.survives_restart:
            pytest.skip("a single-process queue: a restart starts empty")
        async with self.opened(place, clock=clock) as first:
            sent = await first.send("kept")
        async with self.opened(place, clock=clock) as second:
            assert [m.id for m in await second.receive()] == [sent]

    async def test_a_single_process_queue_starts_empty_after_a_restart(
        self, place: Place, clock: FakeClock
    ) -> None:
        if self.capabilities.survives_restart:
            pytest.skip("a durable queue keeps its messages")
        async with self.opened(place, clock=clock) as first:
            await first.send("lost")
        async with self.opened(place, clock=clock) as second:
            assert await second.receive() == []


__all__ = ["EventQueueCapabilities", "EventQueueContract"]
