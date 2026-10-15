"""Upstream cache interfaces over awa's byte store: a LangChain model cache, a LangGraph node cache.

Both take `read(key)` and `write(key, value, ttl)`, so store, scope and failure handling stay
awa's; the process-wide `set_llm_cache` is never set.
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from langchain_core.caches import BaseCache as _LLMCache
from langchain_core.load import dumps, loads
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, Generation
from langgraph.cache.base import BaseCache as _GraphCache
from langgraph.types import CachePolicy

from awa._internal.upstream.checkpoint import state_serde

__all__ = ["CachePolicy", "NodeCache", "ResponseCache"]

_GENERATION_TYPES: list[Any] = [
    ChatGeneration,
    ChatGenerationChunk,
    Generation,
    AIMessage,
    AIMessageChunk,
]
"""The only classes a stored response may revive: a model's answer, never configuration."""

Read = Callable[[str], bytes | None]
Write = Callable[[str, bytes, float | None], None]


class ResponseCache(_LLMCache):
    """Model responses of one chat model under a key of its settings and the prompt.

    Args:
        key: Turns LangChain's `(prompt, llm_string)` into the store key.
        on_hit: Rewrites each cached message before it is returned (for example to mark it).
    """

    def __init__(
        self,
        read: Read,
        write: Write,
        *,
        key: Callable[[str, str], str],
        on_hit: Callable[[Any], Any] | None = None,
    ) -> None:
        self._read = read
        self._write = write
        self._key = key
        self._on_hit = on_hit

    def lookup(self, prompt: str, llm_string: str) -> Any:
        stored = self._read(self._key(prompt, llm_string))
        if stored is None:
            return None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # `loads` is marked beta upstream
                generations = loads(stored.decode("utf-8"), allowed_objects=_GENERATION_TYPES)
        except Exception:  # noqa: BLE001 - an unreadable entry is a miss
            return None
        if self._on_hit is not None:
            for generation in generations:
                message = getattr(generation, "message", None)
                if message is not None:
                    generation.message = self._on_hit(message)
        return generations

    def update(self, prompt: str, llm_string: str, return_val: Sequence[Any]) -> None:
        self._write(self._key(prompt, llm_string), dumps(list(return_val)).encode("utf-8"), None)

    def clear(self, **kwargs: Any) -> None:
        """Entries are cleared through awa's store (`awa cache clear`)."""

    async def alookup(self, prompt: str, llm_string: str) -> Any:
        return await asyncio.to_thread(self.lookup, prompt, llm_string)

    async def aupdate(self, prompt: str, llm_string: str, return_val: Sequence[Any]) -> None:
        await asyncio.to_thread(self.update, prompt, llm_string, return_val)


class NodeCache(_GraphCache[Any]):
    """LangGraph node results in awa's store; a namespace and key become one store key.

    Entries are read with the strict state serializer, so a tampered store cannot revive
    arbitrary classes.
    """

    def __init__(self, read: Read, write: Write) -> None:
        super().__init__(serde=state_serde())
        self._read_bytes = read
        self._write_bytes = write

    @staticmethod
    def _flat(full: tuple[tuple[str, ...], str]) -> str:
        namespace, key = full
        return "/".join((*namespace, key))

    def get(self, keys: Sequence[tuple[tuple[str, ...], str]]) -> dict[Any, Any]:
        found: dict[Any, Any] = {}
        for full in keys:
            stored = self._read_bytes(self._flat(full))
            if stored is None:
                continue
            encoding, _, payload = stored.partition(b"\n")
            try:
                found[full] = self.serde.loads_typed((encoding.decode("ascii"), payload))
            except Exception:  # noqa: BLE001 - an unreadable entry is a miss
                continue
        return found

    async def aget(self, keys: Sequence[tuple[tuple[str, ...], str]]) -> dict[Any, Any]:
        return await asyncio.to_thread(self.get, keys)

    def set(self, pairs: Mapping[tuple[tuple[str, ...], str], tuple[Any, int | None]]) -> None:
        for full, (value, ttl) in pairs.items():
            encoding, payload = self.serde.dumps_typed(value)
            self._write_bytes(
                self._flat(full),
                encoding.encode("ascii") + b"\n" + payload,
                float(ttl) if ttl else None,
            )

    async def aset(
        self, pairs: Mapping[tuple[tuple[str, ...], str], tuple[Any, int | None]]
    ) -> None:
        await asyncio.to_thread(self.set, pairs)

    def clear(self, namespaces: Sequence[tuple[str, ...]] | None = None) -> None:
        """Entries are cleared through awa's store (`awa cache clear`)."""

    async def aclear(self, namespaces: Sequence[tuple[str, ...]] | None = None) -> None:
        """Entries are cleared through awa's store (`awa cache clear`)."""
