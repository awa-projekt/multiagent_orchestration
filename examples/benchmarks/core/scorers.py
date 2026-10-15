"""Code scorers of the core benchmark, for what the scorer specs of `cases.yaml` cannot express.

A case names them in `metadata.checks`; each is a function `(case, trace)` for `awa.evals.Custom`
that returns a bool or a value in [0, 1] with a reason.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from awa.contracts import AgentMessageEvent, ToolResultEvent
from awa.evals import EvalCase, RunTrace
from awa.evals.trace import output_data


def _plain(text: str) -> str:
    """Lower-case words of `text` only (quotes and pages differ in spacing, punctuation and in
    the footnote markers like `[30]` that a page carries inside a sentence)."""

    text = re.sub(r"\[[^\]]{1,20}\]", " ", text.lower().replace("\\n", " "))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def quotes_grounded(case: EvalCase, trace: RunTrace) -> tuple[float, str]:
    """Every `evidence` quote occurs in a page the run fetched and holds its item's year."""

    data = output_data(trace.output)
    items = data.get("items") if isinstance(data, dict) else None
    pages = _plain(
        " ".join(
            event.content
            for event in trace.of_type(ToolResultEvent)
            if event.tool_name == "fetch_url" and not event.is_error
        )
    )
    items = [item for item in items or [] if isinstance(item, dict)]
    found = sum(
        bool(quote := _plain(str(item.get("evidence", ""))))
        and quote in pages
        and str(item.get("year")) in quote.split()
        for item in items
    )
    return (
        found / len(items) if items else 0.0
    ), f"{found} of {len(items)} quotes are on fetched pages and state the year"


def peer_consulted(case: EvalCase, trace: RunTrace) -> tuple[bool, str]:
    """A member consulted a peer (`consult_peer`) at least once."""

    consults = [event for event in trace.delegations() if event.mode == "consult"]
    return bool(consults), f"{len(consults)} peer consultation(s)"


def tasks_completed(case: EvalCase, trace: RunTrace) -> tuple[bool, str]:
    """The network finished: no open or failed task, a handback for every task, normal stop."""

    metadata = trace.result.metadata
    tasks = metadata.get("tasks", {})
    open_or_failed = {k: v for k, v in tasks.items() if k != "completed" and v}
    ok = (
        metadata.get("stop_reason") == "completed"
        and tasks.get("completed", 0) >= 3
        and not open_or_failed
        and metadata.get("handbacks", 0) >= tasks.get("completed", 0)
    )
    return (
        ok,
        f"tasks {tasks}, handbacks {metadata.get('handbacks')}, stop {metadata.get('stop_reason')}",
    )


def _first_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    try:
        value, _ = json.JSONDecoder().raw_decode(text[start:]) if start >= 0 else ({}, 0)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def dependency_order(case: EvalCase, trace: RunTrace) -> tuple[bool, str]:
    """Every task was handed back after all the tasks it depends on."""

    plan = [t for call in trace.tool_calls("create_tasks") for t in call.arguments.get("tasks", [])]
    ids: dict[str, str] = {}
    for event in trace.of_type(ToolResultEvent):
        if event.tool_name == "create_tasks":
            ids |= {k: v for k, v in _first_object(event.content).items() if isinstance(v, str)}
    if len(ids) < len(plan):  # ids are task_<n> in the order of creation
        ids = {task["key"]: f"task_{n}" for n, task in enumerate(plan, start=1)}
    done = [
        message.data["handback"]["task_id"]
        for event in trace.of_type(AgentMessageEvent)
        if isinstance((message := event.message).data, dict)
        and isinstance(message.data.get("handback"), dict)
        and message.data["handback"].get("status") == "completed"
    ]
    problems = []
    for task in plan:
        for dependency in task.get("depends_on", []):
            first, then = ids.get(dependency, dependency), ids[task["key"]]
            if then not in done or first not in done or done.index(first) > done.index(then):
                problems.append(f"{then} after {first}")
    return not problems and bool(plan), f"completed in order {done}; broken: {problems or 'none'}"


def blackboard_used(case: EvalCase, trace: RunTrace) -> tuple[bool, str]:
    """Teammates shared findings on the blackboard and another one read them."""

    wrote, read = trace.tool_calls("write_shared"), trace.tool_calls("read_shared")
    return bool(wrote) and bool(read), f"{len(wrote)} write(s), {len(read)} read(s)"


CHECKS: dict[str, Callable[[EvalCase, RunTrace], tuple[float | bool, str]]] = {
    "quotes_grounded": quotes_grounded,
    "peer_consulted": peer_consulted,
    "tasks_completed": tasks_completed,
    "dependency_order": dependency_order,
    "blackboard_used": blackboard_used,
}
"""The code scorers by name, as `metadata.checks` of a case refers to them."""
