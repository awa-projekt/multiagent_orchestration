"""`Parallel`: run members concurrently on the same input and combine their results."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from typing import Annotated, Any, TypedDict

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
    StepRecord,
    Workflow,
    WorkflowRun,
    _Outcome,
    labeled,
    merge_records,
    record_result,
    restore_result,
    step_text,
)

Aggregator = Callable[[Mapping[str, RunResult[Any]]], Any]
"""Combines the successful branch results (keyed by branch name) into one output."""


class _ParallelState(TypedDict, total=False):
    _input: str | list[BaseMessage]
    _branches: Annotated[dict[str, StepRecord], merge_records]


class Parallel(Workflow):
    """Runs its branches concurrently on the same input.

    The output maps each successful branch's name to its output, or is what `aggregate` returns for
    that mapping; the text is a `str` aggregate or the branch texts labeled `[<agent>]: ...`. With
    `fail_fast=True` the first failing branch cancels its siblings and propagates; with `False`
    failures go to `metadata["errors"]` (`{branch: "ExceptionType: message"}`).
    A limit of the run tree always propagates; a branch that stops without progress fails alone
    (`NoProgressError`). A paused branch lets its siblings finish; `resume`
    runs only the paused branches again.

    Example:
        ```python
        review = Parallel("review", [style_checker, fact_checker], fail_fast=False)
        result = await review.run(draft)
        ```

    Args:
        branches: The members to run; names must be unique.
        description: What the workflow does; generated from the branches if empty.
        max_concurrency: Maximum branches running at once (unbounded when `None`).
    """

    _kind = "parallel"

    def __init__(
        self,
        name: str,
        branches: Sequence[AgentLike],
        *,
        description: str = "",
        aggregate: Aggregator | None = None,
        max_concurrency: int | None = None,
        fail_fast: bool = True,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> None:
        super().__init__(name, description=description, checkpointer=checkpointer)
        if not branches:
            raise ValueError("Parallel needs at least one branch")
        counts = Counter(branch.name for branch in branches)
        if duplicates := sorted(name for name, count in counts.items() if count > 1):
            raise ValueError(f"Duplicate branch names: {', '.join(duplicates)}")
        if max_concurrency is not None and max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._branches = tuple(branches)
        self._aggregate = aggregate
        self._max_concurrency = max_concurrency
        self._fail_fast = fail_fast

    @property
    def branches(self) -> tuple[AgentLike, ...]:
        """The branches, in declared order."""

        return self._branches

    def _members(self) -> Sequence[AgentLike]:
        return self._branches

    def _default_description(self) -> str:
        return "Runs " + ", ".join(branch.name for branch in self._branches) + " concurrently."

    def _input_schema(self, cards: Sequence[AgentCard]) -> dict[str, Any] | None:
        return None

    def _output_schema(self, cards: Sequence[AgentCard]) -> dict[str, Any] | None:
        return None

    def _build_graph(self) -> StateGraph[Any, Any, Any, Any]:
        builder: StateGraph[Any, Any, Any, Any] = StateGraph(_ParallelState)
        names = [branch.name for branch in self._branches]
        for index, name in enumerate(names):
            builder.add_node(name, self._branch_node(index))
            builder.add_edge(name, END)

        def fan_out(state: _ParallelState) -> list[Send]:
            # One `Send` task per branch: each is checkpointed on its own (its writes are
            # saved when it finishes), so `resume` reruns only the branches that paused.
            return [Send(name, {"_input": state.get("_input", "")}) for name in names]

        builder.add_conditional_edges(START, fan_out, names)
        return builder

    def _prepare_run(self, run: WorkflowRun) -> None:
        if self._max_concurrency is not None:
            run.slots = asyncio.Semaphore(self._max_concurrency)

    def _branch_node(self, index: int) -> Any:
        """The graph node running branch `index` (typed `Any`: LangGraph matches nodes by protocol)."""

        branch = self._branches[index]

        async def run_branch(state: _ParallelState) -> dict[str, Any]:
            run = self._current_run()
            try:
                async with run.slots if run.slots is not None else nullcontext():
                    result = await self.run_member(branch, state.get("_input", ""), index)
            except current_engine().control_flow:
                raise
            except Exception as exc:
                if self._fail_fast or ends_the_run(run.ctx, exc):
                    raise
                run.errors[branch.name] = exc
                return {"_branches": {branch.name: {"error": f"{type(exc).__name__}: {exc}"}}}
            return {"_branches": {branch.name: record_result(result)}}

        return run_branch

    def _initial_state(self, input: str | list[BaseMessage]) -> dict[str, Any]:
        return {"_input": input, "_branches": {}}

    def _outcome(self, state: Mapping[str, Any], run: WorkflowRun) -> _Outcome:
        records: Mapping[str, StepRecord] = state.get("_branches", {})
        finished = [(b.name, records[b.name]) for b in self._branches if b.name in records]
        errors = {name: record["error"] for name, record in finished if "error" in record}
        results = {
            name: restore_result(record) for name, record in finished if "error" not in record
        }
        if not results:
            summary = "; ".join(f"{name}: {error}" for name, error in errors.items())
            cause = next(iter(run.errors.values()), None)
            raise AgentRunError(
                f"Every branch of parallel workflow {self.name!r} failed: {summary}"
            ) from cause

        if self._aggregate is not None:
            output = self._aggregate(results)
        else:
            output = {name: result.output for name, result in results.items()}
        if isinstance(output, str):
            text = output
        else:
            text = "\n\n".join(labeled(name, step_text(result)) for name, result in results.items())
        metadata = {} if self._fail_fast else {"errors": errors}
        return _Outcome(output=output, text=text, metadata=metadata)

    def _recursion_limit(self) -> int:
        return 3


__all__ = ["Aggregator", "Parallel"]
