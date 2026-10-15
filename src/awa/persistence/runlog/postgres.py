"""`PostgresRunLog`: the run log in PostgreSQL, shared by every server and worker.

Tables come from the migration files; pass the process's `shared_pool` so every PostgreSQL part
uses the same connections. Needs the `postgres` extra.
"""

from __future__ import annotations

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
from awa.persistence.sql import PostgresMigratedTables, fetch_all, fetch_one, import_psycopg
from awa.runtime.ids import IdSource, Uuid7Ids


class PostgresRunLog(PostgresMigratedTables):
    """The `RunLog` in PostgreSQL (see `awa.persistence.runlog.protocol`).

    Args:
        conninfo: A DSN, or the shared `psycopg_pool.AsyncConnectionPool` (autocommit).
        setup: Apply pending migrations on first use.
        ids: Where result, approval and content ids come from (default time-ordered UUIDv7).
        pseudonym: The key that links people to their runs; without it erasure per person is
            refused.

    Raises:
        OptionalDependencyError: If the `postgres` extra is not installed.
        ConfigurationError: If the schema or DSN is invalid.
    """

    def __init__(
        self,
        conninfo: Any,
        *,
        schema: str = "awa",
        setup: bool = True,
        pool_size: int = 4,
        ids: IdSource | None = None,
        pseudonym: PseudonymKey | None = None,
    ) -> None:
        super().__init__(conninfo, schema=schema, setup=setup, pool_size=pool_size)
        self._ids: IdSource = ids or Uuid7Ids()
        self._pseudonym = pseudonym

    def __repr__(self) -> str:
        return f"PostgresRunLog(schema={self._schema!r})"

    def _sql(self, statement: str) -> str:
        return rows.sql(statement, self._schema)

    async def _head(self, conn: Any, scope: Scope, run_id: str) -> RunHead | None:
        row = await fetch_one(conn, self._sql(rows.HEAD_SELECT), rows.head_params(scope, run_id))
        return rows.head_of(row) if row is not None else None

    async def _step(self, conn: Any, scope: Scope, run_id: str, step_key: str) -> StepRecord | None:
        row = await fetch_one(
            conn, self._sql(rows.STEP_SELECT), rows.step_params(scope, run_id, step_key)
        )
        return rows.step_of(row) if row is not None else None

    async def _approval(self, conn: Any, scope: Scope, approval_id: str) -> Approval | None:
        row = await fetch_one(
            conn, self._sql(rows.APPROVAL_SELECT), rows.owned_params(scope, approval_id)
        )
        return rows.approval_of(row) if row is not None else None

    async def open_run(
        self, scope: Scope, run_id: str, *, now: datetime, initiator: str | None = None
    ) -> RunHead:
        """See `RunLog.open_run`."""

        async with await self._connection() as conn, conn.transaction():
            await conn.execute(
                self._sql(rows.HEAD_INSERT), rows.head_insert_params(scope, run_id, now)
            )
            head = await self._head(conn, scope, run_id)
            if head is None:
                raise StoreConflictError(f"Run {run_id!r} belongs to another tenant or project")
            if initiator:
                await self._link(conn, scope, initiator, "run", run_id, run_id, now)
            return head

    async def _link(
        self,
        conn: Any,
        scope: Scope,
        principal: str,
        ref_kind: str,
        ref_id: str,
        run_id: str,
        now: datetime,
    ) -> None:
        if self._pseudonym is None:
            return
        params = rows.link_params(
            scope,
            self._pseudonym.subject(principal),
            self._pseudonym.key_id,
            ref_kind,
            ref_id,
            run_id,
            now,
        )
        await conn.execute(self._sql(rows.LINK_INSERT), params)

    async def head(self, scope: Scope, run_id: str) -> RunHead | None:
        """See `RunLog.head`."""

        async with await self._connection() as conn:
            return await self._head(conn, scope, run_id)

    async def save_step(
        self, scope: Scope, step: StepRecord, *, expected_revision: int | None
    ) -> StepRecord:
        """See `RunLog.save_step`."""

        psycopg, _ = import_psycopg()
        async with await self._connection() as conn, conn.transaction():
            if await self._head(conn, scope, step.run_id) is None:
                raise RunNotFoundError(f"No run {step.run_id!r} in this scope")
            if expected_revision is None:
                try:
                    async with conn.transaction():
                        await conn.execute(
                            self._sql(rows.STEP_INSERT), rows.step_insert_params(scope, step)
                        )
                except psycopg.errors.UniqueViolation as exc:
                    raise StoreConflictError(
                        f"Step {step.step_key!r} of run {step.run_id!r} is stored already "
                        f"(or its position is taken): {exc}"
                    ) from exc
            else:
                cursor = await conn.execute(
                    self._sql(rows.STEP_UPDATE),
                    rows.step_update_params(scope, step, expected_revision),
                )
                if cursor.rowcount == 0:
                    raise StoreConflictError(
                        f"Step {step.step_key!r} of run {step.run_id!r} changed since revision "
                        f"{expected_revision}; read it again"
                    )
            found = await self._step(conn, scope, step.run_id, step.step_key)
            if found is None:
                raise RunNotFoundError(f"No step {step.step_key!r} of run {step.run_id!r}")
            return found

    async def steps(self, scope: Scope, run_id: str) -> list[StepRecord]:
        """See `RunLog.steps`."""

        async with await self._connection() as conn:
            found = await fetch_all(
                conn, self._sql(rows.STEPS_SELECT), rows.owned_params(scope, run_id)
            )
        return [rows.step_of(row) for row in found]

    async def start_attempt(
        self, scope: Scope, run_id: str, step_key: str, *, now: datetime
    ) -> AttemptRecord:
        """See `RunLog.start_attempt`."""

        params = rows.step_params(scope, run_id, step_key)
        async with await self._connection() as conn, conn.transaction():
            locked = await fetch_one(conn, self._sql(rows.STEP_SELECT) + " FOR UPDATE", params)
            if locked is None:
                raise RunNotFoundError(f"No step {step_key!r} of run {run_id!r} in this scope")
            (last,) = await fetch_one(conn, self._sql(rows.ATTEMPT_LAST), params)
            attempt = AttemptRecord(
                run_id=run_id, step_key=step_key, attempt=int(last) + 1, started_at=now
            )
            await conn.execute(
                self._sql(rows.ATTEMPT_INSERT), rows.attempt_insert_params(scope, attempt)
            )
            await conn.execute(
                self._sql(rows.STEP_STARTED), rows.step_started_params(scope, attempt)
            )
            return attempt

    async def finish_attempt(self, scope: Scope, attempt: AttemptRecord) -> None:
        """See `RunLog.finish_attempt`."""

        async with await self._connection() as conn, conn.transaction():
            cursor = await conn.execute(
                self._sql(rows.ATTEMPT_UPDATE), rows.attempt_update_params(scope, attempt)
            )
            if cursor.rowcount == 0:
                raise RunNotFoundError(
                    f"No attempt {attempt.attempt} of step {attempt.step_key!r} in this scope"
                )
            await conn.execute(self._sql(rows.STEP_TOTALS), rows.totals_params(scope, attempt))

    async def attempts(
        self, scope: Scope, run_id: str, *, step_key: str | None = None
    ) -> list[AttemptRecord]:
        """See `RunLog.attempts`."""

        async with await self._connection() as conn:
            found = await fetch_all(
                conn, self._sql(rows.ATTEMPTS_SELECT), rows.filtered_params(scope, run_id, step_key)
            )
        return [rows.attempt_of(row) for row in found]

    async def put_result(self, scope: Scope, result: StepResult) -> StepResult:
        """See `RunLog.put_result`."""

        stored = result.model_copy(
            update={"result_id": result.result_id or self._ids.new_id("res")}
        )
        async with await self._connection() as conn, conn.transaction():
            if await self._step(conn, scope, stored.run_id, stored.step_key) is None:
                raise RunNotFoundError(
                    f"No step {stored.step_key!r} of run {stored.run_id!r} in this scope"
                )
            if stored.kind == "final":
                await conn.execute(
                    self._sql(rows.FINAL_DELETE), rows.final_delete_params(scope, stored)
                )
            await conn.execute(
                self._sql(rows.RESULT_INSERT), rows.result_insert_params(scope, stored)
            )
        return stored

    async def results(
        self,
        scope: Scope,
        run_id: str,
        *,
        step_key: str | None = None,
        kind: ResultKind | None = None,
    ) -> list[StepResult]:
        """See `RunLog.results`."""

        async with await self._connection() as conn:
            found = await fetch_all(
                conn,
                self._sql(rows.RESULTS_SELECT),
                rows.filtered_params(scope, run_id, step_key, kind),
            )
        return [rows.result_of(row) for row in found]

    async def capture(
        self, scope: Scope, run_id: str, kind: ContentKind, body: Any, *, now: datetime
    ) -> str | None:
        """See `RunLog.capture`."""

        async with await self._connection() as conn, conn.transaction():
            head = await self._head(conn, scope, run_id)
            if head is None or not head.capture:
                return None
            ref = self._ids.new_id("cnt")
            await conn.execute(
                self._sql(rows.CONTENT_INSERT),
                rows.content_insert_params(scope, ref, run_id, kind, body, now),
            )
            return ref

    async def content(self, scope: Scope, content_ref: str) -> CapturedContent | None:
        """See `RunLog.content`."""

        if not scope.is_auditor:
            raise PermissionDeniedError("Reading captured content needs the auditor role")
        reader = (
            self._pseudonym.subject(scope.principal)
            if self._pseudonym is not None and scope.principal
            else None
        )
        async with await self._connection() as conn, conn.transaction():
            row = await fetch_one(
                conn, self._sql(rows.CONTENT_SELECT), rows.content_params(scope, content_ref)
            )
            if row is None:
                return None
            await conn.execute(
                self._sql(rows.CONTENT_READ_INSERT),
                rows.content_read_params(scope, self._ids.new_id("rd"), content_ref, reader),
            )
            return rows.content_of(row)

    async def request_approval(self, scope: Scope, approval: Approval) -> Approval:
        """See `RunLog.request_approval`."""

        stored = approval.model_copy(
            update={"approval_id": approval.approval_id or self._ids.new_id("apr")}
        )
        async with await self._connection() as conn, conn.transaction():
            if await self._head(conn, scope, stored.run_id) is None:
                raise RunNotFoundError(f"No run {stored.run_id!r} in this scope")
            await conn.execute(
                self._sql(rows.APPROVAL_INSERT), rows.approval_insert_params(scope, stored)
            )
        return stored

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
        async with await self._connection() as conn, conn.transaction():
            cursor = await conn.execute(
                self._sql(rows.APPROVAL_UPDATE),
                rows.approval_update_params(scope, approval_id, decision, reviewer, reason, now),
            )
            found = await self._approval(conn, scope, approval_id)
            if found is None:
                raise RunNotFoundError(f"No approval {approval_id!r} in this scope")
            if cursor.rowcount == 0:
                raise StoreConflictError(f"Approval {approval_id!r} was decided before")
            await self._link(conn, scope, reviewer, "approval", approval_id, found.run_id, now)
            return found

    async def approvals(self, scope: Scope, run_id: str) -> list[Approval]:
        """See `RunLog.approvals`."""

        async with await self._connection() as conn:
            found = await fetch_all(
                conn, self._sql(rows.APPROVALS_SELECT), rows.owned_params(scope, run_id)
            )
        return [rows.approval_of(row) for row in found]

    async def subject_runs(self, scope: Scope, principal: str) -> list[str]:
        """See `RunLog.subject_runs`."""

        if self._pseudonym is None:
            raise ConfigurationError(
                "Finding a person's runs needs the pseudonym key (AWA_PSEUDONYM_KEY)"
            )
        async with await self._connection() as conn:
            found = await fetch_all(
                conn,
                self._sql(rows.SUBJECT_RUNS),
                rows.subject_params(scope, self._pseudonym.subject(principal)),
            )
        return [str(row[0]) for row in found]

    async def project_runs(self, scope: Scope) -> list[str]:
        """See `RunLog.project_runs`."""

        async with await self._connection() as conn:
            found = await fetch_all(conn, self._sql(rows.PROJECT_RUNS), rows.owned_params(scope))
        return [str(row[0]) for row in found]

    async def expired(
        self, system: SystemScope, *, before: datetime, limit: int = 1000
    ) -> list[RunRef]:
        """See `RunLog.expired`."""

        async with await self._connection() as conn:
            found = await fetch_all(conn, self._sql(rows.EXPIRED), (before, max(0, limit)))
        return [rows.ref_of(row) for row in found]

    async def delete_runs(self, scope: Scope, run_ids: Sequence[str]) -> int:
        """See `RunLog.delete_runs`; PostgreSQL frees the space when it vacuums, backups keep the rows."""

        wanted = list(dict.fromkeys(run_ids))
        if not wanted:
            return 0
        async with await self._connection() as conn, conn.transaction():
            cursor = await conn.execute(
                f"DELETE FROM {self._schema}.run_log_head WHERE tenant_id = %s AND project_id = %s"  # noqa: S608
                " AND run_id = ANY(%s)",
                (scope.tenant, scope.project, wanted),
            )
            return int(cursor.rowcount)


__all__ = ["PostgresRunLog"]
