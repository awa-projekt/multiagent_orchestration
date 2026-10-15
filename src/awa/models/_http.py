"""OpenAI SDK HTTP clients with one connection pool per event loop, so a model reused by a
second `asyncio.run` does not fail with "Event loop is closed"."""

from __future__ import annotations

import functools
from typing import Any

from awa._internal.aio import LoopLocal

# Keyword arguments that configure the HTTP clients of an OpenAI model: when one
# is given, the caller chose the clients and awa leaves them alone.
_CUSTOM_HTTP_KWARGS = ("http_client", "http_async_client", "openai_proxy", "http_socket_options")


class _NoTransport:
    """The transport of the outer client, which never sends: every loop has its own client."""

    async def handle_async_request(self, request: Any) -> Any:
        raise RuntimeError("loop-local HTTP client: requests are sent by the per-loop client")

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> _NoTransport:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


@functools.cache
def _client_class() -> type[Any]:
    import openai

    # openai >= 3 builds on `httpx2`, older versions on `httpx`: subclass whichever
    # the installed SDK uses, so the client is accepted as its `http_client`.
    base: Any = openai.DefaultAsyncHttpxClient

    class LoopLocalAsyncClient(base):  # type: ignore[misc]  # the SDK's client class, see above
        """An OpenAI SDK async HTTP client with one connection pool per event loop."""

        def __init__(self, **kwargs: Any) -> None:
            shared = sorted(key for key in ("transport", "mounts") if key in kwargs)
            if shared:
                raise TypeError(
                    f"loop_local_openai_client() does not take {', '.join(shared)}: "
                    "a transport instance would be shared by every event loop"
                )
            super().__init__(transport=_NoTransport(), **kwargs)
            self._awa_clients: LoopLocal[Any] = LoopLocal(
                lambda: base(**kwargs), aclose=lambda client: client.aclose()
            )

        async def send(self, request: Any, **kwargs: Any) -> Any:
            if self.is_closed:
                raise RuntimeError("Cannot send a request, as the client has been closed.")
            return await self._awa_clients.get().send(request, **kwargs)

        async def aclose(self) -> None:
            await self._close_current()
            await super().aclose()

        async def __aexit__(self, *exc_info: Any) -> None:
            await self._close_current()
            await super().__aexit__(*exc_info)

        async def _close_current(self) -> None:
            client = self._awa_clients.peek()
            if client is not None:
                await client.aclose()

    return LoopLocalAsyncClient


def loop_local_openai_client(**kwargs: Any) -> Any:
    """Return an async HTTP client for the OpenAI SDK that is safe across event loops and threads.

    awa uses it for every `openai:` and `azure_openai:` model built from a string; pass it yourself
    when you construct `ChatOpenAI` or `AzureChatOpenAI` directly.

    Args:
        **kwargs: Options of each per-loop `openai.DefaultAsyncHttpxClient` (`verify`, `proxy`);
            `transport` and `mounts` are refused, since every loop would share them.

    Example:
        ```python
        model = AzureChatOpenAI(azure_deployment="<your-deployment>", api_version="<api-version>",
                                http_async_client=loop_local_openai_client())
        ```
    """

    return _client_class()(**kwargs)


def add_loop_local_clients(
    model: str, kwargs: dict[str, Any], *, chat_openai: bool = False
) -> None:
    """Give an OpenAI or Azure model loop-local async clients (and `ChatOpenAI` the default sync one)."""

    if any(key in kwargs for key in _CUSTOM_HTTP_KWARGS):
        return
    try:
        import openai
    except ImportError:  # init_chat_model reports the missing integration
        return
    kwargs["http_async_client"] = loop_local_openai_client()
    if chat_openai or model.startswith("openai:"):
        kwargs["http_client"] = openai.DefaultHttpxClient()
