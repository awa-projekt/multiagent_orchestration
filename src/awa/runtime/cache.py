"""awa's caches: one settings block (`CacheSettings`) and one runtime object (`Cache`).

One `Cache` per built system caches `prompt` (provider prompt caching), `tools` (read-only tool
results) and `steps` (deterministic process steps). A broken store is a miss, never a failure;
keys are SHA-256 digests scoped by tenant, never secrets.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from awa._internal.logging import get_logger
from awa.runtime.ids import DEFAULT_TENANT
from awa.runtime.ports import CacheScope, CacheStore

if TYPE_CHECKING:
    from awa.core.settings import Settings
    from awa.runtime.context import RunContext

__all__ = [
    "DEFAULT_SALT",
    "Cache",
    "CacheCounts",
    "CacheSettings",
    "PromptCacheMode",
    "cache_key",
    "canonical_json",
    "tenant_of",
]

PromptCacheMode = Literal["auto", "off", "1h"]
"""How agents use provider prompt caching; see `awa.models.prompt_cache`."""

_log = get_logger("cache")


class CacheSettings(BaseModel):
    """The cache settings block: `defaults.cache` in a blueprint, or `Agent(cache=...)`.

    `AWA_CACHE_*` variables win over these values.

    Attributes:
        enabled: Master switch; `False` turns every cache below off.
        prompt: Provider prompt caching: `auto` (Claude requests marked only when the agent has
            tools or its prefix repeats; OpenAI and Azure get a stable `prompt_cache_key`), `1h`
            (Claude always, one-hour lifetime) or `off`.
        prompt_pad: Lengthen an OpenAI/Azure prompt prefix just short of the 1,024-token caching
            minimum (see `awa.models.prompt_cache`).
        tools_ttl: Seconds a read-only tool result is kept across runs; `None` keeps it for the run.
        steps: Reuse deterministic process steps (document checks) while their files are unchanged.
        path: File of the `sqlite` store; `.awa/cache.db` next to the project when unset.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    prompt: PromptCacheMode = "auto"
    prompt_pad: bool = True
    tools: bool = True
    tools_ttl: float | None = Field(default=None, gt=0)
    steps: bool = True
    store: Literal["memory", "sqlite"] = "memory"
    path: str | None = None

    def with_environment(self, settings: Settings) -> CacheSettings:
        """These settings with the project's component switches, then every set `AWA_CACHE_*` value, applied."""

        from awa.plugins.switches import choice

        updates: dict[str, Any] = {}
        for name in ("prompt", "tools", "steps"):
            picked = choice(f"cache.{name}")
            if picked.set_by == "default":
                continue
            if name == "prompt":
                if picked.is_off:
                    updates["prompt"] = "off"
                elif self.prompt == "off":
                    updates["prompt"] = "auto"
            else:
                updates[name] = not picked.is_off
        for name in ("enabled", "prompt", "prompt_pad", "tools", "tools_ttl", "steps"):
            value = getattr(settings, f"cache_{name}", None)
            if value is not None:
                updates[name] = value
        if settings.cache_store is not None:
            updates["store"] = settings.cache_store
        if settings.cache_path is not None:
            updates["path"] = settings.cache_path
        return self.model_copy(update=updates) if updates else self

    def uses(self, kind: str) -> bool:
        """Whether the cache of `kind` (`prompt`, `tools`, `steps`) is on."""

        if not self.enabled:
            return False
        if kind == "prompt":
            return self.prompt != "off"
        return bool(getattr(self, kind, False))

    @property
    def prompt_mode(self) -> PromptCacheMode:
        """The effective provider prompt caching mode (`off` when the cache is disabled)."""

        return self.prompt if self.enabled else "off"

    def summary(self) -> list[tuple[str, str]]:
        """Each switch and its value, as `awa cache stats` shows them."""

        return [
            ("enabled", _on(self.enabled)),
            ("prompt", self.prompt_mode),
            ("tools", _on(self.uses("tools")) + _ttl(self.tools_ttl)),
            ("steps", _on(self.uses("steps"))),
            ("store", self.store + (f" ({self.path})" if self.path else "")),
        ]


def _on(value: bool) -> str:
    return "on" if value else "off"


def _ttl(ttl: float | None) -> str:
    return " (one run)" if ttl is None else f" ({ttl:g} s across runs)"


def canonical_json(value: Any) -> str:
    """`value` as JSON with sorted keys and no spaces, so equal values give equal text."""

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=repr, ensure_ascii=False
    )


def cache_key(*parts: Any) -> str:
    """The SHA-256 hex digest of `parts` as canonical JSON; reveals none of them."""

    return hashlib.sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()


@dataclass
class CacheCounts:
    """Hits and misses of one `Cache`, per kind, since it was made."""

    hits: dict[str, int] = field(default_factory=dict)
    misses: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, dict[str, int]]:
        """`{kind: {"hits": n, "misses": m}}` for every kind seen."""

        kinds = sorted(set(self.hits) | set(self.misses))
        return {k: {"hits": self.hits.get(k, 0), "misses": self.misses.get(k, 0)} for k in kinds}


DEFAULT_SALT = b"awa-prompt-cache"
"""Salt of provider cache keys when `AWA_CACHE_SALT` is not set."""


class Cache:
    """The caches of one system: settings, a store, the salt and the counts.

    Args:
        store: Where entries live; `None` keeps only provider prompt caching.
        project: The project or system name entries belong to (a blueprint's name).
        salt: The project salt (`AWA_CACHE_SALT`) for keys sent to a provider; without one,
            `DEFAULT_SALT`, so whoever knows the prompt and names can recompute the keys.
    """

    def __init__(
        self,
        settings: CacheSettings | None = None,
        store: CacheStore | None = None,
        *,
        project: str = "default",
        salt: str | None = None,
    ) -> None:
        self.settings = settings or CacheSettings()
        self.store = store
        self.project = _scope_part(project)
        self._salt = salt.encode("utf-8") if salt else None
        self.counts = CacheCounts()
        self._lock = threading.Lock()
        self._failed = False

    @property
    def salt(self) -> bytes:
        """The salt of provider cache keys: the configured one, else the fixed `DEFAULT_SALT`."""

        return self._salt if self._salt is not None else DEFAULT_SALT

    def uses(self, kind: str) -> bool:
        """Whether the cache of `kind` is on and, for stored kinds, has a store."""

        if kind == "replay":
            return self.store is not None
        if not self.settings.uses(kind):
            return False
        return kind == "prompt" or self.store is not None

    def scope(self, kind: str, tenant: str | None) -> CacheScope:
        """The scope of `kind` entries of `tenant` in this cache's project."""

        return CacheScope(_scope_part(tenant or DEFAULT_TENANT), self.project, kind)

    def get(self, kind: str, tenant: str | None, key: str) -> bytes | None:
        """The entry, or `None` on a miss or any store failure (logged once); counts it."""

        value: bytes | None = None
        if self.store is not None:
            try:
                value = self.store.get(self.scope(kind, tenant), key)
            except Exception as exc:  # noqa: BLE001 - a broken cache never breaks a run
                self._report(exc)
        self.count(kind, hit=value is not None)
        return value

    def put(
        self, kind: str, tenant: str | None, key: str, value: bytes, *, ttl: float | None
    ) -> None:
        """Store an entry; a store failure is logged once and ignored."""

        if self.store is None:
            return
        try:
            self.store.put(self.scope(kind, tenant), key, value, ttl=ttl)
        except Exception as exc:  # noqa: BLE001 - a broken cache never breaks a run
            self._report(exc)

    def entries(
        self, kind: str, tenant: str | None, *, limit: int = 100
    ) -> list[tuple[str, bytes]]:
        """Entries of `kind` for `tenant`, newest first; `[]` on any store failure."""

        if self.store is None:
            return []
        try:
            return self.store.entries(self.scope(kind, tenant), limit=limit)
        except Exception as exc:  # noqa: BLE001 - a broken cache never breaks a run
            self._report(exc)
            return []

    async def aget(self, kind: str, tenant: str | None, key: str) -> bytes | None:
        """`get` in a worker thread."""

        return await asyncio.to_thread(self.get, kind, tenant, key)

    async def aput(
        self, kind: str, tenant: str | None, key: str, value: bytes, *, ttl: float | None
    ) -> None:
        """`put` in a worker thread."""

        await asyncio.to_thread(self.put, kind, tenant, key, value, ttl=ttl)

    def count(self, kind: str, *, hit: bool) -> None:
        """Count a hit or a miss of `kind`."""

        with self._lock:
            target = self.counts.hits if hit else self.counts.misses
            target[kind] = target.get(kind, 0) + 1

    def _report(self, exc: Exception) -> None:
        with self._lock:
            first, self._failed = not self._failed, True
        if first:
            _log.warning(
                "The cache store failed (%s: %s); runs go on without the cache",
                type(exc).__name__,
                exc,
            )

    def close(self) -> None:
        """Close the store, ignoring failures."""

        if self.store is not None:
            try:
                self.store.close()
            except Exception as exc:  # noqa: BLE001
                self._report(exc)


def _scope_part(value: str) -> str:
    """`value` usable as a `CacheScope` part: kept when valid, else its short digest."""

    try:
        CacheScope(value, "p", "k")
    except ValueError:
        return "h" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:31]
    return value


def tenant_of(source: RunContext[Any] | Mapping[str, Any] | None) -> str:
    """The tenant of a run (`ctx.ids.tenant`, else `awa.tenant` of a metadata mapping), else `"local"`.

    Pass the run context where possible: its tenant is fixed at the top-level run.
    """

    ids = getattr(source, "ids", None)
    if ids is not None:
        return str(ids.tenant)
    value = (source or {}).get("awa.tenant") if isinstance(source, Mapping) else None
    return str(value) if value else DEFAULT_TENANT
