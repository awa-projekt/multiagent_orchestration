"""Event and text streaming shared by every runnable's `stream()` method."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Iterator, Mapping
from contextlib import contextmanager, suppress
from typing import Any, TypeVar
from uuid import UUID

from awa._internal.upstream.callbacks import AsyncCallbackHandler, Callbacks
from awa._internal.upstream.messages import BaseMessage
from awa._internal.upstream.runnables import ensure_config, var_child_runnable_config
from awa.contracts.events import RunCompletedEvent, RunEvent, TextDeltaEvent
from awa.contracts.results import RunResult
from awa.runtime.context import EventSubscriber, RunContext, current_run_context

T = TypeVar("T")

MODEL_NODE = "model"
"""The LangGraph node `create_agent` runs the model call in."""


class TextDeltaCallback(AsyncCallbackHandler):
    """Emits a `TextDeltaEvent` for every text token of an agent's model calls.

    Its `tap_output_*iter` methods make chat models stream while it is attached. One handler serves
    a whole run tree, attributing each call to the `RunContext` active when it started; model calls
    inside tools are not streamed.
    """

    run_inline = True

    def __init__(self) -> None:
        super().__init__()
        self._calls: dict[UUID, RunContext[Any]] = {}

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        ctx = current_run_context()
        if ctx is not None and (metadata or {}).get("langgraph_node") == MODEL_NODE:
            self._calls[run_id] = ctx

    async def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        chunk: Any = None,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        ctx = self._calls.get(run_id)
        if ctx is None:
            return
        message = getattr(chunk, "message", None)
        text = str(message.text) if isinstance(message, BaseMessage) else token
        if isinstance(text, str) and text:
            await ctx.emit(TextDeltaEvent(delta=text, **ctx.event_fields()))

    async def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._calls.pop(run_id, None)

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._calls.pop(run_id, None)

    async def tap_output_aiter(self, run_id: UUID, output: AsyncIterator[T]) -> AsyncIterator[T]:
        """Pass `output` through unchanged."""

        async for item in output:
            yield item

    def tap_output_iter(self, run_id: UUID, output: Iterator[T]) -> Iterator[T]:
        """Pass `output` through unchanged."""

        yield from output


def text_delta_callbacks() -> Callbacks | None:
    """The enclosing callbacks plus a `TextDeltaCallback`, or `None` when an outer run streams already."""

    callbacks = ensure_config().get("callbacks")
    if callbacks is None:
        return [TextDeltaCallback()]
    handlers = callbacks if isinstance(callbacks, list) else callbacks.handlers
    if any(isinstance(handler, TextDeltaCallback) for handler in handlers):
        return None
    if isinstance(callbacks, list):
        return [*callbacks, TextDeltaCallback()]
    manager = callbacks.copy()
    manager.add_handler(TextDeltaCallback(), inherit=True)
    return manager


async def stream_run(
    start: Callable[[EventSubscriber], Coroutine[Any, Any, RunResult[Any]]],
    completed_fields: Callable[[], Mapping[str, Any]],
) -> AsyncGenerator[RunEvent, None]:
    """Run `start(subscriber)` as a task and yield its events, ending with a `RunCompletedEvent`.

    A failure is raised after the events delivered so far; closing early cancels the run.
    `completed_fields` are read once the run has finished.
    """

    queue: asyncio.Queue[RunEvent | None] = asyncio.Queue()
    task = asyncio.create_task(start(queue.put_nowait))
    task.add_done_callback(lambda _: queue.put_nowait(None))
    try:
        while (event := await queue.get()) is not None:
            yield event
        result = task.result()
        yield RunCompletedEvent(result=result, **completed_fields())
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


@contextmanager
def text_streaming() -> Iterator[None]:
    """Stream the model text of every agent run started inside the block (for runnables without a graph)."""

    callbacks = text_delta_callbacks()
    if callbacks is None:
        yield
        return
    token = var_child_runnable_config.set(
        {**(var_child_runnable_config.get() or {}), "callbacks": callbacks}
    )
    try:
        yield
    finally:
        var_child_runnable_config.reset(token)
