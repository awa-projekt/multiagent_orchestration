"""`SqliteRunStore`: process runs in the local data file (`.awa/awa.db`), for one machine."""

from __future__ import annotations

import asyncio
import builtins
import json
import os
import sqlite3
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from awa.automation._time import as_utc
from awa.automation.models import ProcessRun, statuses
from awa.automation.store import RunChange, claimed_copy
from awa.errors import ProcessRunStateError, RunNotFoundError
from awa.persistence.runlog.rows import stamp
from awa.persistence.sql import SqliteDatabase

_COLUMNS = (
    "id, automation, status, dedup_key, tenant, version, created_at, updated_at, ready_at,"
    " due_at, lease_owner, lease_expires_at, wait_keys, revision, data"
)
_DELETE_CHUNK = 500
"""Run ids per DELETE statement (SQLite allows at most 999 parameters in older builds)."""


class SqliteRunStore:
    """A `RunStore` in a local SQLite file, shared by the processes of one machine.

    Claims run in `BEGIN IMMEDIATE`; the file can be shared with the run log and the thread index,
    so one backup holds all of them.

    Args:
        database: The data file, or an open `SqliteDatabase` to share.

    Raises:
        DataStoreError: See `awa.persistence.runlog.SqliteDatabase`.
    """

    def __init__(self, database: SqliteDatabase | str | os.PathLike[str]) -> None:
        self._owned = not isinstance(database, SqliteDatabase)
        self.database = (
            database if isinstance(database, SqliteDatabase) else SqliteDatabase(database)
        )

    @property
    def path(self) -> Path:
        """The data file (its folder holds the traces)."""

        return self.database.path

    def __repr__(self) -> str:
        return f"SqliteRunStore({str(self.database.path)!r})"

    async def create(
        self, run: ProcessRun, *, dedup_since: datetime | None = None
    ) -> tuple[ProcessRun, bool]:
        """See `RunStore.create`."""

        def work(conn: sqlite3.Connection) -> tuple[ProcessRun, bool]:
            if run.dedup_key is not None:
                since = stamp(as_utc(dedup_since)) if dedup_since is not None else None
                existing = conn.execute(
                    "SELECT data FROM process_runs WHERE automation = ? AND dedup_key = ?"
                    " AND (? IS NULL OR created_at >= ?) ORDER BY created_at DESC LIMIT 1",
                    (run.automation, run.dedup_key, since, since),
                ).fetchone()
                if existing is not None:
                    return _run(existing[0]), False
            try:
                _write(conn, run, insert=True)
            except sqlite3.IntegrityError as exc:
                raise ProcessRunStateError(f"Process run {run.id!r} already exists") from exc
            return run.model_copy(deep=True), True

        return await self.database.write(work)

    async def get(self, run_id: str) -> ProcessRun | None:
        """See `RunStore.get`."""

        row = await self.database.read(
            lambda conn: conn.execute(
                "SELECT data FROM process_runs WHERE id = ?", (run_id,)
            ).fetchone()
        )
        return _run(row[0]) if row is not None else None

    async def list(
        self,
        *,
        automation: str | None = None,
        status: str | Collection[str] | None = None,
        tenant: str | None = None,
        limit: int = 100,
    ) -> builtins.list[ProcessRun]:
        """See `RunStore.list`."""

        wanted = statuses(status)
        query = "SELECT data FROM process_runs WHERE 1 = 1"
        params: builtins.list[Any] = []
        if automation is not None:
            query += " AND automation = ?"
            params.append(automation)
        if wanted is not None:
            query += f" AND status IN ({', '.join('?' for _ in wanted)})"
            params += sorted(wanted)
        if tenant is not None:
            query += " AND tenant = ?"
            params.append(tenant)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(max(0, limit))
        found = await self.database.read(lambda conn: conn.execute(query, params).fetchall())
        return [_run(row[0]) for row in found]

    async def modify(self, run_id: str, change: RunChange, *, now: datetime) -> ProcessRun:
        """See `RunStore.modify`."""

        def work(conn: sqlite3.Connection) -> ProcessRun:
            row = conn.execute("SELECT data FROM process_runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                raise RunNotFoundError(f"No process run {run_id!r}")
            current = _run(row[0])
            changed = change(current.model_copy(deep=True))
            if changed is None:
                return current
            stored = changed.model_copy(
                update={"revision": current.revision + 1, "updated_at": as_utc(now)}, deep=True
            )
            _write(conn, stored, insert=False)
            return stored

        return await self.database.write(work)

    async def claim(
        self,
        automation: str,
        *,
        worker: str,
        lease: timedelta,
        now: datetime,
        versions: Collection[str] | None = None,
        max_running: int | None = None,
    ) -> ProcessRun | None:
        """See `RunStore.claim` (exclusive: SQLite has one writer at a time)."""

        moment = as_utc(now)

        def work(conn: sqlite3.Connection) -> ProcessRun | None:
            if max_running is not None:
                (running,) = conn.execute(
                    "SELECT COUNT(*) FROM process_runs WHERE automation = ? AND status = 'running'",
                    (automation,),
                ).fetchone()
                if int(running) >= max_running:
                    return None
            query = (
                "SELECT data FROM process_runs WHERE automation = ? AND status = 'queued'"
                " AND ready_at <= ?"
            )
            params: builtins.list[Any] = [automation, stamp(moment)]
            if versions is not None:
                allowed = sorted(versions)
                query += f" AND (version IS NULL OR version IN ({', '.join('?' for _ in allowed)}))"
                params += allowed
            row = conn.execute(query + " ORDER BY ready_at, id LIMIT 1", params).fetchone()
            if row is None:
                return None
            claimed = claimed_copy(_run(row[0]), worker=worker, lease=lease, now=moment)
            _write(conn, claimed, insert=False)
            return claimed

        return await self.database.write(work)

    async def renew(
        self, run_id: str, *, worker: str, lease: timedelta, now: datetime, epoch: int | None = None
    ) -> bool:
        """See `RunStore.renew`."""

        expires = as_utc(now) + lease

        def work(conn: sqlite3.Connection) -> bool:
            row = conn.execute(
                "SELECT data FROM process_runs WHERE id = ? AND status = 'running'"
                " AND lease_owner = ?",
                (run_id, worker),
            ).fetchone()
            if row is None:
                return False
            held = _run(row[0])
            if epoch is not None and held.lease_epoch != epoch:
                return False  # a newer claim of the same worker id holds the run
            renewed = held.model_copy(update={"lease_expires_at": expires})
            conn.execute(
                "UPDATE process_runs SET lease_expires_at = ?, data = ? WHERE id = ?",
                (stamp(expires), renewed.model_dump_json(), run_id),
            )
            return True

        return await self.database.write(work)

    async def _select(self, query: str, params: Sequence[Any]) -> builtins.list[ProcessRun]:
        found = await self.database.read(lambda conn: conn.execute(query, params).fetchall())
        return [_run(row[0]) for row in found]

    async def expired(self, automation: str, *, now: datetime) -> builtins.list[ProcessRun]:
        """See `RunStore.expired`."""

        return await self._select(
            "SELECT data FROM process_runs WHERE automation = ? AND status = 'running'"
            " AND lease_expires_at < ?",
            (automation, stamp(as_utc(now))),
        )

    async def due(
        self, automation: str, *, now: datetime, limit: int = 100
    ) -> builtins.list[ProcessRun]:
        """See `RunStore.due`."""

        return await self._select(
            "SELECT data FROM process_runs WHERE automation = ? AND due_at IS NOT NULL"
            " AND due_at <= ? ORDER BY due_at LIMIT ?",
            (automation, stamp(as_utc(now)), max(0, limit)),
        )

    async def waiting_on(self, automation: str, key: str) -> builtins.list[ProcessRun]:
        """See `RunStore.waiting_on`."""

        return await self._select(
            "SELECT r.data FROM process_runs r JOIN process_run_wait_keys k ON k.run_id = r.id"
            " WHERE r.automation = ? AND r.status = 'waiting' AND k.key = ?",
            (automation, key),
        )

    async def get_state(self, key: str) -> str | None:
        """See `RunStore.get_state`."""

        row = await self.database.read(
            lambda conn: conn.execute(
                "SELECT value FROM process_state WHERE key = ?", (key,)
            ).fetchone()
        )
        return str(row[0]) if row is not None else None

    async def compare_and_set_state(self, key: str, expected: str | None, value: str) -> bool:
        """See `RunStore.compare_and_set_state`."""

        def work(conn: sqlite3.Connection) -> bool:
            if expected is None:
                changed = conn.execute(
                    "INSERT INTO process_state (key, value, updated_at) VALUES (?, ?,"
                    " strftime('%Y-%m-%dT%H:%M:%fZ', 'now')) ON CONFLICT (key) DO NOTHING",
                    (key, value),
                ).rowcount
            else:
                changed = conn.execute(
                    "UPDATE process_state SET value = ?,"
                    " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                    " WHERE key = ? AND value = ?",
                    (value, key, expected),
                ).rowcount
            return bool(changed)

        return await self.database.write(work)

    async def delete(self, run_ids: Sequence[str]) -> int:
        """Delete runs for good (retention and erasure); returns how many were deleted."""

        wanted = builtins.list(dict.fromkeys(run_ids))
        if not wanted:
            return 0

        def work(conn: sqlite3.Connection) -> int:
            deleted = 0
            # In chunks, all in one transaction: SQLite limits the parameters of a statement.
            for start in range(0, len(wanted), _DELETE_CHUNK):
                chunk = wanted[start : start + _DELETE_CHUNK]
                deleted += conn.execute(
                    f"DELETE FROM process_runs WHERE id IN ({', '.join('?' for _ in chunk)})",  # noqa: S608
                    chunk,
                ).rowcount
            return deleted

        return await self.database.write(work)

    async def close(self) -> None:
        """Close the data file when this store opened it."""

        if self._owned:
            await asyncio.to_thread(self.database.close)


def _run(data: str) -> ProcessRun:
    return ProcessRun.model_validate(json.loads(data))


def _values(run: ProcessRun) -> tuple[Any, ...]:
    ready = (run.next_attempt_at or run.created_at) if run.status == "queued" else None
    return (
        run.id,
        run.automation,
        run.status,
        run.dedup_key,
        run.tenant,
        run.version,
        stamp(run.created_at),
        stamp(run.updated_at),
        stamp(ready),
        stamp(run.due_at()),
        run.lease_owner,
        stamp(run.lease_expires_at),
        json.dumps(_waiting_keys(run)),
        run.revision,
        run.model_dump_json(),
    )


def _write(conn: sqlite3.Connection, run: ProcessRun, *, insert: bool) -> None:
    values = _values(run)
    if insert:
        conn.execute(
            f"INSERT INTO process_runs ({_COLUMNS}) VALUES ({', '.join('?' for _ in values)})",  # noqa: S608
            values,
        )
    else:
        names = [name.strip() for name in _COLUMNS.split(",")][1:]
        conn.execute(
            f"UPDATE process_runs SET {', '.join(f'{n} = ?' for n in names)} WHERE id = ?",  # noqa: S608
            (*values[1:], run.id),
        )
        conn.execute("DELETE FROM process_run_wait_keys WHERE run_id = ?", (run.id,))
    conn.executemany(
        "INSERT OR IGNORE INTO process_run_wait_keys (run_id, key) VALUES (?, ?)",
        [(run.id, key) for key in _waiting_keys(run)],
    )


def _waiting_keys(run: ProcessRun) -> list[str]:
    return list(run.wait_keys) if run.status == "waiting" else []
