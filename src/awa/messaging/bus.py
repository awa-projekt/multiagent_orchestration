"""The message bus: mailboxes, direct messages, broadcast, pub/sub, and request/reply."""

from __future__ import annotations

import asyncio
import heapq
import itertools
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from awa._internal.logging import get_logger
from awa._internal.util import add_callback, call_watchers
from awa.contracts.communication import AgentMessage, MessageKind
from awa.errors import (
    AgentAlreadyRegisteredError,
    CommunicationNotAllowedError,
    MailboxFullError,
    MessageRequestTimeoutError,
    RecipientNotFoundError,
)
from awa.messaging.policy import AllowAll, CommunicationPolicy

OverflowPolicy = Literal["reject", "drop_oldest"]
"""What a full mailbox does with a new message: raise `MailboxFullError`
(`"reject"`) or evict its oldest queued message (`"drop_oldest"`)."""

DeadLetterReason = Literal["expired", "mailbox_full", "overflow", "unregistered", "run_ended"]
"""Why a message was not delivered.

- `expired`: the message passed its `expires_at` before it was received.
- `mailbox_full`: a broadcast or publication hit a full `"reject"` mailbox.
- `overflow`: the message was evicted from a full `"drop_oldest"` mailbox.
- `unregistered`: the mailbox holding the message was unregistered.
- `run_ended`: the team or network run the message belonged to ended before
  the recipient read it.
"""

MessageListener = Callable[[AgentMessage], Awaitable[None] | None]
"""A callback invoked with every successfully sent message. May be sync or async."""

_logger = get_logger("messaging")

_MAX_OPEN_REQUESTS = 10_000


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DeadLetter(BaseModel):
    """A message the bus could not deliver, why (`reason`), and the mailbox it was meant for."""

    model_config = ConfigDict(frozen=True)

    message: AgentMessage
    reason: DeadLetterReason
    recipient: str | None = None
    recorded_at: datetime = Field(default_factory=_utcnow)


class MessageBus(ABC):
    """Abstract asynchronous message bus between named agent mailboxes.

    `notification`, `request` and `response` go to `recipient`; `broadcast` to every other mailbox
    the policy allows; `publication` to the subscribers of `topic`. Mailboxes are bounded and
    deliver by priority, FIFO within one; senders need no mailbox. Transcript and dead letters are
    bounded.
    """

    @abstractmethod
    def register(
        self, name: str, *, max_size: int = 1000, overflow: OverflowPolicy = "reject"
    ) -> None:
        """Create the mailbox `name` with room for `max_size` messages.

        Raises:
            AgentAlreadyRegisteredError: If the mailbox already exists.
            ValueError: If `max_size` is not positive or `overflow` is unknown.
        """

    @abstractmethod
    def unregister(self, name: str) -> None:
        """Remove the mailbox `name`: messages become dead letters, waiters and requests fail.

        Raises:
            RecipientNotFoundError: If the mailbox does not exist.
        """

    @abstractmethod
    def is_registered(self, name: str) -> bool:
        """Return whether the mailbox `name` exists."""

    @abstractmethod
    def mailboxes(self) -> list[str]:
        """Return the names of all mailboxes, sorted."""

    @abstractmethod
    async def send(self, message: AgentMessage) -> AgentMessage:
        """Route `message` according to its kind and return it.

        An expired message becomes a dead letter. A broadcast or publication skips a full `"reject"`
        mailbox and records a dead letter.

        Raises:
            RecipientNotFoundError: If a direct message's recipient has no mailbox.
            CommunicationNotAllowedError: If the policy forbids the message.
            MailboxFullError: If a direct message meets a full `"reject"` mailbox.
        """

    @abstractmethod
    def subscribe(self, name: str, topic: str) -> None:
        """Subscribe the mailbox `name` to publications on `topic`.

        Raises:
            RecipientNotFoundError: If the mailbox does not exist.
            ValueError: If `topic` is empty.
        """

    @abstractmethod
    async def receive(
        self,
        name: str,
        *,
        max_messages: int | None = None,
        kinds: Collection[MessageKind] | None = None,
        timeout: float | None = None,
    ) -> list[AgentMessage]:
        """Remove and return queued messages in priority order.

        Args:
            kinds: Only take messages of these kinds; others stay queued.
            timeout: With a positive value, wait up to that many seconds for a matching message.

        Raises:
            RecipientNotFoundError: If the mailbox does not exist or is unregistered while waiting.
            ValueError: If `max_messages` is less than 1.
        """

    @abstractmethod
    async def peek(
        self,
        name: str,
        *,
        max_messages: int | None = None,
        kinds: Collection[MessageKind] | None = None,
    ) -> list[AgentMessage]:
        """Queued messages in priority order, without removing them.

        Raises:
            RecipientNotFoundError: If the mailbox does not exist.
            ValueError: If `max_messages` is less than 1.
        """

    @abstractmethod
    def pending_count(self, name: str) -> int:
        """How many unexpired messages are queued in `name`.

        Raises:
            RecipientNotFoundError: If the mailbox does not exist.
        """

    @abstractmethod
    async def request(self, message: AgentMessage, *, timeout: float = 30.0) -> AgentMessage:
        """Send a request and wait for its correlated response.

        The response must come from the recipient, addressed to `reply_to` (or the sender); it is
        returned directly, so the requester needs no mailbox.

        Raises:
            ValueError: If the message is not a request, `timeout` is not positive, or the request
                is already awaiting a response.
            MessageRequestTimeoutError: If no response arrives in time, or the request expired
                first.
            RecipientNotFoundError: If the recipient does not exist or is unregistered before taking
                it.
            CommunicationNotAllowedError: If the policy forbids the request.
            MailboxFullError: If the recipient's mailbox rejects or evicts it.
        """

    async def reply(
        self,
        request: AgentMessage,
        *,
        content: str = "",
        data: dict[str, Any] | None = None,
        sender: str | None = None,
    ) -> AgentMessage:
        """Send the response to `request` (from its recipient unless `sender` is given).

        Raises:
            ValueError: If `request` is not of kind `"request"`.
        """

        if request.kind != "request":
            raise ValueError(f"Only requests can be replied to, not {request.kind!r} messages")
        return await self.send(request.reply(content=content, data=data, sender=sender))

    @abstractmethod
    def history(
        self,
        *,
        agent: str | None = None,
        conversation_id: str | None = None,
        kind: MessageKind | None = None,
        limit: int | None = None,
    ) -> list[AgentMessage]:
        """Sent messages in chronological order, filtered; `limit` keeps the most recent.

        Args:
            agent: Only messages sent by or delivered to this agent.

        Raises:
            ValueError: If `limit` is negative.
        """

    @property
    @abstractmethod
    def dead_letters(self) -> list[DeadLetter]:
        """Messages that could not be delivered, oldest first."""

    @abstractmethod
    def add_listener(self, listener: MessageListener) -> Callable[[], None]:
        """Call `listener` with every sent message; returns a function that removes it.

        Listener errors are logged and swallowed.
        """


class _Mailbox:
    """A bounded priority queue of messages with wake-up futures for receivers."""

    def __init__(self, name: str, max_size: int, overflow: OverflowPolicy) -> None:
        self.name = name
        self.max_size = max_size
        self.overflow: OverflowPolicy = overflow
        self.topics: set[str] = set()
        self.closed = False
        # Entries are (-priority, sequence, message); the unique sequence keeps
        # FIFO order within a priority and means messages are never compared.
        self._heap: list[tuple[int, int, AgentMessage]] = []
        self._waiters: set[asyncio.Future[None]] = set()

    def __len__(self) -> int:
        return len(self._heap)

    def push(self, message: AgentMessage, sequence: int) -> None:
        heapq.heappush(self._heap, (-int(message.priority), sequence, message))
        self.wake()

    def pop_oldest(self) -> AgentMessage:
        oldest = min(self._heap, key=lambda entry: entry[1])
        self._heap.remove(oldest)
        heapq.heapify(self._heap)
        return oldest[2]

    def pop_expired(self, now: datetime) -> list[AgentMessage]:
        expired = [entry for entry in self._heap if entry[2].is_expired(now)]
        if expired:
            self._heap = [entry for entry in self._heap if not entry[2].is_expired(now)]
            heapq.heapify(self._heap)
        return [entry[2] for entry in sorted(expired)]

    def select(
        self, *, limit: int | None, kinds: frozenset[str] | None, remove: bool
    ) -> list[AgentMessage]:
        if kinds is None and remove:
            count = len(self._heap) if limit is None else min(limit, len(self._heap))
            return [heapq.heappop(self._heap)[2] for _ in range(count)]
        chosen: list[tuple[int, int, AgentMessage]] = []
        for entry in sorted(self._heap):
            if kinds is None or entry[2].kind in kinds:
                chosen.append(entry)
                if limit is not None and len(chosen) == limit:
                    break
        if remove and chosen:
            taken = {entry[1] for entry in chosen}
            self._heap = [entry for entry in self._heap if entry[1] not in taken]
            heapq.heapify(self._heap)
        return [entry[2] for entry in chosen]

    def drain(self) -> list[AgentMessage]:
        messages = [entry[2] for entry in sorted(self._heap)]
        self._heap.clear()
        return messages

    async def wait(self) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.add(future)
        try:
            await future
        finally:
            self._waiters.discard(future)

    def wake(self) -> None:
        for future in self._waiters:
            if not future.done():
                future.set_result(None)
        self._waiters.clear()


class InMemoryMessageBus(MessageBus):
    """A `MessageBus` whose state lives in memory within one event loop.

    Args:
        policy: Communication policy; `AllowAll` when omitted.
        max_history: Most messages kept in the transcript and, separately, most dead letters kept;
            `0` keeps none.

    Raises:
        ValueError: If `max_history` is negative.

    Example:
        ```python
        bus = InMemoryMessageBus()
        bus.register("writer")
        await bus.send(AgentMessage(sender="researcher", recipient="writer",
                                    subject="Draft ready", content="See section 2."))
        [message] = await bus.receive("writer")
        ```
    """

    def __init__(
        self, *, policy: CommunicationPolicy | None = None, max_history: int = 10_000
    ) -> None:
        if max_history < 0:
            raise ValueError("max_history must be >= 0")
        self._policy: CommunicationPolicy = policy if policy is not None else AllowAll()
        self._mailboxes: dict[str, _Mailbox] = {}
        self._sequence = itertools.count()
        self._history: deque[tuple[AgentMessage, frozenset[str]]] = deque(maxlen=max_history)
        self._dead_letters: deque[DeadLetter] = deque(maxlen=max_history)
        self._listeners: list[MessageListener] = []
        self._pending: dict[str, tuple[AgentMessage, asyncio.Future[AgentMessage]]] = {}
        self._open_requests: OrderedDict[str, AgentMessage] = OrderedDict()

    @property
    def policy(self) -> CommunicationPolicy:
        """The communication policy this bus enforces."""

        return self._policy

    def register(
        self, name: str, *, max_size: int = 1000, overflow: OverflowPolicy = "reject"
    ) -> None:
        """Create the mailbox `name`. See `MessageBus.register`."""

        if not name:
            raise ValueError("Mailbox name must not be empty")
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        if overflow not in ("reject", "drop_oldest"):
            raise ValueError(f"Unknown overflow policy {overflow!r}")
        if name in self._mailboxes:
            raise AgentAlreadyRegisteredError(f"A mailbox named {name!r} is already registered")
        self._mailboxes[name] = _Mailbox(name, max_size, overflow)

    def unregister(self, name: str) -> None:
        """Remove the mailbox `name`. See `MessageBus.unregister`."""

        mailbox = self._mailbox(name)
        del self._mailboxes[name]
        mailbox.closed = True
        for message in mailbox.drain():
            self._dead_letter(message, "unregistered", name)
            self._fail_request(
                message, RecipientNotFoundError(f"Mailbox {name!r} was unregistered")
            )
        mailbox.wake()

    def drain(self, reason: DeadLetterReason = "run_ended") -> list[DeadLetter]:
        """Move every queued message to the dead letters and return them; waiting requests fail."""

        letters: list[DeadLetter] = []
        for name, mailbox in self._mailboxes.items():
            for message in mailbox.drain():
                letters.append(self._dead_letter(message, reason, name))
                self._fail_request(message, RecipientNotFoundError(f"The run of {name!r} ended"))
        return letters

    def is_registered(self, name: str) -> bool:
        """Return whether the mailbox `name` exists. See `MessageBus.is_registered`."""

        return name in self._mailboxes

    def mailboxes(self) -> list[str]:
        """Return the names of all mailboxes, sorted. See `MessageBus.mailboxes`."""

        return sorted(self._mailboxes)

    def subscribe(self, name: str, topic: str) -> None:
        """Subscribe the mailbox `name` to publications on `topic`. See `MessageBus.subscribe`."""

        if not topic:
            raise ValueError("Topic must not be empty")
        self._mailbox(name).topics.add(topic)

    async def send(self, message: AgentMessage) -> AgentMessage:
        """Route `message` according to its kind and return it. See `MessageBus.send`."""

        if message.is_expired():
            self._dead_letter(message, "expired", message.recipient)
            return message
        if message.kind == "broadcast":
            delivered = self._send_broadcast(message)
        elif message.kind == "publication":
            delivered = self._send_publication(message)
        else:
            delivered = self._send_direct(message)
        self._history.append((message, delivered))
        await call_watchers(
            self._listeners, message, _logger, "Message listener failed on %s", message.id
        )
        return message

    def _send_direct(self, message: AgentMessage) -> frozenset[str]:
        recipient = message.recipient
        assert recipient is not None  # guaranteed by AgentMessage validation
        answered = self._answered_request(message)
        waiter = self._pending.get(answered.id) if answered is not None else None
        if waiter is not None and waiter[1].done():
            waiter = None
        if waiter is None and recipient not in self._mailboxes:
            raise RecipientNotFoundError(f"No mailbox named {recipient!r} is registered")
        if answered is None and not self._policy.allows(message.sender, recipient):
            raise CommunicationNotAllowedError(
                f"{message.sender!r} is not allowed to send messages to {recipient!r}"
            )
        if waiter is not None:
            waiter[1].set_result(message)
        else:
            self._deliver(self._mailboxes[recipient], message, strict=True)
        if answered is not None:
            self._open_requests.pop(answered.id, None)
        if message.kind == "request":
            self._open_requests[message.id] = message
            if len(self._open_requests) > _MAX_OPEN_REQUESTS:
                self._open_requests.popitem(last=False)
        return frozenset((recipient,))

    def _send_broadcast(self, message: AgentMessage) -> frozenset[str]:
        others = [name for name in self._mailboxes if name != message.sender]
        targets = [name for name in others if self._policy.allows_broadcast(message.sender, name)]
        if others and not targets:
            raise CommunicationNotAllowedError(f"{message.sender!r} is not allowed to broadcast")
        return self._fan_out(message, targets)

    def _send_publication(self, message: AgentMessage) -> frozenset[str]:
        topic = message.topic
        assert topic is not None  # guaranteed by AgentMessage validation
        if not self._policy.allows_topic(message.sender, topic):
            raise CommunicationNotAllowedError(
                f"{message.sender!r} is not allowed to publish on topic {topic!r}"
            )
        targets = [
            name
            for name, mailbox in self._mailboxes.items()
            if topic in mailbox.topics and name != message.sender
        ]
        return self._fan_out(message, targets)

    def _fan_out(self, message: AgentMessage, targets: list[str]) -> frozenset[str]:
        return frozenset(
            name for name in targets if self._deliver(self._mailboxes[name], message, strict=False)
        )

    def _answered_request(self, message: AgentMessage) -> AgentMessage | None:
        """Return the open request `message` legitimately answers, if any."""

        if message.kind != "response" or message.correlation_id is None:
            return None
        pending = self._pending.get(message.correlation_id)
        request = (
            pending[0] if pending is not None else self._open_requests.get(message.correlation_id)
        )
        if (
            request is None
            or message.sender != request.recipient
            or message.recipient != (request.reply_to or request.sender)
        ):
            return None
        return request

    def _deliver(self, mailbox: _Mailbox, message: AgentMessage, *, strict: bool) -> bool:
        if len(mailbox) >= mailbox.max_size:
            self._expire(mailbox)
        if len(mailbox) >= mailbox.max_size:
            if mailbox.overflow == "drop_oldest":
                dropped = mailbox.pop_oldest()
                self._dead_letter(dropped, "overflow", mailbox.name)
                self._fail_request(
                    dropped, MailboxFullError(f"Mailbox {mailbox.name!r} overflowed")
                )
            elif strict:
                raise MailboxFullError(
                    f"Mailbox {mailbox.name!r} is full ({mailbox.max_size} messages)"
                )
            else:
                self._dead_letter(message, "mailbox_full", mailbox.name)
                return False
        mailbox.push(message, next(self._sequence))
        return True

    def _dead_letter(
        self, message: AgentMessage, reason: DeadLetterReason, recipient: str | None
    ) -> DeadLetter:
        _logger.debug("Dead letter %s (%s) for %r", message.id, reason, recipient)
        letter = DeadLetter(message=message, reason=reason, recipient=recipient)
        self._dead_letters.append(letter)
        return letter

    def _fail_request(self, message: AgentMessage, error: Exception) -> None:
        pending = self._pending.get(message.id)
        if pending is not None and not pending[1].done():
            pending[1].set_exception(error)

    async def receive(
        self,
        name: str,
        *,
        max_messages: int | None = None,
        kinds: Collection[MessageKind] | None = None,
        timeout: float | None = None,
    ) -> list[AgentMessage]:
        """Remove and return queued messages in priority order. See `MessageBus.receive`."""

        _check_max_messages(max_messages)
        mailbox = self._mailbox(name)
        wanted = None if kinds is None else frozenset(kinds)
        messages = self._take(mailbox, max_messages, wanted)
        if messages or timeout is None or timeout <= 0:
            return messages
        try:
            async with asyncio.timeout(timeout):
                while not messages:
                    await mailbox.wait()
                    if mailbox.closed:
                        raise RecipientNotFoundError(f"Mailbox {name!r} was unregistered")
                    messages = self._take(mailbox, max_messages, wanted)
        except TimeoutError:
            return []
        return messages

    async def peek(
        self,
        name: str,
        *,
        max_messages: int | None = None,
        kinds: Collection[MessageKind] | None = None,
    ) -> list[AgentMessage]:
        """Return queued messages in priority order without removing them. See `MessageBus.peek`."""

        _check_max_messages(max_messages)
        mailbox = self._mailbox(name)
        self._expire(mailbox)
        wanted = None if kinds is None else frozenset(kinds)
        return mailbox.select(limit=max_messages, kinds=wanted, remove=False)

    def pending_count(self, name: str) -> int:
        """How many unexpired messages are queued in `name` (see `MessageBus.pending_count`)."""

        mailbox = self._mailbox(name)
        self._expire(mailbox)
        return len(mailbox)

    def _take(
        self, mailbox: _Mailbox, limit: int | None, kinds: frozenset[str] | None
    ) -> list[AgentMessage]:
        self._expire(mailbox)
        return mailbox.select(limit=limit, kinds=kinds, remove=True)

    def _expire(self, mailbox: _Mailbox) -> None:
        for message in mailbox.pop_expired(_utcnow()):
            self._dead_letter(message, "expired", mailbox.name)
            self._fail_request(
                message,
                MessageRequestTimeoutError(
                    f"Request {message.id} expired in mailbox {mailbox.name!r} before it was taken"
                ),
            )

    async def request(self, message: AgentMessage, *, timeout: float = 30.0) -> AgentMessage:
        """Send a request and wait for its correlated response. See `MessageBus.request`."""

        if message.kind != "request":
            raise ValueError(f"request() needs a 'request' message, not {message.kind!r}")
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        if message.id in self._pending:
            raise ValueError(f"Request {message.id} is already awaiting a response")
        if message.is_expired():
            await self.send(message)
            raise MessageRequestTimeoutError(f"Request {message.id} expired before delivery")
        future: asyncio.Future[AgentMessage] = asyncio.get_running_loop().create_future()
        self._pending[message.id] = (message, future)
        try:
            await self.send(message)
            try:
                async with asyncio.timeout(timeout):
                    return await future
            except TimeoutError as exc:
                raise MessageRequestTimeoutError(
                    f"No response from {message.recipient!r} to request {message.id} "
                    f"within {timeout} seconds"
                ) from exc
        finally:
            self._pending.pop(message.id, None)

    def history(
        self,
        *,
        agent: str | None = None,
        conversation_id: str | None = None,
        kind: MessageKind | None = None,
        limit: int | None = None,
    ) -> list[AgentMessage]:
        """Return sent messages in chronological order. See `MessageBus.history`."""

        if limit is not None and limit < 0:
            raise ValueError("limit must be >= 0")
        matches = [
            message
            for message, delivered in self._history
            if (agent is None or message.sender == agent or agent in delivered)
            and (conversation_id is None or message.conversation_id == conversation_id)
            and (kind is None or message.kind == kind)
        ]
        if limit is None:
            return matches
        return matches[-limit:] if limit else []

    @property
    def dead_letters(self) -> list[DeadLetter]:
        """Messages that could not be delivered, oldest first. See `MessageBus.dead_letters`."""

        return list(self._dead_letters)

    def add_listener(self, listener: MessageListener) -> Callable[[], None]:
        """Call `listener` with every successfully sent message. See `MessageBus.add_listener`."""

        return add_callback(self._listeners, listener)

    def _mailbox(self, name: str) -> _Mailbox:
        mailbox = self._mailboxes.get(name)
        if mailbox is None:
            raise RecipientNotFoundError(f"No mailbox named {name!r} is registered")
        return mailbox


def _check_max_messages(max_messages: int | None) -> None:
    if max_messages is not None and max_messages < 1:
        raise ValueError("max_messages must be >= 1")


__all__ = [
    "DeadLetter",
    "DeadLetterReason",
    "InMemoryMessageBus",
    "MessageBus",
    "MessageListener",
    "OverflowPolicy",
]
