"""The post-build refinement stage: small, measured text changes on top of a raw design that passes.

Deterministic findings from the traces feed one fast-model call that writes at most three
lines per agent, never with a case value. The refined design runs every case twice and is
kept only when its `pass^2` reaches the raw pass rate with no more input tokens per run.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from awa._internal.util import preview
from awa.blueprints._schema_refine import Refinement
from awa.contracts.events import ToolCallEvent, ToolResultEvent
from awa.evals.report import EvalReport

if TYPE_CHECKING:
    from awa.blueprints._project import ProjectDesign
    from awa.blueprints.schema import Blueprint
    from awa.evals.dataset import Dataset

MAX_LINES = 3
"""Refinement lines per agent."""

LONG_RUN = 6
"""Tool calls per run from which the run length is worth naming."""

CONTEXT_KINDS = frozenset({"first", "budget"})
"""Findings that explain the runs but are no reason for a line on their own."""

_FINDING_ID = re.compile(r"\s*\(?\bf\d+\)?\.?\s*$")
"""A finding id the model repeated at the end of a line."""

Evaluate = Callable[["ProjectDesign", int], Awaitable[EvalReport]]
"""Evaluates a design with `k` runs per case."""

_QUOTES = "\u201c\u201d\u201e\u2018\u2019"  # typographic quotes
_QUOTED = re.compile(rf"""["'{_QUOTES}]([^"'{_QUOTES}]{{3,60}})["'{_QUOTES}]""")
_NUMBER = re.compile(r"\d[\d.,:/-]*\d")
_CODE = re.compile(r"\b(?=[\w-]*\d)(?=[\w-]*[A-Za-z])[\w-]{3,}\b")
# A capital inside a sentence (Latin-1 letters included, written as escapes).
_LOWER = "a-z\u00df-\u00f6\u00f8-\u00ff"
_NAME = re.compile(rf"(?<=[{_LOWER},] )[A-Z\u00c0-\u00d6\u00d8-\u00de][{_LOWER}]{{2,}}\b")
_WORD = re.compile(r"[\w-]+")

REFINER_INSTRUCTIONS = (
    "You refine the instructions of agents from facts measured in their test runs. For each "
    "agent you get its task, its tools and numbered findings. Write at most three lines per "
    "agent; each line is one imperative sentence that fixes one finding and saves work, and "
    "gives the finding's id in its own field. Findings marked context only explain the runs: "
    "write no line for them. Name only tools, fields and procedures. Never write a value from "
    "an eval case: no names, numbers, ids, dates or quoted text. Leave an agent out when no "
    "finding warrants a change."
)


@dataclass(frozen=True)
class Finding:
    """One deterministic observation about an agent's runs (tool names and counts, never a case value)."""

    id: str
    agent: str
    kind: str
    text: str


@dataclass
class TraceAnalysis:
    """What the traces of one evaluation show, agent by agent."""

    findings: list[Finding] = field(default_factory=list)
    suggest: dict[str, list[str]] = field(default_factory=dict)

    def of(self, agent: str) -> list[Finding]:
        return [finding for finding in self.findings if finding.agent == agent]

    @property
    def actionable(self) -> bool:
        """Whether a finding points at a problem; the first-tool finding alone is context."""

        return any(finding.kind not in CONTEXT_KINDS for finding in self.findings)


def analyze_traces(
    blueprint: Blueprint, report: EvalReport, flaky: Collection[str] = ()
) -> TraceAnalysis:
    """The findings of `report`'s runs, without a model.

    Calls are grouped per agent run: an agent that runs again starts with a fresh context, so
    reading the same data again there is no duplicate.
    """

    calls: dict[str, list[list[tuple[str, str]]]] = defaultdict(list)
    cases: dict[str, set[str]] = defaultdict(set)
    failed: dict[str, Counter[str]] = defaultdict(Counter)
    for run in report.runs:
        per_run: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for event in run.events:
            if isinstance(event, ToolCallEvent):
                arguments = json.dumps(event.arguments, sort_keys=True, default=str)
                per_run[event.agent, event.run_id].append((event.tool_name, arguments))
            elif isinstance(event, ToolResultEvent) and event.is_error:
                failed[event.agent][event.tool_name] += 1
        ran = {agent for agent, _ in per_run}
        for agent in {*ran, *run.handoff_path, *([run.agent] if run.agent else [])}:
            cases[agent].add(run.case_id)
            if agent not in ran:
                calls[agent].append([])
        for (agent, _), agent_calls in per_run.items():
            calls[agent].append(agent_calls)
    analysis = TraceAnalysis()
    number = 0

    def add(agent: str, kind: str, text: str) -> None:
        nonlocal number
        number += 1
        analysis.findings.append(Finding(f"f{number}", agent, kind, text))

    names = {spec.name for spec in blueprint.agents}
    for agent in sorted(set(calls) & names):
        runs = calls[agent]
        repeated = Counter(
            tool for run in runs for (tool, args), n in Counter(run).items() if n > 1
        )
        for tool, count in sorted(repeated.items()):
            add(
                agent,
                "duplicate",
                f"{tool} was called again with identical arguments in {count} of "
                f"{len(runs)} run(s)",
            )
        for tool, count in sorted(failed[agent].items()):
            add(agent, "errors", f"{tool} failed {count} time(s)")
        firsts = [run[0][0] for run in runs if run]
        if len(firsts) >= 2 and len(set(firsts)) == 1:
            add(agent, "first", f"{firsts[0]} was the first tool call in all {len(firsts)} runs")
        sizes = sorted(len(run) for run in runs)
        if sizes and sizes[-1] >= LONG_RUN:
            p90 = sizes[min(len(sizes) - 1, math.ceil(0.9 * len(sizes)) - 1)]
            add(agent, "budget", f"tool calls per run: p90 {p90}, most {sizes[-1]}")
        shaky = sorted(cases[agent] & set(flaky))
        if shaky:
            add(agent, "flaky", f"{len(shaky)} case(s) passed only on a re-run")
    troubled = {f.agent for f in analysis.findings if f.kind in ("errors", "flaky")}
    if troubled:
        analysis.suggest = _suggestions(blueprint, troubled)
    return analysis


def _suggestions(blueprint: Blueprint, agents: Collection[str]) -> dict[str, list[str]]:
    """The tools the matcher suggests for `agents` but could not grant, with the reason."""

    from awa.blueprints.auto import blueprint_auto_report

    report = blueprint_auto_report(blueprint)
    found: dict[str, list[str]] = {}
    for agent in sorted(agents):
        tools = [
            f"{match.name}: {match.note}"
            for match in report.for_agent(agent).suggested
            if match.kind == "tool"
        ]
        if tools:
            found[agent] = tools
    return found


def case_literals(evals: Dataset, blueprint: Blueprint) -> set[str]:
    """Values of the cases a refinement must not name: quoted strings, numbers, codes and
    names that occur in a case but nowhere in the blueprint (lower case)."""

    own = json.dumps(blueprint.model_dump(mode="json"), ensure_ascii=False).lower()
    found: set[str] = set()
    for case in evals:
        text = json.dumps(
            [case.input, case.expected, case.metadata], ensure_ascii=False, default=str
        )
        for pattern in (_QUOTED, _NUMBER, _CODE, _NAME):
            for match in pattern.finditer(text):
                value = (match.group(1) if pattern is _QUOTED else match.group(0)).strip().lower()
                if len(value) >= 2 and value not in own:
                    found.add(value)
    return found


def literal_in(text: str, literals: Iterable[str]) -> str | None:
    """The first case literal `text` names, or `None`."""

    lowered = f" {' '.join(_WORD.findall(text.lower()))} "
    for literal in sorted(literals, key=len, reverse=True):
        words = " ".join(_WORD.findall(literal))
        if words and f" {words} " in lowered:
            return literal
    return None


class LineDraft(BaseModel):
    """One refinement line the model proposes."""

    model_config = ConfigDict(extra="ignore")

    agent: str
    finding: str = Field(description="The id of the finding the line rests on (f1, f2, ...).")
    text: str = Field(description="One imperative sentence: tools, fields, procedure only.")


class LineBatch(BaseModel):
    """The model's answer: lines for every agent at once."""

    model_config = ConfigDict(extra="ignore")

    lines: list[LineDraft] = Field(default_factory=list)


def refiner_prompt(blueprint: Blueprint, analysis: TraceAnalysis) -> str:
    parts = []
    for spec in blueprint.agents:
        findings = analysis.of(spec.name)
        if all(f.kind in CONTEXT_KINDS for f in findings):
            continue  # nothing to fix (or no findings at all)
        lines = "\n".join(
            f"- {f.id} ({'context' if f.kind in CONTEXT_KINDS else f.kind}): {f.text}"
            for f in findings
        )
        parts.append(
            f"## Agent {spec.name}\nTask: {spec.description or spec.goal or '(none)'}\n"
            f"Tools: {', '.join(spec.tools) or 'none'}\nFindings:\n{lines}"
        )
    return "\n\n".join(parts)


def accepted_lines(
    batch: LineBatch, analysis: TraceAnalysis, literals: Collection[str]
) -> tuple[dict[str, list[Refinement]], list[str]]:
    """The model's lines that rest on a finding of their agent, fit one line and name no case
    literal (at most `MAX_LINES` per agent), and the others with the reason."""

    findings = {(f.agent, f.id): f for f in analysis.findings}
    kept: dict[str, list[Refinement]] = defaultdict(list)
    rejected: list[str] = []
    for line in batch.lines:
        finding = findings.get((line.agent, line.finding.strip()))
        text = _FINDING_ID.sub("", " ".join(line.text.split())).strip()
        reason = _rejection(finding, text, len(kept.get(line.agent, ())), literals)
        if reason or finding is None:
            rejected.append(f"{line.agent}: {preview(text, 120)} ({reason})")
            continue
        kept[line.agent].append(Refinement(text=text, evidence=finding.text))
    return dict(kept), rejected


def _rejection(finding: Finding | None, text: str, kept: int, literals: Collection[str]) -> str:
    """Why a proposed line is not kept, or `""` when it is."""

    if finding is None:
        return "no finding of the agent"
    if finding.kind in CONTEXT_KINDS:
        return "rests on a context finding only"
    if not text or len(text) > 300:
        return "empty or too long"
    if kept >= MAX_LINES:
        return f"more than {MAX_LINES} lines"
    if literal_in(text, literals) is not None:
        return "names a case value"
    return ""


@dataclass(frozen=True)
class Measurement:
    """The accept gate's numbers: raw against refined."""

    raw_pass_rate: float
    pass_hat_k: float
    raw_input_tokens: int
    input_tokens: int

    @property
    def accepted(self) -> bool:
        return self.pass_hat_k >= self.raw_pass_rate and self.input_tokens <= self.raw_input_tokens

    def to_dict(self) -> dict[str, Any]:
        return {**dataclasses.asdict(self), "accepted": self.accepted}


def input_per_run(report: EvalReport) -> int:
    runs = report.runs
    return round(sum(run.usage.input_tokens for run in runs) / len(runs)) if runs else 0


def measure(raw: EvalReport, refined: EvalReport) -> Measurement:
    return Measurement(
        raw_pass_rate=raw.pass_rate,
        pass_hat_k=refined.pass_hat_k if refined.pass_hat_k is not None else refined.pass_rate,
        raw_input_tokens=input_per_run(raw),
        input_tokens=input_per_run(refined),
    )


@dataclass
class StageOutcome:
    """What the refinement stage did (`BuildReport.refinement`)."""

    lines: dict[str, list[str]] = field(default_factory=dict)
    suggested: dict[str, list[str]] = field(default_factory=dict)
    measurement: Measurement | None = None
    skipped: str = ""
    findings: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.measurement is not None and self.measurement.accepted

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "lines": self.lines,
            "suggested": self.suggested,
            "measurement": self.measurement.to_dict() if self.measurement else None,
            "skipped": self.skipped,
            "findings": self.findings,
            "rejected": self.rejected,
        }


def refined_design(
    project: ProjectDesign, lines: Mapping[str, Sequence[Refinement]]
) -> ProjectDesign:
    """`project` with the lines on its agents."""

    blueprint = project.blueprint
    agents = []
    for spec in blueprint.agents:
        if spec.name in lines:
            refinements = [*spec.refinements, *lines[spec.name]][-MAX_LINES:]
            spec = spec.model_copy(update={"refinements": refinements})
        agents.append(spec)
    return dataclasses.replace(project, blueprint=blueprint.model_copy(update={"agents": agents}))


async def refine_stage(
    project: ProjectDesign,
    report: EvalReport,
    flaky: Collection[str],
    *,
    ask: Callable[[str], Awaitable[LineBatch]],
    evaluate: Evaluate,
) -> tuple[ProjectDesign, EvalReport, StageOutcome]:
    """Refine `project` from `report`'s traces; return the kept design, its report and the outcome."""

    analysis = analyze_traces(project.blueprint, report, flaky)
    found = [f"{f.agent}: {f.text}" for f in analysis.findings]
    if not analysis.actionable:
        skipped = "the traces show nothing to refine"
        return project, report, StageOutcome(skipped=skipped, findings=found)
    batch = await ask(refiner_prompt(project.blueprint, analysis))
    literals = case_literals(project.evals, project.blueprint)
    lines, rejected = accepted_lines(batch, analysis, literals)
    if not lines:
        skipped = "no refinement line passed the checks"
        outcome = StageOutcome(
            suggested=analysis.suggest, skipped=skipped, findings=found, rejected=rejected
        )
        return project, report, outcome
    candidate = refined_design(project, lines)
    refined = await evaluate(candidate, 2)
    measured = measure(report, refined)
    outcome = StageOutcome(
        lines={agent: [line.text for line in kept] for agent, kept in lines.items()},
        suggested=analysis.suggest,
        measurement=measured,
        findings=found,
        rejected=rejected,
    )
    if not measured.accepted:
        return project, report, outcome
    stamped = {
        agent: [
            line.model_copy(
                update={
                    "pass_hat_k": measured.pass_hat_k,
                    "raw_pass_rate": measured.raw_pass_rate,
                    "input_tokens": measured.input_tokens,
                    "raw_input_tokens": measured.raw_input_tokens,
                }
            )
            for line in agent_lines
        ]
        for agent, agent_lines in lines.items()
    }
    return refined_design(project, stamped), refined, outcome
