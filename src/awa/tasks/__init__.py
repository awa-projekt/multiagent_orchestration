"""Task boards (extension): shared task boards for several agents.

Preview: it works and is tested, but its API may still change in a minor release.

`TaskBoard` tracks `Task`s with dependencies and atomic claiming, with model tools and
`create_task_manager_agent`; a private checklist of one agent is LangChain's `TodoListMiddleware`.
"""

from awa.errors import (
    TaskClaimError,
    TaskConflictError,
    TaskDependencyError,
    TaskError,
    TaskNotFoundError,
    TaskPermissionError,
    TaskStateError,
    TaskTimeoutError,
)
from awa.tasks.board import BaseTaskBoard, TaskBoard
from awa.tasks.manager import create_task_manager_agent
from awa.tasks.models import Task, TaskChange, TaskNote, TaskSpec, TaskStatus
from awa.tasks.tools import create_task_board_tools, create_wait_for_tasks_tool

__stability__ = "preview"

__all__ = [
    "BaseTaskBoard",
    "Task",
    "TaskBoard",
    "TaskChange",
    "TaskClaimError",
    "TaskConflictError",
    "TaskDependencyError",
    "TaskError",
    "TaskNotFoundError",
    "TaskNote",
    "TaskPermissionError",
    "TaskSpec",
    "TaskStateError",
    "TaskStatus",
    "TaskTimeoutError",
    "create_task_board_tools",
    "create_task_manager_agent",
    "create_wait_for_tasks_tool",
]
