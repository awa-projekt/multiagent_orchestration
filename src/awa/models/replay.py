"""The replay cache: a re-run of an unchanged evaluation answers from recorded model responses.

Inside `replaying(cache)` every resolved model gets a store-backed response cache; replayed
responses carry `response_metadata["awa_replay"] = "hit"` and no usage. For evaluations and test
fixtures only: server, worker and web app refuse to start inside a replay scope.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from awa._internal.upstream.models import BaseChatModel
from awa._internal.util import bound
from awa.errors import ConfigurationError
from awa.models.errors import model_name
from awa.runtime.cache import Cache, cache_key

__all__ = ["REPLAY_METADATA_KEY", "ReplayScope", "active_replay", "refuse_replay", "replaying"]

REPLAY_METADATA_KEY = "awa_replay"
"""`response_metadata` key of a replayed response (`"hit"`)."""

_active: ContextVar[ReplayScope | None] = ContextVar("awa_replay_scope", default=None)


class ReplayScope:
    """Where recorded responses live while a replay scope is open (the cache's `replay` kind)."""

    def __init__(self, cache: Cache, *, tenant: str = "local") -> None:
        if cache.store is None:
            raise ConfigurationError("Replay needs a cache with a store")
        self.cache = cache
        self.tenant = tenant

    @property
    def counts(self) -> dict[str, int]:
        """Replayed (`hits`) and recorded (`misses`) responses so far."""

        return self.cache.counts.as_dict().get("replay", {"hits": 0, "misses": 0})

    def _read(self, key: str) -> bytes | None:
        return self.cache.get("replay", self.tenant, key)

    def _write(self, key: str, value: bytes, ttl: float | None) -> None:
        self.cache.put("replay", self.tenant, key, value, ttl=ttl)

    def bind(self, model: BaseChatModel) -> BaseChatModel:
        """A copy of `model` that answers from and records into this scope."""

        from awa._internal.upstream.caches import ResponseCache

        identity = f"{type(model).__module__}.{type(model).__qualname__}:{model_name(model)}"
        response_cache = ResponseCache(
            self._read,
            self._write,
            key=lambda prompt, settings: cache_key("replay", identity, settings, _stable(prompt)),
            on_hit=_replayed,
        )
        return model.model_copy(update={"cache": response_cache})


_VOLATILE = frozenset({"usage_metadata", "response_metadata", "id"})


def _stable(prompt: str) -> Any:
    """LangChain's serialized prompt without usage, provider metadata and message ids."""

    try:
        data = json.loads(prompt)
    except ValueError:
        return prompt

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in _VOLATILE}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    return clean(data)


def _replayed(message: Any) -> Any:
    """A recorded message marked as replayed, with no token usage."""

    usage = message.usage_metadata
    zeroed = (
        {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0} if usage is not None else None
    )
    return message.model_copy(
        update={
            "usage_metadata": zeroed,
            "response_metadata": {**message.response_metadata, REPLAY_METADATA_KEY: "hit"},
        }
    )


def active_replay() -> ReplayScope | None:
    """The replay scope open in this context, if any."""

    return _active.get()


@contextmanager
def replaying(cache: Cache, *, tenant: str = "local") -> Iterator[ReplayScope]:
    """Open a replay scope: agents resolved inside it record and replay model responses.

    Yields:
        The `ReplayScope`; its `counts` say how many calls were replayed and recorded.

    Raises:
        ConfigurationError: If `cache` has no store.
    """

    scope = ReplayScope(cache, tenant=tenant)
    with bound(_active, scope):
        yield scope


def refuse_replay(where: str) -> None:
    """Refuse to serve live traffic inside a replay scope; `where` names the refusing component.

    Raises:
        ConfigurationError: If a replay scope is open.
    """

    if _active.get() is not None:
        raise ConfigurationError(
            f"{where} refuses the replay cache: replay is for evaluations and tests only"
        )
