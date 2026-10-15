"""LangChain callbacks that account a foreign graph's model and tool calls in a `RunContext`."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from awa._internal.upstream.callbacks import AsyncCallbackHandler
from awa._internal.upstream.messages import AIMessage, BaseMessage, ToolMessage
from awa._internal.upstream.models import ChatGeneration, LLMResult
from awa.agents.middleware import record_model_response
from awa.contracts.events import TextDeltaEvent, ToolResultEvent
from awa.errors import ModelResolutionError
from awa.models.guard import OFFLINE_LLM_TYPES, model_requests_allowed
from awa.runtime.context import RunContext, current_run_context
from awa.runtime.engine import current_engine

# The node `create_agent` runs the model in: `TextDeltaCallback` streams its text already.
_MODEL_NODE = "model"


class GraphRunTracking(AsyncCallbackHandler):
    """Records usage, emits run events and enforces limits for one graph run, through callbacks.

    Only calls made while the graph's own `RunContext` is current are counted, so awa agents inside
    the graph are not counted twice. With `stream_text`, text tokens outside a `"model"` node are
    emitted as `TextDeltaEvent`s.
    """

    run_inline = True
    raise_error = True

    def __init__(self, ctx: RunContext[Any], *, stream_text: bool = False) -> None:
        super().__init__()
        self._ctx = ctx
        self._stream_text = stream_text
        self._calls: dict[UUID, bool] = {}
        self._tools: dict[UUID, tuple[str, str]] = {}

    def _own(self) -> RunContext[Any] | None:
        ctx = current_run_context()
        return ctx if ctx is self._ctx else None

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
        ctx = self._own()
        if ctx is None:
            return
        llm_type = (kwargs.get("invocation_params") or {}).get("_type")
        if not model_requests_allowed() and llm_type not in OFFLINE_LLM_TYPES:
            raise ModelResolutionError(
                f"Real model requests are blocked (set_allow_model_requests(False)); graph "
                f"{ctx.agent!r} called a {llm_type or 'chat'} model. Build the graph with "
                "awa.testing.FakeChatModel or FunctionChatModel for tests."
            )
        ctx.admit_request()
        # `TextDeltaCallback` streams the model node of `create_agent` already.
        streams = (metadata or {}).get("langgraph_node") != _MODEL_NODE
        self._calls[run_id] = self._stream_text and streams

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
        if not self._calls.get(run_id):
            return
        message = getattr(chunk, "message", None)
        text = str(message.text) if isinstance(message, BaseMessage) else token
        if isinstance(text, str) and text:
            await self._ctx.emit(TextDeltaEvent(delta=text, **self._ctx.event_fields()))

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        if self._calls.pop(run_id, None) is None:
            return
        ctx = self._ctx
        ctx.usage.request_ended()
        message = _response_message(response)
        if message is None:
            ctx.usage.record_model_call(ctx.agent)
        else:
            await record_model_response(ctx, message, frozenset(), None)
        ctx.check_limits()

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        if self._calls.pop(run_id, None) is None:
            return
        self._ctx.usage.request_ended()
        # A failed attempt was still a request to the provider.
        self._ctx.usage.record_model_call(self._ctx.agent)

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if self._own() is None:
            return
        name = str(serialized.get("name") or kwargs.get("name") or "tool")
        self._tools[run_id] = (name, str(kwargs.get("tool_call_id") or ""))

    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        call = self._tools.pop(run_id, None)
        if call is None:
            return
        if isinstance(output, ToolMessage):
            content, is_error = str(output.text), output.status == "error"
        else:
            content, is_error = str(output), False
        await self._ctx.emit(
            ToolResultEvent(
                tool_name=call[0],
                call_id=call[1],
                content=content,
                is_error=is_error,
                **self._ctx.event_fields(),
            )
        )

    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        call = self._tools.pop(run_id, None)
        if call is None or isinstance(error, current_engine().control_flow):
            return
        await self._ctx.emit(
            ToolResultEvent(
                tool_name=call[0],
                call_id=call[1],
                content=f"{type(error).__name__}: {error}",
                is_error=True,
                **self._ctx.event_fields(),
            )
        )


def _response_message(response: LLMResult) -> AIMessage | None:
    for generations in response.generations:
        for generation in generations:
            if isinstance(generation, ChatGeneration) and isinstance(generation.message, AIMessage):
                return generation.message
    return None
