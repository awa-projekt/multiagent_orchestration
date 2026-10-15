"""PostgreSQL `PostgresRunStore` and `PostgresEventQueue` (the `postgres` extra): a pool per event
loop, tables created unless `setup=False`; on Windows run them on a selector event loop."""

from __future__ import annotations

import builtins
import json
from collections.abc import Collection, Sequence
from datetime import datetime, timedelta
from typing import Any

from awa._internal.util import DurationLike, to_timedelta
from awa.automation._time import as_utc, require_timedelta, utcnow
from awa.automation.models import ProcessRun, statuses
from awa.automation.queues import QueueMessage
from awa.automation.store import RunChange, claimed_copy
from awa.errors import ProcessRunStateError, RunNotFoundError
from awa.persistence.sql import (
    PostgresMigratedTables,
    PostgresTables,
    check_name,
    fetch_all,
    fetch_one,
    import_psycopg,
)

_SETUP_LOCK_KEY = 0x6177615F70726F63  # "awa_proc"


class PostgresRunStore(PostgresMigratedTables):
    """A `RunStore` in PostgreSQL, shared by any number of workers (table `<schema>.process_runs`).

    Claims use `FOR UPDATE SKIP LOCKED`, so each queued run goes to one worker; `max_running` is
    serialized with an advisory lock.

    Args:
        conninfo: A DSN, or a `psycopg_pool.AsyncConnectionPool` with `autocommit=True`.
        setup: Create the schema and tables on first use.
        pool_size: Size of the pool opened for a DSN.

    Example:
        ```python
        store = PostgresRunStore(os.environ["AWA_POSTGRES_DSN"])
        ```
    """

    def __repr__(self) -> str:
        return f"PostgresRunStore(schema={self._schema!r})"

    @staticmethod
    def _columns(run: ProcessRun) -> dict[str, Any]:
        ready = (run.next_attempt_at or run.created_at) if run.status == "queued" else None
        return {
            "id": run.id,
            "automation": run.automation,
            "status": run.status,
            "dedup_key": run.dedup_key,
            "tenant": run.tenant,
            "version": run.version,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "ready_at": ready,
            "due_at": run.due_at(),
            "lease_owner": run.lease_owner,
            "lease_expires_at": run.lease_expires_at,
            "wait_keys": run.wait_keys if run.status == "waiting" else [],
            "revision": run.revision,
            "data": run.model_dump_json(),
        }

    async def _write(self, conn: Any, run: ProcessRun, *, insert: bool) -> None:
        values = self._columns(run)
        if insert:
            names = ", ".join(values)
            marks = ", ".join(
                f"%({name})s::jsonb" if name == "data" else f"%({name})s" for name in values
            )
            await conn.execute(
                f"INSERT INTO {self._schema}.process_runs ({names}) VALUES ({marks})", values
            )
            return
        assignments = ", ".join(
            f"{name} = %({name})s::jsonb" if name == "data" else f"{name} = %({name})s"
            for name in values
            if name != "id"
        )
        await conn.execute(
            f"UPDATE {self._schema}.process_runs SET {assignments} WHERE id = %(id)s", values
        )

    async def create(
        self, run: ProcessRun, *, dedup_since: datetime | None = None
    ) -> tuple[ProcessRun, bool]:
        """See `RunStore.create`."""

        async with await self._connection() as conn, conn.transaction():
            if run.dedup_key is not None:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"awa.dedup\x1f{run.automation}\x1f{run.dedup_key}",),
                )
                since = as_utc(dedup_since) if dedup_since is not None else None
                existing = await fetch_one(
                    conn,
                    f"SELECT data FROM {self._schema}.process_runs"
                    " WHERE automation = %s AND dedup_key = %s"
                    " AND (%s::timestamptz IS NULL OR created_at >= %s::timestamptz)"
                    " ORDER BY created_at DESC LIMIT 1",
                    (run.automation, run.dedup_key, since, since),
                )
                if existing is not None:
                    return _run(existing[0]), False
            try:
                await self._write(conn, run, insert=True)
            except Exception as exc:
                psycopg, _ = import_psycopg()
                if isinstance(exc, psycopg.errors.UniqueViolation):
                    raise ProcessRunStateError(f"Process run {run.id!r} already exists") from exc
                raise
        return run.model_copy(deep=True), True

    async def get(self, run_id: str) -> ProcessRun | None:
        """See `RunStore.get`."""

        async with await self._connection() as conn:
            row = await fetch_one(
                conn, f"SELECT data FROM {self._schema}.process_runs WHERE id = %s", (run_id,)
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
        async with await self._connection() as conn:
            rows = await fetch_all(
                conn,
                f"SELECT data FROM {self._schema}.process_runs"
                " WHERE (%s::text IS NULL OR automation = %s)"
                " AND (%s::text[] IS NULL OR status = ANY(%s::text[]))"
                " AND (%s::text IS NULL OR tenant = %s)"
                " ORDER BY created_at DESC, id DESC LIMIT %s",
                (
                    automation,
                    automation,
                    list(wanted) if wanted is not None else None,
                    list(wanted) if wanted is not None else None,
                    tenant,
                    tenant,
                    max(0, limit),
                ),
            )
        return [_run(row[0]) for row in rows]

    async def modify(self, run_id: str, change: RunChange, *, now: datetime) -> ProcessRun:
        """See `RunStore.modify`."""

        async with await self._connection() as conn, conn.transaction():
            row = await fetch_one(
                conn,
                f"SELECT data FROM {self._schema}.process_runs WHERE id = %s FOR UPDATE",
                (run_id,),
            )
            if row is None:
                raise RunNotFoundError(f"No process run {run_id!r}")
            current = _run(row[0])
            changed = change(current.model_copy(deep=True))
            if changed is None:
                return current
            stored = changed.model_copy(
                update={"revision": current.revision + 1, "updated_at": as_utc(now)}, deep=True
            )
            await self._write(conn, stored, insert=False)
        return stored

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
        """See `RunStore.claim`."""

        moment = as_utc(now)
        async with await self._connection() as conn, conn.transaction():
            if max_running is not None:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"awa.claim\x1f{automation}",),
                )
                running = await fetch_one(
                    conn,
                    f"SELECT count(*) FROM {self._schema}.process_runs"
                    " WHERE automation = %s AND status = 'running'",
                    (automation,),
                )
                if running is not None and int(running[0]) >= max_running:
                    return None
            version_list = list(versions) if versions is not None else None
            row = await fetch_one(
                conn,
                f"SELECT data FROM {self._schema}.process_runs"
                " WHERE automation = %s AND status = 'queued' AND ready_at <= %s"
                " AND (%s::text[] IS NULL OR version IS NULL OR version = ANY(%s::text[]))"
                " ORDER BY ready_at, id LIMIT 1 FOR UPDATE SKIP LOCKED",
                (automation, moment, version_list, version_list),
            )
            if row is None:
                return None
            claimed = claimed_copy(_run(row[0]), worker=worker, lease=lease, now=moment)
            await self._write(conn, claimed, insert=False)
        return claimed

    async def renew(
        self, run_id: str, *, worker: str, lease: timedelta, now: datetime, epoch: int | None = None
    ) -> bool:
        """See `RunStore.renew`."""

        expires = as_utc(now) + lease
        async with await self._connection() as conn:
            row = await fetch_one(
                conn,
                f"UPDATE {self._schema}.process_runs SET lease_expires_at = %s,"
                " data = jsonb_set(data, '{lease_expires_at}', to_jsonb(%s::text))"
                " WHERE id = %s AND status = 'running' AND lease_owner = %s"
                " AND (%s::bigint IS NULL"
                " OR COALESCE((data->>'lease_epoch')::bigint, 0) = %s::bigint) RETURNING id",
                (expires, expires.isoformat(), run_id, worker, epoch, epoch),
            )
        return row is not None

    async def expired(self, automation: str, *, now: datetime) -> builtins.list[ProcessRun]:
        """See `RunStore.expired`."""

        async with await self._connection() as conn:
            rows = await fetch_all(
                conn,
                f"SELECT data FROM {self._schema}.process_runs WHERE automation = %s"
                " AND status = 'running' AND lease_expires_at < %s",
                (automation, as_utc(now)),
            )
        return [_run(row[0]) for row in rows]

    async def due(
        self, automation: str, *, now: datetime, limit: int = 100
    ) -> builtins.list[ProcessRun]:
        """See `RunStore.due`."""

        async with await self._connection() as conn:
            rows = await fetch_all(
                conn,
                f"SELECT data FROM {self._schema}.process_runs WHERE automation = %s"
                " AND due_at IS NOT NULL AND due_at <= %s ORDER BY due_at LIMIT %s",
                (automation, as_utc(now), max(0, limit)),
            )
        return [_run(row[0]) for row in rows]

    async def waiting_on(self, automation: str, key: str) -> builtins.list[ProcessRun]:
        """See `RunStore.waiting_on`."""

        async with await self._connection() as conn:
            rows = await fetch_all(
                conn,
                f"SELECT data FROM {self._schema}.process_runs WHERE automation = %s"
                " AND status = 'waiting' AND wait_keys @> ARRAY[%s]::text[]",
                (automation, key),
            )
        return [_run(row[0]) for row in rows]

    async def get_state(self, key: str) -> str | None:
        """See `RunStore.get_state`."""

        async with await self._connection() as conn:
            row = await fetch_one(
                conn, f"SELECT value FROM {self._schema}.process_state WHERE key = %s", (key,)
            )
        return str(row[0]) if row is not None else None

    async def compare_and_set_state(self, key: str, expected: str | None, value: str) -> bool:
        """See `RunStore.compare_and_set_state`."""

        async with await self._connection() as conn:
            if expected is None:
                row = await fetch_one(
                    conn,
                    f"INSERT INTO {self._schema}.process_state (key, value) VALUES (%s, %s)"
                    " ON CONFLICT DO NOTHING RETURNING key",
                    (key, value),
                )
            else:
                row = await fetch_one(
                    conn,
                    f"UPDATE {self._schema}.process_state SET value = %s, updated_at = now()"
                    " WHERE key = %s AND value = %s RETURNING key",
                    (value, key, expected),
                )
        return row is not None

    async def delete(self, run_ids: Sequence[str]) -> int:
        """Delete runs for good (retention and erasure); returns how many were deleted."""

        wanted = builtins.list(dict.fromkeys(run_ids))
        if not wanted:
            return 0
        async with await self._connection() as conn:
            cursor = await conn.execute(
                f"DELETE FROM {self._schema}.process_runs WHERE id = ANY(%s)", (wanted,)
            )
        return int(cursor.rowcount)


class PostgresEventQueue(PostgresTables):
    """An `EventQueue` in a PostgreSQL table (`<schema>.event_queue`), shared by every process.

    Args:
        conninfo: A DSN or an `AsyncConnectionPool`.
        name: Queue name; queues with different names share the table.
        max_attempts: Deliveries before a message is dead-lettered (`<schema>.event_queue_dead`).
        setup: Create the tables on first use.
        pool_size: Size of the pool opened for a DSN.
    """

    _setup_lock_key = _SETUP_LOCK_KEY

    def __init__(
        self,
        conninfo: Any,
        *,
        name: str,
        schema: str = "awa",
        max_attempts: int = 5,
        setup: bool = True,
        pool_size: int = 4,
    ) -> None:
        super().__init__(conninfo, schema=schema, setup=setup, pool_size=pool_size)
        self.name = check_name(name, "name")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.max_attempts = max_attempts

    def __repr__(self) -> str:
        return f"PostgresEventQueue({self.name!r}, schema={self._schema!r})"

    def _ddl(self) -> list[str]:
        s = self._schema
        return [
            f"CREATE TABLE IF NOT EXISTS {s}.event_queue ("
            " id TEXT PRIMARY KEY, queue TEXT NOT NULL, payload JSONB,"
            " dedup_key TEXT, enqueued_at TIMESTAMPTZ NOT NULL,"
            " available_at TIMESTAMPTZ NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)",
            f"CREATE INDEX IF NOT EXISTS event_queue_ready ON {s}.event_queue (queue, available_at)",
            f"CREATE TABLE IF NOT EXISTS {s}.event_queue_dead ("
            " id TEXT PRIMARY KEY, queue TEXT NOT NULL, payload JSONB, dedup_key TEXT,"
            " enqueued_at TIMESTAMPTZ NOT NULL, attempts INTEGER NOT NULL,"
            " dead_at TIMESTAMPTZ NOT NULL DEFAULT now())",
        ]

    async def send(
        self, payload: Any, *, dedup_key: str | None = None, delay: DurationLike | None = None
    ) -> str:
        """See `EventQueue.send`."""

        message = QueueMessage(payload=payload, dedup_key=dedup_key)
        available = utcnow() + (to_timedelta(delay, what="delay") or timedelta(0))
        async with await self._connection() as conn:
            await conn.execute(
                f"INSERT INTO {self._schema}.event_queue"
                " (id, queue, payload, dedup_key, enqueued_at, available_at)"
                " VALUES (%s, %s, %s::jsonb, %s, now(), %s)",
                (message.id, self.name, json.dumps(payload, default=str), dedup_key, available),
            )
        return message.id

    async def receive(
        self, *, limit: int = 10, visibility: DurationLike = timedelta(minutes=5)
    ) -> builtins.list[QueueMessage]:
        """See `EventQueue.receive`."""

        hidden = require_timedelta(visibility, what="visibility").total_seconds()
        async with await self._connection() as conn, conn.transaction():
            # A message delivered `max_attempts` times and never acknowledged (its
            # receiver crashed) is dead-lettered instead of being offered again.
            await conn.execute(
                f"WITH dead AS (DELETE FROM {self._schema}.event_queue"
                " WHERE queue = %s AND available_at <= now() AND attempts >= %s"
                " RETURNING id, queue, payload, dedup_key, enqueued_at, attempts)"
                f" INSERT INTO {self._schema}.event_queue_dead"
                " (id, queue, payload, dedup_key, enqueued_at, attempts)"
                " SELECT id, queue, payload, dedup_key, enqueued_at, attempts FROM dead"
                " ON CONFLICT (id) DO NOTHING",
                (self.name, self.max_attempts),
            )
            rows = await fetch_all(
                conn,
                f"UPDATE {self._schema}.event_queue q SET attempts = q.attempts + 1,"
                " available_at = now() + %s * interval '1 second'"
                f" WHERE q.id IN (SELECT id FROM {self._schema}.event_queue"
                " WHERE queue = %s AND available_at <= now()"
                " ORDER BY available_at, enqueued_at LIMIT %s FOR UPDATE SKIP LOCKED)"
                " RETURNING q.id, q.payload, q.dedup_key, q.enqueued_at, q.available_at, q.attempts",
                (hidden, self.name, max(0, limit)),
            )
        return [_message(row) for row in sorted(rows, key=lambda row: (row[3], row[0]))]

    async def ack(self, message_id: str) -> None:
        """See `EventQueue.ack`."""

        async with await self._connection() as conn:
            await conn.execute(
                f"DELETE FROM {self._schema}.event_queue WHERE id = %s AND queue = %s",
                (message_id, self.name),
            )

    async def nack(self, message_id: str, *, delay: DurationLike | None = None) -> None:
        """See `EventQueue.nack`."""

        seconds = (to_timedelta(delay) or timedelta(0)).total_seconds()
        async with await self._connection() as conn, conn.transaction():
            row = await fetch_one(
                conn,
                f"SELECT attempts FROM {self._schema}.event_queue WHERE id = %s AND queue = %s"
                " FOR UPDATE",
                (message_id, self.name),
            )
            if row is None:
                return
            if int(row[0]) >= self.max_attempts:
                await conn.execute(
                    f"INSERT INTO {self._schema}.event_queue_dead"
                    " (id, queue, payload, dedup_key, enqueued_at, attempts)"
                    " SELECT id, queue, payload, dedup_key, enqueued_at, attempts"
                    f" FROM {self._schema}.event_queue WHERE id = %s",
                    (message_id,),
                )
                await conn.execute(
                    f"DELETE FROM {self._schema}.event_queue WHERE id = %s", (message_id,)
                )
                return
            await conn.execute(
                f"UPDATE {self._schema}.event_queue"
                " SET available_at = now() + %s * interval '1 second' WHERE id = %s",
                (seconds, message_id),
            )

    async def dead_letters(self, *, limit: int = 100) -> builtins.list[QueueMessage]:
        """Messages that failed `max_attempts` times, oldest first."""

        async with await self._connection() as conn:
            rows = await fetch_all(
                conn,
                "SELECT id, payload, dedup_key, enqueued_at, enqueued_at AS available_at, attempts"
                f" FROM {self._schema}.event_queue_dead WHERE queue = %s"
                " ORDER BY dead_at LIMIT %s",
                (self.name, max(0, limit)),
            )
        return [_message(row) for row in rows]


def _run(data: Any) -> ProcessRun:
    return ProcessRun.model_validate(json.loads(data) if isinstance(data, str) else data)


def _message(row: Any) -> QueueMessage:
    fields = ("id", "payload", "dedup_key", "enqueued_at", "available_at", "attempts")
    return QueueMessage(**dict(zip(fields, row, strict=True)))
