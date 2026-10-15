"""Provider models built by awa keep working across event loops.

A script that calls `asyncio.run` (or `run_sync`) twice works on the second call too: the
cached model's HTTP client does not keep connections of the first, closed loop (which would
raise `RuntimeError: Event loop is closed`). These tests run the real provider model
classes against a local HTTP server (no network access, dummy keys).
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

import pytest

from awa import Agent
from awa._internal import aio as asyncio_compat
from awa.core.settings import Settings
from awa.models import (
    DEFAULT_REQUEST_TIMEOUT,
    ModelRegistry,
    ModelSettings,
    loop_local_openai_client,
    resolve_model,
)

pytest.importorskip("langchain_openai")

pytestmark = pytest.mark.model_requests_allowed

_CHAT_COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 0,
    "model": "gpt-test",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_ANTHROPIC_MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-test",
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: connections are pooled by the client

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length", 0)))
        payload = _CHAT_COMPLETION if "chat/completions" in self.path else _ANTHROPIC_MESSAGE
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _registry(server_url: str) -> ModelRegistry:
    registry = ModelRegistry(
        Settings(
            azure_openai_api_key="dummy-key",
            azure_openai_endpoint=server_url,
            azure_deployment="deployment",
            azure_api_version="2024-10-21",
            openai_api_key="dummy-key",
            anthropic_api_key="dummy-key",
        )
    )
    registry.register("azure", "azure_openai:gpt-test")
    registry.register(
        "openai", "openai:gpt-test", settings=ModelSettings(extra={"base_url": f"{server_url}/v1"})
    )
    registry.register(
        "anthropic", "anthropic:claude-test", settings=ModelSettings(extra={"base_url": server_url})
    )
    return registry


async def _ask(registry: ModelRegistry, profile: str) -> str:
    model = resolve_model(profile, registry=registry)
    return (await model.ainvoke("hi")).text


@pytest.mark.parametrize("profile", ["azure", "openai"])
def test_a_cached_model_works_in_consecutive_asyncio_runs(server_url: str, profile: str) -> None:
    registry = _registry(server_url)

    answers = [asyncio.run(_ask(registry, profile)) for _ in range(3)]

    assert answers == ["hello"] * 3
    assert resolve_model(profile, registry=registry) is resolve_model(profile, registry=registry)


@pytest.mark.parametrize("profile", ["azure", "openai"])
def test_a_cached_model_works_in_consecutive_asyncio_compat_runs(
    server_url: str, profile: str
) -> None:
    registry = _registry(server_url)

    assert asyncio_compat.run(_ask(registry, profile)) == "hello"
    assert asyncio_compat.run(_ask(registry, profile)) == "hello"


@pytest.mark.parametrize("profile", ["azure", "openai"])
def test_run_sync_can_be_called_twice_with_a_provider_model(server_url: str, profile: str) -> None:
    agent = Agent("assistant", model=profile, model_registry=_registry(server_url))

    assert agent.run_sync("Hi").output == "hello"
    assert agent.run_sync("Hi again").output == "hello"


def test_a_model_works_in_threads_with_their_own_loops(server_url: str) -> None:
    registry = _registry(server_url)
    answers: list[str] = []

    def work() -> None:
        answers.append(asyncio.run(_ask(registry, "azure")))

    threads = [threading.Thread(target=work) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert answers == ["hello"] * 3


def test_anthropic_models_work_in_consecutive_asyncio_runs(server_url: str) -> None:
    pytest.importorskip("langchain_anthropic")
    registry = _registry(server_url)

    assert [asyncio.run(_ask(registry, "anthropic")) for _ in range(2)] == ["hello", "hello"]


@pytest.mark.parametrize("profile", ["azure", "openai"])
def test_models_keep_the_default_request_timeout(server_url: str, profile: str) -> None:
    model: Any = resolve_model(profile, registry=_registry(server_url))

    assert model.request_timeout == DEFAULT_REQUEST_TIMEOUT
    assert model.root_async_client.timeout == DEFAULT_REQUEST_TIMEOUT
    assert type(model.http_async_client) is type(loop_local_openai_client())


def test_http_client_options_in_extra_replace_the_loop_local_clients() -> None:
    registry = ModelRegistry(Settings())
    own = loop_local_openai_client()  # not copied: HTTP clients cannot be deep-copied
    with patch("awa.models.registry.init_chat_model") as init:
        registry.create("openai:gpt-x", ModelSettings(extra={"http_async_client": own}))
        registry.create("openai:gpt-x", ModelSettings(extra={"openai_proxy": "http://proxy:1"}))
        registry.create("azure_openai:gpt-x")
        registry.create("anthropic:claude-x")

    first, proxied, azure, anthropic = (call.kwargs for call in init.call_args_list)
    assert first["http_async_client"] is own
    assert "http_client" not in first
    assert "http_async_client" not in proxied and "http_client" not in proxied
    assert type(azure["http_async_client"]) is type(loop_local_openai_client())
    assert "http_client" not in azure  # AzureChatOpenAI builds the SDK default itself
    assert "http_async_client" not in anthropic


def test_an_explicit_model_instance_is_used_as_is() -> None:
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(model="gpt-test", api_key="dummy-key")  # type: ignore[arg-type]

    assert resolve_model(model) is model
    assert model.http_async_client is None


def test_the_per_loop_client_is_closed_when_its_loop_shuts_down(server_url: str) -> None:
    model: Any = resolve_model("azure", registry=_registry(server_url))
    client = model.http_async_client
    inner: list[Any] = []

    async def ask() -> None:
        await model.ainvoke("hi")
        inner.append(client._awa_clients.peek())

    asyncio.run(ask())
    asyncio.run(ask())

    assert inner[0] is not inner[1]
    assert all(each.is_closed for each in inner)
    assert len(client._awa_clients) == 0
    assert not client.is_closed


async def test_a_closed_loop_local_client_refuses_requests(server_url: str) -> None:
    client = loop_local_openai_client()
    response = await client.post(f"{server_url}/v1/chat/completions", json={})
    assert response.json()["choices"][0]["message"]["content"] == "hello"
    inner = client._awa_clients.peek()

    await client.aclose()

    assert client.is_closed and inner.is_closed
    with pytest.raises(RuntimeError, match="closed"):
        await client.post(f"{server_url}/v1/chat/completions", json={})


async def test_leaving_a_loop_local_client_closes_its_connections(server_url: str) -> None:
    async with loop_local_openai_client() as client:
        await client.post(f"{server_url}/v1/chat/completions", json={})
        inner = client._awa_clients.peek()

    assert client.is_closed and inner.is_closed


def test_a_loop_local_client_refuses_shared_transports() -> None:
    with pytest.raises(TypeError, match="transport"):
        loop_local_openai_client(transport=object())
