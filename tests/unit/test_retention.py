"""Retention and erasure: policies, the thread purge, run log rows, `erase_subject`, `SubjectIndex` and erasure participants."""

from __future__ import annotations

import os
import warnings
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.memory import InMemoryStore
from typing_extensions import TypedDict

from awa.agents import Agent
from awa.automation.models import ProcessRun, RunStatus
from awa.automation.store import InMemoryRunStore, ProcessRunsErasure
from awa.errors import ConfigurationError
from awa.memory import AgentMemory, MemoryScope
from awa.memory.long_term import MemoryErasure
from awa.persistence import ArtifactRepository
from awa.persistence.artifacts import FileArtifactRepository, InMemoryArtifactRepository
from awa.persistence.erasure import (
    DEFAULT_DECISION_NAMESPACES,
    ERASURE_CATEGORIES,
    ErasureReport,
    erase_subject,
)
from awa.persistence.namespaces import namespace_label
from awa.persistence.retention import (
    SUBJECT_METADATA_KEY,
    ErasureParticipant,
    ErasureRequest,
    RetentionPolicy,
    RetentionReport,
    SubjectIndex,
    SubjectLinks,
    _purge_threads,
    apply_retention,
    thread_activity,
)
from awa.persistence.runlog import RunRef, Scope, SqliteRunLog, SystemScope
from awa.persistence.threads import BaseThreadIndex, ThreadOwner, ThreadRecord
from awa.plugins import REGISTRY
from awa.testing import FakeChatModel, FunctionChatModel, tool_call
from awa.tools import DEFAULT_APPROVAL_NAMESPACE

LATER = timedelta(days=10)


async def _threads(checkpointer: InMemorySaver, *thread_ids: str) -> None:
    agent = Agent(
        "assistant", model=FakeChatModel(["ok"], repeat_last=True), checkpointer=checkpointer
    )
    for thread_id in thread_ids:
        await agent.run("hello", thread_id=thread_id)


async def test_purge_threads_respects_older_than() -> None:
    saver = InMemorySaver()
    await _threads(saver, "t-1", "t-2")
    activity = await thread_activity(saver)
    assert set(activity) == {"t-1", "t-2"}
    assert all(when.tzinfo is not None for when in activity.values())

    # Nothing is older than a day yet.
    assert await _purge_threads(saver, older_than=timedelta(days=1)) == []
    # Ten days later both are.
    future = datetime.now(UTC) + LATER
    assert await _purge_threads(saver, older_than=timedelta(days=1), now=future, dry_run=True) == [
        "t-1",
        "t-2",
    ]
    assert set(await thread_activity(saver)) == {"t-1", "t-2"}
    assert await _purge_threads(saver, older_than=timedelta(days=1), now=future) == ["t-1", "t-2"]
    assert await thread_activity(saver) == {}


async def test_purge_threads_returns_ids_sorted_and_keeps_a_thread_at_the_cutoff() -> None:
    saver = InMemorySaver()
    names = ["t-9", "t-3", "t-7", "t-1", "t-5", "t-8", "t-2"]
    await _threads(saver, *names)
    activity = await thread_activity(saver)
    newest = max(activity.values())
    day = timedelta(days=1)

    # A thread whose latest checkpoint is exactly `older_than` old is not older than that.
    assert await _purge_threads(saver, older_than=day, now=min(activity.values()) + day) == []
    after = newest + day + timedelta(microseconds=1)
    assert await _purge_threads(saver, older_than=day, now=after, dry_run=True) == sorted(names)


async def test_a_naive_reference_time_is_utc() -> None:
    saver = InMemorySaver()
    await _threads(saver, "t-1")

    naive_future = datetime.now(UTC).replace(tzinfo=None) + LATER
    assert await _purge_threads(saver, older_than=timedelta(days=1), now=naive_future) == ["t-1"]
    naive = datetime(2026, 1, 1, 12)
    report = await apply_retention(RetentionPolicy(max_age=1), now=naive)
    assert report.ok and report.applied_at == naive.replace(tzinfo=UTC)


def test_retention_policy_ages() -> None:
    policy = RetentionPolicy(max_age=timedelta(days=30), memories=86_400 * 365)
    assert policy.age_for("threads") == timedelta(days=30)
    assert policy.age_for("memories") == timedelta(days=365)
    assert RetentionPolicy(threads=60).age_for("traces") is None
    with pytest.raises(ValueError, match="threads"):
        RetentionPolicy(threads=timedelta(0))
    with pytest.raises(ValueError, match="max_age"):
        RetentionPolicy(max_age=-1)


async def test_apply_retention_deletes_what_the_policy_no_longer_allows() -> None:
    saver, store = InMemorySaver(), InMemoryStore()
    await _threads(saver, "t-old")
    await SubjectIndex(store).link("u-1", threads=["t-old"])
    memory = AgentMemory(store, scope="user", ttl=timedelta(days=400))
    owner = MemoryScope(agent="a", user_id="u-1")
    kept = await memory.remember("recent", owner=owner)
    future = datetime.now(UTC) + LATER

    # Threads older than a week go; memories may stay a year.
    policy = RetentionPolicy(max_age=timedelta(days=7), memories=timedelta(days=365))
    preview = await apply_retention(
        policy, checkpointer=saver, store=store, now=future, dry_run=True
    )
    report = await apply_retention(policy, checkpointer=saver, store=store, now=future)

    assert isinstance(report, RetentionReport) and report.ok, report.errors
    assert report.applied_at == future
    assert preview.dry_run and preview.ids["threads"] == ["t-old"]
    # The preview counts the links of purged threads as the real run does.
    assert preview.ids == report.ids and preview.deleted == report.deleted
    assert preview.deleted["links"] == 1
    assert report.ids["threads"] == ["t-old"]
    assert report.ids["links"] == ["thread:t-old"]
    assert report.ids["memories"] == []
    assert [i.id for i in await memory.list_items(owner=owner)] == [kept.id]
    assert (await SubjectIndex(store).links("u-1")).threads == ()

    # A year and a half later the memory has expired (ttl) and is past its age.
    much_later = datetime.now(UTC) + timedelta(days=550)
    final = await apply_retention(policy, store=store, now=much_later)
    assert final.ids["memories"] == [kept.id]


async def test_memory_prune_deletes_expired_and_old_memories() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    clock = [now]
    memory = AgentMemory(scope="agent", ttl=timedelta(days=1), clock=lambda: clock[0])
    # Reading an owner's memories deletes its expired ones; prune covers every owner.
    expired = await memory.remember("expires", owner=MemoryScope(agent="idle"))
    clock[0] = now + timedelta(days=2)
    fresh = await memory.remember("fresh", owner=MemoryScope(agent="busy"))

    assert await memory.prune(dry_run=True) == [expired.id]
    naive = clock[0].replace(tzinfo=None)
    assert await memory.prune(dry_run=True, now=naive) == [expired.id]  # read as UTC
    assert await memory.prune() == [expired.id]
    assert await memory.prune(older_than=timedelta(hours=1), now=clock[0] + timedelta(hours=2)) == [
        fresh.id
    ]
    with pytest.raises(ValueError):
        await memory.prune(older_than=0)


NOW = datetime(2026, 6, 1, tzinfo=UTC)
OLD = NOW - timedelta(days=100)
FRESH = NOW - timedelta(days=1)
INVOICES, ORDERS = Scope("t1", "invoices"), Scope("t2", "orders")


async def open_runs(log: SqliteRunLog, **runs: tuple[Scope, datetime]) -> None:
    for run_id, (scope, opened) in runs.items():
        await log.open_run(scope, run_id, now=opened)


def process_run(
    run_id: str, created: datetime, status: RunStatus, thread: str | None = None
) -> ProcessRun:
    return ProcessRun(
        id=run_id,
        automation="invoices",
        status=status,
        created_at=created,
        updated_at=created,
        thread_id=thread,
    )


async def test_runs_older_than_the_age_are_deleted_per_scope_and_process_records_with_them(
    tmp_path: Path,
) -> None:
    log = SqliteRunLog(tmp_path / "awa.db")
    runs = InMemoryRunStore()
    try:
        await open_runs(
            log,
            old_a=(INVOICES, OLD),
            old_b=(ORDERS, OLD),
            fresh=(INVOICES, FRESH),
            running=(INVOICES, OLD),
        )
        await runs.create(process_run("old_a", OLD, "succeeded"))
        await runs.create(process_run("fresh", FRESH, "succeeded"))
        await runs.create(process_run("running", OLD, "waiting"))

        report = await apply_retention(
            RetentionPolicy(runs="30d"), run_log=log, run_store=runs, now=NOW
        )

        assert report.ok, report.errors
        assert sorted(report.ids["runs"]) == ["old_a", "old_b"]
        assert report.ids["process_runs"] == ["old_a"]
        assert await log.head(INVOICES, "old_a") is None
        assert await log.head(ORDERS, "old_b") is None
        assert await log.head(INVOICES, "fresh") is not None
        assert await runs.get("old_a") is None and await runs.get("fresh") is not None
    finally:
        await log.close()


async def test_the_log_of_a_run_that_is_not_finished_is_kept(tmp_path: Path) -> None:
    log = SqliteRunLog(tmp_path / "awa.db")
    runs = InMemoryRunStore()
    try:
        await open_runs(log, running=(INVOICES, OLD), done=(INVOICES, OLD))
        await runs.create(process_run("running", OLD, "running"))
        await runs.create(process_run("done", OLD, "failed"))

        report = await apply_retention(
            RetentionPolicy(runs=timedelta(days=30)), run_log=log, run_store=runs, now=NOW
        )

        assert report.ids["runs"] == ["done"]
        assert await log.head(INVOICES, "running") is not None
        assert await runs.get("running") is not None and await runs.get("done") is None
    finally:
        await log.close()


async def test_a_dry_run_deletes_nothing_and_reports_the_same_ids(tmp_path: Path) -> None:
    log = SqliteRunLog(tmp_path / "awa.db")
    runs = InMemoryRunStore()
    try:
        await open_runs(log, old=(INVOICES, OLD))
        await runs.create(process_run("old", OLD, "succeeded"))
        policy = RetentionPolicy(runs="30d")

        preview = await apply_retention(policy, run_log=log, run_store=runs, now=NOW, dry_run=True)
        assert await log.head(INVOICES, "old") is not None and await runs.get("old") is not None
        report = await apply_retention(policy, run_log=log, run_store=runs, now=NOW)

        assert preview.dry_run and preview.ids == report.ids and preview.deleted == report.deleted
    finally:
        await log.close()


async def test_the_threads_of_pruned_process_runs_go_with_them() -> None:
    saver = InMemorySaver()
    agent = Agent("assistant", model=FakeChatModel(["ok"], repeat_last=True), checkpointer=saver)
    for thread in ("t-old", "t-shared", "t-fresh"):
        await agent.run("hello", thread_id=thread)
    runs = InMemoryRunStore()
    await runs.create(process_run("old", OLD, "succeeded", thread="t-old"))
    await runs.create(process_run("old-shared", OLD, "succeeded", thread="t-shared"))
    await runs.create(process_run("fresh", FRESH, "succeeded", thread="t-shared"))
    await runs.create(process_run("fresh-2", FRESH, "succeeded", thread="t-fresh"))

    report = await apply_retention(
        RetentionPolicy(runs="30d"), checkpointer=saver, run_store=runs, now=NOW
    )

    assert report.ok, report.errors
    assert report.ids["threads"] == ["t-old"]
    assert sorted(report.ids["process_runs"]) == ["old", "old-shared"]
    assert await saver.aget_tuple({"configurable": {"thread_id": "t-old"}}) is None
    assert await saver.aget_tuple({"configurable": {"thread_id": "t-shared"}}) is not None


async def test_pruned_threads_without_a_checkpointer_are_reported() -> None:
    runs = InMemoryRunStore()
    await runs.create(process_run("old", OLD, "succeeded", thread="t-old"))

    report = await apply_retention(RetentionPolicy(runs="30d"), run_store=runs, now=NOW)

    assert any(e.startswith("threads:") and "checkpointer" in e for e in report.errors)
    assert report.ids["process_runs"] == ["old"]


class ListedRuns:
    """A run log that only lists and deletes, for the paging of the sweep."""

    def __init__(self, count: int) -> None:
        self.rows = [
            RunRef(
                run_id=f"run-{n:05d}",
                tenant="t1",
                project="invoices",
                opened_at=OLD + timedelta(seconds=n),
            )
            for n in range(count)
        ]
        self.calls = 0

    async def expired(
        self, system: SystemScope, *, before: datetime, limit: int = 1000
    ) -> list[RunRef]:
        self.calls += 1
        return [row for row in self.rows if row.opened_at < before][:limit]

    async def delete_runs(self, scope: Scope, run_ids: Sequence[str]) -> int:
        gone = set(run_ids)
        self.rows = [row for row in self.rows if row.run_id not in gone]
        return len(gone)


async def test_the_sweep_pages_until_no_old_run_is_left() -> None:
    log = ListedRuns(2500)

    report = await apply_retention(
        RetentionPolicy(runs="30d"),
        run_log=log,  # type: ignore[arg-type]
        now=NOW,
    )

    assert report.deleted["runs"] == 2500 and log.rows == []
    assert log.calls == 3


@pytest.mark.parametrize("age", [0, -1, timedelta(0), "0s"])
def test_a_run_age_must_be_positive(age: object) -> None:
    with pytest.raises(ValueError, match="runs"):
        RetentionPolicy(runs=age)  # type: ignore[arg-type]


def test_the_default_age_covers_runs_and_text_ages_are_read_once() -> None:
    policy = RetentionPolicy(max_age="90d", memories=3600)

    assert policy.age_for("runs") == timedelta(days=90)
    assert policy.age_for("memories") == timedelta(hours=1)
    assert RetentionPolicy().age_for("runs") is None


class Backends:
    def __init__(self) -> None:
        self.store = InMemoryStore()
        self.checkpointer = InMemorySaver()
        self.artifacts = InMemoryArtifactRepository()
        self.memory = AgentMemory(self.store, scope="user")

    def erase(self, subject: str = "u-42", **kwargs: Any) -> Any:
        return erase_subject(
            subject,
            store=self.store,
            checkpointer=self.checkpointer,
            artifacts=self.artifacts,
            **kwargs,
        )


async def _thread(checkpointer: InMemorySaver, thread_id: str, text: str) -> str:
    agent = Agent("assistant", model=FakeChatModel([text]), checkpointer=checkpointer)
    result = await agent.run(text, thread_id=thread_id)
    assert result.thread_id is not None
    return result.thread_id


async def _populate(b: Backends) -> None:
    alice = MemoryScope(agent="assistant", user_id="u-42")
    bob = MemoryScope(agent="assistant", user_id="u-7")
    await b.memory.remember("Alice prefers EUR", owner=alice)
    await b.memory.remember("Bob prefers USD", owner=bob)
    # Per-user parts of agent scope, and a global memory saved during Alice's run.
    await AgentMemory(b.store, scope="agent").remember("Alice likes tea", owner=alice)
    shared = AgentMemory(b.store, scope="global")
    await shared.remember("Alice's account is on hold", owner=alice)
    await shared.remember("Office closes at 6", owner=MemoryScope(agent="assistant"))
    await shared.remember("Bob owes 10 EUR", owner=alice, subject="u-7")

    await b.store.aput(DEFAULT_APPROVAL_NAMESPACE, "refund:u-42", {"action": "approve"})
    await b.store.aput(DEFAULT_APPROVAL_NAMESPACE, "refund:u-420", {"action": "approve"})
    await b.store.aput(DEFAULT_APPROVAL_NAMESPACE, "u-42/export", {"action": "reject"})

    alice_thread = await _thread(b.checkpointer, "t-alice", "I am Alice")
    await _thread(b.checkpointer, "t-bob", "I am Bob")
    await b.artifacts.put("contract:alice", {"text": "..."})
    await b.artifacts.put("export:u-42:2026", {"rows": 3})
    await b.artifacts.put("export:u-7:2026", {"rows": 1})

    await SubjectIndex(b.store).link("u-42", threads=[alice_thread], artifacts=["contract:alice"])


async def test_erases_everything_of_the_subject_and_nothing_else() -> None:
    b = Backends()
    await _populate(b)

    report = await b.erase(artifact_prefixes=["export:{subject}:"])

    assert isinstance(report, ErasureReport) and report.ok, report.errors
    assert report.subject_id == "u-42" and not report.dry_run
    assert set(report.deleted) == set(ERASURE_CATEGORIES)
    assert (
        report.deleted.items()
        >= {
            "memories": 3,
            "approvals": 2,
            "store_items": 0,
            "threads": 1,
            "artifacts": 2,
            "traces": 0,
            "links": 2,
        }.items()
    )
    assert sorted(report.ids["approvals"]) == ["refund:u-42", "u-42/export"]
    assert sorted(report.ids["artifacts"]) == ["contract:alice", "export:u-42:2026"]
    assert report.ids["threads"] == ["t-alice"]
    assert report.total == 10

    # Everyone else's data is untouched.
    bob = MemoryScope(agent="assistant", user_id="u-7")
    assert [i.content for i in await b.memory.list_items(owner=bob)] == ["Bob prefers USD"]
    remaining = AgentMemory(b.store, scope="global")
    assert sorted(i.content for i in await remaining.list_items(owner=bob)) == [
        "Bob owes 10 EUR",
        "Office closes at 6",
    ]
    assert await b.store.aget(DEFAULT_APPROVAL_NAMESPACE, "refund:u-420") is not None
    assert await b.checkpointer.aget_tuple({"configurable": {"thread_id": "t-alice"}}) is None
    assert await b.checkpointer.aget_tuple({"configurable": {"thread_id": "t-bob"}}) is not None
    assert await b.artifacts.list_refs() == ["export:u-7:2026"]
    assert not await SubjectIndex(b.store).links("u-42")


async def test_erasure_is_idempotent() -> None:
    b = Backends()
    await _populate(b)
    await b.erase()

    again = await b.erase()

    assert again.ok and again.total == 0
    assert again.deleted == dict.fromkeys(ERASURE_CATEGORIES, 0)


async def test_dry_run_reports_without_deleting() -> None:
    b = Backends()
    await _populate(b)

    preview = await b.erase(dry_run=True)
    real = await b.erase()

    assert preview.dry_run and preview.deleted == real.deleted
    assert sorted(preview.ids["memories"]) == sorted(real.ids["memories"])


async def test_memories_of_other_stores_and_custom_namespaces() -> None:
    store, other_store = InMemoryStore(), InMemoryStore()
    custom = AgentMemory(other_store, namespace=("crm", "memory"), scope="team", team="desk")
    owner = MemoryScope(agent="triage", user_id="jane.doe@example.com", team="desk")
    await custom.remember("Jane prefers email", owner=owner)
    jane = namespace_label("jane.doe@example.com")
    await store.aput(("crm", "profiles", jane), "profile", {"name": "Jane"})
    await store.aput(("crm", "profiles", jane, "notes"), "n1", {"t": "x"})
    await store.aput(("crm", "profiles", "joe"), "profile", {"name": "Joe"})
    await store.aput(("crm", "raw", "u-1"), "profile", {"name": "Uma"})

    report = await erase_subject(
        "jane.doe@example.com",
        store=store,
        memory=[custom, custom],
        namespaces=[("crm", "profiles", "{subject_label}")],
    )

    assert report.deleted["memories"] == 1 and report.deleted["store_items"] == 2
    assert sorted(report.ids["store_items"]) == [
        f"crm/profiles/{jane}/notes/n1",
        f"crm/profiles/{jane}/profile",
    ]
    raw = await erase_subject("u-1", store=store, namespaces=[("crm", "raw", "{subject}")])
    assert raw.ids["store_items"] == ["crm/raw/u-1/profile"]
    assert await store.aget(("crm", "profiles", "joe"), "profile") is not None
    assert await custom.list_items(owner=owner) == []


async def test_threads_tagged_in_checkpoint_metadata_are_found() -> None:
    class State(TypedDict):
        value: int

    graph = (
        StateGraph(State)
        .add_node("step", lambda state: {"value": state["value"] + 1})
        .add_edge(START, "step")
        .add_edge("step", END)
        .compile(checkpointer=InMemorySaver())
    )
    checkpointer = graph.checkpointer
    assert isinstance(checkpointer, InMemorySaver)
    await graph.ainvoke(
        {"value": 1},
        {"configurable": {"thread_id": "g-1"}, "metadata": {SUBJECT_METADATA_KEY: "u-42"}},
    )
    await graph.ainvoke({"value": 1}, {"configurable": {"thread_id": "g-2"}})

    report = await erase_subject("u-42", checkpointer=checkpointer, thread_ids=["unknown"])

    assert report.ids["threads"] == ["g-1"]
    assert await checkpointer.aget_tuple({"configurable": {"thread_id": "g-2"}}) is not None


async def test_linked_data_without_its_backend_is_reported_not_skipped() -> None:
    store = InMemoryStore()
    await SubjectIndex(store).link("u-42", threads=["t-1"], artifacts=["a-1"])

    report = await erase_subject("u-42", store=store)

    assert not report.ok
    assert any("thread(s)" in error and "checkpointer" in error for error in report.errors)
    assert any("artifact(s)" in error for error in report.errors)


async def test_a_failing_backend_is_reported_and_the_rest_is_erased() -> None:
    class ReadOnlyArtifacts(ArtifactRepository):
        async def put(self, ref: str, value: Any) -> None:
            raise NotImplementedError

        async def get(self, ref: str) -> Any:
            raise KeyError(ref)

        async def exists(self, ref: str) -> bool:
            return True

    store = InMemoryStore()
    await AgentMemory(store, scope="user").remember(
        "x", owner=MemoryScope(agent="a", user_id="u-42")
    )
    await SubjectIndex(store).link("u-42", artifacts=["a-1"])

    report = await erase_subject("u-42", store=store, artifacts=ReadOnlyArtifacts())

    assert report.deleted["memories"] == 1
    assert report.errors == [
        "artifact a-1: NotImplementedError: ReadOnlyArtifacts does not implement delete()"
    ]


async def test_memories_saved_by_the_agent_carry_the_subject() -> None:
    memory = AgentMemory(scope="global")
    model = FunctionChatModel(
        lambda call: "ok" if call.index else tool_call("remember", {"content": "Likes tea"})
    )
    await Agent("assistant", model=model, memory=memory).run("x", deps={"user_id": "u-42"})

    [item] = await memory.list_items(owner=MemoryScope(agent="assistant"))
    assert item.subject == "u-42"
    assert await memory.erase_subject("u-42") == [item.id]
    assert await memory.list_items(owner=MemoryScope(agent="assistant")) == []


async def test_erasing_a_subject_leaves_an_agent_named_user_alone() -> None:
    memory = AgentMemory(scope="agent")
    kept = await memory.remember("Office opens at 8", owner=MemoryScope(agent="user"))
    gone = await memory.remember("Likes tea", owner=MemoryScope(agent="a", user_id="anonymous"))

    assert await memory.erase_subject("anonymous") == [gone.id]
    assert [i.id for i in await memory.list_items(owner=MemoryScope(agent="user"))] == [kept.id]


async def test_subject_index_links_are_idempotent_and_removable() -> None:
    index = SubjectIndex(InMemoryStore())
    await index.link("u-1", threads=["t-2", "t-1"])
    await index.link("u-1", threads=["t-1"], artifacts=["a"])
    await index.link("u-2", threads=["t-1"])

    assert await index.links("u-1") == SubjectLinks(threads=("t-1", "t-2"), artifacts=("a",))
    await index.unlink("u-1", threads=["t-2", "unknown"])
    assert (await index.links("u-1")).threads == ("t-1",)
    assert await index.forget_thread("t-1") == 2
    assert not (await index.links("u-2"))
    assert await index.forget("u-1") == 1
    assert not await index.links("u-1")
    with pytest.raises(ValueError):
        await index.link("", threads=["t"])
    with pytest.raises(ValueError):
        await index.link("u-1", threads=[""])
    with pytest.raises(ValueError):
        await index.link("u-3", threads=["t-9"], artifacts=[""])
    assert not await index.links("u-3")  # nothing linked when one id is invalid


async def test_invalid_subject_is_rejected() -> None:
    with pytest.raises(ValueError, match="subject_id"):
        await erase_subject("")


def test_default_decision_namespaces_match_the_approval_modules() -> None:
    assert DEFAULT_DECISION_NAMESPACES == (DEFAULT_APPROVAL_NAMESPACE,)


async def test_file_artifacts_support_erasure(tmp_path: Path) -> None:
    artifacts = FileArtifactRepository(tmp_path)
    await artifacts.put("export:u-42:1", {"x": 1})
    await artifacts.put("export:u-7:1", {"x": 2})

    report = await erase_subject(
        "u-42", artifacts=artifacts, artifact_prefixes=["export:{subject}:"]
    )

    assert report.ids["artifacts"] == ["export:u-42:1"]
    assert await artifacts.list_refs() == ["export:u-7:1"]


NO_BACKENDS: dict[str, Any] = {
    "store": None,
    "run_log": None,
    "run_store": None,
    "settings": None,
    "memory": None,
}


class Recorder:
    category = "crm"

    def __init__(self) -> None:
        self.requests: list[ErasureRequest] = []

    async def erase(self, request: ErasureRequest) -> list[str]:
        self.requests.append(request)
        return [f"crm:{request.subject_id}"]


class Raising:
    category = "broken"

    async def erase(self, request: ErasureRequest) -> list[str]:
        raise RuntimeError("the CRM is down")


class IndexWithoutOwners(BaseThreadIndex):
    async def claim(self, system: str, thread_id: str, owner: ThreadOwner) -> ThreadOwner:
        return owner

    async def owner(self, system: str, thread_id: str) -> ThreadOwner | None:
        return None

    async def threads(
        self, system: str, *, tenant: str | None, principal: str | None = None, limit: int = 50
    ) -> list[ThreadRecord]:
        return []

    async def forget(self, system: str, thread_id: str) -> None:
        return None


def owner(user: str) -> MemoryScope:
    return MemoryScope(agent="a", user_id=user)


def test_awa_registers_its_own_participants_as_erasure_plugins() -> None:
    found = REGISTRY.loadable("erasure")

    assert found["memories"] is MemoryErasure
    assert found["process_runs"] is ProcessRunsErasure
    assert MemoryErasure.category == "memories"
    assert ProcessRunsErasure.category == "process_runs"
    assert MemoryErasure.create(**NO_BACKENDS) is None
    assert ProcessRunsErasure.create(**NO_BACKENDS) is None


async def test_a_project_adds_a_participant_in_its_plugins_file(tmp_path: Path) -> None:
    (tmp_path / "plugins.py").write_text(
        "from awa.plugins import register\n\n\n"
        "class Crm:\n"
        "    category = 'crm'\n\n"
        "    @classmethod\n"
        "    def create(cls, **backends):\n"
        "        return cls()\n\n"
        "    async def erase(self, request):\n"
        "        return [f'crm:{request.subject_id}']\n\n\n"
        "register('erasure', 'crm', Crm)\n",
        encoding="utf-8",
    )
    with REGISTRY.preserved(), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        report = await erase_subject("u-42", store=InMemoryStore(), project=tmp_path)

    assert report.ok, report.errors
    assert report.ids["crm"] == ["crm:u-42"] and report.deleted["crm"] == 1
    assert set(ERASURE_CATEGORIES) <= set(report.deleted)


async def test_explicit_participants_replace_the_discovered_ones() -> None:
    store = InMemoryStore()
    memory = AgentMemory(store, scope="user")
    await memory.remember("kept", owner=owner("u-42"))
    recorder = Recorder()

    report = await erase_subject("u-42", store=store, participants=[recorder])

    assert report.ids["crm"] == ["crm:u-42"] and report.deleted["memories"] == 0
    assert [r.subject_id for r in recorder.requests] == ["u-42"]
    assert len(await memory.list_items(owner=owner("u-42"))) == 1


async def test_a_raising_participant_lands_in_errors_while_the_others_run() -> None:
    recorder = Recorder()
    participants: list[ErasureParticipant] = [Raising(), recorder]

    report = await erase_subject("u-42", participants=participants)

    assert not report.ok
    assert any(e.startswith("broken: RuntimeError") and "CRM is down" in e for e in report.errors)
    assert report.ids["crm"] == ["crm:u-42"]


async def test_the_memory_argument_gives_what_the_memory_participant_gives() -> None:
    via_argument, via_participant = InMemoryStore(), InMemoryStore()
    for store in (via_argument, via_participant):
        await AgentMemory(store, scope="user").remember("one", owner=owner("u-42"))
        await AgentMemory(store, scope="user").remember("two", owner=owner("u-42"))

    first = await erase_subject("u-42", memory=AgentMemory(via_argument, scope="user"))
    created = MemoryErasure.create(
        **{**NO_BACKENDS, "memory": AgentMemory(via_participant, scope="user")}
    )
    assert created is not None
    second = await erase_subject("u-42", participants=[created])

    assert first.deleted["memories"] == second.deleted["memories"] == 2
    assert len(first.ids["memories"]) == len(second.ids["memories"]) == 2


async def test_the_same_memory_is_erased_once_however_it_is_passed() -> None:
    store = InMemoryStore()
    memory = AgentMemory(store, scope="user")
    await memory.remember("one", owner=owner("u-42"))

    report = await erase_subject("u-42", store=store, memory=[memory, memory])

    assert report.deleted["memories"] == 1


async def test_a_thread_index_without_owned_threads_is_reported() -> None:
    report = await erase_subject("u-42", threads_index=IndexWithoutOwners())

    assert any(e.startswith("served_threads:") for e in report.errors)
    assert report.deleted["served_threads"] == 0


async def test_process_runs_of_a_person_are_deleted_through_the_run_store() -> None:
    runs = InMemoryRunStore()
    await runs.create(ProcessRun(id="r-1", automation="a", actor="u-42", thread_id="t-1"))
    await runs.create(ProcessRun(id="r-2", automation="a", actor="u-7", thread_id="t-2"))

    preview = await erase_subject("u-42", run_store=runs, dry_run=True)
    report = await erase_subject("u-42", run_store=runs)

    assert preview.ids["process_runs"] == report.ids["process_runs"] == ["r-1"]
    assert await runs.get("r-1") is None and await runs.get("r-2") is not None


@pytest.mark.parametrize(
    "arguments",
    [
        {"namespaces": [("crm", "profiles")]},
        {"namespaces": [()]},
        {"artifact_prefixes": ["export:"]},
    ],
)
async def test_an_unsafe_template_is_refused_before_anything_is_deleted(
    arguments: dict[str, Any],
) -> None:
    store = InMemoryStore()
    memory = AgentMemory(store, scope="user")
    await memory.remember("kept", owner=owner("u-42"))

    with pytest.raises(ConfigurationError):
        await erase_subject("u-42", store=store, **arguments)

    assert len(await memory.list_items(owner=owner("u-42"))) == 1


async def test_a_run_log_needs_its_scopes() -> None:
    with pytest.raises(ConfigurationError, match="scopes"):
        await erase_subject("u-42", run_log=object())  # type: ignore[arg-type]


async def test_the_retention_reports_are_stable(tmp_path: Path) -> None:
    from awa.observability import TraceStore
    from awa.persistence.runlog import Scope, SqliteRunLog
    from awa.testing.contracts.erasure import NOW, _checkpoint

    log, saver, store = SqliteRunLog(tmp_path / "awa.db"), InMemorySaver(), InMemoryStore()
    runs, traces = InMemoryRunStore(), TraceStore(tmp_path / "traces")
    for thread_id in ("t-old", "t-run"):
        await _checkpoint(saver, thread_id, None)
    await SubjectIndex(store).link("u-1", threads=["t-old", "t-kept"])
    for run_id, status, thread in (("old", "succeeded", "t-run"), ("held", "running", None)):
        await log.open_run(Scope("acme", "invoices"), run_id, now=NOW)
        await runs.create(
            ProcessRun(
                id=run_id,
                automation="invoices",
                status=status,  # type: ignore[arg-type]
                created_at=NOW,
                updated_at=NOW,
                thread_id=thread,
            )
        )
    for age, name in enumerate(("z", "y", "x")):
        traces.recorder(name).close("ok")
        stamp = (NOW + timedelta(days=60)).timestamp() - age * 60
        os.utime(traces.path(name), (stamp, stamp))
    policy = RetentionPolicy(max_age="30d", max_trace_files=2)
    backends: dict[str, Any] = {
        "checkpointer": saver,
        "store": store,
        "run_log": log,
        "run_store": runs,
        "traces": traces,
        "now": NOW + timedelta(days=60),
    }
    try:
        dry = await apply_retention(policy, dry_run=True, **backends)
        real = await apply_retention(policy, **backends)
        again = await apply_retention(policy, **backends)
        await runs.create(
            ProcessRun(
                id="late",
                automation="invoices",
                status="succeeded",
                created_at=NOW,
                updated_at=NOW,
                thread_id="t-gone",
            )
        )
        unchecked = await apply_retention(policy, **{**backends, "checkpointer": None})
    finally:
        await log.close()

    def pinned(report: RetentionReport) -> dict[str, Any]:
        data = report.model_dump(mode="json")
        assert data.pop("deleted") == {key: len(value) for key, value in data["ids"].items()}
        return data

    applied = "2026-05-01T09:00:00Z"
    swept = {
        "threads": ["t-old", "t-run"],
        "memories": [],
        "runs": ["old"],
        "traces": ["x.jsonl"],
        "links": ["thread:t-old"],
        "process_runs": ["old"],
    }
    empty = dict.fromkeys(swept, [])
    assert pinned(dry) == {"applied_at": applied, "dry_run": True, "ids": swept, "errors": []}
    assert pinned(real) == {"applied_at": applied, "dry_run": False, "ids": swept, "errors": []}
    assert pinned(again) == {"applied_at": applied, "dry_run": False, "ids": empty, "errors": []}
    assert pinned(unchecked) == {
        "applied_at": applied,
        "dry_run": False,
        "ids": {**empty, "process_runs": ["late"]},
        "errors": ["threads: 1 thread(s) of pruned runs were not purged; pass the checkpointer"],
    }


def _pinned(report: ErasureReport) -> dict[str, Any]:
    """The report as data, memory ids (random) replaced by their count."""

    data = report.model_dump(mode="json")
    assert data.pop("deleted") == {key: len(value) for key, value in data["ids"].items()}
    data["ids"] = {key: value for key, value in data["ids"].items() if value}
    if "memories" in data["ids"]:
        data["ids"]["memories"] = len(data["ids"]["memories"])
    return data


async def test_the_erasure_reports_are_stable(tmp_path: Path) -> None:
    from _helpers.erasure import WithProcessRuns
    from awa.automation import SqliteRunStore
    from awa.observability import TraceStore
    from awa.persistence.runlog import Scope, SqliteDatabase, SqliteRunLog
    from awa.persistence.threads import SqliteThreadIndex
    from awa.testing.contracts.erasure import KEY, ErasureStack

    database = SqliteDatabase(tmp_path / "awa.db")
    traces = TraceStore(tmp_path / "traces")
    stack = ErasureStack(
        store=InMemoryStore(),
        checkpointer=InMemorySaver(),
        artifacts=InMemoryArtifactRepository(),
        run_log=SqliteRunLog(database, pseudonym=KEY),
        threads_index=SqliteThreadIndex(database),
        run_store=SqliteRunStore(database),
    )
    contract = WithProcessRuns()
    try:
        await contract.seed(stack, "alice")
        await contract.seed(stack, "bob")
        for name, meta in (("a1", {"actor": "alice"}), ("a2", {"thread_id": "thread-alice"})):
            traces.recorder(name, **meta).close("ok")
        traces.recorder("b1", actor="bob").close("ok")

        dry = await contract.erase(stack, "alice", traces=traces, dry_run=True)
        partial = await contract.erase(stack, "alice", traces=traces, artifacts=None)
        rest = await contract.erase(stack, "alice", traces=traces)
        again = await contract.erase(stack, "alice", traces=traces)
        unkeyed = await erase_subject(
            "bob", store=stack.store, run_log=SqliteRunLog(database), scopes=[Scope("acme", "x")]
        )
    finally:
        database.close()

    everything = {
        "approvals": ["refund:alice"],
        "artifacts": ["contract:alice", "export:alice:2026"],
        "links": ["thread:thread-alice", "artifact:contract:alice"],
        "memories": 1,
        "process_runs": ["proc-alice"],
        "runs": ["run-alice"],
        "served_threads": ["desk/served-alice"],
        "store_items": ["crm/profiles/alice/profile"],
        "threads": ["thread-alice", "served-alice"],
        "traces": ["a1.jsonl", "a2.jsonl"],
    }
    assert _pinned(dry) == {"subject_id": "alice", "dry_run": True, "ids": everything, "errors": []}
    without_artifacts = {k: v for k, v in everything.items() if k != "artifacts"}
    assert _pinned(partial) == {
        "subject_id": "alice",
        "dry_run": False,
        "ids": {**without_artifacts, "links": ["thread:thread-alice"]},
        "errors": [
            "artifacts: 1 artifact(s) of the subject were not erased; pass the artifact repository"
        ],
    }
    assert _pinned(rest) == {
        "subject_id": "alice",
        "dry_run": False,
        "ids": {"artifacts": everything["artifacts"], "links": ["artifact:contract:alice"]},
        "errors": [],
    }
    assert _pinned(again) == {"subject_id": "alice", "dry_run": False, "ids": {}, "errors": []}
    assert _pinned(unkeyed) == {
        "subject_id": "bob",
        "dry_run": False,
        "ids": {"approvals": ["refund:bob"], "memories": 1},
        "errors": [
            "runs: no pseudonym key: runs cannot be attributed",
            "threads: 1 thread(s) of the subject were not erased; pass the checkpointer",
            "artifacts: 1 artifact(s) of the subject were not erased; pass the artifact repository",
        ],
    }
