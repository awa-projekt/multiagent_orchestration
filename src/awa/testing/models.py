"""Deterministic chat models for testing agents without network access.

Both are real `BaseChatModel`s with tool binding, so they drive the genuine `create_agent` loop
offline.
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, PrivateAttr

from awa._internal.upstream.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from awa._internal.upstream.messages import AIMessage, AIMessageChunk, BaseMessage, ToolCall
from awa._internal.upstream.models import (
    BaseChatModel,
    ChatGeneration,
    ChatGenerationChunk,
    ChatResult,
    LanguageModelInput,
)
from awa._internal.upstream.runnables import Runnable
from awa._internal.upstream.tools import BaseTool, convert_to_openai_tool


@dataclass(frozen=True)
class ModelCall:
    """One recorded call to a fake model.

    Attributes:
        tool_names: Names of the tools bound for this call.
        index: Zero-based position of the call on this model instance.
    """

    messages: tuple[BaseMessage, ...]
    tool_names: tuple[str, ...]
    index: int

    @property
    def last_message(self) -> BaseMessage:
        """The final message of the prompt (usually the newest user or tool message)."""

        return self.messages[-1]


ScriptItem = AIMessage | str | ToolCall | Sequence[ToolCall] | Callable[[ModelCall], Any]
"""One scripted response.

- `str`: a final text answer.
- `ToolCall` (see `tool_call`) or a list of them: an assistant turn requesting those tool calls.
- `AIMessage`: used as-is.
- a callable taking the `ModelCall` and returning any of the above.
"""


def tool_call(name: str, args: dict[str, Any] | None = None, *, id: str | None = None) -> ToolCall:
    """A tool call for a fake model script (alone, or in a list of one turn's calls); `id` random when omitted."""

    return ToolCall(name=name, args=dict(args or {}), id=id or f"call_{uuid4().hex[:12]}")


def structured_output(schema: type[BaseModel] | str, data: dict[str, Any]) -> ToolCall:
    """The tool call that finishes a run with structured output `data` for `schema` (a class or its name).

    `create_agent` implements `response_format` for models without native structured output as a
    tool named after the schema class.
    """

    name = schema if isinstance(schema, str) else schema.__name__
    return tool_call(name, data)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _to_ai_message(item: Any) -> AIMessage:
    if isinstance(item, AIMessage):
        return item
    if isinstance(item, str):
        return AIMessage(content=item)
    if isinstance(item, dict) and "name" in item and "args" in item:
        item = [item]
    if isinstance(item, Sequence) and all(isinstance(c, dict) for c in item):
        calls = [tool_call(c["name"], c.get("args"), id=c.get("id")) for c in item]
        return AIMessage(content="", tool_calls=calls)
    raise TypeError(
        f"Unsupported fake model response {item!r}; expected str, AIMessage, "
        "a tool call, or a list of tool calls"
    )


class _RecordingChatModel(BaseChatModel):
    """Records calls, attaches usage metadata, and optionally streams text in fixed-size chunks."""

    stream_chunk_size: int | None = None
    model_name: str | None = None
    _calls: list[ModelCall] = PrivateAttr(default_factory=list)
    _calls_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def __init__(self, **kwargs: Any) -> None:
        chunk_size = kwargs.get("stream_chunk_size")
        if chunk_size is not None and chunk_size < 1:
            raise ValueError("stream_chunk_size must be at least 1")
        # Without chunking, never take the streaming path, so every model call
        # produces exactly one message regardless of the callbacks attached.
        kwargs.setdefault("disable_streaming", chunk_size is None)
        super().__init__(**kwargs)

    @property
    def calls(self) -> list[ModelCall]:
        """Every call made to this model so far, in order."""

        return self._calls

    @property
    def call_count(self) -> int:
        """Number of calls made to this model so far."""

        return len(self._calls)

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        formatted = [convert_to_openai_tool(t) for t in tools]
        return self.bind(tools=formatted, **kwargs)

    def _respond(self, call: ModelCall) -> Any:
        raise NotImplementedError

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        tool_names = tuple(t.get("function", {}).get("name", "") for t in kwargs.get("tools") or [])
        with self._calls_lock:
            call = ModelCall(
                messages=tuple(messages), tool_names=tool_names, index=len(self._calls)
            )
            self._calls.append(call)

        item = self._respond(call)
        if callable(item):
            item = item(call)
        message = _to_ai_message(item)
        update: dict[str, Any] = {}
        if self.model_name and not message.response_metadata.get("model_name"):
            update["response_metadata"] = {
                **message.response_metadata,
                "model_name": self.model_name,
            }
        if message.usage_metadata is None:
            input_tokens = _estimate_tokens("".join(str(m.content) for m in messages))
            output_tokens = _estimate_tokens(str(message.content)) + 8 * len(message.tool_calls)
            update["usage_metadata"] = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
        if update:
            message = message.model_copy(update=update)
        return ChatResult(generations=[ChatGeneration(message=message)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        message = self._generate(messages, stop=stop, **kwargs).generations[0].message
        assert isinstance(message, AIMessage)
        text = message.text
        size = self.stream_chunk_size or max(1, len(text))
        for start in range(0, len(text), size):
            chunk = text[start : start + size]
            if run_manager is not None:
                await run_manager.on_llm_new_token(chunk)
            yield ChatGenerationChunk(message=AIMessageChunk(content=chunk))
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                tool_calls=message.tool_calls,
                usage_metadata=message.usage_metadata,
                response_metadata=message.response_metadata,
                chunk_position="last",
            )
        )


class FakeChatModel(_RecordingChatModel):
    """A chat model that returns scripted responses in order.

    Args:
        responses: The script; see `ScriptItem` for the accepted forms.
        repeat_last: When the script is exhausted, keep returning the last response.
        allow_unconsumed: Exempt this model from `expect_scripts_consumed`, for a test that
            deliberately ends before the script does.
        model_name: The model name (the key of `Usage.by_model`), also reported in each response's
            metadata unless a scripted `AIMessage` reports its own.
        stream_chunk_size: Stream text in chunks of this many characters; by default each response
            arrives as one message.

    Raises:
        AssertionError: On a call after the script is exhausted (unless `repeat_last`).

    Example:
        ```python
        model = FakeChatModel([
            tool_call("search", {"query": "hybrid search"}),
            "Hybrid search combines lexical and vector retrieval.",
        ])
        agent = Agent("researcher", model=model, tools=[search])
        ```
    """

    responses: list[Any]
    repeat_last: bool = False
    allow_unconsumed: bool = False

    def __init__(
        self,
        responses: Sequence[ScriptItem],
        *,
        repeat_last: bool = False,
        allow_unconsumed: bool = False,
        model_name: str | None = None,
        stream_chunk_size: int | None = None,
        **kwargs: Any,
    ) -> None:
        if not responses:
            raise ValueError("FakeChatModel needs at least one response")
        super().__init__(
            responses=list(responses),
            repeat_last=repeat_last,
            allow_unconsumed=allow_unconsumed,
            model_name=model_name,
            stream_chunk_size=stream_chunk_size,
            **kwargs,
        )
        for created in _RECORDS.get():
            created.append(self)

    @property
    def _llm_type(self) -> str:
        return "awa-fake"

    @property
    def remaining(self) -> int:
        """Number of scripted responses not yet returned."""

        return max(0, len(self.responses) - self.call_count)

    def _respond(self, call: ModelCall) -> Any:
        if call.index < len(self.responses):
            return self.responses[call.index]
        if self.repeat_last:
            return self.responses[-1]
        raise AssertionError(
            f"FakeChatModel script exhausted after {len(self.responses)} responses; "
            f"call #{call.index + 1} received last message: {call.last_message.content!r}"
        )


_RECORDS: ContextVar[tuple[list[FakeChatModel], ...]] = ContextVar("awa_fake_records", default=())


@contextmanager
def expect_scripts_consumed() -> Iterator[list[FakeChatModel]]:
    """Fail when a `FakeChatModel` created inside the block was called but has responses left.

    Leftover responses mean the test stopped before the run it scripted. A model never called is a
    placeholder and is not checked, nor is one created with `allow_unconsumed=True`.

    Yields:
        The fake models created inside the block; the check covers the ones still in the list.

    Raises:
        AssertionError: On a clean exit, naming each model with responses left over.
    """

    created: list[FakeChatModel] = []
    token = _RECORDS.set((*_RECORDS.get(), created))
    try:
        yield created
    finally:
        _RECORDS.reset(token)
    left = [m for m in created if m.call_count and m.remaining and not m.allow_unconsumed]
    if left:
        details = "; ".join(
            f"{m.remaining} of {len(m.responses)} left, next {m.responses[m.call_count]!r}"
            for m in left
        )
        raise AssertionError(
            f"FakeChatModel script not fully consumed ({details}): the test ended before the run "
            "it scripted. Assert on the missing steps, shorten the script, or pass "
            "allow_unconsumed=True."
        )


class FunctionChatModel(_RecordingChatModel):
    """A chat model whose every response is computed by `function(call)`.

    Args:
        function: Receives the `ModelCall` and returns any `ScriptItem`; an exception it raises is
            raised by the model call, like a provider error.
        model_name: The model name, as for `FakeChatModel`.
        stream_chunk_size: Stream text in chunks of this many characters, as for `FakeChatModel`.

    Example:
        ```python
        model = FunctionChatModel(lambda call: f"You said: {call.last_message.content}")
        ```
    """

    function: Callable[[ModelCall], Any]

    def __init__(
        self,
        function: Callable[[ModelCall], Any],
        *,
        model_name: str | None = None,
        stream_chunk_size: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            function=function, model_name=model_name, stream_chunk_size=stream_chunk_size, **kwargs
        )

    @property
    def _llm_type(self) -> str:
        return "awa-function"

    def _respond(self, call: ModelCall) -> Any:
        return self.function(call)


__all__ = [
    "FakeChatModel",
    "FunctionChatModel",
    "ModelCall",
    "ScriptItem",
    "expect_scripts_consumed",
    "structured_output",
    "tool_call",
]
