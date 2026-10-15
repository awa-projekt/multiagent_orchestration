"""Where a process step's output goes besides the run: the step output backend protocol.

`ProcessWorkflow` reports every try's output (`StepOutput`) to the sink set with
`use_step_output_sink`, before the step's `StepEvent`; outputs never travel on run events.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from awa._internal.util import bound

__all__ = ["StepOutput", "StepOutputSink", "current_step_output_sink", "use_step_output_sink"]


@dataclass(frozen=True)
class StepOutput:
    """What one try of a process step produced.

    Attributes:
        index: The step's position, from 0.
        output: The structured output as plain data (`None` for an answer without one).
        agent_run_id: The run id of the agent that did the step (its `invoke_agent` span).
    """

    step: str
    index: int
    output: Any
    text: str
    agent_run_id: str


StepOutputSink = Callable[[StepOutput], Awaitable[None]]
"""Receives each step output; it must not raise (a failing sink is logged and skipped)."""

_SINK: ContextVar[StepOutputSink | None] = ContextVar("awa_step_output_sink", default=None)


def current_step_output_sink() -> StepOutputSink | None:
    """The sink set for the current context, or `None`."""

    return _SINK.get()


@contextmanager
def use_step_output_sink(sink: StepOutputSink | None) -> Iterator[None]:
    """Send the step outputs of process runs started in this block to `sink`."""

    with bound(_SINK, sink):
        yield
