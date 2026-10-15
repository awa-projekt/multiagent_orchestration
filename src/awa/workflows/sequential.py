"""`Sequential`: run members one after another, feeding each the previous result."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal, TypedDict

from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa._internal.upstream.graph import END, START, StateGraph
from awa._internal.upstream.messages import BaseMessage
from awa.contracts.protocols import AgentInput, AgentLike
from awa.workflows._base import (
    StepRecord,
    Workflow,
    WorkflowRun,
    _Outcome,
    input_text,
    labeled,
    merge_records,
    record_result,
    restore_result,
    step_text,
)

Carry = Literal["previous", "all"]
"""What later steps of a `Sequential` receive: the previous step's text, or all of it."""


class _SequentialState(TypedDict, total=False):
    _input: str | list[BaseMessage]
    _steps: Annotated[dict[str, StepRecord], merge_records]


class Sequential(Workflow):
    """Runs its steps in order; the result is the last step's.

    The first step receives the workflow's input. With `carry="previous"` every later step receives
    the previous step's final text (or the JSON of its output); with `carry="all"`, the original
    request followed by every earlier step's text labeled `[<agent>]: ...`. With a `checkpointer`,
    `resume` continues a paused step without running earlier ones again.

    Example:
        ```python
        workflow = Sequential("draft_and_edit", [writer, editor])
        result = await workflow.run("Write a haiku about rivers.")
        ```

    Args:
        steps: The members to run, in order; the same member may appear more than once.
        description: What the workflow does; generated from the steps if empty.
    """

    _kind = "sequential"

    def __init__(
        self,
        name: str,
        steps: Sequence[AgentLike],
        *,
        description: str = "",
        carry: Carry = "previous",
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> None:
        super().__init__(name, description=description, checkpointer=checkpointer)
        if not steps:
            raise ValueError("Sequential needs at least one step")
        if carry not in ("previous", "all"):
            raise ValueError(f"carry must be 'previous' or 'all', not {carry!r}")
        self._steps = tuple(steps)
        self._carry: Carry = carry
        self._keys = _step_keys(self._steps)

    @property
    def steps(self) -> tuple[AgentLike, ...]:
        """The steps, in execution order."""

        return self._steps

    def _members(self) -> Sequence[AgentLike]:
        return self._steps

    def _default_description(self) -> str:
        return "Runs " + ", then ".join(step.name for step in self._steps) + "."

    def _build_graph(self) -> StateGraph[Any, Any, Any, Any]:
        builder: StateGraph[Any, Any, Any, Any] = StateGraph(_SequentialState)
        previous = START
        for index, key in enumerate(self._keys):
            builder.add_node(key, self._step_node(index))
            builder.add_edge(previous, key)
            previous = key
        builder.add_edge(previous, END)
        return builder

    def _step_node(self, index: int) -> Any:
        """The graph node running step `index` (typed `Any`: LangGraph matches nodes by protocol)."""

        step = self._steps[index]
        key = self._keys[index]

        async def run_step(state: _SequentialState) -> dict[str, Any]:
            result = await self.run_member(step, self._step_input(state, index), index)
            return {"_steps": {key: record_result(result)}}

        return run_step

    def _step_input(self, state: _SequentialState, index: int) -> AgentInput:
        original = state.get("_input", "")
        if index == 0:
            return original
        records = state.get("_steps", {})
        if self._carry == "previous":
            return step_text(restore_result(records[self._keys[index - 1]]))
        transcript = [
            labeled(self._steps[i].name, step_text(restore_result(records[self._keys[i]])))
            for i in range(index)
        ]
        return "\n\n".join([input_text(original), *transcript])

    def _initial_state(self, input: str | list[BaseMessage]) -> dict[str, Any]:
        return {"_input": input, "_steps": {}}

    def _outcome(self, state: Mapping[str, Any], run: WorkflowRun) -> _Outcome:
        return _Outcome.of(restore_result(state["_steps"][self._keys[-1]]))

    def _recursion_limit(self) -> int:
        return len(self._steps) + 2


def _step_keys(steps: Sequence[AgentLike]) -> list[str]:
    """Graph node names: the member name, `name.2` etc. when a member repeats."""

    seen: Counter[str] = Counter()
    keys = []
    for step in steps:
        seen[step.name] += 1
        keys.append(step.name if seen[step.name] == 1 else f"{step.name}.{seen[step.name]}")
    return keys


__all__ = ["Carry", "Sequential"]
