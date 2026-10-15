"""`SqliteCheckpointer`: durable thread state in a local SQLite file (standard library only).

Several processes on one machine can share the file; calls run in a worker thread. Each thread
keeps its newest `keep_checkpoints` checkpoints, and threads idle longer than `retention` are
deleted when the file opens.
"""

from __future__ import annotations

import asyncio
import os
import random
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from awa._internal.upstream.checkpoint import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
    state_serde,
)
from awa._internal.upstream.runnables import RunnableConfig
from awa.errors import ConfigurationError
from awa.persistence.sql import SqliteConnection

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL, checkpoint_id TEXT NOT NULL,
    parent_id TEXT, type TEXT NOT NULL, checkpoint BLOB NOT NULL,
    metadata_type TEXT NOT NULL, metadata BLOB NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id));
CREATE TABLE IF NOT EXISTS blobs (
    thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL, channel TEXT NOT NULL,
    version TEXT NOT NULL, type TEXT NOT NULL, value BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version));
CREATE TABLE IF NOT EXISTS writes (
    thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL, checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL, idx INTEGER NOT NULL, channel TEXT NOT NULL,
    type TEXT NOT NULL, value BLOB, task_path TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx));
"""


FORMAT_VERSION = 1
"""Format version of the checkpoint database (`PRAGMA user_version`)."""

_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}
"""`n` -> a function that upgrades a database of format `n` to `n + 1` inside one transaction.
A new file (`user_version` 0) needs no step."""


class SqliteCheckpointer(BaseCheckpointSaver[str]):
    """A LangGraph checkpointer in a SQLite file, created with its directory on first use.

    Args:
        serde: The serializer (default `state_serde()`, so structured outputs revive as models).
        keep_checkpoints: Checkpoints kept per thread and namespace (`None`: all); dropped in
            batches, so up to half as many more may exist.
        retention: Threads whose newest checkpoint is older are deleted when the file opens, a run
            paused that long included (`None`: keep every thread).

    Raises:
        ConfigurationError: If `keep_checkpoints` is below 1, or the file cannot be opened as a
            checkpoint database.

    Example:
        ```python
        agent = Agent("clerk", checkpointer=SqliteCheckpointer(".awa/checkpoints.db"))
        ```
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        serde: Any = None,
        keep_checkpoints: int | None = 25,
        retention: timedelta | None = timedelta(days=90),
    ) -> None:
        super().__init__(serde=serde or state_serde())
        if keep_checkpoints is not None and keep_checkpoints < 1:
            raise ConfigurationError("keep_checkpoints must be at least 1 (or None)")
        self.path = Path(path)
        self.keep_checkpoints = keep_checkpoints
        try:  # the file holds conversations: owner only, whatever the umask
            self._db = SqliteConnection(self.path, timeout=30.0)
        except sqlite3.DatabaseError as exc:
            raise self._damaged(exc) from exc
        try:
            with self._db.lock:
                self._db.conn.executescript(_SCHEMA)
                self._upgrade_format()
            if retention is not None:
                self.prune_idle(retention)
        except sqlite3.DatabaseError as exc:
            self._db.close()
            raise self._damaged(exc) from exc
        except BaseException:  # whatever the failure: no connection stays open
            self._db.close()
            raise

    def _damaged(self, exc: sqlite3.DatabaseError) -> ConfigurationError:
        return ConfigurationError(
            f"Cannot open the checkpoint database {self.path}: {exc}. If the file is "
            "damaged, move it aside to start fresh (runs paused in it are lost)."
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str(self.path)!r})"

    def _upgrade_format(self) -> None:
        # `PRAGMA user_version` is the format version; a new file reads 0.
        found = int(self._db.conn.execute("PRAGMA user_version").fetchone()[0])
        if found > FORMAT_VERSION:
            raise ConfigurationError(
                f"The checkpoint database {self.path} has format version {found}, but this awa "
                f"reads up to {FORMAT_VERSION}. Upgrade awa, or use the awa version that wrote it; "
                "nothing was changed."
            )
        if found == FORMAT_VERSION:
            return
        with self._db.writing() as conn:
            for version in range(max(found, 0), FORMAT_VERSION):
                if version in _MIGRATIONS:
                    _MIGRATIONS[version](conn)
            conn.execute(f"PRAGMA user_version={FORMAT_VERSION}")

    def close(self) -> None:
        """Close the database connection."""

        self._db.close()

    def backup(self, target: str | os.PathLike[str]) -> Path:
        """Copy the checkpoints consistently to the new, owner-only file `target` (online backup API).

        Raises:
            ConfigurationError: If `target` exists.
        """

        try:
            return self._db.backup(target)
        except FileExistsError:
            raise ConfigurationError(
                f"{Path(target)} exists; choose a new file for the backup"
            ) from None

    def get_tuple(self, config: RunnableConfig) -> Any:
        """The checkpoint `config` names, or the thread's latest one; `None` if there is none."""

        configurable = config["configurable"]
        thread_id, ns = str(configurable["thread_id"]), configurable.get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)
        query = "SELECT * FROM checkpoints WHERE thread_id=? AND checkpoint_ns=?"
        values: list[Any] = [thread_id, ns]
        if checkpoint_id:
            query += " AND checkpoint_id=?"
            values.append(checkpoint_id)
        with self._db.reading() as conn:
            row = conn.execute(query + " ORDER BY checkpoint_id DESC LIMIT 1", values).fetchone()
            return None if row is None else self._tuple(row)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[Any]:
        """Checkpoints newest first, filtered by thread, namespace, id, metadata and `before`."""

        clauses: list[str] = []
        values: list[Any] = []
        if config is not None:
            configurable = config["configurable"]
            clauses.append("thread_id=?")
            values.append(str(configurable["thread_id"]))
            if configurable.get("checkpoint_ns") is not None:
                clauses.append("checkpoint_ns=?")
                values.append(configurable["checkpoint_ns"])
            if checkpoint_id := get_checkpoint_id(config):
                clauses.append("checkpoint_id=?")
                values.append(checkpoint_id)
        if before is not None and (before_id := get_checkpoint_id(before)):
            clauses.append("checkpoint_id<?")
            values.append(before_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cap = ""
        if limit is not None and not filter:  # without a metadata filter the database stops early
            cap = " LIMIT ?"
            values.append(max(limit, 0))
        with self._db.reading() as conn:
            rows = conn.execute(
                f"SELECT * FROM checkpoints{where} "  # noqa: S608 - fixed clauses, bound values
                f"ORDER BY thread_id, checkpoint_ns, checkpoint_id DESC{cap}",
                values,
            ).fetchall()
        remaining = limit
        for row in rows:
            if remaining is not None and remaining <= 0:
                return
            metadata = self.serde.loads_typed((row[6], row[7]))
            if filter and not all(metadata.get(key) == value for key, value in filter.items()):
                continue
            if remaining is not None:
                remaining -= 1
            with self._db.lock:
                found = self._tuple(row)
            yield found

    def _tuple(self, row: tuple[Any, ...]) -> Any:
        thread_id, ns, checkpoint_id, parent_id = row[0], row[1], row[2], row[3]
        checkpoint: dict[str, Any] = self.serde.loads_typed((row[4], row[5]))
        channel_values: dict[str, Any] = {}
        for channel, version in checkpoint.get("channel_versions", {}).items():
            blob = self._db.conn.execute(
                "SELECT type, value FROM blobs WHERE thread_id=? AND checkpoint_ns=? "
                "AND channel=? AND version=?",
                (thread_id, ns, channel, str(version)),
            ).fetchone()
            if blob is not None and blob[0] != "empty":
                channel_values[channel] = self.serde.loads_typed((blob[0], blob[1]))
        writes = self._db.conn.execute(
            "SELECT task_id, channel, type, value FROM writes WHERE thread_id=? "
            "AND checkpoint_ns=? AND checkpoint_id=? ORDER BY task_id, idx",
            (thread_id, ns, checkpoint_id),
        ).fetchall()

        def at(identifier: str) -> RunnableConfig:
            return {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": ns,
                    "checkpoint_id": identifier,
                }
            }

        return CheckpointTuple(
            config=at(checkpoint_id),
            checkpoint={**checkpoint, "channel_values": channel_values},  # type: ignore[typeddict-item]  # decoded values
            metadata=self.serde.loads_typed((row[6], row[7])),
            parent_config=at(parent_id) if parent_id else None,
            pending_writes=[
                (task, channel, self.serde.loads_typed((kind, value)))
                for task, channel, kind, value in writes
            ],
        )

    def put(
        self, config: RunnableConfig, checkpoint: Any, metadata: Any, new_versions: Any
    ) -> RunnableConfig:
        """Save a checkpoint and the channel values of its new versions."""

        configurable = config["configurable"]
        thread_id, ns = str(configurable["thread_id"]), configurable.get("checkpoint_ns", "")
        stored = dict(checkpoint)
        values = stored.pop("channel_values", {})
        blobs = [
            (
                thread_id,
                ns,
                channel,
                str(version),
                *(self.serde.dumps_typed(values[channel]) if channel in values else ("empty", b"")),
            )
            for channel, version in new_versions.items()
        ]
        kind, data = self.serde.dumps_typed(stored)
        meta_kind, meta = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        with self._db.writing() as conn:
            conn.executemany("INSERT OR REPLACE INTO blobs VALUES (?, ?, ?, ?, ?, ?)", blobs)
            conn.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    ns,
                    checkpoint["id"],
                    configurable.get("checkpoint_id"),
                    kind,
                    data,
                    meta_kind,
                    meta,
                ),
            )
            if self.keep_checkpoints is not None:
                self._trim(conn, thread_id, ns, self.keep_checkpoints)
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Save a task's pending writes (a regular write is kept once, special ones replaced)."""

        configurable = config["configurable"]
        key = (
            str(configurable["thread_id"]),
            configurable.get("checkpoint_ns", ""),
            configurable["checkpoint_id"],
        )
        with self._db.writing() as conn:
            for position, (channel, value) in enumerate(writes):
                index = WRITES_IDX_MAP.get(channel, position)
                verb = "INSERT OR REPLACE" if index < 0 else "INSERT OR IGNORE"
                conn.execute(
                    f"{verb} INTO writes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*key, task_id, index, channel, *self.serde.dumps_typed(value), task_path),
                )

    def _trim(self, conn: sqlite3.Connection, thread_id: str, ns: str, keep: int) -> None:
        """Drop the checkpoints of a thread beyond the newest `keep` (in batches of half
        of `keep`), with their pending writes and the values nothing refers to any more."""

        old = [
            row[0]
            for row in conn.execute(
                "SELECT checkpoint_id FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? "
                "ORDER BY checkpoint_id DESC LIMIT -1 OFFSET ?",
                (thread_id, ns, keep),
            )
        ]
        if len(old) < max(keep // 2, 1):
            return
        ids = [(thread_id, ns, item) for item in old]
        conn.executemany(
            "DELETE FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?", ids
        )
        conn.executemany(
            "DELETE FROM writes WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?", ids
        )
        live: set[tuple[str, str]] = set()
        for kind, data in conn.execute(
            "SELECT type, checkpoint FROM checkpoints WHERE thread_id=? AND checkpoint_ns=?",
            (thread_id, ns),
        ):
            versions = self.serde.loads_typed((kind, data)).get("channel_versions", {})
            live.update((channel, str(version)) for channel, version in versions.items())
        stale = [
            (thread_id, ns, channel, version)
            for channel, version in conn.execute(
                "SELECT channel, version FROM blobs WHERE thread_id=? AND checkpoint_ns=?",
                (thread_id, ns),
            ).fetchall()
            if (channel, version) not in live
        ]
        conn.executemany(
            "DELETE FROM blobs WHERE thread_id=? AND checkpoint_ns=? AND channel=? AND version=?",
            stale,
        )

    def prune_idle(self, older_than: timedelta) -> int:
        """Delete the threads whose newest checkpoint is older than `older_than`; return how many."""

        cutoff = datetime.now(UTC) - older_than
        with self._db.reading() as conn:
            threads = [row[0] for row in conn.execute("SELECT DISTINCT thread_id FROM checkpoints")]
        idle = []
        for thread_id in threads:
            with self._db.reading() as conn:
                row = conn.execute(
                    "SELECT type, checkpoint FROM checkpoints WHERE thread_id=? "
                    "ORDER BY checkpoint_id DESC LIMIT 1",
                    (thread_id,),
                ).fetchone()
            try:
                stamp = datetime.fromisoformat(self.serde.loads_typed((row[0], row[1]))["ts"])
            except (TypeError, ValueError, KeyError, IndexError):
                continue  # no readable timestamp: keep the thread
            if (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)) < cutoff:
                idle.append(thread_id)
        for thread_id in idle:
            self.delete_thread(thread_id)
        return len(idle)

    def delete_thread(self, thread_id: str) -> None:
        """Delete every checkpoint, value and write of a thread."""

        with self._db.writing() as conn:
            for table in ("checkpoints", "blobs", "writes"):
                conn.execute(f"DELETE FROM {table} WHERE thread_id=?", (str(thread_id),))  # noqa: S608

    def get_next_version(self, current: str | None, channel: None) -> str:
        """A monotonically increasing version string (the in-memory saver's scheme)."""

        number = 0 if current is None else int(str(current).split(".")[0])
        return f"{number + 1:032}.{random.random():016}"  # noqa: S311 - not security relevant

    async def aget_tuple(self, config: RunnableConfig) -> Any:
        """See `get_tuple`."""

        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[Any]:
        """See `list`."""

        found = await asyncio.to_thread(
            lambda: [*self.list(config, filter=filter, before=before, limit=limit)]
        )
        for item in found:
            yield item

    async def aput(
        self, config: RunnableConfig, checkpoint: Any, metadata: Any, new_versions: Any
    ) -> RunnableConfig:
        """See `put`."""

        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """See `put_writes`."""

        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        """See `delete_thread`."""

        await asyncio.to_thread(self.delete_thread, thread_id)


__all__ = ["SqliteCheckpointer"]
