"""The run log of process runs: the protocol, its records and its SQLite and PostgreSQL backends."""

from __future__ import annotations

from awa.persistence.runlog.postgres import PostgresRunLog
from awa.persistence.runlog.protocol import (
    AUDITOR_ROLE,
    Approval,
    ApprovalDecision,
    AttemptRecord,
    AttemptStatus,
    CapturedContent,
    CheckRecord,
    ContentKind,
    PseudonymKey,
    ResultKind,
    RunHead,
    RunLog,
    RunRef,
    Scope,
    StepRecord,
    StepResult,
    StepState,
    SystemScope,
    ToolCallRecord,
)
from awa.persistence.runlog.sqlite import SqliteRunLog
from awa.persistence.sql import SqliteDatabase

__all__ = [
    "AUDITOR_ROLE",
    "Approval",
    "ApprovalDecision",
    "AttemptRecord",
    "AttemptStatus",
    "CapturedContent",
    "CheckRecord",
    "ContentKind",
    "PostgresRunLog",
    "PseudonymKey",
    "ResultKind",
    "RunHead",
    "RunLog",
    "RunRef",
    "Scope",
    "SqliteDatabase",
    "SqliteRunLog",
    "StepRecord",
    "StepResult",
    "StepState",
    "SystemScope",
    "ToolCallRecord",
]
