"""Lint of agent specs and tool cards: the rules `awa validate` adds to `Blueprint.check`.

The rule codes are listed in docs/develop/agent-spec.md. The `generated` profile is strict;
`authored` (the default) turns E01, the A-rules and the profile-dependent T-rules into
warnings, so a hand-written blueprint is not refused for its layout. Free-text `instructions`
get `info: unstructured` and only the rules not about the section layout.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

from awa._internal.upstream.tools import BaseTool
from awa.blueprints._agent_facts import agent_steps, renders_sections, system_message
from awa.blueprints._resolve import ToolCatalog
from awa.blueprints._risk import agent_tool_risks
from awa.blueprints._schema_agent import AgentBlueprint
from awa.blueprints._schema_process import ApprovalCheckBlueprint
from awa.blueprints._schema_spec import SECTION_NAMES
from awa.blueprints._system_message import RenderedMessage
from awa.blueprints._tool_cards import card_of, lint_card
from awa.blueprints.schema import Blueprint

LintSeverity = Literal["error", "warning", "info"]
Profile = Literal["generated", "authored"]

MAX_CHARS = 8000
"""Longest system message (characters) before E10; move longer material into a skill."""

WORKER_WARN_CHARS = 4000
"""Length above which a worker's system message gets W15."""

ORCHESTRATOR_WARN_CHARS = 6000
"""Length above which an orchestrator's system message gets W15."""

MAX_SKILLS = 3
"""Most skills an agent should have (A04)."""

MAX_TOOLS_WARN = 10
"""Tool count above which an agent gets a warning (tool search is for larger sets)."""

_AUTHORED_SOFT = frozenset({"E01", "A02", "A03", "A04"})


@dataclass(frozen=True)
class LintFinding:
    """One lint finding: rule `code`, `severity` (`error`, `warning`, `info`), `path` and a fix `message`."""

    code: str
    severity: LintSeverity
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity}: {self.path}: {self.code} {self.message}"

    def to_dict(self) -> dict[str, str]:
        """The finding as JSON-compatible data."""

        return {
            "code": self.code,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
        }


_APPROVAL = re.compile(
    r"\b(?:for|after|needs?|requires?|waits?\s+for|awaits?|asks?\s+for|request(?:s|ing)?)\s+"
    r"(?:(?:an?|the|its|their|human|explicit|prior|[\w-]+'s)\s+){0,3}approval\b",
    re.IGNORECASE,
)
"""An approval the agent's own work waits for ("for the manager's approval", "after
approval"), not one somebody else gives later ("before payment approval")."""
_VARIABLE = re.compile(r"\{\{\s*[A-Za-z_][\w.]*\s*\}\}|\{[A-Za-z_][\w.]*\}|\$\{[A-Za-z_]\w*\}")
_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"ghp_[A-Za-z0-9]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}|"
    r"(?i:(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*[^\s`'\"]{8,}))"
)
_BACKTICK = re.compile(r"`([^`\n]{1,80})`")
_DOCUMENT_NAMED = re.compile(
    r"\bdocuments?\s+`([^`\n]+)`|`([^`\n]+)`\s+(?:document|file)\b", re.IGNORECASE
)
_TABLE_LINE = re.compile(r"^\s*(\|.*\||(?:[^,;\t\n]*[,;\t]){2,}[^,;\t\n]*)\s*$")


@dataclass(frozen=True)
class _Text:
    path: str
    section: str
    text: str


class _Linter:
    def __init__(
        self,
        blueprint: Blueprint,
        *,
        profile: Profile,
        tools: ToolCatalog | None,
        skills: Collection[str],
    ) -> None:
        self._bp = blueprint
        self._profile = profile
        self._catalog = tools
        self._skills = set(skills)
        self._documents = {entry.name for entry in blueprint.data}
        self._known_tools = self._all_tool_names()
        self.findings: list[LintFinding] = []

    def _all_tool_names(self) -> set[str]:
        from awa.toolkits import catalog

        names: set[str] = set()
        for entry in catalog().entries():
            names.update({entry.name, entry.reference})
        for spec in self._bp.agents:
            for reference in spec.tools:
                names.update({reference, _plain(reference)})
        if self._catalog is not None:
            names.update(self._catalog.tools)
        return names

    def _add(
        self, spec: AgentBlueprint | None, code: str, severity: LintSeverity, path: str, text: str
    ) -> None:
        if (
            severity == "error"
            and spec is not None
            and code in _AUTHORED_SOFT
            and self._profile == "authored"
        ):
            severity = "warning"
        self.findings.append(LintFinding(code, severity, path, text))

    def run(self) -> list[LintFinding]:
        for index, spec in enumerate(self._bp.agents):
            path = f"agents[{index}]"
            self._picks(spec, path)
            self._tools(spec, path)
            if renders_sections(spec):
                self._structured(spec, path)
            elif spec.instructions.strip():
                self.findings.append(
                    LintFinding(
                        "unstructured",
                        "info",
                        f"{path}.instructions",
                        "free-text instructions; sections (goal, context, rules with reasons, "
                        "output) can be linted in full",
                    )
                )
                self._free_text(spec, path)
        self._cards()
        self._steps()
        return self.findings

    def _picks(self, spec: AgentBlueprint, path: str) -> None:
        if len(spec.skills) > MAX_SKILLS:
            self._add(
                spec,
                "A04",
                "warning",
                f"{path}.skills",
                f"{len(spec.skills)} skills; more than {MAX_SKILLS} makes the right one harder "
                "to pick, keep the closest ones",
            )
        if len(spec.tools) > MAX_TOOLS_WARN:
            self._add(
                spec,
                "W15",
                "warning",
                f"{path}.tools",
                f"{len(spec.tools)} tools; above {MAX_TOOLS_WARN} the model picks less "
                "reliably, split the step or keep fewer tools",
            )

    def _tools(self, spec: AgentBlueprint, path: str) -> None:
        touched = self._profile == "generated"
        risks = agent_tool_risks(self._bp, spec, self._catalog)
        self._approval_gap(spec, path, any(gated for _r, _risk, gated in risks))
        for reference, risk, gated in risks:
            acting = bool(risk.external_action)
            if spec.role in ("judge", "reviewer") and acting:
                self._add(
                    spec,
                    "T21",
                    "error",
                    f"{path}.tools",
                    f"{spec.role} {spec.name!r} has {reference}, which acts outside; a {spec.role}"
                    " only reads, so remove it (a pin does not change this)",
                )
                continue
            if not acting or gated:
                continue
            if not touched:
                acknowledged = reference in spec.acknowledge_ungated
                self._add(
                    spec,
                    "T14",
                    "info" if acknowledged else "warning",
                    f"{path}.tools",
                    f"{reference} acts outside with no approval"
                    + (
                        " (acknowledged in acknowledge_ungated)"
                        if acknowledged
                        else f"; add an approval for {reference} under approvals:, or list it "
                        "under acknowledge_ungated if it runs without a person on purpose"
                    ),
                )
                continue
            self._add(
                spec,
                "T14",
                "error",
                f"{path}.tools",
                f"agent {spec.name!r} can run {reference}, which acts outside, with no "
                f"approval; add an approval for {reference} under approvals:",
            )

    def _approval_gap(self, spec: AgentBlueprint, path: str, any_gated: bool) -> None:
        """T22: the text promises an approval that nothing in the system asks for."""

        if any_gated or spec.role in ("judge", "reviewer") or "approval-requests" in spec.skills:
            return
        if any(
            isinstance(check, ApprovalCheckBlueprint)
            for step in agent_steps(self._bp, spec.name)
            for check in step.checks
        ):
            return
        text = " ".join([spec.instructions, *(item.text for item in self._texts(spec, path))])
        if not _APPROVAL.search(text):
            return
        self._add(
            spec,
            "T22",
            "error" if self._profile == "generated" else "warning",
            f"{path}.tools",
            f"agent {spec.name!r} speaks of an approval, but none of its tools waits for one; "
            "add the acting tool to approvals: (the call then waits for a person by itself), or "
            "remove the approval from the text",
        )

    def _texts(self, spec: AgentBlueprint, path: str) -> list[_Text]:
        texts: list[_Text] = []
        if spec.goal:
            texts.append(_Text(f"{path}.goal", "goal", spec.goal))
        system = spec.system
        if system is None:
            return texts
        for name in SECTION_NAMES:
            value = system.section(name)
            if isinstance(value, str) and value.strip():
                texts.append(_Text(f"{path}.system.{name}", name, value))
            elif isinstance(value, list):
                for position, item in enumerate(value):
                    if isinstance(item, str):
                        texts.append(_Text(f"{path}.system.{name}[{position}]", name, item))
                    else:
                        joined = f"{item.input}\n{item.output}\n{item.why}"
                        texts.append(_Text(f"{path}.system.{name}[{position}]", name, joined))
        return texts

    def _structured(self, spec: AgentBlueprint, path: str) -> None:
        message = system_message(self._bp, spec, self._catalog)
        texts = self._texts(spec, path)
        self._layout(spec, path, message)
        self._names(spec, texts)
        self._safety(spec, path, [*texts, *_spec_text(spec, path)], message.text)

    def _free_text(self, spec: AgentBlueprint, path: str) -> None:
        text = spec.instructions
        texts = [_Text(f"{path}.instructions", "instructions", text)]
        self._safety(spec, path, texts, text)

    def _layout(self, spec: AgentBlueprint, path: str, message: RenderedMessage) -> None:
        if not (spec.goal.strip() or spec.description.strip()):
            self._add(
                spec,
                "E01",
                "error",
                f"{path}.goal",
                "no goal; add one sentence on what it achieves",
            )
        if (
            message.section("output") is None
            and not spec.output_schema
            and spec.role != "orchestrator"
        ):
            self._add(
                spec,
                "E01",
                "error",
                f"{path}.system.output",
                "no output section; describe the answer under system.output or give an "
                "output_schema",
            )
        acting = [
            ref
            for ref, risk, _gated in agent_tool_risks(self._bp, spec, self._catalog)
            if risk.external_action
        ]
        if acting and message.section("special_cases") is None:
            self._add(
                spec,
                "E01",
                "error",
                f"{path}.escalation",
                f"the agent acts outside ({', '.join(acting)}) but says nowhere when to "
                "escalate; add escalation: {to: person, when: [...]}",
            )
        if spec.role == "orchestrator" and not (
            spec.system is not None and spec.system.delegation_brief.strip()
        ):
            self._add(
                spec,
                "W13",
                "warning",
                f"{path}.system.delegation_brief",
                "an orchestrator without a delegation brief; say what every brief to a member "
                "states (objective, output, sources, boundaries)",
            )
        length = len(message.text)
        limit = ORCHESTRATOR_WARN_CHARS if spec.role == "orchestrator" else WORKER_WARN_CHARS
        if length > limit and length <= MAX_CHARS:
            self._add(
                spec,
                "W15",
                "warning",
                path,
                f"the system message has {length:,} characters (above {limit:,}); move "
                "reference material into a skill",
            )

    def _names(self, spec: AgentBlueprint, texts: Sequence[_Text]) -> None:
        own_tools = set(spec.tools) | {_plain(ref) for ref in spec.tools}
        if self._catalog is not None:
            own_tools |= {tool.name for ref in spec.tools if (tool := self._catalog.tools.get(ref))}
        reachable = {
            *spec.documents,
            *(doc for step in agent_steps(self._bp, spec.name) for doc in step.documents),
        }
        for item in texts:
            for name in _BACKTICK.findall(item.text):
                name = name.strip()
                if name in self._known_tools and name not in own_tools:
                    self._add(
                        spec,
                        "E02",
                        "error",
                        item.path,
                        f"the text names the tool `{name}`, which this agent does not have; "
                        "add the tool or change the text",
                    )
                elif name in self._skills and name not in spec.skills:
                    self._add(
                        spec,
                        "E03",
                        "error",
                        item.path,
                        f"the text names the skill `{name}`, which this agent does not have; "
                        "add the skill or change the text",
                    )
            for match in _DOCUMENT_NAMED.finditer(item.text):
                name = (match.group(1) or match.group(2) or "").strip()
                if name in self._known_tools or name in self._skills:
                    continue
                if name not in self._documents:
                    self._add(
                        spec,
                        "E05",
                        "error",
                        item.path,
                        f"the text names the document `{name}`, which is not declared under "
                        "data:; declare it or change the text",
                    )
                elif name not in reachable:
                    self._add(
                        spec,
                        "A02",
                        "error",
                        item.path,
                        f"the text names the document `{name}`, but no step of this agent "
                        "gets it; add it to the step's documents",
                    )
            if _VARIABLE.search(item.text):
                self._add(
                    spec,
                    "E04",
                    "error",
                    item.path,
                    "an unrendered placeholder ({...}); write the value or remove it",
                )

    def _safety(
        self, spec: AgentBlueprint, path: str, texts: Sequence[_Text], rendered: str
    ) -> None:
        for item in texts:
            if _SECRET.search(item.text):
                self._add(
                    spec,
                    "E11",
                    "error",
                    item.path,
                    "something shaped like a secret (key, token or password); remove it and "
                    "read it from the environment instead",
                )
            if "<<<DATA" in item.text or _table_lines(item.text) > 15:
                self._add(
                    spec,
                    "E06",
                    "error",
                    item.path,
                    "data pasted into the instructions; attach the file as a document of "
                    "the step instead",
                )
        if len(rendered) > MAX_CHARS:
            self._add(
                spec,
                "E10",
                "error",
                path,
                f"the system message has {len(rendered):,} characters (more than "
                f"{MAX_CHARS:,}); move reference material into a skill",
            )

    def _cards(self) -> None:
        if self._catalog is None:
            return
        used = {reference for spec in self._bp.agents for reference in spec.tools}
        strict = self._profile == "generated"
        for name, tool in sorted(self._catalog.tools.items()):
            if name not in used or not isinstance(tool, BaseTool):
                continue
            for finding in lint_card(card_of(tool)):
                severity: LintSeverity = finding.severity
                if severity == "error" and finding.code in ("T03", "T05") and not strict:
                    severity = "warning"  # a hand-written blueprint only gets a warning
                self.findings.append(
                    LintFinding(finding.code, severity, f"tools.{name}", finding.message)
                )

    def _steps(self) -> None:
        process = self._bp.process
        if process is None:
            return
        steps = process.steps
        for index, step in enumerate(steps[:-1]):
            later = steps[index + 1]
            reads = later.when is not None or later.name in self._bp.special_cases
            spec = next((a for a in self._bp.agents if a.name == step.agent), None)
            if spec is None or spec.output_schema is not None or not reads:
                continue
            self._add(
                spec,
                "A03",
                "warning",
                f"process.steps[{index}]",
                f"step '{later.name}' decides on fields, but '{step.name}' has no "
                f"output_schema; give {spec.name!r} an output_schema",
            )


def _spec_text(spec: AgentBlueprint, path: str) -> list[_Text]:
    texts = [_Text(f"{path}.description", "description", spec.description)]
    if spec.instructions:
        texts.append(_Text(f"{path}.instructions", "instructions", spec.instructions))
    return texts


def _table_lines(text: str) -> int:
    longest = run = 0
    for line in text.splitlines():
        run = run + 1 if _TABLE_LINE.match(line) else 0
        longest = max(longest, run)
    return longest


def _plain(reference: str) -> str:
    name = reference.removeprefix("builtin:")
    if ":" in name:
        name = name.rpartition(":")[2]
    return name.rpartition("/")[2].rpartition(".")[2] if "/" in name else name


def lint_blueprint(
    blueprint: Blueprint,
    *,
    profile: Profile = "authored",
    tools: ToolCatalog | None = None,
    skills: Collection[str] = (),
) -> list[LintFinding]:
    """Lint the agents and tool cards of `blueprint`, in blueprint order.

    Args:
        tools: The loaded tools whose cards are linted; `None` lints no cards.
        skills: Names of the known skills (for E03).
    """

    return _Linter(blueprint, profile=profile, tools=tools, skills=skills).run()
