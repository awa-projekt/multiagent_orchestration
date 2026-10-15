"""A dry run of a `process:` blueprint: the path a run would take, without a model.

The real workflow runs with stand-in agents that answer given values; checks, special cases,
`when`, retries and `after_last_try` decide as in a real run. No document is read, no tool
called and no wait waited, so the same values always give the same path. It does not prove
what a model would answer or what a tool approval inside an agent would do.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from awa.blueprints._build_process import build_process
from awa.blueprints.errors import BlueprintError
from awa.blueprints.schema import Blueprint
from awa.contracts.cards import AGENT_NAME_PATTERN, AgentCard
from awa.contracts.events import RunEvent, StepEvent
from awa.contracts.protocols import AgentInput
from awa.contracts.results import RunResult
from awa.errors import ModelProviderError, StepCheckFailedError, StepRejectedError

if TYPE_CHECKING:
    from awa._internal.upstream.checkpoint import BaseCheckpointSaver

DryRunOutcome = Literal["completed", "rejected", "failed"]
"""How a dry run ended: the process went through, a special case or a person rejected the
item, or a step failed (a check, a `when` that cannot be decided, or the last try)."""

_MAX_PAUSES = 50
_DRY = "dry run"


@dataclass(frozen=True)
class DryRunLine:
    """One thing that happened on the path: the step, its status, one sentence and the try."""

    step: str
    status: str
    text: str
    attempt: int | None = None


@dataclass
class DryRun:
    """The path of a dry run.

    Attributes:
        message: Why it failed or was rejected; empty when it went through.
        waits: The waits between tries, in seconds, as a real run would wait them.
        notes: What the dry run did not cover (tool approvals, documents).
    """

    lines: list[DryRunLine] = field(default_factory=list)
    outcome: DryRunOutcome = "completed"
    message: str = ""
    pauses: int = 0
    waits: list[float] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class _StandIn:
    """An agent that answers the values of its step, failing the first `failures` tries."""

    def __init__(self, name: str, output: Any, failures: int) -> None:
        self._name = name
        self._output = output
        self.failures = failures
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def card(self) -> AgentCard:
        return AgentCard(name=self._name, description=f"Stand-in for {self._name} ({_DRY}).")

    async def run(self, input: AgentInput, **_: Any) -> RunResult[Any]:
        self.calls += 1
        if self.calls <= self.failures:
            raise ModelProviderError(f"simulated failure of try {self.calls} ({_DRY})")
        output = self._output
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        return RunResult(
            run_id=f"dry-{self._name}-{self.calls}", agent=self._name, output=output, text=text
        )


async def dry_run(
    blueprint: Blueprint,
    values: Mapping[str, Any] | None = None,
    *,
    checkpointer: BaseCheckpointSaver[Any],
    fail: Mapping[str, int] | None = None,
    decide: Literal["approve", "reject"] = "approve",
) -> DryRun:
    """Walk the process of `blueprint` with `values` and return the path a run would take.

    Every step answers its output schema's fields with sample values, then `values`; a key of
    `values` that names a step and holds a mapping applies to that step only. The request is
    the remaining `values` as JSON.

    Args:
        checkpointer: Keeps the walk's state while it pauses, e.g. `StateInMemorySaver()`.
        fail: Step name to the number of tries that fail with an error (from the first).
        decide: The answer to every question to a person.

    Raises:
        BlueprintError: If the blueprint has no process, a step of `fail` does not exist, or
            the process cannot be built.
    """

    from awa.runtime import use_global_subscriber

    values, fail = dict(values or {}), dict(fail or {})
    process = blueprint.process
    if process is None:
        raise BlueprintError(f"Blueprint {blueprint.name!r} has no process to walk")
    names = [step.name for step in process.steps]
    unknown = sorted(set(fail) - set(names))
    if unknown:
        raise BlueprintError(
            f"--fail names {', '.join(unknown)}, which the process does not have "
            f"(steps: {', '.join(names)})"
        )
    per_step = {k: v for k, v in values.items() if k in names and isinstance(v, Mapping)}
    shared = {k: v for k, v in values.items() if k not in per_step}
    by_step: dict[str, _StandIn] = {}
    for step in process.steps:
        spec = next((a for a in blueprint.agents if a.name == step.agent), None)
        sample = _sample(spec.output_schema) if spec and spec.output_schema else None
        if isinstance(sample, dict) or sample is None:
            output: Any = {**(sample or {}), **shared, **per_step.get(step.name, {})}
            if sample is None and not output:
                output = f"answer of {step.agent} ({_DRY})"
        else:
            output = sample
        by_step[step.name] = _StandIn(step.agent, output, fail.get(step.name, 0))
    path = DryRun()

    async def wait(seconds: float) -> None:
        path.waits.append(seconds)

    workflow = build_process(
        blueprint,
        process.steps,
        member=lambda step: by_step[step.name],
        base=None,
        name=f"{blueprint.name}-dry-run"[:48] if _valid(blueprint.name) else "dry-run",
        description="",
        checkpointer=checkpointer,
        documents=False,
        sleep=wait,
        rand=lambda: 0.5,  # no spread: the same waits every time
    )
    events: list[RunEvent] = []
    request = json.dumps(shared, ensure_ascii=False) if shared else "dry run"
    with use_global_subscriber(events.append):
        try:
            result = await workflow.run(request)
            while result.is_interrupted and path.pauses < _MAX_PAUSES:
                path.pauses += 1
                assert result.thread_id is not None
                result = await workflow.resume(result.thread_id, {"action": decide, "note": _DRY})
        except StepRejectedError as exc:
            path.outcome, path.message = "rejected", str(exc)
        except (StepCheckFailedError, ModelProviderError) as exc:
            path.outcome, path.message = "failed", str(exc)
    path.lines = _lines(blueprint, events, decide, path.waits)
    path.notes = _notes(blueprint)
    return path


def _lines(
    blueprint: Blueprint, events: Sequence[RunEvent], decide: str, waits: Sequence[float]
) -> list[DryRunLine]:
    lines: list[DryRunLine] = []
    pending_waits = list(waits)
    for event in events:
        if not isinstance(event, StepEvent) or event.depth:
            continue
        words = _case_words(blueprint, event.step, event.case) if event.case else ""
        case = f" Special case {words}." if words else ""
        tried = f" (try {event.attempt})" if event.attempt is not None else ""
        message = event.message.rstrip(". ")
        if event.status == "checked":
            text = f"passed its checks{tried}.{case}"
        elif event.status == "retrying":
            delay = pending_waits.pop(0) if pending_waits else 0.0
            text = f"{message}. A real run waits {delay:g} s, then tries again."
        elif event.status == "skipped":
            text = event.message
        elif event.status == "needs_approval":
            text = f"PAUSE: a person is asked: {message}. The dry run answers {decide}."
        elif event.status == "rejected" and words:
            text = f"REJECTED by special case {words}."
        elif event.status in ("failed", "rejected"):
            text = f"{event.status.upper()}{tried}: {message}."
        else:
            continue
        lines.append(DryRunLine(event.step, event.status, " ".join(text.split()), event.attempt))
    return lines


def _case_words(blueprint: Blueprint, step: str, case: str | None) -> str:
    """`row 2 (If amount is ...: approved ...)` for the case id `<step>.case.2`."""

    table = blueprint.special_cases.get(step)
    if table is None or case is None:
        return case or ""
    part = case.removeprefix(f"{step}.case.").split(".")
    lines = table.describe()
    if part[0] == "else":
        return f"else ({lines[len(table.cases)]})"
    row = int(part[0])
    if len(part) > 1:
        return f"row {row} was unclear ({lines[len(table.cases) + 1]})"
    return f"row {row} ({lines[row - 1]})"


def _notes(blueprint: Blueprint) -> list[str]:
    notes = ["No model was asked, no tool was called and no document was read."]
    gated = blueprint.approval_agents()
    process = blueprint.process
    assert process is not None
    for step in process.steps:
        if step.agent in gated:
            notes.append(
                f"Step {step.name}: agent {step.agent} uses a tool that waits for approval; a real "
                "run may pause there too."
            )
    return notes


def _sample(schema: Mapping[str, Any]) -> Any:
    """A value that fits `schema`: every property filled, the first allowed word, 1, true."""

    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        return {
            str(name): _sample(sub) for name, sub in properties.items() if isinstance(sub, Mapping)
        }
    if kind == "array":
        return []
    if kind in ("number", "integer"):
        return 1
    if kind == "boolean":
        return True
    if kind == "null":
        return None
    return "sample"


def _valid(name: str) -> bool:
    return bool(AGENT_NAME_PATTERN.fullmatch(f"{name}-dry-run"[:48]))
