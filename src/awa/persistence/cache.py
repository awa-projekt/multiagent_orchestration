"""Cache stores, and choosing one from the settings to build a system's `Cache`.

`SqliteCacheStore` keeps entries in a local file shared by the processes of a machine,
`InMemoryCacheStore` keeps them in this process's memory, bounded. A store that cannot be
opened is reported once, and the system runs with provider prompt caching only.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from awa._internal.logging import get_logger
from awa.errors import CacheStoreError, ConfigurationError
from awa.persistence.sql import SqliteConnection
from awa.runtime.cache import Cache, CacheSettings
from awa.runtime.clock import Clock, as_clock
from awa.runtime.ports import CacheKindStats, CacheScope, CacheStore

if TYPE_CHECKING:
    from awa.core.settings import Settings

DEFAULT_CACHE_FILE = Path(".awa") / "cache.db"
"""The `sqlite` cache file, relative to the project folder, when `path` is unset."""

_log = get_logger("cache")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS awa_cache (
    tenant_id TEXT NOT NULL CHECK (tenant_id <> ''),
    project_id TEXT NOT NULL CHECK (project_id <> ''),
    kind TEXT NOT NULL CHECK (kind <> ''),
    key TEXT NOT NULL,
    value BLOB NOT NULL,
    expires_at REAL,
    written_at REAL NOT NULL,
    hits INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, project_id, kind, key));
CREATE INDEX IF NOT EXISTS awa_cache_expiry ON awa_cache (expires_at);
"""

_SLOT = "tenant_id = ? AND project_id = ? AND kind = ? AND key = ?"
_LIVE = "(expires_at IS NULL OR expires_at > ?)"


class SqliteCacheStore(CacheStore):
    """Cache entries in the SQLite file at `path`; reads skip expired entries, writes delete them.

    Raises:
        CacheStoreError: If the file cannot be opened or set up.
    """

    def __init__(self, path: str | Path, *, clock: Clock | None = None) -> None:
        self._path = Path(path)
        self._clock = as_clock(clock)
        self._db: SqliteConnection | None = None
        self._opening = threading.Lock()
        try:
            self._db = self._open()
        except (OSError, sqlite3.Error) as exc:
            raise CacheStoreError(f"Cannot open the cache file {self._path}: {exc}") from exc

    @property
    def path(self) -> Path:
        """The database file."""

        return self._path

    def _open(self) -> SqliteConnection:
        # The entries hold prompts, answers and tool results: the file is owner-only.
        db = SqliteConnection(self._path, timeout=10)
        try:
            db.conn.executescript(_SCHEMA)
        except BaseException:
            db.close()
            raise
        return db

    @contextmanager
    def _using(self, verb: str, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        try:
            with self._opening:
                if self._db is None:
                    self._db = self._open()
                db = self._db
            with db.writing() if write else db.reading() as conn:
                yield conn
        except (OSError, sqlite3.Error) as exc:
            raise CacheStoreError(f"Cannot {verb} the cache file {self._path}: {exc}") from exc

    def _now(self) -> float:
        return self._clock.now().timestamp()

    def get(self, scope: CacheScope, key: str) -> bytes | None:
        """The live value under `key`, counting the hit."""

        slot = (scope.tenant, scope.project, scope.kind, key)
        with self._using("read", write=True) as conn:
            row = conn.execute(
                f"SELECT value FROM awa_cache WHERE {_SLOT} AND {_LIVE}",  # noqa: S608 - fixed text
                (*slot, self._now()),
            ).fetchone()
            if row is None:
                return None
            hit = f"UPDATE awa_cache SET hits = hits + 1 WHERE {_SLOT}"  # noqa: S608 - fixed text
            conn.execute(hit, slot)
            return bytes(row[0])

    def put(self, scope: CacheScope, key: str, value: bytes, *, ttl: float | None = None) -> None:
        """Store `value`, replacing an earlier entry; drops expired entries."""

        now = self._now()
        slot = (scope.tenant, scope.project, scope.kind, key)
        with self._using("write", write=True) as conn:
            conn.execute(
                "DELETE FROM awa_cache WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
            )
            conn.execute(f"DELETE FROM awa_cache WHERE {_SLOT}", slot)  # noqa: S608 - fixed text
            conn.execute(
                "INSERT INTO awa_cache "
                "(tenant_id, project_id, kind, key, value, expires_at, written_at, hits) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (*slot, sqlite3.Binary(value), None if ttl is None else now + ttl, now),
            )

    def entries(self, scope: CacheScope, *, limit: int = 100) -> list[tuple[str, bytes]]:
        """Live entries of `scope`, newest first."""

        with self._using("read") as conn:
            rows = conn.execute(
                "SELECT key, value FROM awa_cache WHERE tenant_id = ? AND project_id = ? "
                f"AND kind = ? AND {_LIVE} ORDER BY written_at DESC, rowid DESC LIMIT ?",  # noqa: S608
                (scope.tenant, scope.project, scope.kind, self._now(), max(0, limit)),
            ).fetchall()
        return [(str(key), bytes(value)) for key, value in rows]

    def clear(
        self, *, tenant: str | None = None, project: str | None = None, kind: str | None = None
    ) -> int:
        """Delete the entries that match every part given."""

        conditions, params = [], []
        for column, value in (("tenant_id", tenant), ("project_id", project), ("kind", kind)):
            if value is not None:
                conditions.append(f"{column} = ?")
                params.append(value)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._using("clear", write=True) as conn:
            return conn.execute(f"DELETE FROM awa_cache{where}", params).rowcount  # noqa: S608

    def stats(self) -> list[CacheKindStats]:
        """Entries, bytes and hits per scope."""

        with self._using("read") as conn:
            rows = conn.execute(
                "SELECT tenant_id, project_id, kind, COUNT(*), COALESCE(SUM(LENGTH(value)), 0), "
                f"COALESCE(SUM(hits), 0) FROM awa_cache WHERE {_LIVE} "  # noqa: S608 - fixed text
                "GROUP BY tenant_id, project_id, kind ORDER BY tenant_id, project_id, kind",
                (self._now(),),
            ).fetchall()
        return [CacheKindStats(t, p, k, int(n), int(size), int(h)) for t, p, k, n, size, h in rows]

    def close(self) -> None:
        """Close the connection; a later call opens it again."""

        with self._opening:
            db, self._db = self._db, None
        if db is not None:
            db.close()


@dataclass
class _Entry:
    value: bytes
    expires: float | None
    written: int
    hits: int = 0


class InMemoryCacheStore(CacheStore):
    """Cache entries in a dictionary of this process; the least recently used go first.

    Args:
        max_entries: Most entries kept (default 10,000).
        max_bytes: Most value bytes kept (default 256 MiB).
    """

    def __init__(
        self,
        *,
        max_entries: int = 10_000,
        max_bytes: int = 256 * 1024 * 1024,
        clock: Clock | None = None,
    ) -> None:
        if max_entries < 1 or max_bytes < 1:
            raise ConfigurationError("max_entries and max_bytes must be at least 1")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._clock = as_clock(clock)
        self._entries: OrderedDict[tuple[CacheScope, str], _Entry] = OrderedDict()
        self._bytes = 0
        self._writes = 0
        self._lock = threading.Lock()

    def _live(self, slot: tuple[CacheScope, str], now: float) -> _Entry | None:
        entry = self._entries.get(slot)
        if entry is not None and entry.expires is not None and entry.expires <= now:
            self._drop(slot)
            return None
        return entry

    def _drop(self, slot: tuple[CacheScope, str]) -> None:
        entry = self._entries.pop(slot, None)
        if entry is not None:
            self._bytes -= len(entry.value)

    def get(self, scope: CacheScope, key: str) -> bytes | None:
        """The live value under `key`, marking it recently used."""

        with self._lock:
            entry = self._live((scope, key), self._clock.now().timestamp())
            if entry is None:
                return None
            entry.hits += 1
            self._entries.move_to_end((scope, key))
            return entry.value

    def put(self, scope: CacheScope, key: str, value: bytes, *, ttl: float | None = None) -> None:
        """Store `value`; evicts the least recently used entries beyond the limits."""

        with self._lock:
            self._drop((scope, key))
            if len(value) > self._max_bytes:
                return  # larger than the whole cache: never kept, and the old value is stale
            self._writes += 1
            expires = None if ttl is None else self._clock.now().timestamp() + ttl
            self._entries[(scope, key)] = _Entry(bytes(value), expires, self._writes)
            self._bytes += len(value)
            while len(self._entries) > self._max_entries or self._bytes > self._max_bytes:
                self._drop(next(iter(self._entries)))

    def entries(self, scope: CacheScope, *, limit: int = 100) -> list[tuple[str, bytes]]:
        """Live entries of `scope`, newest written first."""

        with self._lock:
            now = self._clock.now().timestamp()
            found = [
                (entry.written, key, entry.value)
                for (entry_scope, key), entry in list(self._entries.items())
                if entry_scope == scope and self._live((entry_scope, key), now) is not None
            ]
        found.sort(reverse=True)
        return [(key, value) for _, key, value in found[: max(0, limit)]]

    def clear(
        self, *, tenant: str | None = None, project: str | None = None, kind: str | None = None
    ) -> int:
        """Delete the entries whose scope matches every part given."""

        with self._lock:
            doomed = [
                slot
                for slot in self._entries
                if (tenant is None or slot[0].tenant == tenant)
                and (project is None or slot[0].project == project)
                and (kind is None or slot[0].kind == kind)
            ]
            for slot in doomed:
                self._drop(slot)
        return len(doomed)

    def stats(self) -> list[CacheKindStats]:
        """Entries, bytes and hits per scope."""

        totals: dict[CacheScope, list[int]] = {}
        with self._lock:
            now = self._clock.now().timestamp()
            for slot, entry in list(self._entries.items()):
                if self._live(slot, now) is None:
                    continue
                line = totals.setdefault(slot[0], [0, 0, 0])
                line[0] += 1
                line[1] += len(entry.value)
                line[2] += entry.hits
        return [
            CacheKindStats(s.tenant, s.project, s.kind, n, size, hits)
            for s, (n, size, hits) in sorted(
                totals.items(), key=lambda item: (item[0].tenant, item[0].project, item[0].kind)
            )
        ]

    def close(self) -> None:
        """Drop every entry."""

        with self._lock:
            self._entries.clear()
            self._bytes = 0


def open_cache_store(cache: CacheSettings, *, base: Path | None = None) -> CacheStore:
    """The store `cache.store` names; a relative `path` resolves against `base`.

    Raises:
        CacheStoreError: If the store cannot be opened.
    """

    if cache.store == "memory":
        return InMemoryCacheStore()
    path = Path(cache.path) if cache.path else DEFAULT_CACHE_FILE
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return SqliteCacheStore(path)


def build_cache(
    cache: CacheSettings | None = None,
    *,
    project: str = "default",
    base: Path | None = None,
    settings: Settings | None = None,
) -> Cache:
    """The `Cache` of one system: `cache` with the environment applied, its store and salt.

    Args:
        cache: The blueprint's `defaults.cache` (defaults when `None`).
        project: The project the entries belong to (the blueprint name).
        base: The project folder (for a relative `sqlite` path).
    """

    from awa.core.settings import get_settings

    settings = settings or get_settings()
    resolved = (cache or CacheSettings()).with_environment(settings)
    salt = settings.cache_salt.get_secret_value() if settings.cache_salt else None
    store: CacheStore | None = None
    if resolved.enabled:
        try:
            store = open_cache_store(resolved, base=base)
        except (CacheStoreError, ValueError) as exc:
            _log.warning(
                "The %s cache store is not available (%s); caching stays off", resolved.store, exc
            )
    return Cache(resolved, store, project=project, salt=salt)


__all__ = [
    "DEFAULT_CACHE_FILE",
    "InMemoryCacheStore",
    "SqliteCacheStore",
    "build_cache",
    "open_cache_store",
]
