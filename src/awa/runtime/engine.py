"""The engine port: the one place where awa builds, runs, pauses and resumes an execution graph.

Agents are built through `current_engine()`, and agents, teams and workflows run, pause and
resume through it. The default engine runs on LangGraph; `use_engine` swaps it for a block, which
is how another engine (or a scripted one in a test) runs the same agents. Teams and workflows
still compile their graphs with LangGraph and hand that graph to `Engine.run`. Limits, approvals
and events stay in awa: the engine only carries the numbers it is given and the `pause` primitive.
"""

from __future__ import annotations

import contextvars
import functools
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from awa._internal.util import bound

__all__ = [
    "AgentSpec",
    "Engine",
    "EngineOutcome",
    "Pause",
    "Resume",
    "RunOptions",
    "ThreadState",
    "current_engine",
    "use_engine",
]


@dataclass(frozen=True)
class AgentSpec:
    """Everything an engine needs to build one tool-calling agent.

    Attributes:
        name: The agent's name, given to the built graph.
        model: The chat model.
        tools: The tools the model may call.
        middleware: awa's model and tool call hooks, outermost first.
        response_format: The structured output type, or `None` for text.
        checkpointer: Where the agent's thread is saved, or `None`.
        store: The long-term store tools and middleware read, or `None`.
    """

    name: str
    model: Any
    tools: Sequence[Any] = ()
    middleware: Sequence[Any] = ()
    response_format: Any = None
    checkpointer: Any = None
    store: Any = None


@dataclass(frozen=True)
class RunOptions:
    """How one run of a built graph executes.

    Attributes:
        run_name: The name the run is traced under.
        thread_id: The thread the run is saved on, or `None` for an unsaved run.
        step_limit: The most engine steps the run may take; `None` keeps the engine's default.
        durable_writes: `True` saves every step before the next starts (the graph writes outside),
            `False` saves in the background, `None` when the graph saves nothing.
        stream_text: Emit the model's text as `TextDeltaEvent`s while it is generated.
        max_concurrency: The most nodes running at once, or `None` for no bound.
        pause_order: Sort key of a pause id, so pauses come back in the order they were raised.
    """

    run_name: str
    thread_id: str | None = None
    step_limit: int | None = None
    durable_writes: bool | None = None
    stream_text: bool = False
    max_concurrency: int | None = None
    pause_order: Callable[[str], int] | None = None


@dataclass(frozen=True)
class Resume:
    """The input that answers a paused run.

    Attributes:
        decision: The value the pending `pause` returns; with several pauses, a mapping of pause
            id to value.
        by_pending_id: When exactly one pause is unfinished and `decision` is not already keyed
            by pause id, address it to that pause.
    """

    decision: Any
    by_pending_id: bool = False


@dataclass(frozen=True)
class Pause:
    """A run waiting for a decision: the id to answer and the value it asked with."""

    id: str
    value: Any


@dataclass(frozen=True)
class ThreadState:
    """What a thread holds between runs.

    Attributes:
        values: The saved graph state.
        pending: The unfinished pauses.
        has_next: Whether the thread stopped before its graph finished.
    """

    values: Mapping[str, Any] = field(default_factory=dict)
    pending: tuple[Pause, ...] = ()
    has_next: bool = False


@dataclass(frozen=True)
class EngineOutcome:
    """The end of one run: the final graph state and the pauses it stopped on."""

    values: Mapping[str, Any]
    pending: tuple[Pause, ...] = ()


class Engine(Protocol):
    """What awa needs from an execution engine.

    Attributes:
        control_flow: Exceptions that hand control to an enclosing run (pauses among them);
            code that catches errors lets them pass unchanged.
    """

    control_flow: tuple[type[BaseException], ...]

    def build_agent(self, spec: AgentSpec) -> Any:
        """A runnable handle for the agent `spec` describes."""
        ...

    async def run(
        self, handle: Any, input: Mapping[str, Any] | Resume | None, options: RunOptions
    ) -> EngineOutcome:
        """Run `handle` on new `input`, a `Resume`, or `None` to continue a stopped thread.

        `handle` is what `build_agent` returned, or a team's or workflow's compiled LangGraph
        graph. A pause inside a run nested in a saved thread propagates as a `control_flow`
        exception.
        """
        ...

    async def state(self, handle: Any, thread_id: str) -> ThreadState:
        """The saved state of `thread_id`."""
        ...

    def pause(self, value: Any) -> Any:
        """Stop the run until a decision arrives, then return it (call inside a node or tool)."""
        ...

    def has_decision(self) -> bool:
        """Whether the current node or tool was resumed with a decision for its `pause`."""
        ...

    def pauses(self, error: BaseException) -> tuple[Pause, ...]:
        """The pauses `error` carries; empty when it is no pause."""
        ...

    def is_step_limit(self, error: BaseException) -> bool:
        """Whether `error` means a run used up its `step_limit`."""
        ...

    def in_thread(self) -> bool:
        """Whether the caller runs inside a run saved on a thread, so it can pause."""
        ...

    def detached(self) -> contextvars.Context:
        """A copy of the current context outside every enclosing run, for a task that outlives it."""
        ...

    def outside_run(self, *, stream_text: bool = False) -> AbstractContextManager[None]:
        """Leave the enclosing run for the block; only its callbacks (and text streaming) carry over."""
        ...


_engine: contextvars.ContextVar[Engine | None] = contextvars.ContextVar("awa_engine", default=None)


@functools.cache
def _langgraph_engine() -> Engine:
    from awa.runtime._langgraph import LangGraphEngine

    return LangGraphEngine()


def current_engine() -> Engine:
    """The engine of the current context: the one `use_engine` set, else the LangGraph engine."""

    return _engine.get() or _langgraph_engine()


@contextmanager
def use_engine(engine: Engine) -> Iterator[Engine]:
    """Run agents, teams and workflows started inside the block on `engine`.

    Example:
        ```python
        with use_engine(MyEngine()):
            result = await agent.run("Summarize the report")
        ```
    """

    with bound(_engine, engine):
        yield engine
