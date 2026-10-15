"""Work queues as triggers: the `EventQueue` protocol, an in-memory queue and `QueueTrigger`."""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from awa._internal.aio import LoopLocal
from awa._internal.ids import new_id
from awa._internal.logging import get_logger
from awa._internal.util import DurationLike, to_timedelta
from awa.automation._time import as_utc, require_timedelta, utcnow
from awa.automation.models import TriggerEvent
from awa.automation.templates import TemplateLike
from awa.automation.triggers import BaseTrigger, TriggerContext
from awa.runtime.clock import Clock, as_clock

_logger = get_logger("automation.queues")


class QueueMessage(BaseModel):
    """One message of an `EventQueue`.

    Attributes:
        dedup_key: Business key; the automation starts one run per key.
        available_at: When it can be received (next).
        attempts: How often it was received.
    """

    id: str = Field(default_factory=lambda: new_id("msg"))
    payload: Any = None
    dedup_key: str | None = None
    enqueued_at: datetime = Field(default_factory=utcnow)
    available_at: datetime = Field(default_factory=utcnow)
    attempts: int = 0


@runtime_checkable
class EventQueue(Protocol):
    """A work queue with visibility timeouts (at-least-once delivery).

    A received message is invisible to others until deleted or its timeout passes; after
    `max_attempts` deliveries it is dead-lettered. Payloads are JSON-compatible and come back as a
    copy, possibly in JSON form (a tuple as a list).
    """

    async def send(
        self, payload: Any, *, dedup_key: str | None = None, delay: DurationLike | None = None
    ) -> str:
        """Enqueue `payload`; return the message id."""
        ...

    async def receive(
        self, *, limit: int = 10, visibility: DurationLike = timedelta(minutes=5)
    ) -> builtins.list[QueueMessage]:
        """Take up to `limit` available messages, invisible to others for `visibility`."""
        ...

    async def ack(self, message_id: str) -> None:
        """Delete a received message (it was handled)."""
        ...

    async def nack(self, message_id: str, *, delay: DurationLike | None = None) -> None:
        """Offer a received message again (after `delay`), or dead-letter it after `max_attempts`."""
        ...


class InMemoryEventQueue:
    """An `EventQueue` in process memory; `dead_letters` holds the failed messages."""

    def __init__(self, *, max_attempts: int = 5, clock: Clock | None = None) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.max_attempts = max_attempts
        self._clock = as_clock(clock)
        self._messages: dict[str, QueueMessage] = {}
        self.dead_letters: list[QueueMessage] = []
        self._lock: LoopLocal[asyncio.Lock] = LoopLocal(asyncio.Lock)  # one per event loop

    def __len__(self) -> int:
        return len(self._messages)

    async def send(
        self, payload: Any, *, dedup_key: str | None = None, delay: DurationLike | None = None
    ) -> str:
        """See `EventQueue.send`."""

        now = as_utc(self._clock.now())
        message = QueueMessage(
            payload=payload,
            dedup_key=dedup_key,
            enqueued_at=now,
            available_at=now + (to_timedelta(delay, what="delay") or timedelta(0)),
        )
        async with self._lock.get():
            self._messages[message.id] = message
        return message.id

    async def receive(
        self, *, limit: int = 10, visibility: DurationLike = timedelta(minutes=5)
    ) -> builtins.list[QueueMessage]:
        """See `EventQueue.receive`."""

        now = as_utc(self._clock.now())
        hidden = require_timedelta(visibility, what="visibility")
        taken: list[QueueMessage] = []
        async with self._lock.get():
            ready = sorted(
                (m for m in self._messages.values() if m.available_at <= now),
                key=lambda m: (m.available_at, m.enqueued_at),
            )
            # Delivered `max_attempts` times and never acknowledged (its receiver crashed).
            for message in [m for m in ready if m.attempts >= self.max_attempts]:
                self.dead_letters.append(self._messages.pop(message.id))
                ready.remove(message)
            for message in ready[: max(0, limit)]:
                updated = message.model_copy(
                    update={"attempts": message.attempts + 1, "available_at": now + hidden}
                )
                self._messages[message.id] = updated
                taken.append(updated.model_copy(deep=True))
        return taken

    async def ack(self, message_id: str) -> None:
        """See `EventQueue.ack`."""

        async with self._lock.get():
            self._messages.pop(message_id, None)

    async def nack(self, message_id: str, *, delay: DurationLike | None = None) -> None:
        """See `EventQueue.nack`."""

        now = as_utc(self._clock.now())
        async with self._lock.get():
            message = self._messages.get(message_id)
            if message is None:
                return
            if message.attempts >= self.max_attempts:
                self.dead_letters.append(self._messages.pop(message_id))
                return
            self._messages[message_id] = message.model_copy(
                update={"available_at": now + (to_timedelta(delay) or timedelta(0))}
            )


class QueueTrigger(BaseTrigger):
    """Start a run for every message of an `EventQueue`.

    A message is acknowledged once its run is queued (or a duplicate recognized) and handed back
    with `nack` when refused.

    Args:
        batch: Messages received per poll.
        visibility: How long a received message stays invisible to others.
        retry_delay: Delay before a refused message is offered again.

    Example:
        ```python
        queue = InMemoryEventQueue()
        automation = Automation(desk, triggers=[QueueTrigger(queue, name="tickets")],
                                input="Handle ticket {payload.id}: {payload.text}")
        await queue.send({"id": "T-1", "text": "Printer on fire"}, dedup_key="T-1")
        ```
    """

    ack_on_end = False
    """The message is acknowledged once the run is queued: the run store, not the queue,
    then holds the work (a failed run is retried or dead-lettered there)."""

    def __init__(
        self,
        queue: EventQueue,
        *,
        name: str = "queue",
        batch: int = 10,
        visibility: DurationLike = timedelta(minutes=5),
        poll_interval: DurationLike = timedelta(seconds=1),
        retry_delay: DurationLike = timedelta(seconds=30),
        input: TemplateLike | None = None,
        thread_id: TemplateLike | None = None,
        dedup: TemplateLike | None = None,
    ) -> None:
        super().__init__(name, input=input, thread_id=thread_id, dedup=dedup)
        if batch < 1:
            raise ValueError("batch must be >= 1")
        self.queue = queue
        self.batch = batch
        self.visibility = require_timedelta(visibility, what="visibility")
        self.poll_interval = require_timedelta(poll_interval, what="poll_interval")
        self.retry_delay = require_timedelta(retry_delay, what="retry_delay")

    async def poll(self) -> builtins.list[TriggerEvent]:
        """Receive one batch of messages as events."""

        messages = await self.queue.receive(limit=self.batch, visibility=self.visibility)
        return [
            TriggerEvent(
                id=message.id,
                source=self.name,
                payload=message.payload,
                received_at=message.enqueued_at,
                dedup_key=f"{self.name}:{message.dedup_key or message.id}",
                metadata={"message_id": message.id, "attempts": message.attempts},
            )
            for message in messages
        ]

    async def events(self, context: TriggerContext) -> AsyncIterator[TriggerEvent]:
        """Receive messages until the worker stops."""

        while not context.stopping.is_set():
            try:
                found = await self.poll()
            except Exception:  # noqa: BLE001 - a broker outage must not stop the worker
                _logger.warning("Queue trigger %s: receive failed", self.name, exc_info=True)
                found = []
            for event in found:
                yield event
            if not found and not await context.pause(self.poll_interval.total_seconds()):
                return

    async def ack(self, event: TriggerEvent, error: BaseException | None) -> None:
        """Delete the message, or hand it back after `retry_delay` when `error` is set."""

        message_id = str(event.metadata.get("message_id", event.id))
        if error is None:
            await self.queue.ack(message_id)
        else:
            await self.queue.nack(message_id, delay=self.retry_delay)


__all__ = ["EventQueue", "InMemoryEventQueue", "QueueMessage", "QueueTrigger"]
