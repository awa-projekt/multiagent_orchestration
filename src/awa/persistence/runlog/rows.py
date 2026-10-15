"""SQL backend kit: the statements, parameters and rows of the run log tables.

Each statement is written once with `?` placeholders and the tokens `{s}` (schema prefix),
`{jsonb}`, `{text}` (PostgreSQL casts) and `{now}` (database clock), rendered per backend by
`sql`.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from functools import cache
from typing import Any

from awa.persistence.runlog.protocol import (
    Approval,
    AttemptRecord,
    CapturedContent,
    CheckRecord,
    RunHead,
    RunRef,
    Scope,
    StepRecord,
    StepResult,
    ToolCallRecord,
)

STEP_COLUMNS = (
    "run_id, step_key, ordinal, status, title, agent_name, model, rule_id, message, attempts,"
    " started_at, ended_at, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
    " cost_micros, error_type, revision"
)
ATTEMPT_COLUMNS = (
    "a.run_id, a.step_key, a.attempt, a.status, a.started_at, a.ended_at, a.model, a.agent_run_id,"
    " a.retry_reason, a.error_type, a.input_tokens, a.output_tokens, a.cache_read_tokens,"
    " a.cache_write_tokens, a.cost_micros"
)
RESULT_COLUMNS = (
    "r.result_id, r.run_id, r.step_key, r.attempt, r.kind, r.output, r.answer_length,"
    " r.answer_ref, r.tool_calls, r.checks, r.created_at"
)
APPROVAL_COLUMNS = (
    "approval_id, run_id, step_key, result_id, requested_at, reasons, decision, reviewer,"
    " reason, decided_at"
)
_OWNED = "tenant_id = ? AND project_id = ?"
_TOTAL_MATCH = (
    "WHERE a.run_id = {s}run_step.run_id AND a.step_key = {s}run_step.step_key"
    " AND a.tenant_id = {s}run_step.tenant_id"
)
TOTALS = (
    "input_tokens = (SELECT COALESCE(SUM(input_tokens), 0) FROM {s}step_attempt a {match}),"
    " output_tokens = (SELECT COALESCE(SUM(output_tokens), 0) FROM {s}step_attempt a {match}),"
    " cache_read_tokens = (SELECT COALESCE(SUM(cache_read_tokens), 0) FROM {s}step_attempt a"
    " {match}),"
    " cache_write_tokens = (SELECT COALESCE(SUM(cache_write_tokens), 0) FROM {s}step_attempt a"
    " {match}),"
    " cost_micros = (SELECT CASE WHEN COUNT(*) > 0 AND COUNT(*) = COUNT(cost_micros)"
    " THEN SUM(cost_micros) END FROM {s}step_attempt a {match})"
).replace("{match}", _TOTAL_MATCH)
"""The token and cost totals of a step, summed from its attempts of the same tenant. The cost is
unknown (NULL) while any attempt has no price."""

HEAD_INSERT = (
    "INSERT INTO {s}run_log_head (run_id, tenant_id, project_id, capture, opened_at)"
    " VALUES (?, ?, ?, ?, ?) ON CONFLICT (run_id) DO NOTHING"
)
HEAD_SELECT = (
    "SELECT run_id, tenant_id, project_id, capture, opened_at FROM {s}run_log_head"
    f" WHERE run_id = ? AND {_OWNED}"
)
LINK_INSERT = (
    "INSERT INTO {s}data_subject_link (tenant_id, project_id, subject_hmac, key_id, ref_kind,"
    " ref_id, run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"
)
STEP_SELECT = (
    f"SELECT {STEP_COLUMNS} FROM {{s}}run_step WHERE run_id = ? AND step_key = ? AND {_OWNED}"
)
STEPS_SELECT = (
    f"SELECT {STEP_COLUMNS} FROM {{s}}run_step WHERE run_id = ? AND {_OWNED} ORDER BY ordinal"
)
STEP_INSERT = (
    "INSERT INTO {s}run_step (run_id, step_key, tenant_id, project_id, ordinal, status, title,"
    " agent_name, model, rule_id, message, started_at, ended_at, error_type, revision)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)"
)
STEP_UPDATE = (
    "UPDATE {s}run_step SET ordinal = ?, status = ?, title = ?, agent_name = ?, model = ?,"
    " rule_id = ?, message = ?, started_at = ?, ended_at = ?, error_type = ?,"
    f" revision = revision + 1 WHERE run_id = ? AND step_key = ? AND {_OWNED} AND revision = ?"
)
ATTEMPT_LAST = (
    "SELECT COALESCE(MAX(attempt), 0) FROM {s}step_attempt WHERE run_id = ? AND step_key = ?"
    f" AND {_OWNED}"
)
ATTEMPT_INSERT = (
    "INSERT INTO {s}step_attempt (run_id, step_key, attempt, tenant_id, project_id, status,"
    " started_at) VALUES (?, ?, ?, ?, ?, 'running', ?)"
)
STEP_STARTED = (
    "UPDATE {s}run_step SET attempts = ?, started_at = COALESCE(started_at, ?)"
    f" WHERE run_id = ? AND step_key = ? AND {_OWNED}"
)
ATTEMPT_UPDATE = (
    "UPDATE {s}step_attempt SET status = ?, ended_at = ?, model = ?, agent_run_id = ?,"
    " retry_reason = ?, error_type = ?, input_tokens = ?, output_tokens = ?,"
    " cache_read_tokens = ?, cache_write_tokens = ?, cost_micros = ?"
    f" WHERE run_id = ? AND step_key = ? AND attempt = ? AND {_OWNED}"
)
STEP_TOTALS = (
    f"UPDATE {{s}}run_step SET {TOTALS}, model = COALESCE(?, model)"
    f" WHERE run_id = ? AND step_key = ? AND {_OWNED}"
)
ATTEMPTS_SELECT = (
    f"SELECT {ATTEMPT_COLUMNS} FROM {{s}}step_attempt a JOIN {{s}}run_step s"
    " ON s.run_id = a.run_id AND s.step_key = a.step_key"
    " WHERE a.run_id = ? AND a.tenant_id = ? AND a.project_id = ?"
    " AND (?{text} IS NULL OR a.step_key = ?) ORDER BY s.ordinal, a.attempt"
)
FINAL_DELETE = (
    "DELETE FROM {s}step_result WHERE run_id = ? AND step_key = ? AND kind = 'final'"
    f" AND {_OWNED}"
)
RESULT_INSERT = (
    "INSERT INTO {s}step_result (result_id, run_id, step_key, attempt, tenant_id, project_id,"
    " kind, output, answer_length, answer_ref, tool_calls, checks, created_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?{jsonb}, ?, ?, ?{jsonb}, ?{jsonb}, ?)"
)
RESULTS_SELECT = (
    f"SELECT {RESULT_COLUMNS} FROM {{s}}step_result r JOIN {{s}}run_step s"
    " ON s.run_id = r.run_id AND s.step_key = r.step_key"
    " WHERE r.run_id = ? AND r.tenant_id = ? AND r.project_id = ?"
    " AND (?{text} IS NULL OR r.step_key = ?) AND (?{text} IS NULL OR r.kind = ?)"
    " ORDER BY s.ordinal, r.attempt, r.created_at, r.result_id"
)
CONTENT_INSERT = (
    "INSERT INTO {s}captured_content (content_ref, run_id, tenant_id, project_id, kind, body,"
    " created_at) VALUES (?, ?, ?, ?, ?, ?{jsonb}, ?)"
)
CONTENT_SELECT = (
    "SELECT content_ref, run_id, kind, body, created_at FROM {s}captured_content"
    f" WHERE content_ref = ? AND {_OWNED}"
)
CONTENT_READ_INSERT = (
    "INSERT INTO {s}captured_content_read (read_id, content_ref, tenant_id, project_id,"
    " reader_hmac, read_at) VALUES (?, ?, ?, ?, ?, {now})"
)
APPROVAL_INSERT = (
    "INSERT INTO {s}approval (approval_id, run_id, step_key, result_id, tenant_id, project_id,"
    " requested_at, reasons) VALUES (?, ?, ?, ?, ?, ?, ?, ?{jsonb})"
)
APPROVAL_UPDATE = (
    "UPDATE {s}approval SET decision = ?, reviewer = ?, reason = ?, decided_at = ?"
    f" WHERE approval_id = ? AND {_OWNED} AND decision IS NULL"
)
APPROVAL_SELECT = f"SELECT {APPROVAL_COLUMNS} FROM {{s}}approval WHERE approval_id = ? AND {_OWNED}"
APPROVALS_SELECT = (
    f"SELECT {APPROVAL_COLUMNS} FROM {{s}}approval WHERE run_id = ? AND {_OWNED}"
    " ORDER BY requested_at, approval_id"
)
SUBJECT_RUNS = (
    f"SELECT DISTINCT run_id FROM {{s}}data_subject_link WHERE {_OWNED} AND subject_hmac = ?"
    " ORDER BY run_id"
)
PROJECT_RUNS = f"SELECT run_id FROM {{s}}run_log_head WHERE {_OWNED} ORDER BY opened_at, run_id"
EXPIRED = (
    "SELECT run_id, tenant_id, project_id, opened_at FROM {s}run_log_head"
    " WHERE opened_at < ? ORDER BY opened_at, run_id LIMIT ?"
)


@cache
def sql(text: str, schema: str | None = None) -> str:
    """A shared statement rendered for SQLite (`schema` is `None`) or for PostgreSQL."""

    if schema is None:
        return (
            text.replace("{s}", "")
            .replace("{jsonb}", "")
            .replace("{text}", "")
            .replace("{now}", "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')")
        )
    return (
        text.replace("{s}", f"{schema}.")
        .replace("{jsonb}", "::jsonb")
        .replace("{text}", "::text")
        .replace("{now}", "now()")
        .replace("?", "%s")
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def when(value: Any) -> datetime | None:
    """A stored time as an aware UTC datetime (SQLite returns text, PostgreSQL datetimes)."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    return _as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))


def stamp(value: datetime | None) -> str | None:
    """A time as SQLite keeps it: ISO-8601 UTC with microseconds and `Z` (sorts as text)."""

    if value is None:
        return None
    return _as_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def loaded(value: Any) -> Any:
    """A JSON column as Python data (SQLite returns text, PostgreSQL decoded values)."""

    if isinstance(value, str | bytes):
        return json.loads(value)
    return value


def dumped(value: Any) -> str:
    """Python data as JSON text for a JSON column; NaN and infinities become text."""

    try:
        return json.dumps(value, default=str, ensure_ascii=False, allow_nan=False)
    except ValueError:
        return json.dumps(_finite(value), default=str, ensure_ascii=False, allow_nan=False)


def _finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_finite(item) for item in value]
    return value


def _scope(scope: Scope) -> tuple[str, str]:
    return scope.tenant, scope.project


def head_params(scope: Scope, run_id: str) -> tuple[Any, ...]:
    """Parameters of `HEAD_SELECT`."""

    return (run_id, *_scope(scope))


def head_insert_params(scope: Scope, run_id: str, now: datetime) -> tuple[Any, ...]:
    """Parameters of `HEAD_INSERT`."""

    return (run_id, *_scope(scope), scope.capture, now)


def link_params(
    scope: Scope, subject: str, key_id: str, ref_kind: str, ref_id: str, run_id: str, now: datetime
) -> tuple[Any, ...]:
    """Parameters of `LINK_INSERT`."""

    return (*_scope(scope), subject, key_id, ref_kind, ref_id, run_id, now)


def step_params(scope: Scope, run_id: str, step_key: str) -> tuple[Any, ...]:
    """Parameters of `STEP_SELECT`, `ATTEMPT_LAST` and the row filters of one step."""

    return (run_id, step_key, *_scope(scope))


def step_insert_params(scope: Scope, step: StepRecord) -> tuple[Any, ...]:
    """Parameters of `STEP_INSERT`."""

    return (step.run_id, step.step_key, *_scope(scope), *_step_values(step))


def step_update_params(scope: Scope, step: StepRecord, expected_revision: int) -> tuple[Any, ...]:
    """Parameters of `STEP_UPDATE`."""

    return (*_step_values(step), step.run_id, step.step_key, *_scope(scope), expected_revision)


def step_started_params(scope: Scope, attempt: AttemptRecord) -> tuple[Any, ...]:
    """Parameters of `STEP_STARTED` for the attempt just opened."""

    return (attempt.attempt, attempt.started_at, attempt.run_id, attempt.step_key, *_scope(scope))


def attempt_insert_params(scope: Scope, attempt: AttemptRecord) -> tuple[Any, ...]:
    """Parameters of `ATTEMPT_INSERT`."""

    return (attempt.run_id, attempt.step_key, attempt.attempt, *_scope(scope), attempt.started_at)


def attempt_update_params(scope: Scope, attempt: AttemptRecord) -> tuple[Any, ...]:
    """Parameters of `ATTEMPT_UPDATE`."""

    return (
        attempt.status,
        attempt.ended_at,
        attempt.model,
        attempt.agent_run_id,
        attempt.retry_reason,
        attempt.error_type,
        attempt.input_tokens,
        attempt.output_tokens,
        attempt.cache_read_tokens,
        attempt.cache_write_tokens,
        attempt.cost_micros,
        attempt.run_id,
        attempt.step_key,
        attempt.attempt,
        *_scope(scope),
    )


def totals_params(scope: Scope, attempt: AttemptRecord) -> tuple[Any, ...]:
    """Parameters of `STEP_TOTALS`."""

    return (attempt.model, attempt.run_id, attempt.step_key, *_scope(scope))


def filtered_params(scope: Scope, run_id: str, *filters: str | None) -> tuple[Any, ...]:
    """Parameters of `ATTEMPTS_SELECT` and `RESULTS_SELECT`: each filter twice (NULL = none)."""

    return (run_id, *_scope(scope), *(value for each in filters for value in (each, each)))


def result_insert_params(scope: Scope, result: StepResult) -> tuple[Any, ...]:
    """Parameters of `RESULT_INSERT`."""

    output = None if result.output is None else dumped(result.output)
    calls = dumped([call.model_dump(mode="json") for call in result.tool_calls])
    checks = dumped([check.model_dump(mode="json") for check in result.checks])
    return (
        result.result_id,
        result.run_id,
        result.step_key,
        result.attempt,
        *_scope(scope),
        result.kind,
        output,
        result.answer_length,
        result.answer_ref,
        calls,
        checks,
        result.created_at,
    )


def final_delete_params(scope: Scope, result: StepResult) -> tuple[Any, ...]:
    """Parameters of `FINAL_DELETE`."""

    return step_params(scope, result.run_id, result.step_key)


def content_insert_params(
    scope: Scope, ref: str, run_id: str, kind: str, body: Any, now: datetime
) -> tuple[Any, ...]:
    """Parameters of `CONTENT_INSERT`."""

    return (ref, run_id, *_scope(scope), kind, dumped(body), now)


def content_params(scope: Scope, content_ref: str) -> tuple[Any, ...]:
    """Parameters of `CONTENT_SELECT`."""

    return (content_ref, *_scope(scope))


def content_read_params(
    scope: Scope, read_id: str, content_ref: str, reader: str | None
) -> tuple[Any, ...]:
    """Parameters of `CONTENT_READ_INSERT`."""

    return (read_id, content_ref, *_scope(scope), reader)


def approval_insert_params(scope: Scope, approval: Approval) -> tuple[Any, ...]:
    """Parameters of `APPROVAL_INSERT`."""

    return (
        approval.approval_id,
        approval.run_id,
        approval.step_key,
        approval.result_id,
        *_scope(scope),
        approval.requested_at,
        dumped(list(approval.reasons)),
    )


def approval_update_params(
    scope: Scope, approval_id: str, decision: str, reviewer: str, reason: str | None, now: datetime
) -> tuple[Any, ...]:
    """Parameters of `APPROVAL_UPDATE`."""

    return (decision, reviewer, reason, now, approval_id, *_scope(scope))


def owned_params(scope: Scope, *keys: str) -> tuple[Any, ...]:
    """The keys followed by the scope's tenant and project (a statement filtered by both)."""

    return (*keys, *_scope(scope))


def subject_params(scope: Scope, subject: str) -> tuple[Any, ...]:
    """Parameters of `SUBJECT_RUNS`."""

    return (*_scope(scope), subject)


def head_of(row: Any) -> RunHead:
    return RunHead(
        run_id=row[0], tenant=row[1], project=row[2], capture=bool(row[3]), opened_at=when(row[4])
    )


def ref_of(row: Any) -> RunRef:
    return RunRef(run_id=row[0], tenant=row[1], project=row[2], opened_at=when(row[3]))


def step_of(row: Any) -> StepRecord:
    return StepRecord(
        run_id=row[0],
        step_key=row[1],
        ordinal=row[2],
        status=row[3],
        title=row[4],
        agent=row[5],
        model=row[6],
        rule_id=row[7],
        message=row[8],
        attempts=row[9],
        started_at=when(row[10]),
        ended_at=when(row[11]),
        input_tokens=row[12],
        output_tokens=row[13],
        cache_read_tokens=row[14],
        cache_write_tokens=row[15],
        cost_micros=row[16],
        error_type=row[17],
        revision=row[18],
    )


def attempt_of(row: Any) -> AttemptRecord:
    return AttemptRecord(
        run_id=row[0],
        step_key=row[1],
        attempt=row[2],
        status=row[3],
        started_at=when(row[4]),
        ended_at=when(row[5]),
        model=row[6],
        agent_run_id=row[7],
        retry_reason=row[8],
        error_type=row[9],
        input_tokens=row[10],
        output_tokens=row[11],
        cache_read_tokens=row[12],
        cache_write_tokens=row[13],
        cost_micros=row[14],
    )


def result_of(row: Any) -> StepResult:
    return StepResult(
        result_id=row[0],
        run_id=row[1],
        step_key=row[2],
        attempt=row[3],
        kind=row[4],
        output=loaded(row[5]) if row[5] is not None else None,
        answer_length=row[6],
        answer_ref=row[7],
        tool_calls=tuple(ToolCallRecord.model_validate(c) for c in loaded(row[8])),
        checks=tuple(CheckRecord.model_validate(c) for c in loaded(row[9])),
        created_at=when(row[10]),
    )


def approval_of(row: Any) -> Approval:
    return Approval(
        approval_id=row[0],
        run_id=row[1],
        step_key=row[2],
        result_id=row[3],
        requested_at=when(row[4]),
        reasons=tuple(loaded(row[5])),
        decision=row[6],
        reviewer=row[7],
        reason=row[8],
        decided_at=when(row[9]),
    )


def content_of(row: Any) -> CapturedContent:
    return CapturedContent(
        content_ref=row[0], run_id=row[1], kind=row[2], body=loaded(row[3]), created_at=when(row[4])
    )


def _step_values(step: StepRecord) -> tuple[Any, ...]:
    """The caller-owned fields of a step, in `save_step` order (the totals are the store's)."""

    return (
        step.ordinal,
        step.status,
        step.title,
        step.agent,
        step.model,
        step.rule_id,
        step.message,
        step.started_at,
        step.ended_at,
        step.error_type,
    )
