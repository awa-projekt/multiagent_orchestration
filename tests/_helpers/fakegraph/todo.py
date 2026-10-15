"""Microsoft To Do plug-in (minimal): lists and tasks, delegated permissions only.

Routes, each under `/users/{user-id}` and `/me`: `GET /todo/lists`, `GET /todo/lists/{id}/tasks`,
`POST /todo/lists/{id}/tasks`, `PATCH /todo/lists/{id}/tasks/{id}`.

The reference pages list no application permission for To Do operations ("Not supported"), so an
app token is refused on every route. `$filter` and `$orderby` answer 501 (not
implemented).

State is `graph.store("todo")`: `add_list(user, name)`, `add_task(list, title)`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from _helpers.fakegraph.core import Ctx, GraphError, Perm, Plugin, Reply, new_guid
from _helpers.fakegraph.identity import resolve_user
from _helpers.fakegraph.odata import collection

plugin = Plugin("todo")

READ = Perm(user=("Tasks.Read", "Tasks.ReadWrite"))
WRITE = Perm(user=("Tasks.ReadWrite",))
STATUSES = ("notStarted", "inProgress", "completed", "waitingOnOthers", "deferred")

R_NO_APP = plugin.rule(
    "todo.application_permissions_not_supported",
    "Every To Do route refuses an app token with 403 ('not supported for application "
    "permissions'); only delegated Tasks.Read / Tasks.ReadWrite work, and a person's lists "
    "belong to that person.",
    source="learn.microsoft.com/graph/api/todotasklist-post-tasks",
    source_date="2025-12-03",
)


@dataclass
class TodoList:
    id: str
    owner: str
    name: str
    tasks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class TodoStore:
    graph: Any
    lists: dict[str, TodoList] = field(default_factory=dict)
    counter: int = 0

    def add_list(self, user: Any, name: str) -> TodoList:
        found = TodoList(new_guid(f"list:{user.id}:{name}"), user.id, name)
        if found.id in self.lists:
            raise ValueError(f"a to-do list named {name!r} exists already")
        self.lists[found.id] = found
        return found

    def add_task(self, todo: TodoList, title: str, *, status: str = "notStarted") -> dict[str, Any]:
        self.counter += 1
        now = self.graph.clock.iso()
        task = {
            "id": new_guid(f"task:{self.counter}"),
            "title": title,
            "status": status,
            "importance": "normal",
            "isReminderOn": False,
            "createdDateTime": now,
            "lastModifiedDateTime": now,
            "body": {"content": "", "contentType": "text"},
        }
        todo.tasks.append(task)
        return task


@plugin.setup
def _setup(graph: Any) -> TodoStore:
    return TodoStore(graph)


def _owner(ctx: Ctx) -> Any:
    user = resolve_user(ctx, ctx.params.get("user_id", "me"), family="graph")
    principal = ctx.principal
    if principal is not None and principal.user_id != user.id:
        raise GraphError(403, "Forbidden", "A To Do list can only be reached by its owner.")
    return user


def _list(ctx: Ctx) -> TodoList:
    store: TodoStore = ctx.store
    user = _owner(ctx)
    found = store.lists.get(ctx.params.get("todoTaskList_id", ""))
    if found is None or found.owner != user.id:
        raise GraphError(404, "ErrorItemNotFound", "The requested To Do list was not found.")
    return found


def _no_query(ctx: Ctx) -> None:
    for name in ("$filter", "$orderby"):
        if ctx.request.q(name) is not None:
            raise GraphError(
                501, "FakeGraphNotImplemented", f"The To Do plug-in does not implement {name}."
            )


def lists(ctx: Ctx) -> Any:
    store: TodoStore = ctx.store
    user = _owner(ctx)
    mine = [
        {"id": t.id, "displayName": t.name, "isOwner": True}
        for t in store.lists.values()
        if t.owner == user.id
    ]
    return collection(ctx, mine, default_top=100, max_top=100, context="todo/lists")


def tasks(ctx: Ctx) -> Any:
    _no_query(ctx)
    return collection(
        ctx, _list(ctx).tasks, default_top=100, max_top=100, context="todo/lists/tasks"
    )


def create_task(ctx: Ctx) -> Reply:
    store: TodoStore = ctx.store
    todo = _list(ctx)
    body = ctx.json()
    if not isinstance(body, dict) or not str(body.get("title") or "").strip():
        raise GraphError(400, "BadRequest", "A task needs a title.")
    status = body.get("status", "notStarted")
    if status not in STATUSES:
        raise GraphError(400, "BadRequest", f"status must be one of {', '.join(STATUSES)}.")
    task = store.add_task(todo, str(body["title"]), status=status)
    return Reply(201, task)


def update_task(ctx: Ctx) -> Any:
    todo = _list(ctx)
    task = next((t for t in todo.tasks if t["id"] == ctx.params["todoTask_id"]), None)
    if task is None:
        raise GraphError(404, "ErrorItemNotFound", "The requested task was not found.")
    body = ctx.json()
    if not isinstance(body, dict):
        raise GraphError(400, "BadRequest", "The request body must be a JSON object.")
    if "status" in body and body["status"] not in STATUSES:
        raise GraphError(400, "BadRequest", f"status must be one of {', '.join(STATUSES)}.")
    task.update(body)
    task["lastModifiedDateTime"] = ctx.clock.iso()
    return task


def _register(prefix: str) -> None:
    plugin.route("GET", f"{prefix}/todo/lists", perm=READ)(lists)
    plugin.route("GET", f"{prefix}/todo/lists/{{todoTaskList-id}}/tasks", perm=READ)(tasks)
    plugin.route("POST", f"{prefix}/todo/lists/{{todoTaskList-id}}/tasks", perm=WRITE)(create_task)
    plugin.route(
        "PATCH", f"{prefix}/todo/lists/{{todoTaskList-id}}/tasks/{{todoTask-id}}", perm=WRITE
    )(update_task)


_register("/users/{user-id}")
_register("/me")
