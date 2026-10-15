"""Waiting inside a run for an external event: `wait_for_event`."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from awa._internal.util import DurationLike, to_timedelta

WAIT_MARKER = "awa_wait"
"""Key of the interrupt payload that marks a `wait_for_event` pause."""

RESULT_MARKER = "awa_wait_result"
"""Key of the resume value that answers a `wait_for_event` pause."""

OnTimeout = Literal["fail", "continue", "escalate"]
"""What a timed-out wait does (see `WaitRequest.on_timeout`)."""


class WaitRequest(BaseModel):
    """What a run waiting on `wait_for_event` waits for (the interrupt's payload).

    Attributes:
        on_timeout: `"fail"` the run, `"continue"` it (the wait returns `default`), or `"escalate"`
            (notify and keep waiting).
        description: What is awaited, for people reading notifications.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    timeout: timedelta | None = None
    remind_every: timedelta | None = None
    on_timeout: OnTimeout = "fail"
    description: str = ""
    default: Any = None


def wait_for_event(
    key: str,
    *,
    timeout: DurationLike | None = None,
    remind_every: DurationLike | None = None,
    on_timeout: OnTimeout = "fail",
    description: str = "",
    default: Any = None,
) -> Any:
    """Pause the run until an event is delivered for `key`, and return its payload.

    Call it in a tool or step of a system with a checkpointer. The run holds no resources while it
    waits and resumes on `Automation.deliver(key, payload)`; outside an automation it returns an
    interrupt (`"awa_wait"`) answered by `system.resume(thread_id, event_delivery(payload))`. The
    tool runs again from its start on resume: do side effects after the wait, or make them
    idempotent.

    Args:
        timeout: Longest wait (`"5d"`, seconds or a `timedelta`).
        remind_every: Send a `process.reminder` at this interval while waiting.
        default: The return value when a timeout continues the run.

    Raises:
        ValueError: If `key` is empty, `on_timeout` is unknown, or `timeout` or
            `remind_every` is not positive.

    Example:
        ```python
        reply = wait_for_event(f"supplier-reply:{invoice_no}", timeout="5d",
                               remind_every="1d", on_timeout="escalate")
        ```
    """

    from awa.runtime.engine import current_engine

    if not isinstance(key, str) or not key.strip():
        raise ValueError("wait_for_event needs a non-empty key")
    if on_timeout not in ("fail", "continue", "escalate"):
        raise ValueError(f"on_timeout must be 'fail', 'continue' or 'escalate', got {on_timeout!r}")
    durations = {
        name: to_timedelta(value, what=name)
        for name, value in (("timeout", timeout), ("remind_every", remind_every))
    }
    for name, duration in durations.items():
        if duration is not None and duration.total_seconds() <= 0:
            raise ValueError(f"wait_for_event {name} must be positive, got {duration}")
    request = WaitRequest(
        key=key.strip(),
        timeout=durations["timeout"],
        remind_every=durations["remind_every"],
        on_timeout=on_timeout,
        description=description,
        default=default,
    )
    answer = current_engine().pause({WAIT_MARKER: request.model_dump(mode="json")})
    if isinstance(answer, dict) and RESULT_MARKER in answer:
        if answer.get(RESULT_MARKER) == "timeout":
            return default
        return answer.get("payload")
    return answer


def event_delivery(payload: Any) -> dict[str, Any]:
    """The resume value that answers a `wait_for_event` with `payload`."""

    return {RESULT_MARKER: "delivered", "payload": payload}


def wait_timeout() -> dict[str, Any]:
    """The resume value that ends a `wait_for_event` as timed out (it returns its `default`)."""

    return {RESULT_MARKER: "timeout"}


def parse_wait_request(value: Any) -> WaitRequest | None:
    """The `WaitRequest` of an interrupt value, or `None` for any other interrupt."""

    if isinstance(value, dict) and isinstance(value.get(WAIT_MARKER), dict):
        try:
            return WaitRequest.model_validate(value[WAIT_MARKER])
        except ValueError:
            return None
    return None


__all__ = [
    "RESULT_MARKER",
    "WAIT_MARKER",
    "OnTimeout",
    "WaitRequest",
    "event_delivery",
    "parse_wait_request",
    "wait_for_event",
    "wait_timeout",
]
