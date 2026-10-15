"""The contract of a `Notifier`: every process event handed to it reaches its receiver, or it raises.

`opened` yields a `NotifierUnderTest`; with `accept=False` the receiver refuses every delivery.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

import pytest

from awa.automation.models import ProcessEvent
from awa.automation.notify import Notifier
from awa.errors import AwaError
from awa.testing.contracts._base import AdapterContract, Place


@dataclass(frozen=True)
class NotifierUnderTest:
    """A notifier and its receiver.

    Attributes:
        received: Returns what the receiver got so far, oldest first, as
            `ProcessEvent.model_dump(mode="json")` objects.
    """

    notify: Notifier
    received: Callable[[], list[dict[str, Any]]]


async def deliver(notify: Notifier, event: ProcessEvent) -> None:
    """Call `notify` the way an automation does: await the result when it is awaitable."""

    result: Any = notify(event)
    if inspect.isawaitable(result):
        result = await result
    assert result is None, "a notifier returns None"


def _key(event: dict[str, Any]) -> tuple[Any, ...]:
    return (event.get("type"), event.get("automation"), event.get("run_id"))


class NotifierContract(AdapterContract):
    """Run this against a `Notifier`: implement `opened`."""

    def opened(
        self, place: Place, *, accept: bool = True
    ) -> AbstractAsyncContextManager[NotifierUnderTest]:
        """An async context manager yielding the notifier and its receiver on `place`."""

        raise NotImplementedError

    async def test_an_event_reaches_the_receiver(self, place: Place) -> None:
        event = ProcessEvent(type="process.dead_lettered", automation="ap-desk", run_id="run-1")
        async with self.opened(place) as target:
            await deliver(target.notify, event)

            assert [_key(e) for e in target.received()] == [
                ("process.dead_lettered", "ap-desk", "run-1")
            ]

    async def test_events_arrive_once_and_in_order(self, place: Place) -> None:
        events = [
            ProcessEvent(
                type="process.escalated", automation="ap-desk", run_id=f"run-{n}", attempt=n
            )
            for n in range(3)
        ]
        async with self.opened(place) as target:
            for event in events:
                await deliver(target.notify, event)

            received = target.received()
            assert [_key(e) for e in received] == [_key(e.model_dump(mode="json")) for e in events]
            assert [e.get("attempt") for e in received] == [0, 1, 2]

    async def test_an_event_without_a_run_is_delivered(self, place: Place) -> None:
        event = ProcessEvent(
            type="trigger.failed", automation="ap-desk", data={"error": "mailbox offline"}
        )
        async with self.opened(place) as target:
            await deliver(target.notify, event)

            [got] = target.received()
            assert _key(got) == ("trigger.failed", "ap-desk", None)
            assert got.get("data") == {"error": "mailbox offline"}

    async def test_a_refused_delivery_raises_an_awa_error(self, place: Place) -> None:
        event = ProcessEvent(type="process.dead_lettered", automation="ap-desk", run_id="run-1")
        async with self.opened(place, accept=False) as target:
            with pytest.raises(AwaError):
                await deliver(target.notify, event)


__all__ = ["NotifierContract", "NotifierUnderTest", "deliver"]
