"""Checks of how a process moves between steps: retries, `after_last_try`, `when` and step special cases.

Errors refuse what could not run or could bypass a decision, e.g. a `go` past a gated step or
a retry of an agent that writes or sends.
"""

from __future__ import annotations

from collections.abc import Sequence

from awa.blueprints._resolve import ToolCatalog
from awa.blueprints._risk import acting_tools
from awa.blueprints._schema_process import (
    ApprovalCheckBlueprint,
    ProcessStepBlueprint,
    RuleCheckBlueprint,
)
from awa.blueprints.errors import BlueprintIssue, Severity
from awa.blueprints.schema import Blueprint
from awa.workflows import CaseAction

_MAX_LISTED = 3


def check_routing(blueprint: Blueprint, catalog: ToolCatalog | None) -> list[BlueprintIssue]:
    """The issues of retries, `after_last_try`, `when` and the special cases of `blueprint`."""

    return _RoutingChecker(blueprint, catalog).run()


class _RoutingChecker:
    def __init__(self, blueprint: Blueprint, catalog: ToolCatalog | None) -> None:
        self._bp = blueprint
        self._catalog = catalog
        self._issues: list[BlueprintIssue] = []
        self._steps: list[ProcessStepBlueprint] = (
            list(blueprint.process.steps) if blueprint.process is not None else []
        )
        self._names = [step.name for step in self._steps]

    def _add(self, severity: Severity, path: str, message: str) -> None:
        self._issues.append(BlueprintIssue(severity, path, message))

    def run(self) -> list[BlueprintIssue]:
        for index, step in enumerate(self._steps):
            self._retry(index, step)
            self._when(index, step)
        for name in self._bp.special_cases:
            self._table(name)
        return self._issues

    def _acting(self, step: ProcessStepBlueprint) -> list[str]:
        return acting_tools(self._bp, step.agent, self._catalog) if step.agent else []

    def _retry(self, index: int, step: ProcessStepBlueprint) -> None:
        path = f"process.steps[{index}]"
        acting = self._acting(step)
        if step.retry is not None and acting:
            self._add(
                "error",
                f"{path}.retry",
                f"Step '{step.name}' retries agent '{step.agent}', which writes or sends with "
                f"{_listing(acting)}: a second try would do it again. Remove the retry, or split "
                "the step so the retried agent only reads and a later step writes.",
            )
        last = step.last_try()
        if last.action != "go" or last.to is None:
            return
        at = f"{path}.after_last_try"
        if acting:
            self._add(
                "error",
                at,
                f"Step '{step.name}' continues at '{last.to}' after its last try, but agent "
                f"'{step.agent}' writes or sends with {_listing(acting)}: a half-done action "
                "would be left behind. Use after_last_try: stop or ask.",
            )
        if self._go(index, last.to, at, f"after_last_try of step '{step.name}'"):
            target = self._steps[self._names.index(last.to)]
            writes = self._acting(target)
            if writes:
                self._add(
                    "error",
                    at,
                    f"Step '{step.name}' continues at '{last.to}' after a failure, and agent "
                    f"'{target.agent}' there writes or sends with {_listing(writes)}: a failed "
                    "step must not lead to an action. Use after_last_try: stop or ask.",
                )

    def _when(self, index: int, step: ProcessStepBlueprint) -> None:
        if step.when is None:
            return
        guarded = [
            check.kind
            for check in step.checks
            if isinstance(check, RuleCheckBlueprint | ApprovalCheckBlueprint)
        ]
        if guarded or step.name in self._bp.special_cases:
            what = ", ".join(dict.fromkeys(guarded)) or "special cases"
            self._add(
                "warning",
                f"process.steps[{index}].when",
                f"When '{step.when}' is false, step '{step.name}' is skipped with its checks "
                f"({what}). Make sure no case that needs them can get past; or remove 'when'.",
            )

    def _table(self, name: str) -> None:
        from awa.workflows import case_notes

        table = self._bp.special_cases[name]
        path = f"special_cases.{name}"
        for warning in table.warnings:
            self._add("warning", f"{path}.else", warning)
        for note in case_notes(table):
            if note.row is not None:
                self._add("warning", f"{path}.cases[{note.row - 1}]", note.text)
        if name not in self._names:
            steps = ", ".join(self._names) if self._names else "the blueprint has no process"
            self._add(
                "error",
                path,
                f"Special cases '{name}' are for a step called '{name}', which the process does "
                f"not have ({steps}). Name the table after the step it decides for.",
            )
            return
        index = self._names.index(name)
        actions: list[tuple[str, CaseAction]] = [
            (f"{path}.cases[{n}]", row) for n, row in enumerate(table.cases)
        ]
        actions.append((f"{path}.else", table.else_))
        for at, action in actions:
            if action.then == "go" and action.to is not None:
                self._go(index, action.to, f"{at}.to", f"special cases of step '{name}'")

    def _go(self, index: int, target: str, path: str, who: str) -> bool:
        """Report a `go` from step `index` that is not forward or jumps over a guarded step;
        `True` when the target is a later step."""

        if target not in self._names:
            self._add(
                "error",
                path,
                f"The {who} continue at '{target}', which is not a step of the process "
                f"(steps: {', '.join(self._names)}).",
            )
            return False
        position = self._names.index(target)
        if position <= index:
            self._add(
                "error",
                path,
                f"The {who} continue at '{target}', which is not later in the process. A 'go' "
                "moves forward only.",
            )
            return False
        guarded = [
            step.name
            for step in self._steps[index + 1 : position]
            if step.name in self._bp.special_cases
            or any(
                isinstance(check, RuleCheckBlueprint | ApprovalCheckBlueprint)
                for check in step.checks
            )
        ]
        if guarded:
            self._add(
                "error",
                path,
                f"The {who} continue at '{target}' and so jump over {_listing(guarded)}, which "
                "has a rule, an approval or special cases that must not be bypassed. Continue at "
                "an earlier step, or move those checks.",
            )
        return True


def _listing(names: Sequence[str]) -> str:
    shown = ", ".join(names[:_MAX_LISTED])
    return shown if len(names) <= _MAX_LISTED else f"{shown} and {len(names) - _MAX_LISTED} more"
