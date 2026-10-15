"""`Loop`: repeat a member until a condition holds or an iteration cap is reached."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypedDict

from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa._internal.upstream.graph import END, START, RemainingSteps, StateGraph
from awa._internal.upstream.messages import BaseMessage
from awa.contracts.protocols import AgentLike
from awa.contracts.results import RunResult
from awa.runtime.termination import ceilings
from awa.workflows._base import (
    StepRecord,
    Workflow,
    WorkflowRun,
    _Outcome,
    out_of_steps,
    progress,
    record_result,
    restore_result,
    step_limit_stop,
    step_text,
    stop_diagnosis,
)

Until = Callable[[RunResult[Any]], bool] | str
"""A `Loop` stop condition: a predicate on the result, or the name of a boolean output field."""


class _LoopState(TypedDict, total=False):
    _input: str | list[BaseMessage]
    _iterations: int
    _last: StepRecord | None
    _stopped_reason: str | None
    _same: int
    _digest: str | None
    _remaining: RemainingSteps


class Loop(Workflow):
    """Runs `body` repeatedly; the result is the last iteration's.

    Each later iteration receives `next_input(previous_result)`, by default the previous final text
    (or the JSON of its structured output). The loop stops after the first iteration for which
    `until` holds, after `max_iterations`, or once `max_repeats` iterations in a row returned the
    same result. `until` is a predicate on the result or, preferably, the name of a boolean field of
    the body's structured output.

    `metadata` records `"iterations"` and `"stopped_reason"` (`"condition"`, `"max_iterations"`,
    `"no_progress"` or `"step_limit"`); any other stop also records `"stop"`, a short diagnosis.
    `"step_limit"` ends with the last result instead of a `GraphRecursionError`. With a
    `checkpointer`, `resume` continues a paused iteration with the count preserved.

    Example:
        ```python
        class Draft(BaseModel):
            text: str
            done: bool  # the editor sets it when the draft needs no further pass

        editor = Agent("editor", model=model, output_type=Draft)
        refine = Loop("refine", editor, until="done", max_iterations=3)
        ```

    Args:
        until: A predicate on each iteration's result, or a boolean output field name.
        max_repeats: Identical results in a row that end the loop with `"no_progress"` (at least 2);
            default `AWA_MAX_IDENTICAL_OUTPUTS` (2).
        description: What the workflow does; generated if empty.
        next_input: Builds the next iteration's input from the previous result.
    """

    _kind = "loop"

    def __init__(
        self,
        name: str,
        body: AgentLike,
        *,
        until: Until,
        max_iterations: int,
        max_repeats: int | None = None,
        description: str = "",
        next_input: Callable[[RunResult[Any]], str] | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> None:
        super().__init__(name, description=description, checkpointer=checkpointer)
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        if max_repeats is not None and max_repeats < 2:
            raise ValueError("max_repeats must be at least 2")
        self._max_repeats = max_repeats
        self._body = body
        self._until = _field_condition(body, until) if isinstance(until, str) else until
        self._max_iterations = max_iterations
        self._next_input = next_input if next_input is not None else step_text

    @property
    def body(self) -> AgentLike:
        """The repeated member."""

        return self._body

    @property
    def max_iterations(self) -> int:
        """Hard cap on the number of iterations."""

        return self._max_iterations

    def _members(self) -> Sequence[AgentLike]:
        return (self._body,)

    def _default_description(self) -> str:
        return (
            f"Repeats {self._body.name} until its result meets a condition "
            f"(at most {self._max_iterations} iterations)."
        )

    def _build_graph(self) -> StateGraph[Any, Any, Any, Any]:
        builder: StateGraph[Any, Any, Any, Any] = StateGraph(_LoopState)
        node = self._body.name
        builder.add_node(node, self._iterate)
        builder.add_edge(START, node)
        builder.add_conditional_edges(node, self._route, [node, END])
        return builder

    async def _iterate(self, state: _LoopState) -> dict[str, Any]:
        """One iteration: run the body, then decide whether to stop."""

        last = state.get("_last")
        if last is None:
            current: Any = state.get("_input", "")
        else:
            current = self._next_input(restore_result(last))
        result = await self.run_member(self._body, current, 0)
        iterations = state.get("_iterations", 0) + 1
        repeated = progress(state, result)
        stopped: str | None = None
        if self._until(result):
            stopped = "condition"
        elif iterations >= self._max_iterations:
            stopped = "max_iterations"
        elif repeated["_same"] >= self._repeats():
            stopped = "no_progress"
        elif out_of_steps(state.get("_remaining"), 1):
            stopped = "step_limit"
        return {
            "_iterations": iterations,
            "_last": record_result(result),
            "_stopped_reason": stopped,
            **repeated,
        }

    def _repeats(self) -> int:
        return self._max_repeats or ceilings().max_identical_outputs

    def _route(self, state: _LoopState) -> str:
        return END if state.get("_stopped_reason") else self._body.name

    def _initial_state(self, input: str | list[BaseMessage]) -> dict[str, Any]:
        return {
            "_input": input,
            "_iterations": 0,
            "_last": None,
            "_stopped_reason": None,
            "_same": 0,
            "_digest": None,
        }

    def _outcome(self, state: Mapping[str, Any], run: WorkflowRun) -> _Outcome:
        reason = state.get("_stopped_reason") or "max_iterations"
        iterations = state.get("_iterations", 0)
        extra: dict[str, Any] = {}
        if reason == "step_limit":
            extra["stop"] = step_limit_stop(self.name, self._recursion_limit(), iterations)
        elif reason != "condition":
            maximum = self._repeats() if reason == "no_progress" else self._max_iterations
            extra["stop"] = _stop(self.name, reason, iterations, maximum, state)
        return _Outcome.of(
            restore_result(state["_last"]), iterations=iterations, stopped_reason=reason, **extra
        )

    def _recursion_limit(self) -> int:
        return self._max_iterations + 2


def _field_condition(body: AgentLike, field: str) -> Callable[[RunResult[Any]], bool]:
    """The condition reading the boolean output field `field` of the body's result."""

    schema = body.card.output_schema or {}
    if field not in (schema.get("properties") or {}):
        raise ValueError(
            f"until={field!r} names no field of {body.name!r}'s structured output: give the "
            "body an output_type with that boolean field, or pass a predicate"
        )

    def holds(result: RunResult[Any]) -> bool:
        output = result.output
        value = output.get(field) if isinstance(output, Mapping) else getattr(output, field, None)
        return value is True

    return holds


def _stop(
    name: str, reason: str, iterations: int, maximum: int, state: Mapping[str, Any]
) -> dict[str, Any]:
    """The diagnosis of a loop that ended without its condition (no model call)."""

    if reason == "no_progress":
        fix = (
            "the body returned the same result again, so the next iteration would get the "
            "same input: change next_input so each iteration gets new information, or the "
            "body's instructions"
        )
        value, bound = state.get("_same", 0), "identical_outputs"
    else:
        fix = (
            "the condition never held: check `until` against the body's real output, or "
            "raise max_iterations if more rounds really help"
        )
        value, bound = iterations, "max_iterations"
    return stop_diagnosis(name, bound, value, maximum, fix, iterations=iterations)


__all__ = ["Loop", "Until"]
