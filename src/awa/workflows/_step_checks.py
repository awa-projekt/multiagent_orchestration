"""Pure checks of one process step: the data of a check, how a result is read, and the verdict.

Expressions read the top-level fields of the step's output plus `text`, the answer text. A rule
says when the step has FAILED and fails closed: an unevaluable rule fails the step.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from awa.contracts.results import RunResult
from awa.tools.approval import ApprovalCondition

CheckKind = Literal["documents", "answer", "schema", "rule", "approval"]
"""What a check looks at: `documents` and `answer` always run; the others the user adds."""

_MAX_VALUE_CHARS = 80
_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class CheckResult:
    """The verdict of one check, as plain data.

    Attributes:
        id: The check's id (`"<step>.<kind>.<n>"` unless the user named it).
        message: Why it did not pass (what is wrong and what to do); empty when it passed.
    """

    id: str
    kind: CheckKind
    passed: bool
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """The result as JSON-compatible data."""

        return {"id": self.id, "kind": self.kind, "passed": self.passed, "message": self.message}


@dataclass(frozen=True)
class SchemaCheck:
    """The output must have the right fields.

    Attributes:
        problems: Returns what is wrong with the output (as data), one plain sentence each; empty
            when it matches. Built by the caller from the JSON schema.
        fields: The fields the schema names, for the message.
    """

    id: str
    problems: Callable[[Any], list[str]]
    fields: tuple[str, ...] = ()
    title: str = ""


@dataclass(frozen=True)
class RuleCheck:
    """The step fails when `fail_when` is true or cannot be evaluated.

    Attributes:
        fail_when: Expression over the output fields and `text`.
        message: What is wrong when the rule fires; shown to the person.
    """

    id: str
    fail_when: str
    message: str
    title: str = ""

    def __post_init__(self) -> None:
        ApprovalCondition(self.fail_when)  # a malformed expression is refused at build time


@dataclass(frozen=True)
class ApprovalCheck:
    """A person decides on the step's result before the next step starts.

    Attributes:
        when: Expression over the output fields and `text` saying when to ask; `None` always. An
            unevaluable expression asks.
        by: Role allowed to decide (recorded and shown; not enforced here).
    """

    id: str
    when: str | None = None
    reason: str = ""
    by: str | None = None
    title: str = ""

    def __post_init__(self) -> None:
        if self.when is not None:
            ApprovalCondition(self.when)


StepCheck = SchemaCheck | RuleCheck | ApprovalCheck
"""A check the user adds to a step."""


@dataclass(frozen=True)
class ApprovalRequest:
    """What a person is asked about a step's result, one entry per applying approval check."""

    reasons: tuple[str, ...]
    by: tuple[str, ...]
    check_ids: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    """What the checks of a step found.

    Attributes:
        approval: What to ask a person, when every other check passed and an approval applies.
    """

    results: tuple[CheckResult, ...] = field(default_factory=tuple)
    approval: ApprovalRequest | None = None

    @property
    def passed(self) -> bool:
        """Whether every check that ran passed."""

        return all(result.passed for result in self.results)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        """The results that did not pass."""

        return tuple(result for result in self.results if not result.passed)


def output_values(result: RunResult[Any]) -> dict[str, Any]:
    """The names an expression can read: the output's top-level fields, plus `text`.

    The output is the structured output, or a JSON object in the answer text (whole text or first
    fenced block). A field called `text` wins over the answer text.
    """

    data = output_data(result)
    values: dict[str, Any] = {"text": result.text}
    if isinstance(data, Mapping):
        values.update({str(key): value for key, value in data.items()})
    return values


def output_data(result: RunResult[Any]) -> Any:
    """The output as plain data (a model as a dict, else the JSON in the text), or `None`."""

    output = result.output
    if isinstance(output, BaseModel):
        return output.model_dump(mode="python")
    if isinstance(output, Mapping):
        return dict(output)
    if output is not None and not isinstance(output, str):
        return output
    return json_in_text(output if isinstance(output, str) else result.text)


def json_in_text(text: str) -> Any:
    """The JSON object in `text` (the whole text, or the first fenced block), else `None`."""

    candidates = [text.strip()]
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    for candidate in candidates:
        if candidate.startswith("{"):
            try:
                loaded = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(loaded, dict):
                return loaded
    return None


def answer_check(step: str, agent: str, result: RunResult[Any]) -> CheckResult:
    """The implicit check that the agent gave an answer: not empty text and not empty output."""

    output = result.output
    no_output = output is None or (isinstance(output, str) and not output.strip())
    empty = not result.text.strip() and no_output
    return CheckResult(
        f"{step}.answer",
        "answer",
        not empty,
        (
            f"The agent '{agent}' returned nothing. Open the trace to see what it did, then "
            "run this step again."
            if empty
            else ""
        ),
    )


def documents_check(step: str, problems: Sequence[str]) -> CheckResult:
    """The implicit check that the step's documents are there; `problems` from `StepDocuments`."""

    return CheckResult(f"{step}.documents", "documents", not problems, " ".join(problems))


def run_checks(checks: Sequence[StepCheck], result: RunResult[Any]) -> Verdict:
    """Run `schema` and `rule` checks, then `approval` only when they all passed."""

    values = output_values(result)
    data = output_data(result)
    results: list[CheckResult] = []
    for check in checks:
        if isinstance(check, SchemaCheck):
            results.append(_schema_result(check, data))
        elif isinstance(check, RuleCheck):
            results.append(_rule_result(check, values))
    if not all(r.passed for r in results):
        return Verdict(tuple(results))
    asks = [c for c in checks if isinstance(c, ApprovalCheck) and _applies(c, values)]
    if not asks:
        return Verdict(tuple(results))
    request = ApprovalRequest(
        reasons=tuple(c.reason for c in asks if c.reason),
        by=tuple(dict.fromkeys(c.by for c in asks if c.by)),
        check_ids=tuple(c.id for c in asks),
    )
    return Verdict(tuple(results), request)


def approval_result(request: ApprovalRequest, *, approved: bool, note: str) -> CheckResult:
    """The result of the approval check after a person decided."""

    who = f" by {', '.join(request.by)}" if request.by else ""
    return CheckResult(
        request.check_ids[0], "approval", approved, "" if approved else f"Rejected{who}: {note}"
    )


def _schema_result(check: SchemaCheck, data: Any) -> CheckResult:
    if data is None:
        fields = f" ({', '.join(check.fields)})" if check.fields else ""
        return CheckResult(
            check.id,
            "schema",
            False,
            f"The answer has no output fields{fields}. Open the trace, then run this step again.",
        )
    problems = check.problems(data)
    return CheckResult(check.id, "schema", not problems, "; ".join(problems))


def _rule_result(check: RuleCheck, values: Mapping[str, Any]) -> CheckResult:
    condition = ApprovalCondition(check.fail_when)
    if not condition(values):
        return CheckResult(check.id, "rule", True)
    missing = sorted(name for name in condition.names if name not in values)
    shown = ", ".join(f"{name} = {_show(values.get(name))}" for name in sorted(condition.names))
    note = f" The output has no field {', '.join(repr(m) for m in missing)}." if missing else ""
    return CheckResult(check.id, "rule", False, f"{check.message}{note} Values: {shown}.")


def _applies(check: ApprovalCheck, values: Mapping[str, Any]) -> bool:
    if check.when is None:
        return True
    return ApprovalCondition(check.when)(values)


def _show(value: Any) -> str:
    text = "empty" if value is None else str(value)
    return text if len(text) <= _MAX_VALUE_CHARS else text[: _MAX_VALUE_CHARS - 3] + "..."
