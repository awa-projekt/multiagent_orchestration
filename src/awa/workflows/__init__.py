"""Runtime layer: deterministic workflows that run like agents.

`Sequential`, `Parallel`, `Loop` and `Map` compose agents, teams or other workflows with fixed
control flow; each is `AgentLike` (`Workflow` is the base). `ProcessWorkflow` runs checked steps
with retries, run conditions and `SpecialCases`, a rule table that `boundary_cases` and
`case_notes` test without a model.
"""

from awa.workflows._base import Workflow
from awa.workflows._boundaries import BoundaryCase, CaseNote, boundary_cases, case_notes
from awa.workflows._process import ProcessStep, ProcessWorkflow
from awa.workflows._special_cases import (
    CaseAction,
    CaseCondition,
    CaseOutcome,
    CaseRow,
    SpecialCases,
)
from awa.workflows._step_checks import ApprovalCheck, RuleCheck, SchemaCheck
from awa.workflows._step_flow import AfterLastTry, RunCondition, StepRetry
from awa.workflows.checked import Checked, CriterionResult, Verdict
from awa.workflows.loop import Loop
from awa.workflows.map import Map
from awa.workflows.parallel import Parallel
from awa.workflows.ports import StepDocuments
from awa.workflows.sequential import Sequential

__all__ = [
    "AfterLastTry",
    "ApprovalCheck",
    "BoundaryCase",
    "CaseAction",
    "CaseCondition",
    "CaseNote",
    "CaseOutcome",
    "CaseRow",
    "Checked",
    "CriterionResult",
    "Loop",
    "Map",
    "Parallel",
    "ProcessStep",
    "ProcessWorkflow",
    "RuleCheck",
    "RunCondition",
    "SchemaCheck",
    "Sequential",
    "SpecialCases",
    "StepDocuments",
    "StepRetry",
    "Verdict",
    "Workflow",
    "boundary_cases",
    "case_notes",
]
