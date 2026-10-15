"""Checks of the `process:` section and of `agents[].documents` (see `Blueprint.check`).

Whether a tool reads files is a heuristic on its reference, so those findings are warnings,
except the one about hidden personal data.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from awa.blueprints._schema_process import (
    ProcessStepBlueprint,
    RuleCheckBlueprint,
    SchemaCheckBlueprint,
    check_id,
)
from awa.blueprints.errors import BlueprintIssue, Severity
from awa.blueprints.schema import Blueprint
from awa.contracts.cards import AGENT_NAME_PATTERN
from awa.errors import ConfigurationError
from awa.tools.approval import ApprovalCondition

if TYPE_CHECKING:
    from awa.blueprints._schema_agent import AgentBlueprint
    from awa.blueprints._schema_data import DataBlueprint
    from awa.data import DataProfile

MAX_COMFORTABLE_STEPS = 12
"""More steps than this in a row draw a warning (every step costs time and model calls)."""

_READ_WORD = re.compile(r"(?:^|[._:/-])(?:read|list|query|search|fetch|load)", re.IGNORECASE)
_FILE_WORD = re.compile(r"pdf|csv|xlsx|docx|file|document|data|knowledge", re.IGNORECASE)
_BACKTICKED_NAME = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{1,40})`")
_SUGGESTED_READER = {
    "pdf": "connector:files.read_pdf",
    "csv": "connector:files.read_csv",
    "tsv": "connector:files.read_csv",
    "xlsx": "connector:files.read_xlsx",
    "xls": "connector:files.read_xlsx",
    "docx": "connector:files.read_docx",
}


def check_process(
    blueprint: Blueprint, *, structure_names: Sequence[str] = (), data: DataProfile | None = None
) -> list[BlueprintIssue]:
    """The issues of the `process:` section and of the agents' `documents`.

    Args:
        structure_names: Names of the team and network, which a step may name instead of an agent.
        data: The profile of the data files, to find a document whose file is missing.
    """

    return _ProcessChecker(blueprint, structure_names, data).run()


def reads_files(reference: str) -> bool:
    """Whether a `builtin:read_file`, `connector:files.*` or reading-and-file-word reference reads files."""

    if reference.startswith(("mcp:", "plugin:")):
        return False
    if reference.startswith(("connector:files", "builtin:read_file", "builtin:list_files")):
        return True
    return bool(_READ_WORD.search(reference)) and bool(_FILE_WORD.search(reference))


def reader_tool(tools: Sequence[str]) -> str | None:
    """The first tool reference of `tools` that `reads_files`, if any."""

    return next((reference for reference in tools if reads_files(reference)), None)


class _ProcessChecker:
    def __init__(
        self, blueprint: Blueprint, structure_names: Sequence[str], data: DataProfile | None
    ) -> None:
        self._bp = blueprint
        self._structures = list(structure_names)
        self._profile = data
        self._issues: list[BlueprintIssue] = []
        self._documents = {entry.name: entry for entry in blueprint.data}
        self._agents = {spec.name: spec for spec in blueprint.agents}

    def _add(self, severity: Severity, path: str, message: str) -> None:
        self._issues.append(BlueprintIssue(severity, path, message))

    def run(self) -> list[BlueprintIssue]:
        self._agent_documents()
        process = self._bp.process
        if process is None:
            return self._issues
        if self._bp.workflow is not None:
            self._add(
                "error",
                "process",
                "This blueprint has both a 'process' and a 'workflow'. Keep only 'process' "
                "(the Process page writes it), or delete the 'process' to keep the workflow.",
            )
        if not process.steps:
            self._add(
                "error",
                "process.steps",
                "A process needs at least one step. Add a step and choose its agent.",
            )
            return self._issues
        self._step_names()
        for index, step in enumerate(process.steps):
            self._step(index, step)
        self._whole_process()
        return self._issues

    def _whole_process(self) -> None:
        process = self._bp.process
        assert process is not None
        if self._bp.limits is None and len(process.steps) >= 2:
            self._add(
                "warning",
                "limits",
                "This process has no limits. Add limits (for example max_requests and "
                "max_duration) so a run cannot use more than you expect.",
            )
        if len(process.steps) > MAX_COMFORTABLE_STEPS:
            self._add(
                "warning",
                "process.steps",
                f"{len(process.steps)} steps in a row: each step costs time and model calls, "
                "and an error in an early step reaches all later ones. Consider splitting the "
                "process.",
            )
        if process.carry == "previous":
            self._carried_fields()

    def _carried_fields(self) -> None:
        """A field the agent's instructions name in backticks that an earlier step returns but
        the previous step does not: with `carry: previous` it never reaches the agent."""

        process = self._bp.process
        assert process is not None
        for index in range(1, len(process.steps)):
            step, previous = process.steps[index], process.steps[index - 1]
            spec = self._agents.get(step.agent)
            carried = _fields(self._agents.get(previous.agent))
            if spec is None or not spec.instructions or carried is None:
                continue
            earlier = [
                _fields(self._agents.get(s.agent)) or set() for s in process.steps[: index - 1]
            ]
            missing = sorted(
                {
                    name
                    for name in _BACKTICKED_NAME.findall(spec.instructions)
                    if name not in carried and any(name in fields for fields in earlier)
                }
            )
            if missing:
                self._add(
                    "warning",
                    f"process.steps[{index}]",
                    f"Step '{step.name}' needs the fields {', '.join(missing)}, but step "
                    f"'{previous.name}' does not return them. Add them to the output fields "
                    f"of '{previous.agent}', or use carry: all.",
                )

    def _step_names(self) -> None:
        process = self._bp.process
        assert process is not None
        seen: set[str] = set()
        ids: set[str] = set()
        for index, step in enumerate(process.steps):
            path = f"process.steps[{index}].name"
            if not AGENT_NAME_PATTERN.fullmatch(step.name):
                self._add(
                    "error",
                    path,
                    f"Step name '{step.name}' is not allowed: use letters, digits, '_' or '-', "
                    "starting with a letter, up to 48 characters.",
                )
            elif step.name in seen:
                self._add(
                    "error",
                    path,
                    f"Two steps are called '{step.name}'. Give one of them another name.",
                )
            seen.add(step.name)
            for position in range(len(step.checks)):
                identifier = check_id(step, position)
                if identifier in ids:
                    self._add(
                        "error",
                        f"process.steps[{index}].checks[{position}].id",
                        f"Two checks have the id '{identifier}'. Give one of them another id, "
                        "or remove the id so awa numbers it.",
                    )
                ids.add(identifier)

    def _step(self, index: int, step: ProcessStepBlueprint) -> None:
        path = f"process.steps[{index}]"
        spec = self._step_agent(index, step)
        for position, document in enumerate(step.documents):
            at = f"{path}.documents[{position}]"
            self._document(document, at, f"Step '{step.name}'", step, spec)
            if step.documents.index(document) != position:
                self._add(
                    "warning",
                    at,
                    f"Document '{document}' is listed twice in step '{step.name}'. List it once.",
                )
            elif spec is not None and document in spec.documents:
                self._add(
                    "warning",
                    at,
                    f"Document '{document}' is attached twice to '{spec.name}' in step "
                    f"'{step.name}'. Keep it on the agent or on the step.",
                )
        self._checks(path, step, spec)

    def _step_agent(self, index: int, step: ProcessStepBlueprint) -> AgentBlueprint | None:
        path = f"process.steps[{index}].agent"
        if not step.agent:
            self._add(
                "error",
                path,
                f"Step {index + 1} has no agent yet. Choose an agent for it, or remove the step.",
            )
            return None
        spec = self._agents.get(step.agent)
        if spec is None and step.agent not in self._structures:
            known = [*self._agents, *self._structures]
            close = difflib.get_close_matches(step.agent, known, n=1)
            hint = f" Did you mean '{close[0]}'?" if close else ""
            self._add(
                "error",
                path,
                f"Step '{step.name}' uses the agent '{step.agent}', which does not exist."
                f"{hint} Choose an existing agent or create it first.",
            )
        return spec

    def _checks(self, path: str, step: ProcessStepBlueprint, spec: AgentBlueprint | None) -> None:
        if not step.checks:
            self._add(
                "warning",
                path,
                f"Step '{step.name}' has only the automatic checks (documents there, answer not "
                "empty). Add a rule or an approval if its result matters.",
            )
        for position, check in enumerate(step.checks):
            at = f"{path}.checks[{position}]"
            if isinstance(check, SchemaCheckBlueprint):
                if check.schema_ is None and spec is not None and spec.output_schema is None:
                    self._add(
                        "error",
                        at,
                        f"Step '{step.name}' checks the output fields, but agent "
                        f"'{spec.name}' has no output fields. Add output fields to the agent, "
                        "or remove this check.",
                    )
            elif isinstance(check, RuleCheckBlueprint):
                if not check.message.strip():
                    self._add(
                        "error",
                        f"{at}.message",
                        f"Say in one sentence what is wrong when the rule of step "
                        f"'{step.name}' fires. The person reading the result sees this sentence.",
                    )
                subject = f"The rule of step '{step.name}'"
                self._expression(check.fail_when, f"{at}.fail_when", subject, spec)
            else:
                if check.when is not None:
                    subject = f"The approval condition of step '{step.name}'"
                    self._expression(check.when, f"{at}.when", subject, spec)

    def _expression(
        self, expression: str, path: str, subject: str, spec: AgentBlueprint | None
    ) -> None:
        try:
            parsed = ApprovalCondition(expression)
        except ConfigurationError as exc:
            reason = str(exc).split(": ", 1)[-1]
            self._add(
                "error",
                path,
                f"{subject} is not valid: {reason}. Write it like: total <= 0 or empty(supplier).",
            )
            return
        if spec is None:
            return
        fields = _fields(spec)
        allowed = {"text"} | (fields or set())
        unknown = parsed.unknown_names(allowed)
        if not unknown:
            return
        if spec.output_schema is None:
            self._add(
                "error",
                path,
                f"{subject} reads '{unknown[0]}', but this agent returns plain text, so an "
                "expression can only read 'text', for example contains(text, 'REJECT'). Add "
                "output fields to the agent to read more.",
            )
        elif fields is not None:
            self._add(
                "error",
                path,
                f"{subject} reads '{unknown[0]}', which is not an output field of agent "
                f"'{spec.name}'. Fields you can use: {', '.join(sorted(allowed))}.",
            )

    def _agent_documents(self) -> None:
        for index, spec in enumerate(self._bp.agents):
            for position, document in enumerate(spec.documents):
                path = f"agents[{index}].documents[{position}]"
                self._document(document, path, f"Agent '{spec.name}'", None, spec)
                if spec.documents.index(document) != position:
                    self._add(
                        "warning",
                        path,
                        f"Document '{document}' is listed twice for agent '{spec.name}'. "
                        "List it once.",
                    )

    def _document(
        self,
        name: str,
        path: str,
        owner: str,
        step: ProcessStepBlueprint | None,
        spec: AgentBlueprint | None,
    ) -> None:
        entry = self._documents.get(name)
        if entry is None:
            names = ", ".join(self._documents) or "none yet"
            self._add(
                "error",
                path,
                f"{owner} lists the document '{name}', which is not declared. Attach one of: "
                f"{names}, or add the file on the Data page first.",
            )
            return
        if entry.role == "output":
            self._add(
                "warning",
                path,
                f"Document '{name}' is a result file but is attached as something the agent "
                "reads. Change its use to Input or Knowledge.",
            )
        if self._profile is not None and not self._in_profile(entry):
            self._add(
                "warning",
                path,
                f"Document '{name}' points to {entry.path}, which is not in the project. Drop "
                "the file on the Data page again or take the document off this step.",
            )
        if spec is None:
            return
        tool = reader_tool(spec.tools)
        if tool is not None and entry.mask in ("placeholders", "drop"):
            self._add(
                "error",
                path,
                f"Document '{name}' is set to hide personal data, but the tool '{tool}' of "
                f"agent '{spec.name}' reads the original. The hidden version covers only the "
                "summary the model gets, not what a tool reads from the file. Remove the tool, "
                "or set the document to show everything and say so in the step.",
            )
        elif tool is None and not _has_unjudged_tool(spec):
            suffix = entry.path.rsplit(".", 1)[-1].lower()
            suggested = _SUGGESTED_READER.get(suffix, "builtin:read_file")
            who = (
                f"Step '{step.name}' gives '{spec.name}'"
                if step is not None
                else f"Agent '{spec.name}' has"
            )
            self._add(
                "warning",
                path,
                f"{who} the document '{name}' ({suffix}), but the agent has no tool to read "
                f"it. Add the tool '{suggested}' to the agent.",
            )

    def _in_profile(self, entry: DataBlueprint) -> bool:
        from awa.data._validate import path_matches

        assert self._profile is not None
        if entry.role == "output":
            return True
        return any(path_matches(entry.path, file.path) for file in self._profile.files)


def _fields(spec: AgentBlueprint | None) -> set[str] | None:
    """The top-level output fields of the agent, or `None` when it declares none."""

    if spec is None or spec.output_schema is None:
        return None
    properties: Any = spec.output_schema.get("properties")
    if not isinstance(properties, dict):
        return None
    return {str(name) for name in properties}


def _has_unjudged_tool(spec: AgentBlueprint) -> bool:
    """Whether the agent has a tool a reference alone does not describe (MCP, plugin, import
    reference): it might read the file, so no warning is given."""

    return any(
        reference.startswith(("mcp:", "plugin:"))
        or (":" in reference and not reference.startswith(("builtin:", "connector:")))
        for reference in spec.tools
    )
