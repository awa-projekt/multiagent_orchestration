"""Turn finished process runs into eval cases: what happened once becomes a regression test.

Each case gets the run's `input`, its approval `decisions`, scorers for the tools, delegations
and handoffs it made (plus a `judge:` against the reference), and its output as `expected`.
Texts pass the trace store's redaction; look at the answer before you keep it as the reference.
"""

from __future__ import annotations

import json
import re
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from awa.automation.models import OUTPUT_CHARS, ProcessRun
from awa.errors import EvalError
from awa.evals.dataset import Dataset, EvalCase
from awa.observability.trace_store import TraceStore, read_trace
from awa.observability.trace_view import Node, build_tree

JUDGE_CRITERIA = (
    "The answer says the same as the reference answer in substance: the same facts, figures, "
    "decisions and next steps. Different wording is fine; a missing or contradicting point is not."
)
"""What the judge of an exported case checks."""


@dataclass
class ExportedCases:
    """The result of `cases_from_runs`.

    Attributes:
        skipped: One line per run that was not exported, with the reason.
        warnings: One line per case that needs a look: masked text or a cut reference answer.
    """

    dataset: Dataset
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def cases_from_runs(
    runs: Sequence[ProcessRun], traces: TraceStore, *, judge: bool = True, name: str = "from-runs"
) -> ExportedCases:
    """Build eval cases from finished process runs and their traces.

    Args:
        runs: The runs; only succeeded runs with an output are exported.
        judge: Add a `judge:` scorer that compares the answer with the reference output.
    """

    cases: list[EvalCase] = []
    skipped: list[str] = []
    warnings: list[str] = []
    for run in runs:
        reason = _unusable(run)
        if reason is not None:
            skipped.append(f"{run.id}: {reason}")
            continue
        files = traces.find(run.id)
        if not files:
            skipped.append(f"{run.id}: no trace found in {traces.directory}")
            continue
        facts = _Facts()
        for path in files:
            facts.add(path)
        scorers = [
            *(f"tool:{tool}" for tool in facts.tools),
            *(f"delegated:{agent}" for agent in facts.delegated),
            *(f"handoff:{agent}" for agent in facts.handoffs),
        ]
        if judge:
            scorers.append(f"judge:{JUDGE_CRITERIA}")
        original = _input_of(run)
        shown, expected = traces.redact(original), traces.redact(run.output)
        if shown != original or expected != run.output:
            warnings.append(
                f"{run.id}: secrets or personal data were masked in the input or the answer; "
                "check that the case still says what it should"
            )
        if len(run.output or "") >= OUTPUT_CHARS:
            warnings.append(
                f"{run.id}: the answer is cut (the run record keeps its first {OUTPUT_CHARS} "
                "characters), so the judge compares with a shortened reference; shorten "
                "`expected` by hand or export with --no-judge"
            )
        cases.append(
            EvalCase(
                id=f"from-{run.id}",
                input=shown,
                expected=expected,
                decisions=tuple(facts.decisions),
                scorers=tuple(scorers),
                tags=("from-runs", run.automation),
                metadata={"run_id": run.id, "automation": run.automation, "trigger": run.trigger},
            )
        )
    return ExportedCases(Dataset(name=name, cases=tuple(cases)), skipped, warnings)


def add_cases(path: str | Path, new: Dataset) -> tuple[list[str], list[str]]:
    """Add the missing cases of `new` to the file `path`, keeping its cases and comments untouched.

    A new file is written as YAML; an existing one needs `cases:` as its last key. Returns the ids
    added and those already there.

    Raises:
        EvalError: If the file cannot be read, or its layout does not allow adding to it.
    """

    file = Path(path)
    if not file.is_file():
        file.parent.mkdir(parents=True, exist_ok=True)
        new.to_yaml(file)
        return [case.id for case in new.cases], []
    known = {case.id for case in Dataset.load(file).cases}
    kept = [case.id for case in new.cases if case.id in known]
    missing = Dataset(name=new.name, cases=tuple(c for c in new.cases if c.id not in known))
    if missing.cases:
        text = file.read_text(encoding="utf-8")
        block = _yaml_items(text, missing, file)
        with file.open("a", encoding="utf-8", newline="") as handle:
            handle.write(("" if text.endswith("\n") or not text else "\n") + block)
    return [case.id for case in missing.cases], kept


def _yaml_items(text: str, cases: Dataset, file: Path) -> str:
    """The YAML of `cases` as list items that continue the `cases:` list ending `text`."""

    document = yaml.safe_load(text)
    heading = re.findall(r"^cases:[ \t]*(?:#.*)?$", text, re.MULTILINE)
    first = re.search(r"^cases:.*\n(?:[ \t]*(?:#.*)?\n)*?( *)- ", text, re.MULTILINE)
    if not (isinstance(document, dict) and list(document)[-1] == "cases" and heading and first):
        raise EvalError(
            f"Cannot add cases to {file} without rewriting it: put `cases:` last in the file, "
            "with the cases listed below it, or export to a new file and merge by hand"
        )
    items = yaml.safe_load(cases.to_yaml())["cases"]
    block = yaml.safe_dump(items, sort_keys=False, allow_unicode=True, width=100)
    return textwrap.indent(block, first.group(1))


def _unusable(run: ProcessRun) -> str | None:
    if run.status != "succeeded":
        return f"status is {run.status}; only succeeded runs have an answer to keep"
    if not run.output:
        return "the run has no output"
    if run.input in (None, ""):
        return "the run has no input"
    return None


def _input_of(run: ProcessRun) -> Any:
    value = run.input
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(isinstance(m, dict) for m in value):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


class _Facts:
    """What the traces of one run say: tools called, agents asked, decisions taken."""

    def __init__(self) -> None:
        self.tools: list[str] = []
        self.delegated: list[str] = []
        self.handoffs: list[str] = []
        self.decisions: list[dict[str, Any]] = []

    def add(self, path: Path) -> None:
        tree = build_tree(read_trace(path))
        for root in tree.roots:
            self._walk(root)
        for record in tree.data.events:
            if record.get("type") == "approval_decided" and not record.get("remembered"):
                self.decisions.append(_decision(record))

    def _walk(self, node: Node) -> None:
        for walked in node.walk():
            for item in walked.items:
                info = item.info
                if item.kind == "tool" and not item.hidden:
                    _once(self.tools, str(info.get("tool")))
                elif item.kind == "delegation" and info.get("mode") == "delegate":
                    _once(self.delegated, str(info.get("target")))
                elif item.kind == "handoff":
                    _once(self.handoffs, str(info.get("target")))


def _once(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _decision(record: dict[str, Any]) -> dict[str, Any]:
    # An edited decision is exported as an approval: the edited arguments are not in the trace.
    approved = record.get("decision") in ("approve", "edit") and not record.get("expired")
    decision: dict[str, Any] = {"action": "approve" if approved else "reject"}
    if record.get("tool"):
        decision["tool"] = record["tool"]
    if not approved:
        decision["note"] = record.get("note") or "Rejected."
    return decision


__all__ = ["JUDGE_CRITERIA", "ExportedCases", "add_cases", "cases_from_runs"]
