"""Default data-endpoint backends over `awa.blueprints` (imported on use)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from awa._internal.logging import get_logger
from awa.contracts.usage import merge_limits
from awa.server._config import ServedSystem
from awa.server.errors import BuildDesignInvalidError
from awa.server.ports import BuildEngine, BuildOutcome, BuildSpec, Emit, Question

_log = get_logger("server.data")


def _plain(value: Any, name: str, default: Any = None) -> Any:
    """`name` of a pydantic model, dataclass or mapping, whichever `value` is."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _questions(found: Any) -> tuple[Question, ...]:
    """The questions of `BuildReport.question` (a `BuildQuestions`: `id`, `text`, `options`,
    `evidence` each)."""

    items = _plain(found, "questions", ()) if found is not None else ()
    return tuple(
        Question(
            id=str(_plain(item, "id")),
            text=str(_plain(item, "text")),
            options=tuple(str(option) for option in _plain(item, "options", ()) or ()),
            evidence=tuple(str(ref) for ref in _plain(item, "evidence", ()) or ()),
        )
        for item in items
    )


def _failures(report: Any) -> list[Any]:
    evaluation = report.eval_report
    return list(evaluation.failures()) if evaluation is not None else []


class BlueprintsEngine(BuildEngine):
    """Runs `awa.blueprints.build_project` for a `BuildSpec`.

    The design sees the uploaded files only (`environment=None`), so server configuration never
    leaks into a tenant's design. Progress steps become events (`build.progress` by default).
    """

    async def __call__(self, spec: BuildSpec, emit: Emit) -> BuildOutcome:
        from awa.blueprints import build_project
        from awa.contracts.usage import UsageLimits

        def on_progress(step: Any) -> None:
            event = _plain(step, "event", "")
            if event:
                emit(str(event), dict(_plain(step, "data", {}) or {}))
                return
            emit(
                "build.progress",
                {
                    "stage": _plain(step, "stage"),
                    "round": _plain(step, "round", 0),
                    "message": _plain(step, "message", ""),
                    "pass_rate": _plain(step, "pass_rate"),
                },
            )

        report = await build_project(
            spec.description,
            out_dir=spec.out_dir,
            model=spec.model,
            max_rounds=spec.rounds,
            target_pass_rate=spec.target,
            usage_limits=UsageLimits(max_requests=spec.max_requests),
            on_progress=on_progress,
            environment=None,
            guidance=spec.guidance,
            force=True,
            data=[spec.data],
            notes=dict(spec.notes),
            mask=spec.mask,
            answers=dict(spec.answers) or None,
        )
        found = getattr(report, "question", None)
        if found is not None:
            questions = _questions(found)
            if questions and _plain(found, "status", "question") != "blocked":
                return BuildOutcome(questions=questions)
            reason = _plain(found, "reason", "") or "the files do not fit the description"
            raise BuildDesignInvalidError(
                f"Nothing can be built from these files: {reason}", details={"issues": []}
            )
        usage = report.usage
        return BuildOutcome(
            passed=report.passed,
            pass_rate=report.pass_rate,
            rounds=report.rounds,
            stopped=str(report.stopped),
            target=report.target_pass_rate,
            # the first line only: the others name the project folder of this machine
            summary=report.summary().splitlines()[0],
            flaky=tuple(report.flaky),
            failing=tuple(dict.fromkeys(case.case_id for case in _failures(report))),
            assumptions=tuple(str(item) for item in getattr(report.design, "assumptions", ())),
            usage={"requests": usage.requests, "total_tokens": usage.total_tokens},
        )


def blueprint_system(project: Path) -> ServedSystem:
    """The system of a built project (`blueprint.yaml`, `tools.py`) working on its own `data/`.

    The blueprint may not import other code; paused runs are kept in memory.
    """

    from awa._internal.upstream.checkpoint import StateInMemorySaver
    from awa.blueprints import Blueprint
    from awa.blueprints._project_files import on_workspace
    from awa.blueprints._resolve import load_tools, skill_registry, tools_files

    path = project / "blueprint.yaml"
    spec = on_workspace(Blueprint.from_yaml(path), project)
    approvals = bool(spec.needs_checkpointer())
    if approvals:
        _log.warning(
            "Approvals of runs of %s are kept in memory and are lost when the server restarts",
            spec.name,
        )
    system = spec.build(
        tools=load_tools(tools_files([], path)),
        skill_registry=skill_registry([], path),
        checkpointer=StateInMemorySaver() if approvals else None,
    )
    return ServedSystem(
        system, usage_limits=merge_limits(None, spec.usage_limits()), diagram=spec.to_mermaid()
    )
