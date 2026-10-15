"""`Map`: run one member over a list in parts, concurrently, and check that every item has a result."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Any, TypedDict

import pydantic_core

from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa._internal.upstream.graph import END, START, Send, StateGraph
from awa._internal.upstream.messages import BaseMessage
from awa.contracts.cards import AgentCard
from awa.contracts.protocols import AgentLike
from awa.contracts.results import RunResult
from awa.errors import AgentRunError
from awa.runtime.engine import current_engine
from awa.runtime.termination import ends_the_run
from awa.workflows._base import (
    Workflow,
    WorkflowRun,
    _Outcome,
    input_text,
    labeled,
    merge_records,
    step_text,
)

Splitter = Callable[[str], Sequence[Any]]
"""Turns the workflow input text into the list of items."""

_ROUNDS = 2
"""The first round plus one retry of the items that got no result."""


class _MapState(TypedDict, total=False):
    _input: str | list[BaseMessage]
    _round: int
    _results: Annotated[dict[str, Any], merge_records]
    _errors: Annotated[dict[str, str], merge_records]


class _Part(TypedDict):
    """The payload a `Send` hands to one part run."""

    indices: list[int]
    items: list[Any]
    part: int


class _PartState(TypedDict):
    _part: _Part


class Map(Workflow):
    """Runs `member` once per part of a list, concurrently, and merges the results in order.

    `split` turns the input into items; by default a JSON array (or an object with exactly one list
    field), otherwise one item per non-empty line. Items are grouped into parts of `part_size`, each
    one LangGraph `Send`; with a `checkpointer`, only a paused part runs again on `resume`. With
    `part_size=1` the member gets the item itself; otherwise a JSON array of its items, and must
    answer with a list of the same length and order.

    Every item must get exactly one result: items of a failed or miscounted part are tried once more,
    then reported in `metadata["missing"]` (item numbers from 1) with reasons in
    `metadata["errors"]`. The output is the results in input order (`None` for a missing item).

    Example:
        ```python
        extract = Map("extract", invoice_reader, part_size=5, max_concurrency=4)
        result = await extract.run(json.dumps(invoices))
        result.metadata["missing"]  # [] when every invoice has a result
        ```

    Args:
        part_size: Items per part (at least 1).
        max_concurrency: Maximum parts running at once (unbounded when `None`).
        description: What the workflow does; generated if empty.
    """

    _kind = "map"

    def __init__(
        self,
        name: str,
        member: AgentLike,
        *,
        part_size: int = 1,
        max_concurrency: int | None = None,
        split: Splitter | None = None,
        description: str = "",
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> None:
        super().__init__(name, description=description, checkpointer=checkpointer)
        if part_size < 1:
            raise ValueError("part_size must be at least 1")
        if max_concurrency is not None and max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._member = member
        self._part_size = part_size
        self._max_concurrency = max_concurrency
        self._split = split if split is not None else split_items

    @property
    def member(self) -> AgentLike:
        """The member run for each part."""

        return self._member

    @property
    def part_size(self) -> int:
        """Items per part."""

        return self._part_size

    def _members(self) -> Sequence[AgentLike]:
        return (self._member,)

    def _default_description(self) -> str:
        unit = "each item" if self._part_size == 1 else f"parts of {self._part_size} items"
        return f"Runs {self._member.name} on {unit} of a list, concurrently."

    def _input_schema(self, cards: Sequence[AgentCard]) -> dict[str, Any] | None:
        return None

    def _output_schema(self, cards: Sequence[AgentCard]) -> dict[str, Any] | None:
        return None

    def _node_concurrency(self) -> int | None:
        return self._max_concurrency

    def _build_graph(self) -> StateGraph[Any, Any, Any, Any]:
        builder: StateGraph[Any, Any, Any, Any] = StateGraph(_MapState)
        builder.add_node("part", self._run_part)
        builder.add_node("check", self._check)
        builder.add_conditional_edges(START, self._send_missing, ["part", END])
        builder.add_edge("part", "check")
        builder.add_conditional_edges("check", self._send_missing, ["part", END])
        return builder

    def _items(self, state: Mapping[str, Any]) -> list[Any]:
        return list(self._split(input_text(state.get("_input", ""))))

    def _send_missing(self, state: _MapState) -> list[Send] | str:
        """One `Send` per part of the items without a result, while a round is left."""

        if state.get("_round", 0) >= _ROUNDS:
            return END
        items = self._items(state)
        results = state.get("_results", {})
        missing = [index for index in range(len(items)) if str(index) not in results]
        if not missing:
            return END
        size = self._part_size
        sends = []
        for part, start in enumerate(range(0, len(missing), size)):
            indices = missing[start : start + size]
            payload: _Part = {
                "indices": indices,
                "items": [items[index] for index in indices],
                "part": part,
            }
            sends.append(Send("part", {"_part": payload}))
        return sends

    async def _run_part(self, state: _PartState) -> dict[str, Any]:
        part: _Part = state["_part"]
        indices = part["indices"]
        try:
            result = await self.run_member(
                self._member, _part_input(part["items"], self._part_size), part["part"]
            )
            outputs = _part_outputs(result, len(indices), self._part_size)
        except current_engine().control_flow:
            raise
        except Exception as exc:
            if ends_the_run(self._current_run().ctx, exc):
                raise
            reason = f"{type(exc).__name__}: {exc}"
            return {"_errors": {str(index): reason for index in indices}}
        return {
            "_results": {str(index): value for index, value in zip(indices, outputs, strict=True)}
        }

    def _check(self, state: _MapState) -> dict[str, Any]:
        return {"_round": state.get("_round", 0) + 1}

    def _initial_state(self, input: str | list[BaseMessage]) -> dict[str, Any]:
        return {"_input": input, "_round": 0, "_results": {}, "_errors": {}}

    def _outcome(self, state: Mapping[str, Any], run: WorkflowRun) -> _Outcome:
        items = self._items(state)
        results: Mapping[str, Any] = state.get("_results", {})
        errors: Mapping[str, str] = state.get("_errors", {})
        missing = [index for index in range(len(items)) if str(index) not in results]
        if items and len(missing) == len(items):
            summary = "; ".join(sorted({errors.get(str(i), "no result") for i in missing}))
            raise AgentRunError(f"Every item of map workflow {self.name!r} failed: {summary}")
        output = [results.get(str(index)) for index in range(len(items))]
        lines = []
        for index, value in enumerate(output):
            label = f"item {index + 1}"
            if str(index) in results:
                lines.append(labeled(label, _text(value)))
            else:
                reason = errors.get(str(index), "no result")
                lines.append(labeled(label, f"(no result: {reason})"))
        return _Outcome(
            output=output,
            text="\n\n".join(lines),
            metadata={
                "items": len(items),
                "rounds": state.get("_round", 0),
                "missing": [index + 1 for index in missing],
                "errors": {
                    str(index + 1): errors.get(str(index), "no result") for index in missing
                },
            },
        )

    def _recursion_limit(self) -> int:
        return 2 * _ROUNDS + 2


def split_items(text: str) -> list[Any]:
    """The default `Map` splitter: a JSON array, a JSON object with one list field, else lines."""

    stripped = text.strip()
    try:
        value = json.loads(stripped)
    except ValueError:
        value = None
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and (found := _only_list(value)) is not None:
        return found
    return [line.strip() for line in stripped.splitlines() if line.strip()]


def _only_list(value: Mapping[str, Any]) -> list[Any] | None:
    """The one list field of `value`, or `None` when it has none or several."""

    lists = [field for field in value.values() if isinstance(field, list)]
    return lists[0] if len(lists) == 1 else None


def _part_input(items: list[Any], part_size: int) -> str:
    if part_size == 1:
        return _text(items[0])
    return pydantic_core.to_json(items).decode()


def _part_outputs(result: RunResult[Any], count: int, part_size: int) -> list[Any]:
    """The per-item results of one part run; raises when their number is not `count`."""

    if part_size == 1:
        return [result.output if result.output is not None else step_text(result)]
    value: Any = result.output
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        value = _only_list(value)
    if not isinstance(value, list):
        try:
            value = json.loads(step_text(result))
        except ValueError:
            value = None
        if isinstance(value, dict):
            value = _only_list(value)
    if not isinstance(value, list):
        raise AgentRunError(f"the part of {count} items got no list of results")
    if len(value) != count:
        raise AgentRunError(f"the part of {count} items got {len(value)} results")
    return value


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return pydantic_core.to_json(value).decode()


__all__ = ["Map", "Splitter", "split_items"]
