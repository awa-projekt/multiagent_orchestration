"""The message bus, the blackboard, message expiry and messaging policies."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from awa.contracts.communication import AgentMessage, MessagePriority
from awa.errors import (
    AgentAlreadyRegisteredError,
    BlackboardConflictError,
    CommunicationNotAllowedError,
    MailboxFullError,
    MessagingError,
    RecipientNotFoundError,
)
from awa.messaging import (
    AllowAll,
    Blackboard,
    BlackboardEntry,
    CommunicationPolicy,
    MessageBus,
    TopologyPolicy,
)


def note(sender: str, recipient: str, content: str = "", **fields: Any) -> AgentMessage:
    return AgentMessage(sender=sender, recipient=recipient, content=content, **fields)


def expired(sender: str, recipient: str) -> AgentMessage:
    past = datetime.now(UTC) - timedelta(seconds=10)
    return AgentMessage(sender=sender, recipient=recipient, created_at=past, expires_at=past)


@pytest.fixture
async def bus(durable: Any) -> MessageBus:
    bus: MessageBus = await durable.bus()
    for name in ("a", "b", "c"):
        bus.register(name)
    return bus


# -- registration -------------------------------------------------------------


async def test_register_and_list_mailboxes(durable: Any) -> None:
    bus = await durable.bus()
    assert isinstance(bus, MessageBus)
    bus.register("zeta")
    bus.register("alpha")
    assert bus.mailboxes() == ["alpha", "zeta"]
    assert bus.is_registered("alpha")
    assert not bus.is_registered("beta")


async def test_register_rejects_duplicates_and_bad_arguments(durable: Any) -> None:
    bus = await durable.bus()
    bus.register("a")
    with pytest.raises(AgentAlreadyRegisteredError):
        bus.register("a")
    with pytest.raises(ValueError):
        bus.register("b", max_size=0)
    with pytest.raises(ValueError):
        bus.register("b", overflow="explode")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        bus.register("")


async def test_constructor_rejects_negative_history(durable: Any) -> None:
    with pytest.raises(ValueError):
        await durable.bus(max_history=-1)


async def test_unregister_unknown_raises(bus: MessageBus) -> None:
    with pytest.raises(RecipientNotFoundError):
        bus.unregister("nobody")


async def test_unregister_dead_letters_queued_messages(bus: MessageBus) -> None:
    await bus.send(note("a", "b", "hi"))
    bus.subscribe("b", "news")
    bus.unregister("b")
    assert not bus.is_registered("b")
    [letter] = bus.dead_letters
    assert letter.reason == "unregistered"
    assert letter.recipient == "b"
    assert letter.message.content == "hi"
    bus.register("b")
    await bus.send(AgentMessage(kind="publication", sender="a", topic="news"))
    assert bus.pending_count("b") == 0  # the new mailbox has no subscriptions


async def test_unregister_wakes_waiting_receiver(bus: MessageBus) -> None:
    waiter = asyncio.create_task(bus.receive("b", timeout=5))
    await asyncio.sleep(0)
    bus.unregister("b")
    with pytest.raises(RecipientNotFoundError):
        await waiter


# -- direct messages ----------------------------------------------------------


async def test_send_and_receive(bus: MessageBus) -> None:
    sent = await bus.send(note("a", "b", "hello"))
    assert bus.pending_count("b") == 1
    received = await bus.receive("b")
    assert received == [sent]
    assert bus.pending_count("b") == 0
    assert await bus.receive("b") == []


async def test_history_limit_returns_the_most_recent_matches(bus: MessageBus) -> None:
    sent = [await bus.send(note("a", "b", str(i))) for i in range(3)]
    assert [m.id for m in bus.history(limit=2)] == [m.id for m in sent[1:]]
    assert [m.id for m in bus.history(limit=5)] == [m.id for m in sent]
    assert bus.history(limit=0) == []


async def test_sender_needs_no_mailbox(bus: MessageBus) -> None:
    await bus.send(note("user", "a", "task"))
    [message] = await bus.receive("a")
    assert message.sender == "user"


async def test_unknown_recipient(bus: MessageBus) -> None:
    with pytest.raises(RecipientNotFoundError):
        await bus.send(note("a", "nobody"))
    assert bus.history() == []


async def test_priority_order_and_fifo_within_priority(bus: MessageBus) -> None:
    await bus.send(note("a", "b", "low", priority=MessagePriority.LOW))
    await bus.send(note("a", "b", "normal-1"))
    await bus.send(note("a", "b", "urgent", priority=MessagePriority.URGENT))
    await bus.send(note("a", "b", "normal-2"))
    await bus.send(note("a", "b", "high", priority=MessagePriority.HIGH))
    await bus.send(note("a", "b", "normal-3"))
    contents = [message.content for message in await bus.receive("b")]
    assert contents == ["urgent", "high", "normal-1", "normal-2", "normal-3", "low"]


async def test_receive_max_messages(bus: MessageBus) -> None:
    for index in range(5):
        await bus.send(note("a", "b", str(index)))
    first = await bus.receive("b", max_messages=2)
    assert [m.content for m in first] == ["0", "1"]
    rest = await bus.receive("b", max_messages=10)
    assert [m.content for m in rest] == ["2", "3", "4"]
    with pytest.raises(ValueError):
        await bus.receive("b", max_messages=0)


async def test_receive_filters_by_kind(bus: MessageBus) -> None:
    await bus.send(note("a", "b", "n1"))
    await bus.send(AgentMessage(kind="request", sender="a", recipient="b", content="r1"))
    await bus.send(note("a", "b", "n2", priority=MessagePriority.HIGH))
    requests = await bus.receive("b", kinds={"request"})
    assert [m.content for m in requests] == ["r1"]
    remaining = await bus.receive("b")
    assert [m.content for m in remaining] == ["n2", "n1"]


async def test_receive_unknown_mailbox(bus: MessageBus) -> None:
    with pytest.raises(RecipientNotFoundError):
        await bus.receive("nobody")
    with pytest.raises(RecipientNotFoundError):
        bus.pending_count("nobody")
    with pytest.raises(RecipientNotFoundError):
        await bus.peek("nobody")


async def test_receive_waits_for_message(bus: MessageBus) -> None:
    waiter = asyncio.create_task(bus.receive("b", timeout=5))
    await asyncio.sleep(0)
    assert not waiter.done()
    sent = await bus.send(note("a", "b", "late"))
    assert await waiter == [sent]


async def test_receive_timeout_returns_empty(bus: MessageBus) -> None:
    assert await bus.receive("b", timeout=0.01) == []


async def test_waiting_receiver_ignores_non_matching_kinds(bus: MessageBus) -> None:
    waiter = asyncio.create_task(bus.receive("b", kinds={"request"}, timeout=5))
    await asyncio.sleep(0)
    await bus.send(note("a", "b", "notification"))
    await asyncio.sleep(0)
    assert not waiter.done()
    await bus.send(AgentMessage(kind="request", sender="a", recipient="b", content="req"))
    [message] = await waiter
    assert message.content == "req"
    assert bus.pending_count("b") == 1


async def test_cancelled_receiver_leaves_message_queued(bus: MessageBus) -> None:
    waiter = asyncio.create_task(bus.receive("b", timeout=5))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await bus.send(note("a", "b", "kept"))
    assert bus.pending_count("b") == 1


async def test_concurrent_receivers_each_get_distinct_messages(bus: MessageBus) -> None:
    waiters = [asyncio.create_task(bus.receive("b", max_messages=1, timeout=5)) for _ in range(3)]
    await asyncio.sleep(0)
    for index in range(3):
        await bus.send(note("a", "b", str(index)))
    results = await asyncio.gather(*waiters)
    assert sorted(batch[0].content for batch in results) == ["0", "1", "2"]


async def test_peek_does_not_remove(bus: MessageBus) -> None:
    await bus.send(note("a", "b", "one"))
    await bus.send(note("a", "b", "two", priority=MessagePriority.HIGH))
    peeked = await bus.peek("b", max_messages=1)
    assert [m.content for m in peeked] == ["two"]
    assert [m.content for m in await bus.peek("b", kinds={"notification"})] == ["two", "one"]
    assert bus.pending_count("b") == 2
    with pytest.raises(ValueError):
        await bus.peek("b", max_messages=0)


# -- expiry -------------------------------------------------------------------


async def test_expired_message_goes_to_dead_letters(bus: MessageBus) -> None:
    message = expired("a", "b")
    assert await bus.send(message) is message
    assert bus.pending_count("b") == 0
    [letter] = bus.dead_letters
    assert letter.reason == "expired"
    assert letter.message == message
    assert bus.history() == []


async def test_message_expiring_in_mailbox_is_dead_lettered(bus: MessageBus) -> None:
    message = AgentMessage.with_ttl(timedelta(milliseconds=20), sender="a", recipient="b")
    await bus.send(message)
    await asyncio.sleep(0.05)
    assert await bus.receive("b") == []
    assert [letter.reason for letter in bus.dead_letters] == ["expired"]


async def test_pending_count_purges_expired(bus: MessageBus) -> None:
    await bus.send(AgentMessage.with_ttl(timedelta(milliseconds=10), sender="a", recipient="b"))
    await bus.send(note("a", "b", "fresh"))
    await asyncio.sleep(0.03)
    assert bus.pending_count("b") == 1
    assert [m.content for m in await bus.peek("b")] == ["fresh"]


# -- overflow -----------------------------------------------------------------


async def test_reject_overflow_raises(durable: Any) -> None:
    bus = await durable.bus()
    bus.register("b", max_size=2)
    await bus.send(note("a", "b", "1"))
    await bus.send(note("a", "b", "2"))
    with pytest.raises(MailboxFullError):
        await bus.send(note("a", "b", "3"))
    assert [m.content for m in await bus.receive("b")] == ["1", "2"]
    assert len(bus.history()) == 2


async def test_full_mailbox_purges_expired_before_rejecting(durable: Any) -> None:
    bus = await durable.bus()
    bus.register("b", max_size=1)
    await bus.send(AgentMessage.with_ttl(timedelta(milliseconds=10), sender="a", recipient="b"))
    await asyncio.sleep(0.03)
    await bus.send(note("a", "b", "fits"))
    assert [m.content for m in await bus.receive("b")] == ["fits"]


async def test_drop_oldest_overflow_evicts_oldest(durable: Any) -> None:
    bus = await durable.bus()
    bus.register("b", max_size=2, overflow="drop_oldest")
    await bus.send(note("a", "b", "1", priority=MessagePriority.URGENT))
    await bus.send(note("a", "b", "2"))
    await bus.send(note("a", "b", "3"))
    assert [m.content for m in await bus.receive("b")] == ["2", "3"]
    [letter] = bus.dead_letters
    assert (letter.reason, letter.message.content, letter.recipient) == ("overflow", "1", "b")


async def test_fan_out_skips_full_reject_mailbox(durable: Any) -> None:
    bus = await durable.bus()
    bus.register("a")
    bus.register("b", max_size=1)
    bus.register("c")
    await bus.send(note("a", "b", "fills b"))
    await bus.send(AgentMessage(kind="broadcast", sender="a", content="all"))
    assert bus.pending_count("c") == 1
    assert bus.pending_count("b") == 1
    [letter] = bus.dead_letters
    assert (letter.reason, letter.recipient) == ("mailbox_full", "b")
    assert bus.history(agent="b", kind="broadcast") == []
    assert len(bus.history(agent="c", kind="broadcast")) == 1


# -- policy -------------------------------------------------------------------


async def test_policy_denies_direct_message(durable: Any) -> None:
    bus = await durable.bus(policy=TopologyPolicy({"a": ["b"]}))
    for name in ("a", "b"):
        bus.register(name)
    await bus.send(note("a", "b"))
    with pytest.raises(CommunicationNotAllowedError):
        await bus.send(note("b", "a"))
    assert bus.pending_count("a") == 0


async def test_policy_property(durable: Any) -> None:
    policy = TopologyPolicy({})
    assert (await durable.bus(policy=policy)).policy is policy


# -- broadcast ----------------------------------------------------------------


async def test_broadcast_reaches_every_other_mailbox(bus: MessageBus) -> None:
    message = await bus.send(AgentMessage(kind="broadcast", sender="a", content="all hands"))
    assert await bus.receive("a") == []
    assert await bus.receive("b") == [message]
    assert await bus.receive("c") == [message]


async def test_broadcast_with_no_other_mailboxes_is_a_no_op(durable: Any) -> None:
    bus = await durable.bus()
    bus.register("a")
    await bus.send(AgentMessage(kind="broadcast", sender="a"))
    assert bus.pending_count("a") == 0
    assert len(bus.history()) == 1


async def test_broadcast_respects_policy(durable: Any) -> None:
    bus = await durable.bus(policy=TopologyPolicy({"lead": ["a"], "a": ["lead"]}))
    for name in ("lead", "a", "b"):
        bus.register(name)
    await bus.send(AgentMessage(kind="broadcast", sender="lead"))
    assert (bus.pending_count("a"), bus.pending_count("b")) == (1, 0)
    denied = await durable.bus(policy=TopologyPolicy({}, allow_broadcast=["lead"]))
    for name in ("lead", "a"):
        denied.register(name)
    with pytest.raises(CommunicationNotAllowedError):
        await denied.send(AgentMessage(kind="broadcast", sender="a"))
    await denied.send(AgentMessage(kind="broadcast", sender="lead"))
    assert denied.pending_count("a") == 1


# -- publications -------------------------------------------------------------


async def test_publication_reaches_subscribers_except_sender(bus: MessageBus) -> None:
    bus.subscribe("a", "news")
    bus.subscribe("b", "news")
    bus.subscribe("c", "other")
    message = await bus.send(AgentMessage(kind="publication", sender="a", topic="news"))
    assert await bus.receive("a") == []
    assert await bus.receive("b") == [message]
    assert await bus.receive("c") == []


async def test_publication_without_subscribers(bus: MessageBus) -> None:
    await bus.send(AgentMessage(kind="publication", sender="a", topic="void"))
    assert bus.dead_letters == []


async def test_subscription_management(bus: MessageBus) -> None:
    bus.subscribe("b", "a")
    bus.subscribe("b", "a")
    await bus.send(AgentMessage(kind="publication", sender="a", topic="a"))
    assert bus.pending_count("b") == 1  # subscribing twice delivers once
    with pytest.raises(ValueError):
        bus.subscribe("b", "")
    with pytest.raises(RecipientNotFoundError):
        bus.subscribe("nobody", "a")


async def test_publication_respects_topic_policy(durable: Any) -> None:
    bus = await durable.bus(policy=TopologyPolicy({}, topic_publishers={"news": ["a"]}))
    for name in ("a", "b"):
        bus.register(name)
        bus.subscribe(name, "news")
    await bus.send(AgentMessage(kind="publication", sender="a", topic="news"))
    with pytest.raises(CommunicationNotAllowedError):
        await bus.send(AgentMessage(kind="publication", sender="b", topic="news"))
    assert bus.pending_count("b") == 1
    assert bus.pending_count("a") == 0


# -- history and listeners ----------------------------------------------------


async def test_history_filters(bus: MessageBus) -> None:
    first = await bus.send(note("a", "b", "1"))
    await bus.send(note("b", "c", "2"))
    follow_up = await bus.send(note("b", "a", "3", conversation_id=first.conversation_id))
    await bus.send(AgentMessage(kind="broadcast", sender="c", content="4"))
    assert [m.content for m in bus.history()] == ["1", "2", "3", "4"]
    assert [m.content for m in bus.history(agent="a")] == ["1", "3", "4"]
    assert [m.content for m in bus.history(conversation_id=first.id)] == ["1", "3"]
    assert bus.history(kind="broadcast")[0].content == "4"
    assert [m.content for m in bus.history(limit=2)] == ["3", "4"]
    assert bus.history(limit=0) == []
    assert follow_up in bus.history(agent="b", kind="notification")
    with pytest.raises(ValueError):
        bus.history(limit=-1)


async def test_history_is_bounded(durable: Any) -> None:
    bus = await durable.bus(max_history=2)
    bus.register("b")
    for index in range(4):
        await bus.send(note("a", "b", str(index)))
    assert [m.content for m in bus.history()] == ["2", "3"]
    for _ in range(3):
        await bus.send(expired("a", "b"))
    assert len(bus.dead_letters) == 2


async def test_listeners_sync_async_and_removal(bus: MessageBus) -> None:
    seen: list[str] = []

    def sync_listener(message: AgentMessage) -> None:
        seen.append(f"sync:{message.content}")

    async def async_listener(message: AgentMessage) -> None:
        seen.append(f"async:{message.content}")

    remove = bus.add_listener(sync_listener)
    bus.add_listener(async_listener)
    await bus.send(note("a", "b", "1"))
    remove()
    remove()
    await bus.send(note("a", "b", "2"))
    await bus.send(expired("a", "b"))
    assert seen == ["sync:1", "async:1", "async:2"]


async def test_listener_errors_are_swallowed(
    bus: MessageBus, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[str] = []

    def broken(message: AgentMessage) -> None:
        raise RuntimeError("observer bug")

    bus.add_listener(broken)
    bus.add_listener(lambda message: seen.append(message.id))
    sent = await bus.send(note("a", "b"))
    assert seen == [sent.id]
    assert bus.pending_count("b") == 1
    assert "Message listener failed" in caplog.text


def test_a_naive_timestamp_is_read_as_utc_and_never_breaks_expiry() -> None:
    naive = AgentMessage(
        sender="a",
        recipient="b",
        content="x",
        expires_at=datetime(2020, 1, 1),  # noqa: DTZ001
    )
    assert naive.expires_at == datetime(2020, 1, 1, tzinfo=UTC)
    assert naive.is_expired()
    assert not naive.is_expired(now=datetime(2019, 1, 1))  # noqa: DTZ001
    aware = AgentMessage(
        sender="a", recipient="b", content="x", expires_at=datetime(2999, 1, 1, tzinfo=UTC)
    )
    assert not aware.is_expired()


def test_with_ttl_accepts_a_naive_creation_time_and_refuses_expires_at() -> None:
    message = AgentMessage.with_ttl(
        timedelta(hours=1),
        created_at=datetime(2020, 1, 1),  # noqa: DTZ001
        sender="a",
        recipient="b",
        content="x",
    )
    assert message.expires_at == datetime(2020, 1, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="ttl or expires_at"):
        AgentMessage.with_ttl(
            timedelta(hours=1),
            expires_at=datetime(2999, 1, 1, tzinfo=UTC),
            sender="a",
            recipient="b",
            content="x",
        )


def test_allow_all_permits_everything() -> None:
    policy = AllowAll()
    assert isinstance(policy, CommunicationPolicy)
    assert policy.allows("a", "b")
    assert policy.allows_topic("a", "news")
    assert policy.allows_broadcast("a", "b")


def test_topology_policy_directed_edges() -> None:
    policy = TopologyPolicy({"lead": ["researcher", "writer"], "researcher": ["writer"]})
    assert isinstance(policy, CommunicationPolicy)
    assert policy.allows("lead", "writer")
    assert policy.allows("researcher", "writer")
    assert not policy.allows("writer", "researcher")
    assert not policy.allows("writer", "lead")
    assert not policy.allows("unknown", "lead")


def test_topology_policy_symmetric() -> None:
    policy = TopologyPolicy({"a": ["b"]}, symmetric=True)
    assert policy.allows("a", "b")
    assert policy.allows("b", "a")
    assert not policy.allows("a", "a")


def test_topology_policy_topics_open_by_default() -> None:
    policy = TopologyPolicy({})
    assert policy.allows_topic("anyone", "anything")


def test_topology_policy_topic_publishers_are_closed_world() -> None:
    policy = TopologyPolicy({}, topic_publishers={"findings": ["researcher"]})
    assert policy.allows_topic("researcher", "findings")
    assert not policy.allows_topic("writer", "findings")
    assert not policy.allows_topic("researcher", "other")


def test_topology_policy_broadcast_follows_edges_by_default() -> None:
    policy = TopologyPolicy({"lead": ["a"]})
    assert policy.allows_broadcast("lead", "a")
    assert not policy.allows_broadcast("lead", "b")
    assert not policy.allows_broadcast("a", "lead")


def test_topology_policy_explicit_broadcasters_reach_everyone() -> None:
    policy = TopologyPolicy({"lead": ["a"], "a": ["lead"]}, allow_broadcast=["lead"])
    assert policy.allows_broadcast("lead", "a")
    assert policy.allows_broadcast("lead", "b")
    assert not policy.allows_broadcast("a", "lead")


def test_topology_policy_accepts_generators() -> None:
    policy = TopologyPolicy({"a": (name for name in ["b", "c"])})
    assert policy.allows("a", "c")


async def test_write_get_read(durable: Any) -> None:
    board = await durable.blackboard()
    entry = await board.write("plan", {"steps": ["a"]}, author="lead")
    assert (entry.key, entry.version, entry.author, entry.deleted) == ("plan", 1, "lead", False)
    fetched = await board.get("plan")
    assert fetched is not None and fetched.value == {"steps": ["a"]}
    assert await board.read("plan") == {"steps": ["a"]}
    assert await board.get("missing") is None
    assert await board.read("missing", default="fallback") == "fallback"


async def test_versions_increase(durable: Any) -> None:
    board = await durable.blackboard()
    await board.write("k", 1, author="a")
    second = await board.write("k", 2, author="b")
    assert second.version == 2
    assert [(e.value, e.version, e.author) for e in board.history("k")] == [
        (1, 1, "a"),
        (2, 2, "b"),
    ]
    assert board.history("missing") == []


async def test_values_are_copied_in_and_out(durable: Any) -> None:
    board = await durable.blackboard()
    original = {"items": [1, 2]}
    await board.write("k", original, author="a")
    original["items"].append(3)
    value = await board.read("k")
    assert value == {"items": [1, 2]}
    value["items"].append(99)
    entry = await board.get("k")
    assert entry is not None
    entry.value["items"].append(98)
    assert await board.read("k") == {"items": [1, 2]}
    snapshot = await board.snapshot()
    snapshot["k"]["items"].clear()
    assert await board.read("k") == {"items": [1, 2]}


async def test_values_are_normalized_to_json(durable: Any) -> None:
    board = await durable.blackboard()
    await board.write("k", (1, 2), author="a")
    assert await board.read("k") == [1, 2]


async def test_write_validation(durable: Any) -> None:
    board = await durable.blackboard()
    with pytest.raises(TypeError):
        await board.write("k", object(), author="a")
    with pytest.raises(ValueError):
        await board.write("k", float("nan"), author="a")
    with pytest.raises(ValueError):
        await board.write("", 1, author="a")
    with pytest.raises(ValueError):
        await board.write("k", 1, author="")
    with pytest.raises(ValueError):
        Blackboard(max_versions=0)
    assert await board.keys() == []


async def test_compare_and_set(durable: Any) -> None:
    board = await durable.blackboard()
    created = await board.write("k", "v1", author="a", expected_version=0)
    with pytest.raises(BlackboardConflictError, match="is at version 1"):
        await board.write("k", "clobber", author="b", expected_version=0)
    updated = await board.write("k", "v2", author="b", expected_version=created.version)
    with pytest.raises(BlackboardConflictError):
        await board.write("k", "stale", author="c", expected_version=created.version)
    assert await board.read("k") == "v2"
    assert updated.version == 2
    with pytest.raises(BlackboardConflictError, match="does not exist"):
        await board.write("new", 1, author="a", expected_version=3)
    assert issubclass(BlackboardConflictError, MessagingError)


async def test_concurrent_cas_writers_exactly_one_wins(durable: Any) -> None:
    board = await durable.blackboard()
    base = await board.write("counter", 0, author="init")

    async def bump(author: str) -> bool:
        try:
            await board.write("counter", 1, author=author, expected_version=base.version)
        except BlackboardConflictError:
            return False
        return True

    outcomes = await asyncio.gather(*(bump(f"w{index}") for index in range(10)))
    assert outcomes.count(True) == 1
    entry = await board.get("counter")
    assert entry is not None and entry.version == 2


async def test_delete_records_tombstone_and_allows_recreate(durable: Any) -> None:
    board = await durable.blackboard()
    await board.write("k", 1, author="a")
    await board.delete("k", author="b")
    assert await board.get("k") is None
    assert await board.keys() == []
    assert "k" not in await board.snapshot()
    await board.delete("k", author="b")
    await board.delete("never", author="b")
    with pytest.raises(BlackboardConflictError):
        await board.write("k", 2, author="a", expected_version=1)
    recreated = await board.write("k", 2, author="a", expected_version=0)
    assert recreated.version == 3
    assert [(e.version, e.deleted) for e in board.history("k")] == [
        (1, False),
        (2, True),
        (3, False),
    ]
    with pytest.raises(ValueError):
        await board.delete("", author="a")


async def test_keys_and_snapshot(durable: Any) -> None:
    board = await durable.blackboard()
    for key in ("task/2", "task/1", "note"):
        await board.write(key, key.upper(), author="a")
    assert await board.keys() == ["note", "task/1", "task/2"]
    assert await board.keys("task/") == ["task/1", "task/2"]
    assert await board.snapshot() == {"note": "NOTE", "task/1": "TASK/1", "task/2": "TASK/2"}


async def test_the_blackboard_history_is_bounded(durable: Any) -> None:
    board = await durable.blackboard(max_versions=2)
    for value in range(4):
        await board.write("k", value, author="a")
    assert [e.version for e in board.history("k")] == [3, 4]


async def test_watchers_see_writes_and_deletes(durable: Any) -> None:
    board = await durable.blackboard()
    seen: list[tuple[str, int, bool]] = []
    async_seen: list[int] = []

    def watcher(entry: BlackboardEntry) -> None:
        seen.append((entry.key, entry.version, entry.deleted))

    async def async_watcher(entry: BlackboardEntry) -> None:
        async_seen.append(entry.version)

    unwatch = board.watch(watcher)
    board.watch(async_watcher)
    await board.write("k", 1, author="a")
    await board.delete("k", author="a")
    unwatch()
    unwatch()
    await board.write("k", 2, author="a")
    assert seen == [("k", 1, False), ("k", 2, True)]
    assert async_seen == [1, 2, 3]


async def test_watcher_may_write_and_errors_are_swallowed(
    caplog: pytest.LogCaptureFixture, durable: Any
) -> None:
    board = await durable.blackboard()

    async def mirror(entry: BlackboardEntry) -> None:
        if entry.key == "source":
            await board.write("mirror", entry.value, author="watcher")

    def broken(entry: BlackboardEntry) -> None:
        raise RuntimeError("watcher bug")

    board.watch(broken)
    board.watch(mirror)
    await board.write("source", "x", author="a")
    assert await board.read("mirror") == "x"
    assert "Blackboard watcher failed" in caplog.text


async def test_watcher_cannot_mutate_stored_value(durable: Any) -> None:
    board = await durable.blackboard()

    def mutate(entry: BlackboardEntry) -> None:
        entry.value.append("tampered")

    board.watch(mutate)
    await board.write("k", ["clean"], author="a")
    assert await board.read("k") == ["clean"]
