"""Run a built project on its own files: one run per input file, results only in `data/output/`.

Every run works on a fresh copy of `data/`; its outputs are copied back below
`data/output/<input name>/` and its report goes to `runs/<id>.json`. Writes wait for a
person: `decide` answers each approval, without it a run that asks ends `paused`.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from awa._internal.ids import new_id as new_uuid_id
from awa._internal.upstream.checkpoint import StateInMemorySaver
from awa._internal.upstream.models import BaseChatModel
from awa.blueprints import _project_files as project_files
from awa.blueprints._build_loop import model_registry
from awa.blueprints._data_files import system_clock
from awa.blueprints.errors import BlueprintError
from awa.blueprints.ports import RunFiles
from awa.blueprints.schema import Blueprint, ToolSource
from awa.contracts.events import ToolCallEvent
from awa.contracts.usage import Usage, UsageLimits
from awa.data import DataError
from awa.errors import UsageLimitExceededError
from awa.evals.trace import output_text, requested_approvals
from awa.runtime.context import RunContext, use_run_context
from awa.skills import SkillRegistry

Decide = Callable[[dict[str, Any]], Mapping[str, Any] | None]
"""Answers one pending approval (`{"id", "value"}`) with `{"action": "approve"}` or
`{"action": "reject", "note": ...}`; `None` leaves it open and the run `paused`."""

_MAX_PAUSES = 50
"""Approvals one run may ask for before it is left paused (a run that keeps asking)."""
_MAX_ERROR = 400


@dataclass(frozen=True)
class RunOutcome:
    """What one run on one input file did.

    Attributes:
        status: `completed`, `paused` (an approval was left open) or `failed`.
        approvals: The tools the run asked a person about, in order.
        outputs: The files it wrote, copied to `data/output/<input name>/`.
        report: The report file (`runs/<run_id>.json`, relative to the project).
    """

    file: str
    status: Literal["completed", "paused", "failed"]
    answer: str = ""
    error: str = ""
    approvals: tuple[str, ...] = ()
    tool_calls: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    run_id: str = ""
    report: str = ""
    usage: Usage = field(default_factory=Usage)

    def to_dict(self) -> dict[str, Any]:
        """The outcome as JSON data."""

        return {
            "file": self.file,
            "status": self.status,
            "answer": self.answer,
            "error": self.error,
            "approvals": list(self.approvals),
            "tool_calls": list(self.tool_calls),
            "outputs": list(self.outputs),
            "run_id": self.run_id,
            "report": self.report,
            "usage": {"requests": self.usage.requests, "total_tokens": self.usage.total_tokens},
        }


async def run_project(
    blueprint: Blueprint,
    files: RunFiles,
    *,
    select: Sequence[str] | None = None,
    tools: ToolSource | None = None,
    skills: SkillRegistry | None = None,
    model: str | BaseChatModel | None = None,
    usage_limits: UsageLimits | None = None,
    decide: Decide | None = None,
    allow_imports: bool = False,
    clock: Callable[[], datetime] = system_clock,
    new_id: Callable[[str], str] = new_uuid_id,
    new_checkpointer: Callable[[], Any] = StateInMemorySaver,
    on_outcome: Callable[[RunOutcome], None] | None = None,
) -> list[RunOutcome]:
    """Run the process once for each input file of `blueprint`'s `data:` section; a failure does not stop the others.

    Args:
        files: The project's files (`LocalRunFiles` for a folder).
        select: Input file names or globs (matched against the path below `data/files/` and its
            last part); default every input file.
        model: Overrides the models of the blueprint.
        usage_limits: The limits for each run; without `max_requests` a run may make
            `default_requests(blueprint)` model calls.
        decide: Answers each approval; without it a run that asks ends `paused`.
        new_checkpointer: Makes the checkpointer of a run that needs approval (default: one in
            memory per run).
        on_outcome: Called after each run.

    Raises:
        BlueprintError: If the blueprint names no input file.
        DataError: If `select` matches nothing.

    Example:
        ```python
        outcomes = await run_project(blueprint, LocalRunFiles("invoices"), select=["*.pdf"])
        ```
    """

    inputs = _inputs(blueprint, files)
    if not inputs:
        raise BlueprintError(
            "the blueprint's data: section names no input file; build the project with --data "
            "or add a `data:` entry with role input"
        )
    chosen = _select(inputs, select)
    limits = usage_limits or UsageLimits()
    if limits.max_requests is None:
        limits = limits.model_copy(update={"max_requests": default_requests(blueprint)})
    gated = bool(blueprint.needs_checkpointer())
    registry = skills if skills is not None else SkillRegistry.builtin()
    outcomes = []
    for name, (request, handling) in chosen.items():
        text = request.replace("{file}", name) if request else f"Process {name}"
        text = f"{text}\n\nWhat must happen with it: {handling}" if handling else text
        outcome = await _run_one(
            blueprint, files, name, text, tools, registry, model, limits, decide,
            new_checkpointer if gated else None, allow_imports, clock, new_id,
        )  # fmt: skip
        outcomes.append(outcome)
        if on_outcome is not None:
            on_outcome(outcome)
    return outcomes


def default_requests(blueprint: Blueprint) -> int:
    """The model calls one run may make without a limit: the `max_steps` of every agent added up."""

    return sum(spec.max_steps or blueprint.defaults.max_steps for spec in blueprint.agents)


def unlabelled_inputs(blueprint: Blueprint, files: RunFiles) -> list[str]:
    """The input files of a run on every file when more than one exists and no `data:` entry is a `reference`.

    Such a lookup table would otherwise run (and cost model calls) like an input.
    """

    if any(entry.role == "reference" for entry in blueprint.data):
        return []
    names = list(_inputs(blueprint, files))
    return names if len(names) > 1 else []


def _inputs(blueprint: Blueprint, files: RunFiles) -> dict[str, tuple[str, str]]:
    """The input files of the `data:` entries with their request template and handling text."""

    found: dict[str, tuple[str, str]] = {}
    for entry in blueprint.data:
        if entry.role == "input":
            for name in files.inputs(entry.path):
                found.setdefault(name, (entry.request or "", entry.handling))
    return found


def _select(
    inputs: dict[str, tuple[str, str]], select: Sequence[str] | None
) -> dict[str, tuple[str, str]]:
    if select is None:
        return inputs
    chosen: dict[str, tuple[str, str]] = {}
    for pattern in select:
        wanted = pattern.replace("\\", "/").lower()
        hits = [
            name
            for name in inputs
            if fnmatch.fnmatchcase(name.lower(), wanted)
            or fnmatch.fnmatchcase(name.lower().rsplit("/", 1)[-1], wanted)
        ]
        if not hits:
            raise DataError(
                f"no input file matches {pattern!r}; the input files are {list(inputs)}"
            )
        chosen.update((name, inputs[name]) for name in hits)
    return {name: chosen[name] for name in inputs if name in chosen}


def _file_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:_MAX_ERROR]}"


async def _run_one(
    blueprint: Blueprint,
    files: RunFiles,
    name: str,
    request: str,
    tools: ToolSource | None,
    skills: SkillRegistry,
    model: str | BaseChatModel | None,
    limits: UsageLimits | None,
    decide: Decide | None,
    new_checkpointer: Callable[[], Any] | None,
    allow_imports: bool,
    clock: Callable[[], datetime],
    new_id: Callable[[str], str],
) -> RunOutcome:
    run_id = new_id("run")
    started = clock()
    root: RunContext[None] = RunContext(
        run_id=run_id, agent="awa_data_run", deps=None, limits=limits
    )
    calls: list[str] = []
    root.events.subscribe(
        lambda e: calls.append(e.tool_name) if isinstance(e, ToolCallEvent) else None
    )
    segments: list[Any] = []
    applied: list[str] = []
    status: Literal["completed", "paused", "failed"] = "completed"
    answer = error = ""
    outputs: list[str] = []
    try:
        with files.workspace() as workspace:
            try:
                system = await project_files.on_workspace(blueprint, workspace).abuild(
                    tools=tools,
                    skill_registry=skills,
                    model_registry=model_registry(model),
                    checkpointer=new_checkpointer() if new_checkpointer else None,
                    allow_imports=allow_imports,
                )
                with use_run_context(root):
                    result = await system.run(request)
                    segments.append(result)
                    resume = getattr(system, "resume", None)
                    pauses = 0
                    while result.is_interrupted and decide and resume and result.thread_id:
                        pauses += 1
                        if pauses > _MAX_PAUSES:
                            error = f"stopped after {_MAX_PAUSES} approvals in one run"
                            break  # a run that keeps asking stays paused instead of looping
                        answers = {
                            p["id"]: d for p in result.interrupts if (d := decide(p)) is not None
                        }
                        if len(answers) < len(result.interrupts):
                            break  # an approval was left open: the run stays paused
                        applied += [str(d.get("action")) for d in answers.values()]
                        one = next(iter(answers.values())) if len(answers) == 1 else answers
                        result = await resume(result.thread_id, one)
                        segments.append(result)
                if result.is_interrupted:
                    status = "paused"
                else:
                    answer = output_text(result.output)
            except Exception as exc:  # noqa: BLE001 - one failed run must not stop the others
                status = "failed"
                error = _file_error(exc)
                if isinstance(exc, UsageLimitExceededError):
                    error = (
                        f"the usage limit was reached: {str(exc)[:_MAX_ERROR]}; raise it with "
                        "--max-requests or `limits:` in blueprint.yaml"
                    )
            outputs = files.collect(workspace, name)
    except (OSError, DataError) as exc:
        status, error = "failed", error or _file_error(exc)
    try:
        sha256 = files.digest(name)
    except (OSError, DataError) as exc:
        sha256 = ""
        status, error = "failed", error or _file_error(exc)
    approvals = requested_approvals(segments)
    usage = root.usage.snapshot()
    report = {
        "run_id": run_id,
        "file": name,
        "sha256": sha256,
        "request": request,
        "status": status,
        "started": started.isoformat(),
        "finished": clock().isoformat(),
        "answer": answer,
        "error": error,
        "tool_calls": calls,
        "approvals": approvals,
        "decisions": applied,
        "outputs": outputs,
        "usage": {"requests": usage.requests, "total_tokens": usage.total_tokens},
    }
    try:
        report_file = files.record(run_id, report)
    except OSError as exc:
        report_file = ""
        status, error = "failed", error or _file_error(exc)
    return RunOutcome(
        file=name,
        status=status,
        answer=answer,
        error=error,
        approvals=tuple(approvals),
        tool_calls=tuple(calls),
        outputs=tuple(outputs),
        run_id=run_id,
        report=report_file,
        usage=usage,
    )
