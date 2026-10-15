"""What scorers see of one run: the result, every event of all its segments, timing and usage."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import TypeAdapter

from awa.contracts.events import (
    ApprovalDecidedEvent,
    DelegationEvent,
    HandoffEvent,
    RunEvent,
    ToolCallEvent,
)
from awa.contracts.results import RunResult
from awa.contracts.usage import Usage

EventT = TypeVar("EventT")

_ANY: TypeAdapter[Any] = TypeAdapter(Any)


@dataclass(frozen=True)
class RunTrace:
    """One finished run of the evaluated system, as recorded by `evaluate`.

    Attributes:
        result: The final `RunResult` (after the last decision was applied).
        events: Every event of the run tree, across all segments, in arrival order.
        segments: The result of every segment; empty means `(result,)`.
        decisions: The applied decisions, in order (a repeating one once per request).
    """

    result: RunResult[Any]
    events: tuple[RunEvent, ...] = ()
    duration_ms: int = 0
    usage: Usage | None = None
    segments: tuple[RunResult[Any], ...] = ()
    decisions: tuple[dict[str, Any], ...] = ()

    @property
    def output(self) -> Any:
        """The run's output (`result.output`)."""

        return self.result.output

    @property
    def total_usage(self) -> Usage:
        """`usage`, falling back to `result.usage`."""

        return self.usage if self.usage is not None else self.result.usage

    def of_type(self, event_type: type[EventT]) -> list[EventT]:
        """Return the events that are instances of `event_type`, in order."""

        return [event for event in self.events if isinstance(event, event_type)]

    def tool_calls(self, name: str | None = None) -> list[ToolCallEvent]:
        """Return the tool calls of the run tree, optionally only those of tool `name`."""

        return [
            event
            for event in self.of_type(ToolCallEvent)
            if name is None or event.tool_name == name
        ]

    def delegations(self) -> list[DelegationEvent]:
        """Return every delegation and peer consultation of the run tree."""

        return self.of_type(DelegationEvent)

    def handoffs(self) -> list[HandoffEvent]:
        """Return every handoff of the run tree."""

        return self.of_type(HandoffEvent)

    @property
    def runs(self) -> tuple[RunResult[Any], ...]:
        """The result of every segment, the final one last."""

        return self.segments or (self.result,)

    def approval_requests(self, tool: str | None = None) -> list[str]:
        """The tools the run paused on, in order, every pause once (`""` without a tool); optionally only `tool`."""

        return requested_approvals(self.runs, tool)

    def approval_decisions(self, tool: str | None = None) -> list[ApprovalDecidedEvent]:
        """Return the decisions applied to approval-gated calls (optionally only `tool`'s)."""

        return [
            event
            for event in self.of_type(ApprovalDecidedEvent)
            if tool is None or event.tool == tool
        ]


def requested_approvals(runs: Sequence[RunResult[Any]], tool: str | None = None) -> list[str]:
    """The tools the runs paused on, in order, each pause once (see `RunTrace.approval_requests`)."""

    seen: set[str] = set()
    tools: list[str] = []
    for run in runs:
        for item in run.interrupts:
            key = str(item.get("id") or "")
            if key:
                if key in seen:
                    continue  # still pending after a partial resume
                seen.add(key)
            value = item.get("value")
            name = str(value.get("tool") or "") if isinstance(value, Mapping) else ""
            if tool is None or name == tool:
                tools.append(name)
    return tools


def output_text(output: Any) -> str:
    """Render a run output as text: strings as-is, structured values as JSON."""

    if isinstance(output, str):
        return output
    if output is None:
        return ""
    return json.dumps(output_data(output), ensure_ascii=False, sort_keys=True, default=str)


def output_data(output: Any) -> Any:
    """Return a JSON-compatible form of a run output (models and dataclasses are dumped)."""

    try:
        return _ANY.dump_python(output, mode="json")
    except Exception:  # noqa: BLE001 - fall back to text for unknown objects
        return str(output)


__all__ = ["RunTrace", "output_data", "output_text", "requested_approvals"]
