"""Thread ownership and idempotency records: the protocols and their SQLite and PostgreSQL backends.

`BaseThreadIndex` records who created each thread (others get 404); `DecisionClaims` makes one
approval apply once; `BaseIdempotencyCache` replays the response of a request repeated with its
`Idempotency-Key`.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from awa.errors import DataStoreError
from awa.persistence.runlog.rows import stamp, when
from awa.persistence.sql import (
    PostgresMigratedTables,
    PostgresTables,
    SqliteDatabase,
    fetch_all,
    fetch_one,
)


@dataclass(frozen=True)
class ThreadOwner:
    """Who created a thread.

    Attributes:
        tenant: The creator's tenant (`None` in single-tenant deployments).
        principal: The creator's principal id; `None` for a thread without an ownership record,
            which belongs to the default tenant only.
    """

    tenant: str | None
    principal: str | None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class ThreadRecord:
    """A thread in a listing: its id and owner."""

    thread_id: str
    owner: ThreadOwner


class BaseThreadIndex(ABC):
    """Records which tenant and principal each thread of a served system belongs to.

    `claim` must be atomic: of two concurrent claims of a new thread, one wins and both return it.
    """

    @abstractmethod
    async def claim(self, system: str, thread_id: str, owner: ThreadOwner) -> ThreadOwner:
        """Record `owner` for the thread unless it has one; return the thread's owner."""

    @abstractmethod
    async def owner(self, system: str, thread_id: str) -> ThreadOwner | None:
        """The recorded owner of the thread, or `None`."""

    @abstractmethod
    async def threads(
        self, system: str, *, tenant: str | None, principal: str | None = None, limit: int = 50
    ) -> list[ThreadRecord]:
        """The newest threads of a tenant (and principal, when given), newest first."""

    @abstractmethod
    async def forget(self, system: str, thread_id: str) -> None:
        """Remove the thread's record (for example after erasing the thread)."""

    async def ping(self) -> None:
        """Check the backend is reachable (readiness); raise if it is not."""

        return None


@runtime_checkable
class DecisionClaims(Protocol):
    """Capability of a thread index: record which paused states of a thread were answered.

    Without it, the server keeps the claims in its own process.
    """

    async def claim_decision(self, system: str, thread_id: str, version: str) -> bool:
        """Claim the right to answer the paused state `version` of a thread; only the first claim wins.

        A claim stays until `release_decision` (after a failed resume), so a repeated resume is refused
        even after the first one finished.
        """
        ...

    async def release_decision(self, system: str, thread_id: str, version: str) -> None:
        """Drop a claim, so the paused state can be answered again (after a failed resume)."""
        ...


@runtime_checkable
class OwnedThreads(Protocol):
    """Capability of a thread index: list the threads one person created (used by erasure)."""

    async def owned_by(self, principal: str) -> list[tuple[str, str]]:
        """`(system, thread_id)` of every thread whose recorded owner is `principal`."""
        ...


@dataclass(frozen=True)
class IdempotencyRecord:
    """A request seen with an `Idempotency-Key`, and its response once there is one.

    Attributes:
        fingerprint: Digest of what the request asked for (system, mode, body).
        status: HTTP status of the stored response; `None` while the first request runs.
        headers: Stored response headers worth replaying (such as `Location`).
    """

    fingerprint: str
    status: int | None = None
    body: Any = None
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def completed(self) -> bool:
        """Whether the response is stored."""

        return self.status is not None


class BaseIdempotencyCache(ABC):
    """Stores the responses of requests sent with an `Idempotency-Key`.

    Keys are scoped (tenant and principal for HTTP, tenant for background jobs), expire after a
    TTL, and `begin` must claim a key atomically.
    """

    @abstractmethod
    async def begin(
        self, scope: str, key: str, fingerprint: str, ttl: float
    ) -> IdempotencyRecord | None:
        """Claim `key` for a new request: `None` if claimed (then `complete` or `release`), else the record."""

    @abstractmethod
    async def complete(self, scope: str, key: str, record: IdempotencyRecord) -> None:
        """Store the response of a claimed key."""

    @abstractmethod
    async def release(self, scope: str, key: str) -> None:
        """Drop a claimed key without a response, so a retry runs again."""

    async def ping(self) -> None:
        """Check the backend is reachable (readiness); raise if it is not."""

        return None


def _sqlite_owner(row: Any) -> ThreadOwner:
    created = when(row[2])
    if created is None:
        raise DataStoreError("A thread record in the data file has no creation time")
    return ThreadOwner(tenant=row[0], principal=row[1], created_at=created)


_OWNER_QUERY = (
    "SELECT tenant, principal, created_at FROM server_threads WHERE system = ? AND thread_id = ?"
)


class SqliteThreadIndex(BaseThreadIndex):
    """Thread ownership and decision claims in a local SQLite file.

    Claims run in `BEGIN IMMEDIATE`, so two processes get one winner.

    Args:
        database: The data file, or an open `SqliteDatabase` to share; a shared one is never
            closed by the index.

    Raises:
        DataStoreError: See `awa.persistence.SqliteDatabase`; also on any call after `close()`.
    """

    def __init__(self, database: SqliteDatabase | str | os.PathLike[str]) -> None:
        self._owned = not isinstance(database, SqliteDatabase)
        self.database = (
            database if isinstance(database, SqliteDatabase) else SqliteDatabase(database)
        )

    def __repr__(self) -> str:
        return f"SqliteThreadIndex({str(self.database.path)!r})"

    async def claim(self, system: str, thread_id: str, owner: ThreadOwner) -> ThreadOwner:
        """Record `owner` unless the thread has one; return the thread's owner."""

        def work(conn: sqlite3.Connection) -> ThreadOwner:
            conn.execute(
                "INSERT INTO server_threads (system, thread_id, tenant, principal, created_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (system, thread_id, owner.tenant, owner.principal, stamp(owner.created_at)),
            )
            return _sqlite_owner(conn.execute(_OWNER_QUERY, (system, thread_id)).fetchone())

        return await self.database.write(work)

    async def owner(self, system: str, thread_id: str) -> ThreadOwner | None:
        """The recorded owner, or `None`."""

        row = await self.database.read(
            lambda conn: conn.execute(_OWNER_QUERY, (system, thread_id)).fetchone()
        )
        return _sqlite_owner(row) if row is not None else None

    async def threads(
        self, system: str, *, tenant: str | None, principal: str | None = None, limit: int = 50
    ) -> list[ThreadRecord]:
        """The newest threads of `tenant` (and `principal`), newest first."""

        query = (
            "SELECT thread_id, tenant, principal, created_at FROM server_threads"
            " WHERE system = ? AND tenant IS ?"
        )
        params: list[Any] = [system, tenant]
        if principal is not None:
            query += " AND principal = ?"
            params.append(principal)
        query += " ORDER BY created_at DESC, thread_id DESC LIMIT ?"
        params.append(max(0, limit))
        found = await self.database.read(lambda conn: conn.execute(query, params).fetchall())
        return [ThreadRecord(row[0], _sqlite_owner(row[1:])) for row in found]

    async def forget(self, system: str, thread_id: str) -> None:
        """Remove the thread's record and its answered decisions."""

        def work(conn: sqlite3.Connection) -> None:
            for table in ("server_threads", "server_decisions"):
                conn.execute(
                    f"DELETE FROM {table} WHERE system = ? AND thread_id = ?",  # noqa: S608
                    (system, thread_id),
                )

        await self.database.write(work)

    async def owned_by(self, principal: str) -> list[tuple[str, str]]:
        """`(system, thread_id)` of every thread owned by `principal`."""

        found = await self.database.read(
            lambda conn: conn.execute(
                "SELECT system, thread_id FROM server_threads WHERE principal = ?"
                " ORDER BY system, thread_id",
                (principal,),
            ).fetchall()
        )
        return [(str(row[0]), str(row[1])) for row in found]

    async def claim_decision(self, system: str, thread_id: str, version: str) -> bool:
        """Claim the right to answer one paused state of a thread; only the first claim wins."""

        def work(conn: sqlite3.Connection) -> bool:
            return bool(
                conn.execute(
                    "INSERT INTO server_decisions (system, thread_id, version, claimed_at)"
                    " VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))"
                    " ON CONFLICT DO NOTHING",
                    (system, thread_id, version),
                ).rowcount
            )

        return await self.database.write(work)

    async def release_decision(self, system: str, thread_id: str, version: str) -> None:
        """Drop a claim, so the paused state can be answered again."""

        await self.database.write(
            lambda conn: conn.execute(
                "DELETE FROM server_decisions WHERE system = ? AND thread_id = ? AND version = ?",
                (system, thread_id, version),
            )
        )

    async def ping(self) -> None:
        """Run `SELECT 1` on the data file."""

        await self.database.read(lambda conn: conn.execute("SELECT 1").fetchone())

    async def close(self) -> None:
        """Close the data file when this index opened it."""

        if self._owned:
            await asyncio.to_thread(self.database.close)


class PostgresThreadIndex(PostgresMigratedTables, BaseThreadIndex):
    """Thread ownership in PostgreSQL, shared by every worker (table `<schema>.server_threads`).

    Answered paused states live in `<schema>.server_decisions`. Delete rows older than your longest
    pause to keep it small; delete a row by hand only to retry a decision whose worker died.

    Args:
        conninfo: A DSN, or a `psycopg_pool.AsyncConnectionPool` (autocommit) to share.
        setup: Create the schema and table on first use.
        pool_size: Size of the pool opened for a DSN.
    """

    async def claim(self, system: str, thread_id: str, owner: ThreadOwner) -> ThreadOwner:
        """Record `owner` unless the thread has one; return the thread's owner."""

        async with await self._connection() as conn:
            row = await fetch_one(
                conn,
                f"WITH inserted AS (INSERT INTO {self._schema}.server_threads"
                " (system, thread_id, tenant, principal, created_at) VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT DO NOTHING RETURNING tenant, principal, created_at)"
                " SELECT tenant, principal, created_at FROM inserted UNION ALL"
                f" SELECT tenant, principal, created_at FROM {self._schema}.server_threads"
                " WHERE system = %s AND thread_id = %s LIMIT 1",
                (
                    system,
                    thread_id,
                    owner.tenant,
                    owner.principal,
                    owner.created_at,
                    system,
                    thread_id,
                ),
            )
            if row is not None:
                return _postgres_owner(row)
            # A concurrent claim committed after this statement's snapshot was
            # taken (its insert made ours do nothing); a new statement sees it.
            return await self._owner_of(conn, system, thread_id) or owner

    async def owner(self, system: str, thread_id: str) -> ThreadOwner | None:
        """The recorded owner, or `None`."""

        async with await self._connection() as conn:
            return await self._owner_of(conn, system, thread_id)

    async def _owner_of(self, conn: Any, system: str, thread_id: str) -> ThreadOwner | None:
        row = await fetch_one(
            conn,
            f"SELECT tenant, principal, created_at FROM {self._schema}.server_threads"
            " WHERE system = %s AND thread_id = %s",
            (system, thread_id),
        )
        return _postgres_owner(row) if row is not None else None

    async def threads(
        self, system: str, *, tenant: str | None, principal: str | None = None, limit: int = 50
    ) -> list[ThreadRecord]:
        """The newest threads of `tenant` (and `principal`), newest first."""

        query = (
            f"SELECT thread_id, tenant, principal, created_at FROM {self._schema}.server_threads"
            " WHERE system = %s AND tenant IS NOT DISTINCT FROM %s"
        )
        params: tuple[Any, ...] = (system, tenant)
        if principal is not None:
            query += " AND principal = %s"
            params += (principal,)
        query += " ORDER BY created_at DESC LIMIT %s"
        async with await self._connection() as conn:
            rows = await fetch_all(conn, query, (*params, limit))
        return [ThreadRecord(row[0], _postgres_owner(row[1:])) for row in rows]

    async def forget(self, system: str, thread_id: str) -> None:
        """Remove the thread's record and its answered decisions."""

        async with await self._connection() as conn:
            for table in ("server_threads", "server_decisions"):
                await conn.execute(
                    f"DELETE FROM {self._schema}.{table} WHERE system = %s AND thread_id = %s",
                    (system, thread_id),
                )

    async def owned_by(self, principal: str) -> list[tuple[str, str]]:
        """`(system, thread_id)` of every thread owned by `principal`."""

        async with await self._connection() as conn:
            found = await fetch_all(
                conn,
                f"SELECT system, thread_id FROM {self._schema}.server_threads"
                " WHERE principal = %s ORDER BY system, thread_id",
                (principal,),
            )
        return [(str(row[0]), str(row[1])) for row in found]

    async def claim_decision(self, system: str, thread_id: str, version: str) -> bool:
        """Claim the right to answer one paused state of a thread, across every worker."""

        async with await self._connection() as conn:
            row = await fetch_one(
                conn,
                f"INSERT INTO {self._schema}.server_decisions (system, thread_id, version)"
                " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING 1",
                (system, thread_id, version),
            )
        return row is not None

    async def release_decision(self, system: str, thread_id: str, version: str) -> None:
        """Drop a claim, so the paused state can be answered again."""

        async with await self._connection() as conn:
            await conn.execute(
                f"DELETE FROM {self._schema}.server_decisions"
                " WHERE system = %s AND thread_id = %s AND version = %s",
                (system, thread_id, version),
            )


def _postgres_owner(row: Any) -> ThreadOwner:
    created = row[2]
    if isinstance(created, datetime) and created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return ThreadOwner(tenant=row[0], principal=row[1], created_at=created)


class PostgresIdempotencyCache(PostgresTables, BaseIdempotencyCache):
    """Idempotency records in PostgreSQL, shared by every worker (table `<schema>.server_idempotency`).

    Expired records are replaced when their key is used again; delete old rows
    (`expires_at < now()`) periodically.

    Args:
        conninfo: A DSN, or a `psycopg_pool.AsyncConnectionPool` (autocommit) to share.
        setup: Create the schema and table on first use.
        pool_size: Size of the pool opened for a DSN.
    """

    def _ddl(self) -> list[str]:
        return [
            f"CREATE TABLE IF NOT EXISTS {self._schema}.server_idempotency ("
            " scope TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, status INTEGER,"
            " body JSONB, headers JSONB, expires_at TIMESTAMPTZ NOT NULL,"
            " PRIMARY KEY (scope, key))"
        ]

    async def begin(
        self, scope: str, key: str, fingerprint: str, ttl: float
    ) -> IdempotencyRecord | None:
        """Claim `key`, or return its unexpired record."""

        table = f"{self._schema}.server_idempotency"
        async with await self._connection() as conn:
            claimed = await fetch_one(
                conn,
                f"INSERT INTO {table} AS t (scope, key, fingerprint, expires_at)"
                " VALUES (%s, %s, %s, now() + make_interval(secs => %s))"
                " ON CONFLICT (scope, key) DO UPDATE SET fingerprint = EXCLUDED.fingerprint,"
                " status = NULL, body = NULL, headers = NULL, expires_at = EXCLUDED.expires_at"
                " WHERE t.expires_at <= now() RETURNING 1",
                (scope, key, fingerprint, float(ttl)),
            )
            if claimed is not None:
                return None
            row = await fetch_one(
                conn,
                f"SELECT fingerprint, status, body, headers FROM {table}"
                " WHERE scope = %s AND key = %s",
                (scope, key),
            )
        if row is None:  # expired and deleted in between: claim again
            return await self.begin(scope, key, fingerprint, ttl)
        return IdempotencyRecord(row[0], row[1], _json_value(row[2]), _json_value(row[3]) or {})

    async def complete(self, scope: str, key: str, record: IdempotencyRecord) -> None:
        """Store the response for a claimed key."""

        async with await self._connection() as conn:
            await conn.execute(
                f"UPDATE {self._schema}.server_idempotency SET status = %s, body = %s,"
                " headers = %s WHERE scope = %s AND key = %s",
                (
                    record.status,
                    json.dumps(record.body, default=str),
                    json.dumps(dict(record.headers)),
                    scope,
                    key,
                ),
            )

    async def release(self, scope: str, key: str) -> None:
        """Drop a claimed key that has no response."""

        async with await self._connection() as conn:
            await conn.execute(
                f"DELETE FROM {self._schema}.server_idempotency"
                " WHERE scope = %s AND key = %s AND status IS NULL",
                (scope, key),
            )


def _json_value(value: Any) -> Any:
    if isinstance(value, str | bytes):
        return json.loads(value)
    return value


__all__ = [
    "BaseIdempotencyCache",
    "BaseThreadIndex",
    "DecisionClaims",
    "IdempotencyRecord",
    "OwnedThreads",
    "PostgresIdempotencyCache",
    "PostgresThreadIndex",
    "SqliteThreadIndex",
    "ThreadOwner",
    "ThreadRecord",
]
