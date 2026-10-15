"""Backend protocols of the runtime: what awa's caches need from a store.

`awa.persistence.cache` ships an in-memory and a SQLite backend (`open_cache_store` picks one);
every backend passes `awa.testing.contracts.CacheStoreContract`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

__all__ = ["CacheKindStats", "CacheScope", "CacheStore"]

_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}")


@dataclass(frozen=True)
class CacheScope:
    """Where a cache entry belongs; entries of two scopes never meet.

    Each part is letters, digits and `_ . : @ -`, at most 128 characters.

    Attributes:
        tenant: The tenant (`"local"` for a single-tenant install).
        project: The project or system (a blueprint name).
        kind: `"tools"`, `"steps"` or `"replay"`.
    """

    tenant: str
    project: str
    kind: str

    def __post_init__(self) -> None:
        for name in ("tenant", "project", "kind"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _PART.fullmatch(value):
                raise ValueError(
                    f"Cache scope {name} must be 1-128 letters, digits or _ . : @ -; got {value!r}"
                )


@dataclass(frozen=True)
class CacheKindStats:
    """What one scope of a store holds.

    Attributes:
        entries: Entries that have not expired.
        bytes: Their total size (values only).
        hits: How often entries of the scope were read since they were written.
    """

    tenant: str
    project: str
    kind: str
    entries: int
    bytes: int
    hits: int


class CacheStore(Protocol):
    """A store of cache entries: bytes under a key, inside a scope, with an optional lifetime.

    Methods are synchronous and short. A missing or expired entry is no error; any other failure
    raises `awa.errors.CacheStoreError`, and awa's caches carry on without the cache.
    """

    def get(self, scope: CacheScope, key: str) -> bytes | None:
        """The value stored under `key` in `scope`, or `None` when there is none or it expired."""
        ...

    def put(self, scope: CacheScope, key: str, value: bytes, *, ttl: float | None = None) -> None:
        """Store `value` under `key` (at most 256 characters) in `scope`; `ttl` `None` never expires."""
        ...

    def entries(self, scope: CacheScope, *, limit: int = 100) -> list[tuple[str, bytes]]:
        """Up to `limit` entries of `scope` that have not expired, newest first."""
        ...

    def clear(
        self, *, tenant: str | None = None, project: str | None = None, kind: str | None = None
    ) -> int:
        """Delete the entries matching every part given (all when none is) and return how many."""
        ...

    def stats(self) -> list[CacheKindStats]:
        """One line per scope that holds entries, sorted by tenant, project and kind."""
        ...

    def close(self) -> None:
        """Release the store's resources; a closed store is not used again."""
        ...
