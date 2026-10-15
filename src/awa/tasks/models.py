"""Data models of the task board: tasks, their status, notes and change records."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from awa.contracts.communication import MessagePriority


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    """Lifecycle status of a task.

    `blocked` means a dependency failed or was cancelled (`Task.blocked_by` names it; cleared on
    retry) or the owner reported being stuck (`blocked_by` empty). `completed`, `failed` and
    `cancelled` are terminal.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """Whether the status is `completed`, `failed` or `cancelled`."""

        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)


class TaskNote(BaseModel):
    """A comment an agent (`author`) attached to a task."""

    model_config = ConfigDict(frozen=True)

    author: str
    text: str
    created_at: datetime = Field(default_factory=_utcnow)


class Task(BaseModel):
    """One unit of work on a `TaskBoard`.

    Attributes:
        id: Board-unique id (`task_<n>`).
        description: Self-contained brief: inputs, expected output, done criteria.
        owner: Agent working on (or assigned to) the task, if any.
        blocked_by: Dependencies that failed or were cancelled (or are blocked by one), when the
            board blocked this task automatically.
        priority: Higher priorities are offered first.
        result: The result of a completed task, or why it failed or was cancelled.
        version: Starts at 1 and increases with every change.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    owner: str | None = None
    created_by: str = Field(min_length=1)
    depends_on: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    priority: MessagePriority = MessagePriority.NORMAL
    required_capabilities: tuple[str, ...] = ()
    result: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    notes: tuple[TaskNote, ...] = ()
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @field_validator("title", mode="before")
    @classmethod
    def _one_line(cls, value: Any) -> Any:
        # The rendered board is one line per task: a title cannot add lines to it.
        return " ".join(value.split()) if isinstance(value, str) else value

    version: int = Field(default=1, ge=1)

    @property
    def is_settled(self) -> bool:
        """Whether the task will not progress on its own (terminal, or blocked by a dependency)."""

        return self.status.is_terminal or (
            self.status is TaskStatus.BLOCKED and bool(self.blocked_by)
        )


class TaskSpec(BaseModel):
    """One task of a batch created with `TaskBoard.create_many`.

    Attributes:
        key: Batch-local name other tasks of the batch use in `depends_on`.
        depends_on: Keys of tasks in the same batch, or ids of existing tasks.
    """

    model_config = ConfigDict(frozen=True)

    key: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    depends_on: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    owner: str | None = None
    priority: MessagePriority = MessagePriority.NORMAL
    data: dict[str, Any] = Field(default_factory=dict)


TaskChangeKind = Literal[
    "created",
    "claimed",
    "updated",
    "completed",
    "failed",
    "released",
    "cancelled",
    "blocked",
    "unblocked",
]
"""What happened to a task. `blocked`/`unblocked` are automatic changes
caused by a dependency failing or being retried."""


class TaskChange(BaseModel):
    """A change on the task board (`kind`), with the task after it, delivered to watchers."""

    model_config = ConfigDict(frozen=True)

    kind: TaskChangeKind
    task: Task


__all__ = ["Task", "TaskChange", "TaskChangeKind", "TaskNote", "TaskSpec", "TaskStatus"]
