"""Contracts of chat models, `init_chat_model` and the callbacks awa streams text through."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any, TypeVar
from uuid import UUID

import pytest
from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage

from awa.testing import FakeChatModel

T = TypeVar("T")


def broken(awa_module: str, what: str) -> str:
    return f"Upstream contract broken: {what}. awa depends on it in {awa_module}."


@pytest.mark.parametrize(
    ("spec", "module"),
    [
        ("openai:gpt-4o-mini", "langchain_openai"),
        ("anthropic:claude-sonnet-4-5", "langchain_anthropic"),
    ],
)
def test_init_chat_model_parses_provider_prefixes(spec: str, module: str) -> None:
    pytest.importorskip(module)
    model = init_chat_model(spec, api_key="sk-test", max_retries=0)
    assert isinstance(model, BaseChatModel), broken(
        "awa.models.registry", f"init_chat_model({spec!r}) returns a chat model"
    )
    assert type(model).__module__.startswith(module), broken(
        "awa.models.registry", f"the '{spec.split(':')[0]}:' prefix selects {module}"
    )
    name = getattr(model, "model_name", None) or getattr(model, "model", None)
    assert name == spec.split(":", 1)[1], broken(
        "awa.models.registry", "the text after the prefix is the model name"
    )


def test_init_chat_model_rejects_an_unknown_provider() -> None:
    with pytest.raises(ValueError):
        init_chat_model("no-such-provider:model")


class _Tap(AsyncCallbackHandler):
    """What `awa.runtime._streaming.TextDeltaCallback` relies on, reduced to a probe."""

    run_inline = True

    def __init__(self) -> None:
        super().__init__()
        self.nodes: list[Any] = []
        self.tokens: list[str] = []

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.nodes.append((metadata or {}).get("langgraph_node"))

    async def on_llm_new_token(self, token: str, *, chunk: Any = None, **kwargs: Any) -> None:
        message = getattr(chunk, "message", None)
        self.tokens.append(str(message.text) if isinstance(message, BaseMessage) else token)

    async def tap_output_aiter(self, run_id: UUID, output: AsyncIterator[T]) -> AsyncIterator[T]:
        async for item in output:
            yield item

    def tap_output_iter(self, run_id: UUID, output: Iterator[T]) -> Iterator[T]:
        yield from output


async def test_a_streaming_callback_receives_text_deltas_of_the_model_node() -> None:
    tap = _Tap()
    model = FakeChatModel(["Hello there, world"], stream_chunk_size=5)
    agent = create_agent(model)
    result = await agent.ainvoke({"messages": [HumanMessage("hi")]}, {"callbacks": [tap]})
    assert tap.nodes == ["model"], broken(
        "awa.runtime._streaming", "create_agent's model node is named 'model' (langgraph_node)"
    )
    assert len(tap.tokens) > 1 and "".join(tap.tokens) == "Hello there, world", broken(
        "awa.runtime._streaming (TextDeltaEvent)",
        "a handler implementing tap_output_aiter/tap_output_iter makes ainvoke stream "
        "on_llm_new_token chunks",
    )
    assert result["messages"][-1].content == "Hello there, world"
