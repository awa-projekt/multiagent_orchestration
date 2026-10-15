"""`SqliteRunLog`: the `RunLog` in the local SQLite data file, shared with run store and thread index."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from awa.errors import (
    ConfigurationError,
    PermissionDeniedError,
    RunNotFoundError,
    StoreConflictError,
)
from awa.persistence.runlog import rows
from awa.persistence.runlog.protocol import (
    Approval,
    ApprovalDecision,
    AttemptRecord,
    CapturedContent,
    ContentKind,
    PseudonymKey,
    ResultKind,
    RunHead,
    RunRef,
    Scope,
    StepRecord,
    StepResult,
    SystemScope,
)
from awa.persistence.sql import SqliteDatabase
from awa.runtime.ids import IdSource, Uuid7Ids

_DELETE_CHUNK = 500
"""Run ids per DELETE statement (SQLite allows at most 999 parameters in older builds)."""


def _query(conn: sqlite3.Connection, statement: str, params: Sequence[Any]) -> sqlite3.Cursor:
    return conn.execute(
        rows.sql(statement), tuple(rows.stamp(p) if isinstance(p, datetime) else p for p in params)
    )


def _head(conn: sqlite3.Connection, scope: Scope, run_id: str) -> RunHead | None:
    row = _query(conn, rows.HEAD_SELECT, rows.head_params(scope, run_id)).fetchone()
    return rows.head_of(row) if row is not None else None


def _step(conn: sqlite3.Connection, scope: Scope, run_id: str, step_key: str) -> StepRecord | None:
    row = _query(conn, rows.STEP_SELECT, rows.step_params(scope, run_id, step_key)).fetchone()
    return rows.step_of(row) if row is not None else None


def _approval(conn: sqlite3.Connection, scope: Scope, approval_id: str) -> Approval | None:
    row = _query(conn, rows.APPROVAL_SELECT, rows.owned_params(scope, approval_id)).fetchone()
    return rows.approval_of(row) if row is not None else None


class SqliteRunLog:
    """The `RunLog` in a local SQLite file (see `awa.persistence.runlog.protocol`).

    Args:
        database: The data file, or an open `SqliteDatabase` to share with the run store and thread
            index.
        ids: Where result, approval and content ids come from (default time-ordered UUIDv7).
        pseudonym: The key that links people to their runs; without it erasure per person is
            refused.

    Raises:
        DataStoreError: See `SqliteDatabase`.
    """

    def __init__(
        self,
        database: SqliteDatabase | str | os.PathLike[str],
        *,
        ids: IdSource | None = None,
        pseudonym: PseudonymKey | None = None,
    ) -> None:
        self._owned = not isinstance(database, SqliteDatabase)
        self.database = (
            database if isinstance(database, SqliteDatabase) else SqliteDatabase(database)
        )
        self._ids: IdSource = ids or Uuid7Ids()
        self._pseudonym = pseudonym

    def __repr__(self) -> str:
        return f"SqliteRunLog({str(self.database.path)!r})"

    async def open_run(
        self, scope: Scope, run_id: str, *, now: datetime, initiator: str | None = None
    ) -> RunHead:
        """See `RunLog.open_run`."""

        def work(conn: sqlite3.Connection) -> RunHead:
            _query(conn, rows.HEAD_INSERT, rows.head_insert_params(scope, run_id, now))
            head = _head(conn, scope, run_id)
            if head is None:
                raise StoreConflictError(f"Run {run_id!r} belongs to another tenant or project")
            if initiator:
                self._link(conn, scope, initiator, "run", run_id, run_id, now)
            return head

        return await self.database.write(work)

    def _link(
        self,
        conn: sqlite3.Connection,
        scope: Scope,
        principal: str,
        ref_kind: str,
        ref_id: str,
        run_id: str,
        now: datetime,
    ) -> None:
        if self._pseudonym is None:
            return
        subject = self._pseudonym.subject(principal)
        params = rows.link_params(
            scope, subject, self._pseudonym.key_id, ref_kind, ref_id, run_id, now
        )
        _query(conn, rows.LINK_INSERT, params)

    async def head(self, scope: Scope, run_id: str) -> RunHead | None:
        """See `RunLog.head`."""

        return await self.database.read(lambda conn: _head(conn, scope, run_id))

    async def save_step(
        self, scope: Scope, step: StepRecord, *, expected_revision: int | None
    ) -> StepRecord:
        """See `RunLog.save_step`."""

        def work(conn: sqlite3.Connection) -> StepRecord:
            if _head(conn, scope, step.run_id) is None:
                raise RunNotFoundError(f"No run {step.run_id!r} in this scope")
            if expected_revision is None:
                try:
                    _query(conn, rows.STEP_INSERT, rows.step_insert_params(scope, step))
                except sqlite3.IntegrityError as exc:
                    raise StoreConflictError(
                        f"Step {step.step_key!r} of run {step.run_id!r} is stored already "
                        f"(or its position is taken): {exc}"
                    ) from exc
            else:
                params = rows.step_update_params(scope, step, expected_revision)
                if _query(conn, rows.STEP_UPDATE, params).rowcount == 0:
                    raise StoreConflictError(
                        f"Step {step.step_key!r} of run {step.run_id!r} changed since revision "
                        f"{expected_revision}; read it again"
                    )
            found = _step(conn, scope, step.run_id, step.step_key)
            if found is None:
                raise RunNotFoundError(f"No step {step.step_key!r} of run {step.run_id!r}")
            return found

        return await self.database.write(work)

    async def steps(self, scope: Scope, run_id: str) -> list[StepRecord]:
        """See `RunLog.steps`."""

        def work(conn: sqlite3.Connection) -> list[StepRecord]:
            found = _query(conn, rows.STEPS_SELECT, rows.owned_params(scope, run_id)).fetchall()
            return [rows.step_of(row) for row in found]

        return await self.database.read(work)

    async def start_attempt(
        self, scope: Scope, run_id: str, step_key: str, *, now: datetime
    ) -> AttemptRecord:
        """See `RunLog.start_attempt`."""

        def work(conn: sqlite3.Connection) -> AttemptRecord:
            if _step(conn, scope, run_id, step_key) is None:
                raise RunNotFoundError(f"No step {step_key!r} of run {run_id!r} in this scope")
            params = rows.step_params(scope, run_id, step_key)
            (last,) = _query(conn, rows.ATTEMPT_LAST, params).fetchone()
            attempt = AttemptRecord(
                run_id=run_id, step_key=step_key, attempt=int(last) + 1, started_at=now
            )
            _query(conn, rows.ATTEMPT_INSERT, rows.attempt_insert_params(scope, attempt))
            _query(conn, rows.STEP_STARTED, rows.step_started_params(scope, attempt))
            return attempt

        return await self.database.write(work)

    async def finish_attempt(self, scope: Scope, attempt: AttemptRecord) -> None:
        """See `RunLog.finish_attempt`."""

        def work(conn: sqlite3.Connection) -> None:
            changed = _query(conn, rows.ATTEMPT_UPDATE, rows.attempt_update_params(scope, attempt))
            if changed.rowcount == 0:
                raise RunNotFoundError(
                    f"No attempt {attempt.attempt} of step {attempt.step_key!r} in this scope"
                )
            _query(conn, rows.STEP_TOTALS, rows.totals_params(scope, attempt))

        await self.database.write(work)

    async def attempts(
        self, scope: Scope, run_id: str, *, step_key: str | None = None
    ) -> list[AttemptRecord]:
        """See `RunLog.attempts`."""

        def work(conn: sqlite3.Connection) -> list[AttemptRecord]:
            params = rows.filtered_params(scope, run_id, step_key)
            found = _query(conn, rows.ATTEMPTS_SELECT, params).fetchall()
            return [rows.attempt_of(row) for row in found]

        return await self.database.read(work)

    async def put_result(self, scope: Scope, result: StepResult) -> StepResult:
        """See `RunLog.put_result`."""

        stored = result.model_copy(
            update={"result_id": result.result_id or self._ids.new_id("res")}
        )

        def work(conn: sqlite3.Connection) -> StepResult:
            if _step(conn, scope, stored.run_id, stored.step_key) is None:
                raise RunNotFoundError(
                    f"No step {stored.step_key!r} of run {stored.run_id!r} in this scope"
                )
            if stored.kind == "final":
                _query(conn, rows.FINAL_DELETE, rows.final_delete_params(scope, stored))
            _query(conn, rows.RESULT_INSERT, rows.result_insert_params(scope, stored))
            return stored

        return await self.database.write(work)

    async def results(
        self,
        scope: Scope,
        run_id: str,
        *,
        step_key: str | None = None,
        kind: ResultKind | None = None,
    ) -> list[StepResult]:
        """See `RunLog.results`."""

        def work(conn: sqlite3.Connection) -> list[StepResult]:
            params = rows.filtered_params(scope, run_id, step_key, kind)
            found = _query(conn, rows.RESULTS_SELECT, params).fetchall()
            return [rows.result_of(row) for row in found]

        return await self.database.read(work)

    async def capture(
        self, scope: Scope, run_id: str, kind: ContentKind, body: Any, *, now: datetime
    ) -> str | None:
        """See `RunLog.capture`."""

        def work(conn: sqlite3.Connection) -> str | None:
            head = _head(conn, scope, run_id)
            if head is None or not head.capture:
                return None
            ref = self._ids.new_id("cnt")
            params = rows.content_insert_params(scope, ref, run_id, kind, body, now)
            _query(conn, rows.CONTENT_INSERT, params)
            return ref

        return await self.database.write(work)

    async def content(self, scope: Scope, content_ref: str) -> CapturedContent | None:
        """See `RunLog.content`."""

        if not scope.is_auditor:
            raise PermissionDeniedError("Reading captured content needs the auditor role")
        reader = (
            self._pseudonym.subject(scope.principal)
            if self._pseudonym is not None and scope.principal
            else None
        )
        read_id = self._ids.new_id("rd")

        def work(conn: sqlite3.Connection) -> CapturedContent | None:
            cursor = _query(conn, rows.CONTENT_SELECT, rows.content_params(scope, content_ref))
            found = cursor.fetchone()
            if found is None:
                return None
            params = rows.content_read_params(scope, read_id, content_ref, reader)
            _query(conn, rows.CONTENT_READ_INSERT, params)
            return rows.content_of(found)

        return await self.database.write(work)

    async def request_approval(self, scope: Scope, approval: Approval) -> Approval:
        """See `RunLog.request_approval`."""

        stored = approval.model_copy(
            update={"approval_id": approval.approval_id or self._ids.new_id("apr")}
        )

        def work(conn: sqlite3.Connection) -> Approval:
            if _head(conn, scope, stored.run_id) is None:
                raise RunNotFoundError(f"No run {stored.run_id!r} in this scope")
            _query(conn, rows.APPROVAL_INSERT, rows.approval_insert_params(scope, stored))
            return stored

        return await self.database.write(work)

    async def decide_approval(
        self,
        scope: Scope,
        approval_id: str,
        *,
        decision: ApprovalDecision,
        reviewer: str,
        reason: str | None,
        now: datetime,
    ) -> Approval:
        """See `RunLog.decide_approval`."""

        if not reviewer or not reviewer.strip():
            raise ConfigurationError("An approval decision needs a reviewer")

        def work(conn: sqlite3.Connection) -> Approval:
            params = rows.approval_update_params(
                scope, approval_id, decision, reviewer, reason, now
            )
            changed = _query(conn, rows.APPROVAL_UPDATE, params).rowcount
            found = _approval(conn, scope, approval_id)
            if found is None:
                raise RunNotFoundError(f"No approval {approval_id!r} in this scope")
            if changed == 0:
                raise StoreConflictError(f"Approval {approval_id!r} was decided before")
            self._link(conn, scope, reviewer, "approval", approval_id, found.run_id, now)
            return found

        return await self.database.write(work)

    async def approvals(self, scope: Scope, run_id: str) -> list[Approval]:
        """See `RunLog.approvals`."""

        def work(conn: sqlite3.Connection) -> list[Approval]:
            found = _query(conn, rows.APPROVALS_SELECT, rows.owned_params(scope, run_id)).fetchall()
            return [rows.approval_of(row) for row in found]

        return await self.database.read(work)

    async def subject_runs(self, scope: Scope, principal: str) -> list[str]:
        """See `RunLog.subject_runs`."""

        if self._pseudonym is None:
            raise ConfigurationError(
                "Finding a person's runs needs the pseudonym key (AWA_PSEUDONYM_KEY)"
            )
        subject = self._pseudonym.subject(principal)

        def work(conn: sqlite3.Connection) -> list[str]:
            found = _query(conn, rows.SUBJECT_RUNS, rows.subject_params(scope, subject))
            return [str(row[0]) for row in found.fetchall()]

        return await self.database.read(work)

    async def project_runs(self, scope: Scope) -> list[str]:
        """See `RunLog.project_runs`."""

        def work(conn: sqlite3.Connection) -> list[str]:
            found = _query(conn, rows.PROJECT_RUNS, rows.owned_params(scope))
            return [str(row[0]) for row in found.fetchall()]

        return await self.database.read(work)

    async def expired(
        self, system: SystemScope, *, before: datetime, limit: int = 1000
    ) -> list[RunRef]:
        """See `RunLog.expired`."""

        def work(conn: sqlite3.Connection) -> list[RunRef]:
            found = _query(conn, rows.EXPIRED, (before, max(0, limit))).fetchall()
            return [rows.ref_of(row) for row in found]

        return await self.database.read(work)

    async def delete_runs(self, scope: Scope, run_ids: Sequence[str]) -> int:
        """See `RunLog.delete_runs`."""

        wanted = list(dict.fromkeys(run_ids))
        if not wanted:
            return 0

        def work(conn: sqlite3.Connection) -> int:
            deleted = 0
            # In chunks, all in one transaction: SQLite limits the parameters of a statement.
            for start in range(0, len(wanted), _DELETE_CHUNK):
                chunk = wanted[start : start + _DELETE_CHUNK]
                marks = ", ".join("?" for _ in chunk)
                deleted += conn.execute(
                    "DELETE FROM run_log_head WHERE tenant_id = ? AND project_id = ?"  # noqa: S608
                    f" AND run_id IN ({marks})",
                    (scope.tenant, scope.project, *chunk),
                ).rowcount
            return deleted

        deleted = await self.database.write(work)
        await asyncio.to_thread(self.database.compact)
        return deleted

    async def close(self) -> None:
        """Close the database when this run log opened it."""

        if self._owned:
            await asyncio.to_thread(self.database.close)


__all__ = ["SqliteRunLog"]
