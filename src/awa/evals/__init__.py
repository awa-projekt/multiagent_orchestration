"""Compiler layer: evaluation, to measure a multi-agent system before and after a change.

`evaluate` runs a system on a `Dataset` of `EvalCase`s and scores every run with deterministic
scorers or a `ModelJudge`; the `EvalReport` renders as Markdown or JSON and `compare`s with a
baseline.
"""

from awa.errors import EvalError
from awa.evals.dataset import DECISION_ACTIONS, Dataset, EvalCase, EvalInput
from awa.evals.judge import JudgeVerdict, ModelJudge
from awa.evals.report import (
    CaseCaching,
    CaseResult,
    CaseStatus,
    Comparison,
    EvalReport,
    ScoreChange,
    ScorerDelta,
    ScorerSummary,
)
from awa.evals.runner import IsolatedRun, SystemFactory, evaluate, evaluate_sync, isolated_run
from awa.evals.scorers import (
    ApprovalDecided,
    ApprovalPresent,
    ApprovalRequested,
    Contains,
    Custom,
    CustomFunction,
    CustomOutcome,
    DelegatedTo,
    ExactMatch,
    HandedOffTo,
    JsonFieldsMatch,
    MaxLatency,
    MaxTokens,
    MaxToolCalls,
    NoApprovalRequested,
    RegexMatch,
    RequiresApproval,
    Score,
    Scorer,
    ToolArgsMatch,
    ToolNotCalled,
    ToolWasCalled,
    VerdictIs,
)
from awa.evals.specs import parse_scorer
from awa.evals.trace import RunTrace

__all__ = [
    "DECISION_ACTIONS",
    "ApprovalDecided",
    "ApprovalPresent",
    "ApprovalRequested",
    "CaseCaching",
    "CaseResult",
    "CaseStatus",
    "Comparison",
    "Contains",
    "Custom",
    "CustomFunction",
    "CustomOutcome",
    "Dataset",
    "DelegatedTo",
    "EvalCase",
    "EvalError",
    "EvalInput",
    "EvalReport",
    "ExactMatch",
    "HandedOffTo",
    "IsolatedRun",
    "JsonFieldsMatch",
    "JudgeVerdict",
    "MaxLatency",
    "MaxTokens",
    "MaxToolCalls",
    "ModelJudge",
    "NoApprovalRequested",
    "RegexMatch",
    "RequiresApproval",
    "RunTrace",
    "Score",
    "ScoreChange",
    "Scorer",
    "ScorerDelta",
    "ScorerSummary",
    "SystemFactory",
    "ToolArgsMatch",
    "ToolNotCalled",
    "ToolWasCalled",
    "VerdictIs",
    "evaluate",
    "evaluate_sync",
    "isolated_run",
    "parse_scorer",
]
