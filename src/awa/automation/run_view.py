"""The ordered steps of a process run, as `awa runs show --steps` returns them."""

from __future__ import annotations

from typing import Any

from awa.automation.models import ProcessRun
from awa.persistence.runlog import RunLog, Scope


def run_scope(run: ProcessRun, *, principal: str | None = None) -> Scope:
    """The run-log scope of a process run: its tenant (`"local"` without one) and automation."""

    return Scope(run.tenant or "local", run.automation, principal=principal)


async def run_steps(log: RunLog, scope: Scope, run_id: str) -> dict[str, Any]:
    """The run's steps in order, with their attempts, results and approvals, as JSON data.

    Captured content is referenced (`answer_ref`, `arguments_ref`, `result_ref`), never inlined.

    Returns:
        `{"run_id": ..., "recorded": bool, "steps": [...]}`; `recorded` is `False` when the run log
        has no record of the run.
    """

    head = await log.head(scope, run_id)
    steps = await log.steps(scope, run_id)
    attempts = await log.attempts(scope, run_id)
    results = await log.results(scope, run_id)
    approvals = await log.approvals(scope, run_id)
    shown: list[dict[str, Any]] = []
    for step in steps:
        entry = step.model_dump(mode="json", exclude={"run_id", "revision"})
        entry["attempts"] = []
        for attempt in (a for a in attempts if a.step_key == step.step_key):
            data = attempt.model_dump(mode="json", exclude={"run_id", "step_key"})
            data["results"] = [
                r.model_dump(mode="json", exclude={"run_id", "step_key", "attempt"})
                for r in results
                if r.step_key == step.step_key and r.attempt == attempt.attempt
            ]
            entry["attempts"].append(data)
        entry["approvals"] = [
            a.model_dump(mode="json", exclude={"run_id"})
            for a in approvals
            if a.step_key == step.step_key
        ]
        shown.append(entry)
    return {
        "run_id": run_id,
        "recorded": head is not None,
        "capture": bool(head and head.capture),
        "steps": shown,
    }


__all__ = ["run_scope", "run_steps"]
