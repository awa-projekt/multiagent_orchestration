"""Artifact storage for payloads that should not live in graph state.

Large documents go into an `ArtifactRepository` under a string ref; values are stored as JSON,
so a value read back is an equal copy. Backends: in memory, one JSON file per artifact, or
PostgreSQL (`open_artifact_repository`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from awa._internal.serialize import to_json
from awa.core.settings import Settings
from awa.errors import ConfigurationError, MissingConfigurationError
from awa.persistence.sql import (
    acquire_setup_lock,
    fetch_all,
    fetch_one,
    import_psycopg,
    release_setup_lock,
)

DEFAULT_ARTIFACT_DIR = ".awa_artifacts"
"""Directory used by the file backend when `Settings.artifact_dir` is unset."""


class ArtifactRepository(ABC):
    """Async key-value storage for JSON-serializable artifacts."""

    @abstractmethod
    async def put(self, ref: str, value: Any) -> None:
        """Store the JSON-serializable `value` under `ref`, replacing any existing value.

        NaN and infinity are refused; a ref with a NUL character cannot be stored in PostgreSQL.
        """

    @abstractmethod
    async def get(self, ref: str) -> Any:
        """Return the value stored under `ref`; `KeyError` if there is none."""

    @abstractmethod
    async def exists(self, ref: str) -> bool:
        """Return whether a value is stored under `ref`."""

    async def delete(self, ref: str) -> bool:
        """Delete the value under `ref` and return whether there was one (an unknown ref is a no-op)."""

        raise NotImplementedError(f"{type(self).__name__} does not implement delete()")

    async def list_refs(self, prefix: str = "") -> list[str]:
        """Return the stored refs that start with `prefix`, sorted."""

        raise NotImplementedError(f"{type(self).__name__} does not implement list_refs()")


class InMemoryArtifactRepository(ArtifactRepository):
    """Process-local backend. Contents are lost when the process exits."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def put(self, ref: str, value: Any) -> None:
        """Store a JSON copy of `value` under `ref`."""

        self._store[ref] = _dumps(value)

    async def get(self, ref: str) -> Any:
        """Return a fresh copy of the value stored under `ref`."""

        return json.loads(self._store[ref])

    async def exists(self, ref: str) -> bool:
        """Return whether `ref` is stored."""

        return ref in self._store

    async def delete(self, ref: str) -> bool:
        """Delete `ref`; return whether it was stored."""

        return self._store.pop(ref, None) is not None

    async def list_refs(self, prefix: str = "") -> list[str]:
        """Return the stored refs starting with `prefix`, sorted."""

        return sorted(ref for ref in self._store if ref.startswith(prefix))


def _storable(text: str) -> bool:
    """Whether PostgreSQL text can hold `text` (it cannot hold NUL, so no such ref was stored)."""

    return "\x00" not in text


def _dumps(value: Any) -> str:
    # Strict JSON on every backend, so a value accepted in memory or on disk is also accepted
    # by the PostgreSQL backend (`json` rejects NaN/Infinity). The spaced, ASCII-escaped form is
    # the stored format of every backend.
    return to_json(value, compact=False, ensure_ascii=True)


def _write_atomically(path: Path, text: str) -> None:
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            # Without fsync a crash can persist the rename before the data.
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


class FileArtifactRepository(ArtifactRepository):
    """Backend storing one JSON file per artifact in `directory` (created if missing).

    Writes are atomic; without cross-process locking the last writer wins. File names are hashes,
    so `list_refs` reads every file.
    """

    def __init__(self, directory: str | Path = DEFAULT_ARTIFACT_DIR) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, ref: str) -> Path:
        # Refs may contain characters that are unsafe in file names (":", "/").
        return self._dir / (hashlib.sha256(ref.encode("utf-8")).hexdigest() + ".json")

    async def put(self, ref: str, value: Any) -> None:
        """Write `value` to the file for `ref`."""

        payload = _dumps({"ref": ref, "value": value})
        await asyncio.to_thread(_write_atomically, self._path(ref), payload)

    async def get(self, ref: str) -> Any:
        """Read the value stored under `ref`."""

        try:
            text = await asyncio.to_thread(self._path(ref).read_text, encoding="utf-8")
        except FileNotFoundError:
            raise KeyError(ref) from None
        return json.loads(text)["value"]

    async def exists(self, ref: str) -> bool:
        """Return whether the file for `ref` exists."""

        return await asyncio.to_thread(self._path(ref).exists)

    async def delete(self, ref: str) -> bool:
        """Delete the file for `ref`; return whether it existed."""

        def unlink() -> bool:
            try:
                self._path(ref).unlink()
            except FileNotFoundError:
                return False
            return True

        return await asyncio.to_thread(unlink)

    async def list_refs(self, prefix: str = "") -> list[str]:
        """Return the stored refs starting with `prefix`, sorted."""

        def scan() -> list[str]:
            refs: list[str] = []
            for path in self._dir.glob("*.json"):
                if path.name.startswith(".tmp-"):
                    continue
                try:
                    ref = json.loads(path.read_text(encoding="utf-8"))["ref"]
                except (FileNotFoundError, KeyError, TypeError, ValueError):
                    continue  # deleted meanwhile, or not an artifact file
                if isinstance(ref, str) and ref.startswith(prefix):
                    refs.append(ref)
            return sorted(refs)

        return await asyncio.to_thread(scan)


# `json`, not `jsonb`: `json` keeps the text as written, so a value reads back
# exactly (`jsonb` turns 1e20 into an integer and rejects "\u0000" in strings).
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS awa_artifacts (
    ref TEXT PRIMARY KEY,
    payload JSON NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# A table created with `jsonb` is converted to `json` once.
_MIGRATE_PAYLOAD_SQL = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'awa_artifacts'
            AND column_name = 'payload' AND data_type = 'jsonb'
    ) THEN
        ALTER TABLE awa_artifacts ALTER COLUMN payload TYPE json USING payload::json;
    END IF;
END
$$
"""


class PostgresArtifactRepository(ArtifactRepository):
    """Backend storing artifacts in the `awa_artifacts` table; create it with `open_artifact_repository`.

    Args:
        conn: An open psycopg `AsyncConnection` with `autocommit=True`.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def setup(self) -> None:
        """Create the `awa_artifacts` table if missing, serialized across processes."""

        await acquire_setup_lock(self._conn)
        try:
            await self._conn.execute(_CREATE_TABLE_SQL)
            await self._conn.execute(_MIGRATE_PAYLOAD_SQL)
        finally:
            await release_setup_lock(self._conn)

    async def put(self, ref: str, value: Any) -> None:
        """Insert or replace the artifact stored under `ref`."""

        if not _storable(ref):
            raise ConfigurationError(
                "An artifact ref cannot contain a NUL character in PostgreSQL (its text cannot "
                "hold one): remove it from the ref."
            )
        await self._conn.execute(
            "INSERT INTO awa_artifacts (ref, payload) VALUES (%s, %s::json) "
            "ON CONFLICT (ref) DO UPDATE SET payload = EXCLUDED.payload",
            (ref, _dumps(value)),
        )

    async def get(self, ref: str) -> Any:
        """Return the artifact stored under `ref`."""

        if not _storable(ref):
            raise KeyError(ref)
        row = await fetch_one(
            self._conn, "SELECT payload::text FROM awa_artifacts WHERE ref = %s", (ref,)
        )
        if row is None:
            raise KeyError(ref)
        return json.loads(row[0])

    async def exists(self, ref: str) -> bool:
        """Return whether an artifact is stored under `ref`."""

        if not _storable(ref):
            return False
        row = await fetch_one(self._conn, "SELECT 1 FROM awa_artifacts WHERE ref = %s", (ref,))
        return row is not None

    async def delete(self, ref: str) -> bool:
        """Delete the artifact stored under `ref`; return whether there was one."""

        if not _storable(ref):
            return False
        async with self._conn.cursor() as cursor:
            await cursor.execute("DELETE FROM awa_artifacts WHERE ref = %s", (ref,))
            return bool(cursor.rowcount)

    async def list_refs(self, prefix: str = "") -> list[str]:
        """Return the stored refs starting with `prefix`, sorted."""

        if not _storable(prefix):
            return []
        rows = await fetch_all(
            self._conn,
            'SELECT ref FROM awa_artifacts WHERE starts_with(ref, %s) ORDER BY ref COLLATE "C"',
            (prefix,),
        )
        return [row[0] for row in rows]


@asynccontextmanager
async def open_artifact_repository(settings: Settings) -> AsyncIterator[ArtifactRepository]:
    """Open the configured artifact backend (`artifact_backend`) for the duration of a block.

    On Windows, run the caller with `awa.ext.run_sync(..., selector_loop=True)` for `postgres`.

    Raises:
        MissingConfigurationError: If `postgres` is selected but `postgres_dsn` is unset.
        OptionalDependencyError: If `postgres` is selected but its extra is not installed.

    Example:
        ```python
        async with open_artifact_repository(settings) as artifacts:
            await artifacts.put("report:42", {"text": "..."})
        ```
    """

    if settings.artifact_backend == "memory":
        yield InMemoryArtifactRepository()
        return
    if settings.artifact_backend == "file":
        yield FileArtifactRepository(settings.artifact_dir or DEFAULT_ARTIFACT_DIR)
        return

    if not settings.postgres_dsn:
        raise MissingConfigurationError(
            "AWA_POSTGRES_DSN must be set for the postgres artifact backend"
        )
    psycopg, _ = import_psycopg()
    async with await psycopg.AsyncConnection.connect(
        settings.postgres_dsn.get_secret_value(), autocommit=True
    ) as conn:
        repository = PostgresArtifactRepository(conn)
        await repository.setup()
        yield repository


__all__ = [
    "DEFAULT_ARTIFACT_DIR",
    "ArtifactRepository",
    "FileArtifactRepository",
    "InMemoryArtifactRepository",
    "PostgresArtifactRepository",
    "open_artifact_repository",
]
