"""Invariants of the in-memory `TaskBoard` under random operation sequences.

A Hypothesis state machine drives a board with random creates, claims,
updates, completions, failures, releases and cancellations by random agents.
After every step it checks that no task is claimed twice, that automatic
blocking always matches the state of the dependencies, and that versions only
grow. Refused operations must raise a `TaskError`, never anything else.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TypeVar

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from awa.contracts.communication import MessagePriority
from awa.errors import TaskClaimError, TaskError
from awa.tasks import TaskBoard, TaskChange, TaskStatus

T = TypeVar("T")

AGENTS = st.sampled_from(["lead", "ana", "bo", "cy"])
CAPABILITIES = st.lists(st.sampled_from(["research", "write"]), unique=True, max_size=2)


class TaskBoardMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.board = TaskBoard()
        self.versions: dict[str, int] = {}
        self.changes: list[TaskChange] = []
        self.board.watch(self.changes.append)

    def teardown(self) -> None:
        self.loop.close()

    def run(self, operation: Coroutine[object, object, T]) -> T | None:
        """Run a board operation; a refusal must be a `TaskError`."""

        try:
            return self.loop.run_until_complete(operation)
        except TaskError:
            return None

    # ------------------------------------------------------------------ rules

    @rule(
        data=st.data(),
        creator=AGENTS,
        owner=st.none() | AGENTS,
        capabilities=CAPABILITIES,
        priority=st.sampled_from(MessagePriority),
    )
    def create(
        self,
        data: st.DataObject,
        creator: str,
        owner: str | None,
        capabilities: list[str],
        priority: MessagePriority,
    ) -> None:
        existing = [task.id for task in self.board.list()]
        depends_on = data.draw(st.lists(st.sampled_from(existing), max_size=3)) if existing else []
        task = self.run(
            self.board.create(
                "work",
                created_by=creator,
                depends_on=depends_on,
                owner=owner,
                required_capabilities=capabilities,
                priority=priority,
            )
        )
        assert task is not None, "creating a task with known dependencies must succeed"
        assert task.version == 1

    @precondition(lambda self: len(self.board) > 0)
    @rule(data=st.data(), agent=AGENTS, capabilities=st.none() | CAPABILITIES)
    def claim(self, data: st.DataObject, agent: str, capabilities: list[str] | None) -> None:
        task_id = data.draw(st.sampled_from([task.id for task in self.board.list()]))
        before = self.board.get(task_id)
        ready = {task.id for task in self.board.ready(agent, capabilities)}
        try:
            claimed = self.loop.run_until_complete(
                self.board.claim(task_id, agent, capabilities=capabilities)
            )
        except TaskClaimError:
            assert task_id not in ready, "a ready task must be claimable"
            return
        assert task_id in ready, "only a ready task may be claimed"
        assert before.status is TaskStatus.PENDING
        assert before.owner in (None, agent)
        assert claimed.owner == agent
        assert claimed.status is TaskStatus.IN_PROGRESS

    @rule(agent=AGENTS, capabilities=st.none() | CAPABILITIES)
    def claim_next(self, agent: str, capabilities: list[str] | None) -> None:
        ready = self.board.ready(agent, capabilities)
        claimed = self.run(self.board.claim_next(agent, capabilities=capabilities))
        if not ready:
            assert claimed is None
        else:
            assert claimed is not None and claimed.id == ready[0].id

    @precondition(lambda self: len(self.board) > 0)
    @rule(data=st.data(), author=AGENTS, action=st.sampled_from(["complete", "fail", "release"]))
    def finish(self, data: st.DataObject, author: str, action: str) -> None:
        task_id = data.draw(st.sampled_from([task.id for task in self.board.list()]))
        if action == "complete":
            self.run(self.board.complete(task_id, author=author, result="done"))
        elif action == "fail":
            self.run(self.board.fail(task_id, author=author, reason="broken"))
        else:
            self.run(self.board.release(task_id, author=author))

    @precondition(lambda self: len(self.board) > 0)
    @rule(data=st.data(), author=AGENTS)
    def cancel(self, data: st.DataObject, author: str) -> None:
        task_id = data.draw(st.sampled_from([task.id for task in self.board.list()]))
        self.run(self.board.cancel(task_id, author=author, reason="not needed"))

    @precondition(lambda self: len(self.board) > 0)
    @rule(
        data=st.data(),
        author=AGENTS,
        status=st.none() | st.sampled_from(TaskStatus),
        note=st.none() | st.just("a note"),
        stale=st.booleans(),
    )
    def update(
        self,
        data: st.DataObject,
        author: str,
        status: TaskStatus | None,
        note: str | None,
        stale: bool,
    ) -> None:
        task_id = data.draw(st.sampled_from([task.id for task in self.board.list()]))
        if status is None and note is None:
            note = "a note"
        version = self.board.get(task_id).version - (1 if stale else 0)
        updated = self.run(
            self.board.update(
                task_id, author=author, status=status, note=note, expected_version=version
            )
        )
        if stale:
            assert updated is None, "a stale expected_version must be refused"

    # ------------------------------------------------------------------ invariants

    @invariant()
    def versions_only_grow(self) -> None:
        for task in self.board.list():
            assert task.version >= self.versions.get(task.id, 1)
            self.versions[task.id] = task.version

    @invariant()
    def no_task_is_claimed_twice(self) -> None:
        for task in self.board.list():
            if task.status is TaskStatus.IN_PROGRESS:
                assert task.owner is not None
        claims: dict[str, str] = {}
        for change in self.changes:
            if change.kind == "claimed":
                assert change.task.id not in claims or change.task.owner == claims[change.task.id]
                claims[change.task.id] = change.task.owner or ""
            elif change.kind == "released":
                claims.pop(change.task.id, None)

    @invariant()
    def blocking_matches_the_dependencies(self) -> None:
        tasks = {task.id: task for task in self.board.list()}

        def broken(task_id: str) -> bool:
            task = tasks[task_id]
            return task.status in (TaskStatus.FAILED, TaskStatus.CANCELLED) or (
                task.status is TaskStatus.BLOCKED and bool(task.blocked_by)
            )

        for task in tasks.values():
            expected = tuple(d for d in task.depends_on if broken(d))
            if task.status is TaskStatus.PENDING:
                assert expected == (), f"{task.id} is pending although {expected} broke"
            if task.blocked_by and not task.status.is_terminal:
                assert task.status is TaskStatus.BLOCKED
                assert task.blocked_by == expected
            if task.status in (TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED):
                assert all(tasks[d].status is TaskStatus.COMPLETED for d in task.depends_on)

    @invariant()
    def ready_tasks_are_claimable(self) -> None:
        for task in self.board.ready():
            assert task.status is TaskStatus.PENDING
            assert all(self.board.get(d).status is TaskStatus.COMPLETED for d in task.depends_on)

    @invariant()
    def summary_and_snapshot_are_consistent(self) -> None:
        assert sum(self.board.summary().values()) == len(self.board)
        # `from_dict` may reorder tasks (dependencies first), so compare by id.
        restored = TaskBoard.from_dict(self.board.to_dict())
        assert {t.id: t for t in restored.list()} == {t.id: t for t in self.board.list()}


TaskBoardMachine.TestCase.settings = settings(
    max_examples=max(10, settings().max_examples // 2), stateful_step_count=30
)
test_task_board_invariants = TaskBoardMachine.TestCase
