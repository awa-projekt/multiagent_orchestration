"""Build the `process:` section of a blueprint into a `ProcessWorkflow`."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa.blueprints._process_documents import ProjectDocuments, reader_hint_for
from awa.blueprints._schema_process import (
    ApprovalCheckBlueprint,
    ProcessStepBlueprint,
    RuleCheckBlueprint,
    SchemaCheckBlueprint,
    check_id,
)
from awa.blueprints.errors import BlueprintError
from awa.blueprints.json_schema import model_from_json_schema
from awa.blueprints.schema import Blueprint
from awa.contracts.protocols import AgentLike
from awa.errors import ConfigurationError
from awa.runtime.cache import Cache
from awa.workflows import ApprovalCheck, ProcessStep, ProcessWorkflow, RuleCheck, SchemaCheck
from awa.workflows._step_checks import StepCheck


def build_process(
    blueprint: Blueprint,
    steps: Sequence[ProcessStepBlueprint],
    *,
    member: Callable[[ProcessStepBlueprint], AgentLike],
    base: Path | None,
    name: str,
    description: str,
    checkpointer: BaseCheckpointSaver[Any] | None,
    documents: bool = True,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    rand: Callable[[], float] | None = None,
    cache: Cache | None = None,
) -> ProcessWorkflow:
    """The `ProcessWorkflow` of `steps`, with their retries, `after_last_try`, `when` and special cases.

    Args:
        member: Gives the agent (or team, network) that runs a step.
        base: Folder the documents' paths are relative to.
        documents: Whether the steps get their documents (a dry run leaves them out).
        sleep: Waits between tries; a dry run passes one that does not wait.
        rand: Uniform values in `[0, 1)` for the retry jitter; a dry run passes a fixed one.
        cache: The system's caches (a step's documents check is reused while unchanged).

    Raises:
        BlueprintError: If the workflow cannot be built.
    """

    assert blueprint.process is not None
    built = [_step(blueprint, step, member(step), base if documents else False) for step in steps]
    options: dict[str, Any] = {"sleep": sleep} if sleep is not None else {}
    if rand is not None:
        options["rand"] = rand
    try:
        return ProcessWorkflow(
            name,
            built,
            description=description,
            carry=blueprint.process.carry,
            checkpointer=checkpointer,
            cache=cache,
            **options,
        )
    except (ValueError, ConfigurationError) as exc:
        raise BlueprintError(f"Cannot build process {name!r}: {exc}") from exc


def _step(
    blueprint: Blueprint,
    step: ProcessStepBlueprint,
    agent: AgentLike,
    base: Path | None | Literal[False],
) -> ProcessStep:
    """The workflow step of `step`; `base=False` leaves its documents out."""

    spec = next((a for a in blueprint.agents if a.name == step.agent), None)
    names = list(dict.fromkeys([*(spec.documents if spec else []), *step.documents]))
    if base is False:
        names = []
    documents = (
        ProjectDocuments(
            blueprint,
            names,
            base=base or None,
            reader_hint=reader_hint_for(spec.tools if spec else []),
        )
        if names
        else None
    )
    return ProcessStep(
        step.name,
        agent,
        title=step.title,
        documents=documents,
        checks=tuple(_checks(blueprint, step)),
        retry=step.retry.step_retry() if step.retry is not None else None,
        after_last_try=step.last_try(),
        when=step.run_condition(),
        cases=blueprint.special_cases.get(step.name),
    )


def _checks(blueprint: Blueprint, step: ProcessStepBlueprint) -> list[StepCheck]:
    spec = next((a for a in blueprint.agents if a.name == step.agent), None)
    checks: list[StepCheck] = []
    for index, check in enumerate(step.checks):
        identifier = check_id(step, index)
        if isinstance(check, SchemaCheckBlueprint):
            schema = check.schema_ or (spec.output_schema if spec else None)
            if schema is None:
                raise BlueprintError(
                    f"Step {step.name!r} checks the output fields, but agent {step.agent!r} has "
                    "no output fields; add output fields to the agent, or remove the check"
                )
            checks.append(_schema_check(identifier, schema, check.title))
        elif isinstance(check, RuleCheckBlueprint):
            checks.append(RuleCheck(identifier, check.fail_when, check.message, title=check.title))
        elif isinstance(check, ApprovalCheckBlueprint):
            checks.append(
                ApprovalCheck(identifier, check.when, check.reason, check.by, title=check.title)
            )
    return checks


def _schema_check(identifier: str, schema: dict[str, Any], title: str) -> SchemaCheck:
    model = model_from_json_schema(schema, name="step_output")
    properties = schema.get("properties")
    fields = tuple(str(name) for name in properties) if isinstance(properties, dict) else ()

    def problems(data: Any) -> list[str]:
        return schema_problems(model, data)

    return SchemaCheck(identifier, problems, fields, title=title)


def schema_problems(model: type[BaseModel], data: Any) -> list[str]:
    """What is wrong with `data` for `model` as sentences (`missing: a, b`, then each wrong value); empty when it fits."""

    try:
        model.model_validate(data)
    except ValidationError as exc:
        missing: list[str] = []
        wrong: list[str] = []
        for error in exc.errors(include_input=False, include_url=False):
            where = ".".join(str(part) for part in error["loc"]) or "output"
            if error["type"] == "missing":
                missing.append(where)
            else:
                wrong.append(f"wrong value: {where} ({error['msg']})")
        return ([f"missing: {', '.join(missing)}"] if missing else []) + wrong
    return []
