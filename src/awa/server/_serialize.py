"""JSON shapes of results, events and pending approvals, and Server-Sent Events framing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic_core import to_jsonable_python

from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa._internal.upstream.runnables import RunnableConfig
from awa.contracts.events import (
    RUN_EVENT_ADAPTER,
    ErrorEvent,
    RunCompletedEvent,
    RunEvent,
    ToolResultEvent,
)
from awa.contracts.results import RunResult
from awa.contracts.usage import Usage
from awa.server.errors import INTERNAL_ERROR_MESSAGE, ErrorInfo, is_safe_error_type

# Channel under which LangGraph checkpointers store pending interrupts.
_INTERRUPT_CHANNEL = "__interrupt__"
_RESUME_CHANNEL = "__resume__"
_CONTROL_CHANNELS = frozenset({_INTERRUPT_CHANNEL, _RESUME_CHANNEL, "__error__", "__scheduled__"})
# How `RunTrackingMiddleware` reports a tool that raised: "<ExceptionType>: <message>".
_RAISED_TOOL_ERROR = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)): ")


def jsonable(value: Any) -> Any:
    """Convert `value` to JSON-compatible data (pydantic models, dataclasses,
    dates, ...); objects without a JSON form become their `str()`."""

    return to_jsonable_python(value, fallback=str)


def usage_json(usage: Usage) -> dict[str, Any]:
    """A `Usage` as JSON, with `total_tokens` added."""

    data: dict[str, Any] = usage.model_dump(mode="json")
    data["total_tokens"] = usage.total_tokens
    return data


def http_interrupts(
    interrupts: list[dict[str, Any]], thread_id: str | None
) -> list[dict[str, Any]]:
    """Pending interrupts as an HTTP client reads them.

    The `agent.resume(...)` hint of an approval payload becomes the relative request that answers
    it (`.../threads/<id>/resume`; `<thread_id>` when `thread_id` is `None`).
    """

    base = f".../threads/{thread_id or '<thread_id>'}"
    text = (
        f"Answer over HTTP: POST {base}/resume (under the address that started the run) with "
        '{"decision": {"action": "approve"}}, {"decision": {"action": "reject", "note": "..."}} '
        'or {"decision": {"action": "edit", "args": {...}}}; '
        f"GET {base}/interrupts lists what waits"
    )
    shown = []
    for item in interrupts:
        value = item.get("value") if isinstance(item, dict) else None
        if isinstance(value, dict) and isinstance(value.get("instructions"), str):
            item = {**item, "value": {**value, "instructions": text}}
        shown.append(item)
    return shown


def result_json(result: RunResult[Any]) -> dict[str, Any]:
    """The JSON body of a finished (or paused) run.

    `status` is `"interrupted"` (with `interrupts`) or `"completed"`; no message transcript.
    """

    interrupts = http_interrupts(jsonable(result.interrupts), result.thread_id)
    return {
        "run_id": result.run_id,
        "status": "interrupted" if result.is_interrupted else "completed",
        "agent": result.agent,
        "output": jsonable(result.output),
        "text": result.text,
        "thread_id": result.thread_id,
        "handoff_path": list(result.handoff_path),
        "interrupts": interrupts,
        "usage": usage_json(result.usage),
        "duration_ms": result.duration_ms,
        "metadata": jsonable(result.metadata),
    }


def event_json(event: RunEvent) -> dict[str, Any]:
    """A run event as JSON (the `RUN_EVENT_ADAPTER` shape).

    A `run_completed` result has the shape of `result_json`; messages of non-library errors in
    `error` and failed `tool_result` events are redacted, except a tool's `ValueError`.
    """

    if isinstance(event, RunCompletedEvent):
        data: dict[str, Any] = event.model_dump(mode="json", exclude={"result"})
        data["result"] = result_json(event.result)
        return data
    if isinstance(event, ErrorEvent) and not is_safe_error_type(event.error_type):
        event = event.model_copy(update={"message": INTERNAL_ERROR_MESSAGE})
    elif isinstance(event, ToolResultEvent) and event.is_error:
        raised = _RAISED_TOOL_ERROR.match(event.content)
        if raised and raised[1] != "ValueError" and not is_safe_error_type(raised[1]):
            event = event.model_copy(update={"content": f"{raised[1]}: {INTERNAL_ERROR_MESSAGE}"})
    dumped: dict[str, Any] = RUN_EVENT_ADAPTER.dump_python(event, mode="json", fallback=str)
    return dumped


def terminal_error_json(
    info: ErrorInfo, fields: dict[str, Any], request_id: str | None = None
) -> dict[str, Any]:
    """The final `error` event of a failed stream, with status, code, retryable and request id."""

    event = ErrorEvent(error_type=info.type, message=info.message, **fields)
    data: dict[str, Any] = event.model_dump(mode="json")
    data["status"] = info.status
    data["code"] = info.code
    data["retryable"] = info.retryable
    data["request_id"] = request_id
    return data


def sse_frame(event: str, data: Any, event_id: int | None = None) -> bytes:
    """Encode one Server-Sent Event; with `event_id` a client can resume after it (`Last-Event-ID`)."""

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    identity = "" if event_id is None else f"id: {event_id}\n"
    return f"{identity}event: {event}\ndata: {payload}\n\n".encode()


SSE_PING = b": ping\n\n"
"""A comment line keeping idle connections open (and detecting disconnected clients)."""


async def pending_interrupts(
    checkpointer: BaseCheckpointSaver[Any], thread_id: str
) -> list[dict[str, Any]] | None:
    """The pending interrupts (`id`, `value`) of a thread in order; `None` without saved state.

    Interrupts of steps already finished in a partial resume are left out.
    """

    state = await pending_state(checkpointer, thread_id)
    return None if state is None else state.interrupts


@dataclass(frozen=True)
class PendingState:
    """What a paused thread waits for, and which saved state a decision would answer.

    Attributes:
        interrupts: The pending interrupts (`id` and `value`), as `pending_interrupts` returns.
        version: Names the saved state the interrupts belong to; equal for two readers of the
            same paused state (a decision is claimed once per version), changed once one applied.
    """

    interrupts: list[dict[str, Any]]
    version: str


async def pending_state(
    checkpointer: BaseCheckpointSaver[Any], thread_id: str
) -> PendingState | None:
    """The pending interrupts and state version of a thread; `None` without saved state."""

    config: RunnableConfig = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    try:
        saved = await checkpointer.aget_tuple(config)
    except NotImplementedError:
        # A synchronous-only checkpointer.
        saved = await asyncio.to_thread(checkpointer.get_tuple, config)
    if saved is None:
        return None
    writes = saved.pending_writes or ()
    # A step that interrupted and has written its results since has finished.
    finished = {write[0] for write in writes if write[1] not in _CONTROL_CHANNELS}
    # Resume values a step consumed: one per interrupt of that step answered so far.
    answered: dict[Any, int] = {}
    for write in writes:
        if write[1] == _RESUME_CHANNEL:
            values = write[2] if isinstance(write[2], list | tuple) else [write[2]]
            answered[write[0]] = answered.get(write[0], 0) + len(values)
    found: list[dict[str, Any]] = []
    marks: list[str] = []
    seen: set[Any] = set()
    for write in writes:
        if write[1] != _INTERRUPT_CHANNEL or write[0] in finished:
            continue
        values = write[2] if isinstance(write[2], list | tuple) else [write[2]]
        for interrupt in values:
            interrupt_id = getattr(interrupt, "id", None)
            if interrupt_id is not None and interrupt_id in seen:
                continue
            seen.add(interrupt_id)
            found.append(
                {"id": interrupt_id, "value": jsonable(getattr(interrupt, "value", interrupt))}
            )
            marks.append(f"{interrupt_id}:{answered.get(write[0], 0)}")
    checkpoint = (saved.config.get("configurable") or {}).get("checkpoint_id")
    digest = hashlib.sha256("|".join([str(checkpoint), *sorted(marks)]).encode()).hexdigest()
    return PendingState(found, digest[:32])
