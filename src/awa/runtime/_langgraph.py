"""The LangGraph engine: the default adapter of `awa.runtime.engine.Engine`."""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, TypeVar

from awa._internal.upstream.agents import create_agent
from awa._internal.upstream.graph import (
    CONFIG_KEY_SCRATCHPAD,
    Command,
    GraphBubbleUp,
    GraphInterrupt,
    GraphRecursionError,
    Interrupt,
    StateSnapshot,
    interrupt,
)
from awa._internal.upstream.runnables import (
    RunnableConfig,
    ensure_config,
    var_child_runnable_config,
)
from awa.runtime._streaming import text_delta_callbacks
from awa.runtime.engine import AgentSpec, EngineOutcome, Pause, Resume, RunOptions, ThreadState

_Item = TypeVar("_Item", Pause, Interrupt)


class LangGraphEngine:
    """Builds agents with `create_agent` and runs every graph as a compiled LangGraph graph."""

    control_flow: tuple[type[BaseException], ...] = (GraphBubbleUp,)

    def build_agent(self, spec: AgentSpec) -> Any:
        return create_agent(
            spec.model,
            tools=list(spec.tools),
            middleware=list(spec.middleware),
            response_format=spec.response_format,
            checkpointer=spec.checkpointer,
            store=spec.store,
            name=spec.name,
        )

    async def run(
        self, handle: Any, input: Mapping[str, Any] | Resume | None, options: RunOptions
    ) -> EngineOutcome:
        config = _config(options)
        graph_input: Any = input
        if isinstance(input, Resume):
            graph_input = await self._command(handle, config, input)
        durability = (
            {}
            if options.durable_writes is None
            else {"durability": "sync" if options.durable_writes else "async"}
        )
        try:
            state = await handle.ainvoke(graph_input, config=config, **durability)
        except GraphInterrupt as exc:
            raised = exc.args[0] if exc.args else ()
            if options.pause_order is None or len(raised) < 2:
                raise
            raise GraphInterrupt(_sorted(raised, options.pause_order)) from exc
        values = dict(state)
        pending = tuple(_pause(item) for item in values.pop("__interrupt__", ()))
        if options.pause_order is not None:
            pending = _sorted(pending, options.pause_order)
        return EngineOutcome(values=values, pending=pending)

    async def state(self, handle: Any, thread_id: str) -> ThreadState:
        snapshot = await handle.aget_state({"configurable": {"thread_id": thread_id}})
        return ThreadState(
            values=snapshot.values,
            pending=tuple(_pause(item) for item in _unfinished(snapshot)),
            has_next=bool(snapshot.next),
        )

    def pause(self, value: Any) -> Any:
        return interrupt(value)

    def has_decision(self) -> bool:
        pad = (ensure_config().get("configurable") or {}).get(CONFIG_KEY_SCRATCHPAD)
        return pad is not None and (bool(pad.resume) or pad.get_null_resume(False) is not None)

    def pauses(self, error: BaseException) -> tuple[Pause, ...]:
        if not isinstance(error, GraphInterrupt) or not error.args:
            return ()
        return tuple(_pause(item) for item in error.args[0] if isinstance(item, Interrupt))

    def is_step_limit(self, error: BaseException) -> bool:
        return isinstance(error, GraphRecursionError)

    def in_thread(self) -> bool:
        return bool((ensure_config().get("configurable") or {}).get("thread_id"))

    def detached(self) -> contextvars.Context:
        context = contextvars.copy_context()
        context.run(var_child_runnable_config.set, None)
        return context

    @contextmanager
    def outside_run(self, *, stream_text: bool = False) -> Iterator[None]:
        enclosing = ensure_config().get("callbacks")
        callbacks = (text_delta_callbacks() if stream_text else None) or enclosing
        token = var_child_runnable_config.set({"callbacks": callbacks} if callbacks else None)
        try:
            yield
        finally:
            var_child_runnable_config.reset(token)

    @staticmethod
    async def _command(handle: Any, config: RunnableConfig, resume: Resume) -> Command[Any]:
        """`resume` as a command; `by_pending_id` keys a lone unfinished pause by its id."""

        if not resume.by_pending_id:
            return Command(resume=resume.decision)
        snapshot = await handle.aget_state(config)
        pending = _unfinished(snapshot)
        known = {item.id for task in snapshot.tasks for item in task.interrupts}
        decision = resume.decision
        by_id = isinstance(decision, Mapping) and bool(decision) and set(decision) <= known
        if not by_id and len({item.id for item in pending}) == 1:
            return Command(resume={pending[0].id: decision})
        return Command(resume=decision)


def _config(options: RunOptions) -> RunnableConfig:
    config: RunnableConfig = {"run_name": options.run_name}
    if options.step_limit is not None:
        config["recursion_limit"] = options.step_limit
    if options.max_concurrency is not None:
        config["max_concurrency"] = options.max_concurrency
    if options.thread_id is not None:
        config["configurable"] = {"thread_id": options.thread_id}
    if options.stream_text and (callbacks := text_delta_callbacks()) is not None:
        config["callbacks"] = callbacks
    return config


def _unfinished(snapshot: StateSnapshot) -> list[Interrupt]:
    """The interrupts of unfinished tasks: answered ones keep stale records until the step ends."""

    return [item for task in snapshot.tasks if task.result is None for item in task.interrupts]


def _pause(item: Interrupt) -> Pause:
    return Pause(id=item.id, value=item.value)


def _sorted(items: Sequence[_Item], key: Callable[[str], int]) -> tuple[_Item, ...]:
    return tuple(sorted(items, key=lambda item: key(item.id)))
