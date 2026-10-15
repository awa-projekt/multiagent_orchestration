"""`SimpleEngine`: an engine without LangGraph, so tests can show the engine port carries an agent."""

from __future__ import annotations

import contextvars
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from awa._internal.upstream.messages import AIMessage, BaseMessage, ToolCall, ToolMessage
from awa.runtime import AgentSpec, EngineOutcome, Pause, Resume, RunOptions, ThreadState


class Paused(Exception):
    """A pause, carried out of the tool that asked for a decision."""

    def __init__(self, pause: Pause) -> None:
        super().__init__(pause.id)
        self.pause = pause


class SimpleEngine:
    """Runs an agent as a loop: call the model, run the tools it asks for, until it answers.

    A tool that pauses stops the run; a `Resume` runs that tool again, and its `pause` returns the
    decision. Middleware does not run.
    """

    control_flow: tuple[type[BaseException], ...] = (Paused,)

    def __init__(self) -> None:
        self.specs: list[AgentSpec] = []
        self._threads: dict[str, list[BaseMessage]] = {}
        self._pending: dict[str, tuple[Pause, ...]] = {}
        self._decisions: list[Any] = []

    def build_agent(self, spec: AgentSpec) -> AgentSpec:
        self.specs.append(spec)
        return spec

    async def run(
        self, handle: AgentSpec, input: Mapping[str, Any] | Resume | None, options: RunOptions
    ) -> EngineOutcome:
        thread = options.thread_id or ""
        messages = self._threads.setdefault(thread, [])
        if isinstance(input, Mapping):
            messages += input["messages"]
        self._decisions = [input.decision] if isinstance(input, Resume) else []
        tools = {tool.name: tool for tool in handle.tools}
        self._pending[thread] = ()
        try:
            while (calls := _open_calls(messages)) or not isinstance(messages[-1], AIMessage):
                if calls:
                    messages.append(await tools[calls[0]["name"]].ainvoke(calls[0]))
                else:
                    messages.append(await handle.model.ainvoke(messages))
        except Paused as paused:
            self._pending[thread] = (paused.pause,)
        return EngineOutcome(values={"messages": list(messages)}, pending=self._pending[thread])

    async def state(self, handle: AgentSpec, thread_id: str) -> ThreadState:
        pending = self._pending.get(thread_id, ())
        messages = list(self._threads.get(thread_id, []))
        return ThreadState(values={"messages": messages}, pending=pending, has_next=bool(pending))

    def pause(self, value: Any) -> Any:
        if self._decisions:
            return self._decisions.pop()
        raise Paused(Pause("pause-1", value))

    def has_decision(self) -> bool:
        return bool(self._decisions)

    def pauses(self, error: BaseException) -> tuple[Pause, ...]:
        return (error.pause,) if isinstance(error, Paused) else ()

    def is_step_limit(self, error: BaseException) -> bool:
        return False

    def in_thread(self) -> bool:
        return False

    def detached(self) -> contextvars.Context:
        return contextvars.copy_context()

    @contextmanager
    def outside_run(self, *, stream_text: bool = False) -> Iterator[None]:
        yield


def _open_calls(messages: list[BaseMessage]) -> list[ToolCall]:
    """The tool calls of the last model message that have no result yet."""

    asked = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    if asked is None:
        return []
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return [call for call in asked.tool_calls if call["id"] not in answered]
