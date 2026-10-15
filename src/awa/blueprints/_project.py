"""Design a project from a process description: acceptance tests first, then the blueprint."""

from __future__ import annotations

import copy
import dataclasses
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from awa._internal.upstream.models import BaseChatModel
from awa._internal.upstream.tools import BaseTool
from awa.agents import Agent
from awa.blueprints import _project_files as files
from awa.blueprints._automation import TriggerBlueprint
from awa.blueprints._build_roles import share_check_tools
from awa.blueprints._catalog import reference_section
from awa.blueprints._data_design import (
    TEST_DESIGNER_DATA_INSTRUCTIONS,
    acceptance_data_problems,
    blueprint_data_problems,
    declares,
    is_code,
)
from awa.blueprints._data_map import DataMap, StandIn, data_map
from awa.blueprints._process_documents import merge_data_entries
from awa.blueprints._project_data import ProjectData, systems_from_profile
from awa.blueprints._resolve import ToolCatalog
from awa.blueprints._risk import ungated_action_issues
from awa.blueprints._schema_data import DataBlueprint
from awa.blueprints._schema_limits import LimitsBlueprint
from awa.blueprints._systems import DeclaredSystem, declared_tools, systems_section
from awa.blueprints.architect import architect_agent, design_brief, draft_blueprint, name_lines
from awa.blueprints.errors import BlueprintError, BlueprintIssue
from awa.blueprints.ports import DataStager, Mask
from awa.blueprints.schema import ApprovalBlueprint, Blueprint, ToolSource
from awa.connectors import EnvironmentReport
from awa.errors import StructuredOutputError
from awa.evals.dataset import Dataset, EvalCase
from awa.skills import SkillRegistry
from awa.tools.risk import ToolRisk, infer_tool_risk

if TYPE_CHECKING:
    from awa.data import ProfileEvent
    from awa.evals.report import EvalReport

TEST_DESIGNER_INSTRUCTIONS = """\
You write the acceptance tests and the sample data for a system that will automate a business \
process. The system is designed after you, against your tests, and runs on your sample data \
until the real systems are connected. Ask no questions: where the process is silent, make the \
sane choice and list it in assumptions. Return:

criteria: the observable outcomes the process must achieve, one sentence each, usually 3 to 6.

systems: every outside system the process reads or changes (database, ERP, ticketing, CRM, \
email, file share), and nothing else.
- name in plain words ("Order database"); key: a short snake_case id ("orders").
- tables: the records the process needs, with typed columns (the first column identifies a \
record) and 3 to 8 realistic sample rows, one CSV line each, values in column order. A system \
the process only sends to (email, chat) gets one table for what is sent, with no rows.
- writes: true when the process changes data in it or sends through it; such changes wait \
for a human.
- connector: the connector name when a system under "Your systems" reaches it; otherwise \
leave it empty and set kind, how it is reached: database, rest_api, odata, graphql, email or \
files (a file share with spreadsheets).
Each system becomes tools named after its key: <key>_schema (tables, columns, keys), \
<key>_query (read with SQL) and, when it writes, <key>_execute; for kind files <key>_list_files, <key>_read_csv, <key>_query_csv and, \
when it writes, <key>_write_csv.

rules: the calculations and decisions the process states exactly (thresholds, tolerances, \
totals, deadlines), nothing it leaves to judgment. Each is a tool: typed arguments, an \
expression over them (arithmetic, comparisons, and/or/not, x if condition else y, abs, min, \
max, round) and at least two examples, each with the arguments as a JSON object and the \
result as JSON.

approvals: the decisions the process leaves to a person (a manager approves from an amount \
on, a lead signs off a contract), and nothing else; changes to a system wait for a human by \
themselves. Each is an approval step, a tool the system calls when the step is due: name \
(snake_case, e.g. department_head_approval), description (who decides what), typed arguments \
with what the person decides on, and when: the condition over the arguments under which the \
person must decide (comparisons, and/or/not, in, e.g. total_eur >= 5000); leave it empty when \
the person always decides. Where the condition does not hold, the step is approved straight \
away. Give examples: the arguments as a JSON object and asks, whether the person is asked; \
with a condition, at least one on each side of it.

triggers: what starts the process by itself, only when the process says so ("every \
Monday", "when an invoice arrives by mail", "when a file lands in a folder", "after a \
webhook"); otherwise none. kind schedule (cron with five fields in the process's time \
zone, or every), mail (host only when named; the mailbox's folder), file-drop (a folder \
inside the project, pattern) or webhook. name: kebab-case. input: the request each event \
sends, as a template with {payload.field}. An event carries these fields: schedule date \
and scheduled_for; mail from, to, subject, date, text; file-drop name, text; webhook the \
JSON body. sample_payload: a JSON object with realistic values of the fields the input \
uses, matching your sample data.

assumptions: one sentence each for what you assumed where the process is silent (amounts, \
recipients, deadlines, time zone, who approves). Do not restate that changes wait for a \
person; the system adds that.

cases: 3 to 6 eval cases: the main path, one edge case, and each approval, both where \
it is asked and where it is not. input is a realistic request with concrete ids from the \
sample data. A call that waits for a human (a writing system tool, or an approval step whose \
condition holds) pauses the run; decisions answer the pauses in order, one each: approve, or \
reject with a note, each naming the tool it answers (calls may come in another order or at \
the same time). With triggers, at least one case goes through a trigger: set trigger and \
payload and leave input empty; the request is then the trigger's input filled in. After \
each decision the run goes on, so a case follows the process to its \
end. A run with more pauses than decisions ends paused at the first unanswered one. When \
the run may call a tool that waits for a human several times (once per record, e.g. one \
refund per order), answer all of its pauses with one decision naming the tool with repeat: \
true, and give that tool one approval expectation. \
expectations check the outcome:
- contains: text the answer must include, taken from the sample data or the request (a \
name, id, date, amount or one status word), never a phrase the system may word \
differently ("ask HR to check"): check wording with judge. The answer is the system's final \
message: expect what the process makes the system state (what it looks up, computes, decides \
or drafts); a phrase that only occurs in the request is no check of the system;
- regex: a pattern the answer must match;
- tool_called / tool_not_called: a tool name (a system tool, a rule, an approval step or a \
builtin tool). tool_called only a tool the case cannot be done without: when the request \
carries the text to work on (a mail's body, a pasted record), do not expect a tool that would \
only read it again;
- approval: a tool that must wait for a human in this run (a writing system tool or an \
approval step), one per pause, in the order they happen;
- no_approval: an approval step (or writing tool) that must not wait for a human in this run;
- judge: a concrete quality of the answer no other check captures; at most one per case, a \
single pass/fail criterion in at most 40 words. The judge sees only the request and the final \
answer, not the tool calls or messages sent.
Every case has at least one deterministic expectation (contains, regex, tool_called, \
tool_not_called, approval, no_approval); a case never expects a tool both called and not \
called. Before you answer, run each case in your head against your sample data: every rule \
example, every approval condition and every check must give the result you wrote. The cases \
are evaluated on the sample data before anything runs, and a case that cannot pass is sent \
back.
Give one decision per approval expectation. A case may leave the last approval unanswered to \
end paused there; it then checks no answer (contains, regex, judge). Every expectation must \
hold on your sample data.
"""

ConnectionStatus = Literal["ready", "configure", "map"]
"""How a system gets connected: through a configured catalog connector, through a
catalog connector that still needs its settings, or through a generic connector."""

SystemKind = Literal["database", "rest_api", "odata", "graphql", "email", "files"]
"""How a system without a catalog connector is reached."""

GENERIC_CONNECTORS: Mapping[str, str] = {
    "database": "sql",
    "rest_api": "openapi",
    "odata": "odata",
    "graphql": "graphql",
    "email": "smtp",
    "files": "workspace",
}
"""The generic connector a system of each kind is linked through."""

_LOCAL_CONNECTORS = frozenset({"workspace"})
"""Connectors that stand in for a system until it is connected; never a system's own."""

_TRIGGERED_MAX_REQUESTS = 200
"""Model calls one run may make in a built process that starts by itself (see `limits:`)."""

# Connectors with one instance per system: their variable is renamed to `<KEY>_<suffix>`.
_RENAMES: Mapping[str, str] = {"sql": "DATABASE_URL", "workspace": "AWA_WORKSPACE_DIR"}
_SUFFIXES: Mapping[str, str] = {"sql": "DATABASE_URL", "workspace": "FOLDER"}

_AMOUNT = re.compile(
    r"\d{4,}(?:[.,]\d{1,2})?|\d{1,3}(?:[.,]\d{3})*[.,]\d{1,2}|\d{1,3}(?:[.,]\d{3})+"
)

_SPEC_PREFIX = {
    "contains": "contains",
    "regex": "regex",
    "tool_called": "tool",
    "tool_not_called": "no_tool",
    "approval": "approval_requested",
    "no_approval": "no_approval",
    "judge": "judge",
}
_ANSWER_KINDS = ("contains", "regex", "judge")

_JUDGE_RUBRIC_WORDS = 40
"""The longest judge criterion, in words: a short pass/fail rubric the judge applies the same way."""


def _amount_pattern(value: str) -> str:
    """A pattern for the number `value` as an answer may write it: any thousands separator or
    none, zero decimals optional (`1250.00` matches `1,250.00`, `1.250` and `1250`)."""

    match = re.fullmatch(r"([\d.,]*?)(?:[.,](\d{1,2}))?", value)
    assert match is not None  # _AMOUNT matched
    digits, fraction = re.sub(r"\D", "", match.group(1)), match.group(2) or ""
    groups = [digits[max(i - 3, 0) : i] for i in range(len(digits), 0, -3)][::-1]
    pattern = r"(?<!\d)" + r"[,.\s]?".join(groups)
    if fraction.strip("0"):
        pattern += f"[.,]{fraction.rstrip('0')}0?"
    elif fraction:
        pattern += r"(?:[.,]00?)?"
    return pattern + r"(?!\d)"


class ColumnDesign(BaseModel):
    """One typed column of a workspace table."""

    name: str = Field(description="snake_case column name, e.g. 'order_id'.")
    type: Literal["text", "integer", "real", "boolean"] = "text"


class TableDesign(BaseModel):
    """A table of a system's workspace, with sample rows."""

    name: str = Field(description="snake_case table name, e.g. 'orders'.")
    description: str = Field(description="What one row is.")
    columns: list[ColumnDesign]
    rows: list[str] = Field(
        default_factory=list, description="Sample rows, one CSV line each, in column order."
    )


class SystemDesign(BaseModel):
    """An outside system the process uses, with the sample data of its workspace."""

    name: str = Field(description="The system in plain words, e.g. 'Order database'.")
    key: str = Field(description="Short snake_case id, e.g. 'orders'.")
    description: str = Field(description="One sentence: what the process uses it for.")
    tables: list[TableDesign]
    writes: bool = Field(default=False, description="The process changes data or sends through it.")
    connector: str | None = Field(
        default=None, description="A connector listed under Your systems."
    )
    kind: SystemKind = Field(default="rest_api", description="How it is reached otherwise.")


class RuleArgument(BaseModel):
    """One typed argument of a rule."""

    name: str = Field(description="snake_case name, e.g. 'amount'.")
    type: Literal["string", "integer", "number", "boolean"] = "number"
    description: str = Field(default="", description="What the value is, with its unit.")


class RuleExample(BaseModel):
    """A worked example of a rule."""

    arguments: str = Field(description='The arguments as a JSON object, e.g. {"amount": 12000}.')
    result: str = Field(description="The result as JSON, e.g. true or 1190.0.")


class RuleDesign(BaseModel):
    """An exact calculation or decision of the process, as a tool."""

    name: str = Field(description="snake_case verb or question, e.g. 'needs_manager_approval'.")
    description: str = Field(description="One sentence: what it computes or decides.")
    arguments: list[RuleArgument]
    expression: str = Field(description="Expression over the arguments, e.g. 'amount > 10000'.")
    examples: list[RuleExample]


class ApprovalExample(BaseModel):
    """A worked example of an approval step: whether the person is asked."""

    arguments: str = Field(description='The arguments as a JSON object, e.g. {"total_eur": 6200}.')
    asks: bool = Field(description="Whether the person must decide for these arguments.")


class ApprovalStepDesign(BaseModel):
    """A decision the process leaves to a person, as a tool that waits for it."""

    name: str = Field(description="snake_case name, e.g. 'department_head_approval'.")
    description: str = Field(
        description="Who decides what, e.g. 'The department head approves purchases from "
        "5,000 EUR.'"
    )
    arguments: list[RuleArgument]
    when: str = Field(
        default="",
        description="Condition over the arguments under which the person decides, e.g. "
        "'total_eur >= 5000'; empty: always.",
    )
    examples: list[ApprovalExample] = Field(default_factory=list)


class Expectation(BaseModel):
    """One check of an eval case."""

    kind: Literal[
        "contains", "regex", "tool_called", "tool_not_called", "approval", "no_approval", "judge"
    ]
    value: str = Field(default="", description="Text, pattern, tool name or judging criteria.")
    check: str = Field(
        default="",
        description="Only with real data files, for a contains expectation of a computed result: "
        "the expression over the file's columns that gives the value, e.g. "
        "\"sum(Betrag_EUR where Status == 'offen')\".",
    )
    file: str = Field(
        default="", description="With check: the profiled file it reads (default: the case's file)."
    )

    def spec(self) -> str:
        """The scorer specification (see `awa.evals.parse_scorer`), e.g. `tool:orders_query`."""

        value = self.value.strip() if self.kind == "regex" else " ".join(self.value.split())
        if self.kind == "contains" and _AMOUNT.fullmatch(value):
            return f"regex:{_amount_pattern(value)}"  # 1,250.00 and 1250 are the same amount
        prefix = _SPEC_PREFIX[self.kind]
        return f"{prefix}:{value}" if value else prefix


class DecisionDesign(BaseModel):
    """A human decision that answers one pause of a case's run."""

    action: Literal["approve", "reject"]
    note: str = Field(default="", description="Why, for a rejection.")
    tool: str = Field(
        default="", description="The tool whose pause this decision answers, e.g. 'orders_execute'."
    )
    repeat: bool = Field(
        default=False,
        description="Answer every pause of `tool`, for a tool the run may call several times.",
    )


class TriggerDesign(BaseModel):
    """What starts the process by itself: a schedule, a web request, a file or an email."""

    kind: Literal["schedule", "webhook", "file-drop", "mail"]
    name: str = Field(description="kebab-case id, e.g. 'monday-report'.")
    input: str = Field(
        description="The request each event sends, as a template: {payload.field} fills in "
        "the event's values, e.g. 'Process the invoice mail from {payload.from}: "
        "{payload.subject}\n{payload.text}'."
    )
    sample_payload: str = Field(
        description="A JSON object with example values of every field the input uses, "
        'e.g. {"from": "ap@acme.example", "subject": "Invoice 4711", "text": "..."}.'
    )
    cron: str = Field(default="", description="Schedule: five fields, e.g. '0 7 * * MON'.")
    every: str = Field(default="", description="Schedule: an interval instead of cron, e.g. '15m'.")
    timezone: str = Field(default="", description="Schedule: IANA zone, e.g. 'Europe/Berlin'.")
    directory: str = Field(default="inbox", description="File drop: the folder to watch.")
    pattern: str = Field(default="*", description="File drop: which names count, e.g. '*.pdf'.")
    host: str = Field(default="", description="Mail: the IMAP server when the process names it.")
    folder: str = Field(default="INBOX", description="Mail: the folder to read.")


class CaseDesign(BaseModel):
    """One eval case as the test designer writes it."""

    id: str = Field(description="Short kebab-case id, e.g. 'order-status'.")
    input: str = Field(default="", description="A realistic request to the system.")
    trigger: str = Field(
        default="",
        description="A trigger's name: the request is then that trigger's input filled with "
        "`payload`, and `input` stays empty.",
    )
    payload: str = Field(default="", description="With `trigger`: the event's values as JSON.")
    files: list[str] = Field(
        default_factory=list,
        description="Only with real data files: the profiled files this case works on.",
    )
    expectations: list[Expectation]
    decisions: list[DecisionDesign] = Field(
        default_factory=list, description="One per pause for a human, in order."
    )


class DataNote(BaseModel):
    """What the test designer says about one of the user's real files."""

    file: str = Field(description="The file, exactly as profiled.")
    role: Literal["input", "reference"] = Field(
        default="input",
        description="input: the process works on it; reference: master data it looks things up in.",
    )
    description: str = Field(default="", description="One sentence: what the file is.")
    handling: str = Field(
        default="", description="What must happen with each piece of data of this file."
    )


class AcceptanceDesign(BaseModel):
    """What the test designer returns: criteria, systems with sample data, rules, approval
    steps and cases (with real data files: notes on the files instead of invented systems)."""

    criteria: list[str]
    systems: list[SystemDesign] = Field(default_factory=list)
    rules: list[RuleDesign] = Field(default_factory=list)
    approvals: list[ApprovalStepDesign] = Field(default_factory=list)
    triggers: list[TriggerDesign] = Field(default_factory=list)
    data_notes: list[DataNote] = Field(
        default_factory=list, description="Only with real data files: one note per file used."
    )
    assumptions: list[str] = Field(
        default_factory=list, description="What you assumed where the process is silent."
    )
    cases: list[CaseDesign]


@dataclass(frozen=True)
class ConnectionStep:
    """What connecting one outside system takes.

    Attributes:
        connector: The catalog connector, or for `"map"` the generic one (`sql`, `openapi`,
            `odata`, `graphql`, `smtp`, `workspace`).
        env_vars: Names of the settings to set (never values).
        status: `"ready"`, `"configure"` or `"map"`.
        tools: The references the agents use until it is connected.
        config: The instance's `connectors:` entry once it is connected.
        switch: References that change on connecting, to their replacements.
        login: Variables of a login the system may ask for (optional to set).
    """

    system: str
    connector: str | None
    env_vars: tuple[str, ...]
    status: ConnectionStatus
    instructions: str
    tools: tuple[str, ...] = ()
    config: Mapping[str, Any] = field(default_factory=dict)
    switch: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    login: tuple[str, ...] = ()


@dataclass
class ProjectDesign:
    """A designed project: blueprint, eval cases, workspace, rules and connections.

    Attributes:
        blueprint: The blueprint, wired to the local workspace and the rules.
        evals: The eval cases, written before the blueprint.
        rules: The exact rules of the process, each a tool.
        approvals: The decisions the process leaves to a person, each a gated tool.
        triggers: What starts the process by itself (also in the blueprint's `triggers:`).
        assumptions: What the designer assumed where the process was silent.
        environment: The reachable systems the connections were chosen from.
        data: The user's files (profile, notes, staged copies); `None` for invented sample data.
        data_notes: What the test designer said about each file.
        declared: Systems the user declared, reached live and read-only.
        mask: What the designers saw of personal data.
    """

    process: str
    blueprint: Blueprint
    evals: Dataset
    connections: list[ConnectionStep]
    systems: list[SystemDesign] = field(default_factory=list)
    rules: list[RuleDesign] = field(default_factory=list)
    approvals: list[ApprovalStepDesign] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    triggers: list[TriggerDesign] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    guidance: str = ""
    tools: list[BaseTool] = field(default_factory=list, repr=False)
    skills: SkillRegistry = field(default_factory=SkillRegistry.builtin, repr=False)
    environment: EnvironmentReport = field(default_factory=EnvironmentReport, repr=False)
    data: ProjectData | None = field(default=None, repr=False)
    data_notes: list[DataNote] = field(default_factory=list)
    declared: list[DeclaredSystem] = field(default_factory=list)
    mask: Mask = "none"

    def own_tools(self) -> list[BaseTool]:
        """The tools the blueprint names besides connectors: the existing tools, the rules and
        the approval steps."""

        return [*self.tools, *files.rule_tools(self.rules, self.approvals)]

    def data_map(self, connect_hint: str = "with `awa connect .` (CONNECT.md)") -> DataMap:
        """Where the data of the design comes from: per agent, which source it reads or writes,
        how, and whether it is live, a copy of the user's file or a stand-in."""

        keys = {" ".join(system.name.split()): system.key for system in self.systems}
        stand_ins = [
            StandIn(keys.get(step.system, step.system), step.system, step.connector, step.env_vars)
            for step in self.connections
        ]
        files = [f.path for f in self.data.profile.files] if self.data is not None else []
        return data_map(
            self.blueprint,
            declared=self.declared,
            stand_ins=stand_ins,
            files=files,
            connect_hint=connect_hint,
        )

    def templates(self) -> list[SystemDesign]:
        """The systems whose sample data is written to `data/`: with the user's files, all but outboxes."""

        return [s for s in self.systems if self.data is None or not files.is_outbox(s)]

    def write(self, directory: str | Path, *, report: EvalReport | None = None) -> Path:
        """Write the project folder and return it; existing project files are overwritten.

        Writes `blueprint.yaml`, `evals/cases.yaml`, `CONNECT.md`, `.env.example`, `README.md`,
        `data/`, a missing `.gitignore` and, for rules or approvals, `tools.py` with its tests.

        Args:
            report: An evaluation of the design, summarized in the README.
        """

        root = Path(directory)
        for relative, text in files.render(self, report).items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        if not (root / ".gitignore").exists():
            ignore = resources.files("awa") / "templates" / "_common" / "dot_gitignore.tmpl"
            (root / ".gitignore").write_bytes(ignore.read_bytes())
        files.write_workspace(root / "data", self.templates())
        if self.data is not None:
            self.data.materialize(root / "data", share=False, replace=True)
        for trigger in self.triggers:
            if trigger.kind == "file-drop":
                (root / (trigger.directory.strip() or "inbox")).mkdir(parents=True, exist_ok=True)
        return root


async def design_project(
    process: str,
    *,
    model: str | BaseChatModel | None = None,
    environment: EnvironmentReport | Literal["auto"] | None = "auto",
    tools: ToolSource | None = None,
    skills: SkillRegistry | None = None,
    max_repairs: int = 2,
    guidance: str = "",
    data: ProjectData | str | os.PathLike[str] | Sequence[str | os.PathLike[str]] | None = None,
    notes: Mapping[str, str] | None = None,
    mask: Mask = "none",
    stager: DataStager | None = None,
    on_event: Callable[[str, Mapping[str, Any]], None] | None = None,
    declared_systems: Sequence[DeclaredSystem] | None = None,
) -> ProjectDesign:
    """Write acceptance tests and sample data for `process`, then design a blueprint for them.

    A test designer writes criteria, cases, the outside systems with sample records and the
    exact rules; the architect designs the blueprint against them. Every system starts on the
    local `workspace` connector, every rule becomes a checked tool and every decision left to a
    person an approval step; each system gets a `ConnectionStep`. With `data` the design works
    on copies of the user's files and every draft is checked against what they hold.

    Args:
        model: Model of both designers (a string is also `defaults.model`).
        environment: The reachable systems: `"auto"` discovers them from the environment and
            `.env` (names only), `None` assumes none.
        max_repairs: Repair rounds for invalid tests and for blueprint check errors.
        data: The user's files: paths or a `ProjectData` from a stager.
        notes: With paths in `data`: what must happen with a file (a name or glob to a text).
        mask: With paths in `data`: what the model sees of personal data in the digest.
        stager: Copies and profiles paths; default `prepare_data`.
        on_event: Called with `design.acceptance`, `design.validation` and
            `design.architecture` and a dict of facts.
        declared_systems: Declared systems (see `read_declared_systems`); the agents are bound
            to them live and read-only, and `mask` applies to their example rows.

    Raises:
        ValueError: If `process` is empty or `max_repairs` is negative.
        DataError: If a path is not readable or a note matches no file.
        BlueprintError: If the tests or the blueprint still have errors after `max_repairs`.

    Example:
        ```python
        project = await design_project(PROCESS, model="strong", environment=None)
        project.write("order_desk")
        ```
    """

    if not process.strip():
        raise ValueError("process must describe the process to automate")
    if max_repairs < 0:
        raise ValueError(f"max_repairs must be at least 0, got {max_repairs}")
    report = _environment(environment)
    catalog = ToolCatalog.from_source(tools)
    registry = skills if skills is not None else SkillRegistry.builtin()
    staged = staged_data(data, notes, mask, stager)
    declared = list(declared_systems or ())
    acceptance = await _acceptance(
        _acceptance_brief(process, report, catalog, guidance, staged, declared, mask),
        process=process,
        existing=set(catalog.tools),
        model=model,
        max_repairs=max_repairs,
        data=staged,
        on_event=on_event,
        declared=declared,
    )
    systems = [_resolve_connector(system, report) for system in acceptance.systems]
    project = ProjectDesign(
        process=process.strip(),
        blueprint=Blueprint.from_dict({"name": "draft", "agents": {"draft": "Draft."}}),
        evals=Dataset(
            name="acceptance", cases=tuple(_cases(acceptance.cases, acceptance.triggers))
        ),
        connections=[],
        systems=systems,
        rules=list(acceptance.rules),
        approvals=list(acceptance.approvals),
        criteria=[" ".join(c.split()) for c in acceptance.criteria if c.strip()],
        triggers=list(acceptance.triggers),
        assumptions=[" ".join(a.split()) for a in acceptance.assumptions if a.strip()],
        guidance=guidance.strip(),
        tools=list(catalog.tools.values()),
        skills=registry,
        environment=report,
        data=staged,
        data_notes=list(acceptance.data_notes),
        declared=declared,
        mask=mask,
    )
    return await design_architecture(
        project, model=model, max_repairs=max_repairs, on_event=on_event
    )


async def redesign(
    project: ProjectDesign,
    feedback: str,
    *,
    model: str | BaseChatModel | None = None,
    max_repairs: int = 2,
    on_event: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> ProjectDesign:
    """Let the architect change only the blueprint of `project` so the failing cases pass.

    Raises:
        BlueprintError: If the new blueprint still has check errors after `max_repairs`.
    """

    suffix = (
        f"## Your previous blueprint\n```yaml\n{project.blueprint.to_yaml()}```\n\n"
        f"{feedback.strip()}\n\n"
        "Change the blueprint so these cases pass. The tools, the data and the cases are "
        "fixed; change roles, instructions, tools per agent, structure or approvals. A "
        "case's decisions answer its approval pauses in order. Keep what works."
    )
    return await design_architecture(
        project, model=model, max_repairs=max_repairs, suffix=suffix, on_event=on_event
    )


def switch_tools(blueprint: Blueprint, switch: Mapping[str, Sequence[str]]) -> Blueprint:
    """Replace workspace tools by connector references in agents, their instructions and
    approvals."""

    def replaced(references: Sequence[str]) -> list[str]:
        result: list[str] = []
        for reference in references:
            for target in switch.get(reference, (reference,)):
                if target not in result:
                    result.append(target)
        return result

    def reworded(text: str) -> str:
        """The instructions with each switched reference named by its replacement."""

        for reference, targets in switch.items():
            if targets and reference in text:
                text = re.sub(
                    rf"{re.escape(reference)}(?![A-Za-z0-9_])", " and ".join(targets), text
                )
        return text

    agents = [
        spec.model_copy(
            update={"tools": replaced(spec.tools), "instructions": reworded(spec.instructions)}
        )
        for spec in blueprint.agents
    ]
    approvals: list[ApprovalBlueprint] = []
    for approval in blueprint.approvals:
        for target in switch.get(approval.tool, (approval.tool,)):
            if all(existing.tool != target for existing in approvals):
                approvals.append(approval.model_copy(update={"tool": target}))
    return blueprint.model_copy(update={"agents": agents, "approvals": approvals})


def _environment(environment: EnvironmentReport | Literal["auto"] | None) -> EnvironmentReport:
    if isinstance(environment, EnvironmentReport):
        return environment
    if environment is None:
        return EnvironmentReport()
    if environment != "auto":
        raise ValueError(
            f"environment must be an EnvironmentReport, 'auto' or None, got {environment!r}"
        )
    from awa.connectors import discover

    return discover()


def staged_data(
    data: ProjectData | str | os.PathLike[str] | Sequence[str | os.PathLike[str]] | None,
    notes: Mapping[str, str] | None,
    mask: Mask,
    stager: DataStager | None,
    on_progress: Callable[[ProfileEvent], None] | None = None,
) -> ProjectData | None:
    """The files of a design: given staged, or staged now (by `stager`, default the local file system)."""

    if data is None or isinstance(data, ProjectData):
        return data
    if stager is None:
        from awa.blueprints._data_files import prepare_data

        stager = prepare_data
    paths = [data] if isinstance(data, str | os.PathLike) else list(data)
    return stager(paths, notes=notes or {}, mask=mask, limits=None, on_progress=on_progress)


def _acceptance_brief(
    process: str,
    environment: EnvironmentReport,
    catalog: ToolCatalog,
    guidance: str,
    data: ProjectData | None = None,
    declared: Sequence[DeclaredSystem] = (),
    mask: Mask = "none",
) -> str:
    from awa.toolkits import catalog as builtin_catalog

    systems = EnvironmentReport(
        providers=environment.providers,
        connectors=tuple(c for c in environment.connectors if c.name not in _LOCAL_CONNECTORS),
    )
    sections = [f"## Process\n{process.strip()}", f"## Your systems\n{systems.brief()}"]
    if data is not None:
        sections.append(data.section())
    if declared:
        sections.append(systems_section(declared, mask))
    if similar := reference_section(_query(process, data)):
        sections.append(similar)
    if catalog.tools:
        sections.append(
            "## Existing tools (use their names in expectations; they need no system)\n"
            + name_lines(catalog.describe())
        )
    sections.append(
        "## Builtin tools (always available; use their names in expectations)\n"
        + ", ".join(builtin_catalog().names())
    )
    if guidance.strip():
        sections.append(f"## Additional requirements\n{guidance.strip()}")
    return "\n\n".join(sections)


def _query(process: str, data: ProjectData | None) -> str:
    """The text similar packages are looked up with: the process, and the names in the data."""

    return process if data is None else f"{process} {data.keywords()}"


def issue_facts(issues: Sequence[BlueprintIssue]) -> list[dict[str, str]]:
    """Issues as plain dicts: `severity`, `path`, `message` and the `DF-V-nnn` `code` the message ends with."""

    facts = []
    for issue in issues:
        code = re.search(r"\[(DF-V-\d{3})\]\s*$", issue.message)
        facts.append(
            {
                "severity": issue.severity,
                "path": issue.path,
                "message": issue.message,
                "code": code[1] if code else "",
            }
        )
    return facts


async def _acceptance(
    brief: str,
    *,
    process: str,
    existing: set[str],
    model: str | BaseChatModel | None,
    max_repairs: int,
    data: ProjectData | None = None,
    on_event: Callable[[str, Mapping[str, Any]], None] | None = None,
    declared: Sequence[DeclaredSystem] = (),
) -> AcceptanceDesign:
    designer: Agent[None, AcceptanceDesign] = Agent(
        "test_designer",
        description="Writes acceptance tests and sample data for a process.",
        instructions=TEST_DESIGNER_INSTRUCTIONS + (TEST_DESIGNER_DATA_INSTRUCTIONS if data else ""),
        model=model,
        output_type=AcceptanceDesign,
    )
    prompt = brief
    problems: list[BlueprintIssue] = []
    for round_ in range(max_repairs + 1):
        try:
            draft = (await designer.run(prompt)).output
        except StructuredOutputError as exc:
            problems = [BlueprintIssue("error", "$", f"no acceptance tests were returned ({exc})")]
            continue
        problems = acceptance_problems(draft, existing, data, process=process, declared=declared)
        if on_event is not None and problems:
            facts = {"source": "acceptance", "round": round_ + 1, "issues": issue_facts(problems)}
            on_event("design.validation", facts)
        if not problems:
            if on_event is not None:
                on_event(
                    "design.acceptance",
                    {
                        "criteria": len(draft.criteria),
                        "cases": [_case_id(c.id, i) for i, c in enumerate(draft.cases)],
                        "systems": [s.key for s in draft.systems],
                        "files": [n.file for n in draft.data_notes],
                        "repairs": round_,
                    },
                )
            return draft
        listing = "\n".join(f"- {issue.path}: {issue.message}" for issue in problems)
        prompt = (
            f"{brief}\n\n## Your previous answer\n```json\n{draft.model_dump_json(indent=2)}\n```"
            f"\n\n## Problems to fix\n{listing}\n\nReturn the corrected acceptance tests."
        )
    raise BlueprintError(
        f"The acceptance tests still have problems after {max_repairs} repair round(s)", problems
    )


async def design_architecture(
    project: ProjectDesign,
    *,
    model: str | BaseChatModel | None,
    max_repairs: int,
    suffix: str = "",
    on_event: Callable[[str, Mapping[str, Any]], None] | None = None,
    reshape: Callable[[Blueprint], Blueprint] | None = None,
) -> ProjectDesign:
    """`project` with the architect's blueprint for its cases, tools and data; `reshape` adjusts every draft first."""

    tools = project.own_tools()
    data = project.data
    catalog = ToolCatalog.from_source(tools)
    brief = design_brief(
        project.process,
        catalog,
        project.skills,
        project.guidance,
        [_acceptance_section(project), reference_section(_query(project.process, data))],
    )

    def adjust(draft: Blueprint) -> Blueprint:
        finished = _finish(
            draft if reshape is None else reshape(draft),
            project.systems,
            model,
            project.approvals,
            project.triggers,
            data,
            project.rules,
            project.declared,
        )
        open_actions = ungated_action_issues(finished, catalog)
        if open_actions:
            raise BlueprintError("The design can act without a person's approval", open_actions)
        if data is not None:
            problems = blueprint_data_problems(finished, data, _data_entries(finished, project))
            if problems:
                raise BlueprintError("The design disagrees with the data", problems)
        return finished

    def report(round_: int, errors: Sequence[BlueprintIssue]) -> None:
        if on_event is not None:
            on_event(
                "design.validation",
                {"source": "architecture", "round": round_, "issues": issue_facts(errors)},
            )

    blueprint = await draft_blueprint(
        architect_agent(model, with_data=data is not None),
        brief,
        tools=tools,
        skills=project.skills,
        max_repairs=max_repairs,
        prompt=f"{brief}\n\n{suffix}" if suffix else None,
        adjust=adjust,
        on_issues=report,
    )
    if data is not None:
        blueprint = blueprint.model_copy(update={"data": _data_entries(blueprint, project)})
    if on_event is not None:
        on_event(
            "design.architecture",
            {
                "name": blueprint.name,
                "agents": [spec.name for spec in blueprint.agents],
                "entrypoint": blueprint.resolved_entrypoint(),
                "connectors": sorted(blueprint.connectors),
            },
        )
    used = [system for system in project.systems if system.key in blueprint.connectors]
    return dataclasses.replace(
        project,
        blueprint=blueprint,
        connections=_connections(used, project.environment),
        evals=project.evals.model_copy(update={"name": blueprint.name}),
    )


def _data_entries(blueprint: Blueprint, project: ProjectDesign) -> list[DataBlueprint]:
    """The `data:` section for a blueprint on real files (the instructions name the columns)."""

    assert project.data is not None
    instructions = " ".join(spec.instructions or spec.description for spec in blueprint.agents)
    uses = {ref for spec in blueprint.agents for ref in spec.tools}
    writes = any(ref.startswith("connector:output.") for ref in uses)
    derived = project.data.entries(project.data_notes, instructions, writes_output=writes)
    return merge_data_entries(blueprint, derived)


def _acceptance_section(project: ProjectDesign) -> str:
    criteria = "\n".join(f"- {criterion}" for criterion in project.criteria)
    cases = "\n".join(
        f"- {case.id}: {' '.join(str(case.input).split())!r} -> {'; '.join(case.scorers)}"
        + (
            f"; decisions: {', '.join(_decision_text(d) for d in case.decisions)}"
            if case.decisions
            else ""
        )
        for case in project.evals
    )
    text = (
        f"## Acceptance criteria\n{criteria}\n\n"
        "## Eval cases (design the smallest system that passes all of them)\n"
        f"{cases}\n(contains: the final answer includes the text; tool: the tool is called, "
        "by its name; approval_requested: the run pauses for a human decision on that tool; "
        "no_approval: it does not; decisions: a human's answers to the pauses, in order, "
        "after each of which the run goes on. The answer is the last agent's final message: "
        "it must state the values the contains and judge checks look for)"
    )
    if project.triggers:
        starts = "\n".join(
            f"- {t.name} ({t.kind}): each event sends {' '.join(t.input.split())!r}"
            for t in project.triggers
        )
        text += (
            "\n\n## Triggers (they start runs by themselves and are added to the blueprint; "
            "design the system to handle such a request)\n" + starts
        )
    if project.approvals:
        steps = []
        for step in project.approvals:
            condition = f"when {step.when.strip()}" if step.when.strip() else "always"
            steps.append(
                f"- {step.name}: {' '.join(step.description.split())} (waits for the person "
                f"{condition}; otherwise approved straight away)"
            )
        text += (
            "\n\n## Approval steps (tools; list them in agents' tools exactly as written)\n"
            + "\n".join(steps)
            + "\nTell agents to call the step when it is due, with the values it decides on; "
            "it waits for the person by itself, and a rejection comes back as the reviewer's "
            "note. Its approval is added to the blueprint."
        )
    if project.systems:
        lines = []
        for system in project.systems:
            _, writes = files.workspace_operations(system)
            for reference in files.workspace_references(system):
                operation = reference.rpartition(".")[2]
                change = " [changes data; approval added]" if operation in writes else ""
                lines.append(
                    f"- {reference} (tool {files.tool_name(reference)}){change}: "
                    f"{files.describe_system(system)}"
                )
        text += (
            "\n\n## System tools (list them in agents' tools exactly as written)\n"
            + "\n".join(lines)
            + "\nThe query tools take one read-only SQL statement; the schema tools list a "
            "database's tables, columns and keys. Write instructions in terms of the records "
            "and the process, and name the tables an agent needs. A call of a "
            "tool that changes data waits for a human's approval by itself: tell agents to "
            "call it when the step is due, not to ask for approval."
        )
    if project.declared:
        text += f"\n\n{systems_section(project.declared, project.mask)}"
    if project.data is not None:
        text += f"\n\n{project.data.section()}"
        if project.data_notes:
            roles = "\n".join(
                f"- {n.file}: {n.role}" + (f", {n.handling}" if n.handling else "")
                for n in project.data_notes
            )
            text += f"\n\n## What each file is for (from the test designer)\n{roles}"
    return text


def _finish(
    blueprint: Blueprint,
    systems: Sequence[SystemDesign],
    model: str | BaseChatModel | None,
    approvals: Sequence[ApprovalStepDesign] = (),
    triggers: Sequence[TriggerDesign] = (),
    data: ProjectData | None = None,
    rules: Sequence[RuleDesign] = (),
    live_systems: Sequence[DeclaredSystem] = (),
) -> Blueprint:
    """Wire workspaces, connectors, gates, rules, triggers and the default model into `blueprint`.

    Rules are marked as computing only and never wait for a person (a rule named `reply_action`
    reads like a send), and a system tool written by its name (`orders_query`) becomes its
    reference.
    """

    references = {
        files.tool_name(ref): ref
        for system in systems
        for ref in files.workspace_references(system)
    }
    references.update(data.references() if data is not None else {})
    references.update(declared_tools(live_systems))
    blueprint = blueprint.model_copy(
        update={
            "agents": [
                spec.model_copy(
                    update={
                        "tools": _with_listing(
                            [references.get(t, t) for t in spec.tools],
                            data.connectors if data is not None else (),
                        )
                    }
                )
                for spec in blueprint.agents
            ],
            "approvals": [
                a.model_copy(update={"tool": references.get(a.tool, a.tool)})
                for a in blueprint.approvals
            ],
        }
    )
    blueprint = share_check_tools(blueprint, {rule.name for rule in rules})
    used = {ref for spec in blueprint.agents for ref in spec.tools}
    steps = {step.name: step for step in approvals if step.name in used}
    computing = {rule.name for rule in rules}
    declared = [a for a in blueprint.approvals if a.tool not in steps and a.tool not in computing]
    declared += [
        ApprovalBlueprint(tool=name, when=" ".join(step.when.split()) or None)
        for name, step in steps.items()
    ]
    connectors = dict(blueprint.connectors)
    writes: list[str] = []
    for system in systems:
        if used & set(files.workspace_references(system)):
            config = files.workspace_config(system, on_files=data is not None)
            connectors.setdefault(system.key, config)
        _, operations = files.workspace_operations(system)
        writes += [f"connector:{system.key}.{op}" for op in operations]
    for connector in data.connectors if data is not None else ():
        if used & set(connector.references()):
            connectors.setdefault(connector.key, copy.deepcopy(dict(connector.config)))
        writes += [f"connector:{connector.key}.{op}" for op in connector.writes]
    # A declared system is live and read-only: its instance has no tool that changes data.
    for live in live_systems:
        if used & set(live.references()):
            connectors.setdefault(live.key, live.connector_entry())
    # A change to a system waits for a human on every call: no condition un-gates it.
    declared = [
        a.model_copy(update={"when": None}) if a.tool in writes and a.when else a for a in declared
    ]
    gated = {approval.tool for approval in declared}
    declared += [ApprovalBlueprint(tool=r) for r in writes if r in used and r not in gated]
    risks = {
        rule.name: ToolRisk(external_action=False)
        for rule in rules
        if rule.name in used
        and rule.name not in blueprint.tool_risks
        and infer_tool_risk(rule.name).external_action
    }
    update: dict[str, Any] = {
        "connectors": connectors,
        "tool_risks": {**blueprint.tool_risks, **risks},
        "approvals": declared,
        "triggers": [TriggerBlueprint.model_validate(files.trigger_entry(t)) for t in triggers],
    }
    if triggers:  # a process that starts by itself never runs unbounded
        update["limits"] = LimitsBlueprint(max_requests=_TRIGGERED_MAX_REQUESTS)
    if isinstance(model, str) and model != "default":
        update["defaults"] = blueprint.defaults.model_copy(update={"model": model})
    finished = blueprint.model_copy(update=update)
    live_keys = [system.key for system in live_systems]
    problems = outside_workspace(finished, live_keys) + _toolkit_folders(finished, data is not None)
    if problems:
        raise BlueprintError("The design uses systems outside its local workspace", problems)
    return finished


def _with_listing(tools: Sequence[str], connectors: Sequence[Any]) -> list[str]:
    """`tools` without repeats, plus `list_files` of every data connector the agent reads from."""

    chosen = list(dict.fromkeys(tools))
    for connector in connectors:
        listing = f"connector:{connector.key}.list_files"
        reads = {f"connector:{connector.key}.{op}" for op in connector.reads} - {listing}
        if listing in connector.references() and reads & set(chosen) and listing not in chosen:
            chosen.append(listing)
    return chosen


def _toolkit_folders(blueprint: Blueprint, data: bool = False) -> list[BlueprintIssue]:
    """Toolkit folders that do not exist before the project is written (`data/...` with the user's files does)."""

    return [
        BlueprintIssue(
            "error",
            f"toolkits.{toolkit}.{option}",
            f"{value!r} does not exist in the project's workspace; read and write the sample "
            "data with the system tools listed, and do not use builtin tools that need a folder",
        )
        for toolkit, options in blueprint.toolkits.items()
        for option, value in options.items()
        if option in ("root", "path", "database")
        and isinstance(value, str)
        and not (data and files.in_data_folder(value))
    ]


def outside_workspace(blueprint: Blueprint, live: Sequence[str] = ()) -> list[BlueprintIssue]:
    """Connector tools of instances not on the local workspace, which an evaluation must never reach.

    Declared instances in `live` are allowed while they are `read_only: true`.
    """

    issues: list[BlueprintIssue] = []
    for i, spec in enumerate(blueprint.agents):
        for j, reference in enumerate(spec.tools):
            instance = reference.removeprefix("connector:").partition(".")[0]
            options = blueprint.connectors.get(instance) or {}
            if instance in live and options.get("read_only") is True:
                continue
            if reference.startswith("connector:") and options.get("use") != "workspace":
                issues.append(
                    BlueprintIssue(
                        "error",
                        f"agents[{i}].tools[{j}]",
                        f"{reference!r} is not a tool of a system of this process; use the "
                        "system tools listed",
                    )
                )
    return issues


def acceptance_problems(
    draft: AcceptanceDesign,
    existing: set[str],
    data: ProjectData | None = None,
    *,
    process: str = "",
    declared: Sequence[DeclaredSystem] = (),
) -> list[BlueprintIssue]:
    """Everything the test designer must fix, as issues with a path into its answer.

    With `data` invented file shares are refused and the cases are checked against the files;
    with `process` a `contains` code must be stated somewhere the system can see it.
    """

    from awa.toolkits import catalog as builtin_catalog

    problems: list[BlueprintIssue] = []

    def add(path: str, message: str) -> None:
        problems.append(BlueprintIssue("error", path, message))

    if not any(criterion.strip() for criterion in draft.criteria):
        add("criteria", "list at least one acceptance criterion")
    if not draft.cases:
        add("cases", "write at least one eval case")
    tools, writes = _system_problems(draft, data, declared, add)
    writes |= existing
    _rule_problems(draft, tools, existing, add)
    known = set(tools) | existing | set(declared_tools(declared))
    if data is not None:
        known |= set(data.references())
        writes |= data.writing() | {
            e.name for e in builtin_catalog().entries() if e.requires_approval
        }
    problems += _trigger_problems(draft.triggers)
    problems += _approval_step_problems(draft.approvals, known)
    writes |= {step.name for step in draft.approvals if files.is_name(step.name)}
    known |= set(builtin_catalog().names())
    requests = _case_problems(draft, known, writes, data, process, add)
    if draft.triggers and not any(case.trigger for case in draft.cases):
        add("cases", "a case must go through a trigger: set its trigger and payload")
    if data is not None and not problems:
        problems += acceptance_data_problems(draft, data, requests)
    return problems


def _system_problems(
    draft: AcceptanceDesign,
    data: ProjectData | None,
    declared: Sequence[DeclaredSystem],
    add: Callable[[str, str], None],
) -> tuple[list[str], set[str]]:
    """Check the invented systems; return the names of their tools and of those that write."""

    tools: list[str] = []
    writes: set[str] = set()
    keys: list[str] = []
    taken = {c.key for c in systems_from_profile(data.profile)} if data is not None else set()
    live = {system.key: system for system in declared}
    for i, system in enumerate(draft.systems):
        path = f"systems[{i}]"
        if system.key in live:
            add(
                f"{path}.key",
                f"{live[system.key].name} ({system.key}) is a real system the user declared: do "
                "not describe it under systems; use its tools "
                f"{', '.join(declared_tools([live[system.key]]))} as listed",
            )
            continue
        if data is not None and (system.kind == "files" or system.key in taken):
            add(
                path,
                "the files are real: do not describe them as a system (no file share, no key "
                f"{', '.join(sorted(taken))}); put what the process does with each file into "
                "data_notes",
            )
            continue
        if not files.is_name(system.key) or len(system.key) > 32:
            add(f"{path}.key", f"{system.key!r} is not a snake_case name (at most 32 characters)")
            continue
        if system.key in keys:
            add(f"{path}.key", f"key {system.key!r} is used twice")
        keys.append(system.key)
        _, operations = files.workspace_operations(system)
        for reference in files.workspace_references(system):
            tools.append(files.tool_name(reference))
            if reference.rpartition(".")[2] in operations:
                writes.add(files.tool_name(reference))
        if not system.tables:
            add(f"{path}.tables", "give at least one table")
        _table_problems(system, path, add)
    return tools, writes


def _rule_problems(
    draft: AcceptanceDesign, tools: list[str], existing: set[str], add: Callable[[str, str], None]
) -> None:
    """Check the rules and run every example of the sound ones; adds the rule names to `tools`."""

    sound: list[RuleDesign] = []
    for i, rule in enumerate(draft.rules):
        path = f"rules[{i}]"
        if not files.is_tool_name(rule.name):
            add(f"{path}.name", _name_problem(rule.name))
            continue
        if rule.name in tools or rule.name in existing:
            add(f"{path}.name", f"the tool name {rule.name!r} is taken")
        tools.append(rule.name)
        arguments = [argument.name for argument in rule.arguments]
        bad = [n for n in arguments if not files.is_tool_name(n) or arguments.count(n) > 1]
        if bad:
            add(f"{path}.arguments", f"argument {bad[0]!r} is not a unique snake_case name")
            continue
        problem = files.rule_problem(rule)
        if problem:
            add(f"{path}.expression", problem)
        elif len(rule.examples) < 2:
            add(f"{path}.examples", "give at least two examples")
        else:
            sound.append(rule)
    if not sound:
        return
    # Every example of every sound rule is run now, so a wrong one is fixed before any agent.
    try:
        failures = files.rule_failures(sound)
    except (SyntaxError, BlueprintError) as exc:  # rules the generator cannot compile
        add("rules", f"the rules do not compile into tools: {exc}")
        failures = []
    for name, index, why in failures:
        position = next(i for i, rule in enumerate(draft.rules) if rule.name == name)
        add(f"rules[{position}].examples[{index}]", why)


def _case_problems(
    draft: AcceptanceDesign,
    known: set[str],
    writes: set[str],
    data: ProjectData | None,
    process: str,
    add: Callable[[str, str], None],
) -> list[str]:
    """Check the eval cases; return the request each case sends, in case order."""

    sample = " ".join(
        row for system in draft.systems for table in system.tables for row in table.rows
    )
    rules = _rules_text(draft)
    ids: list[str] = []
    requests: list[str] = []
    for i, case in enumerate(draft.cases):
        path = f"cases[{i}]"
        case_id = _case_id(case.id, i)
        if case_id in ids:
            add(f"{path}.id", f"case id {case_id!r} is used twice")
        ids.append(case_id)
        request = _request(case, draft.triggers)
        if case.trigger:
            chosen = next((t for t in draft.triggers if t.name == case.trigger), None)
            try:
                if chosen is None:
                    raise ValueError(f"unknown trigger {case.trigger!r}")
                files.trigger_request(chosen, case.payload)
            except ValueError as exc:
                add(f"{path}.trigger", str(exc))
        elif not case.input.strip():
            add(f"{path}.input", "give the request the case sends")
        if not case.expectations:
            add(f"{path}.expectations", "give at least one expectation")
        sources = _case_text(case, request, sample)
        requests.append(request)
        for j, expectation in enumerate(case.expectations):
            problem = _expectation_problem(expectation, known, writes)
            if not problem:
                problem = _wording_problem(expectation, sources)
                if problem and data is not None and _in_data(expectation, data):
                    problem = ""
            if not problem and process:
                problem = _code_problem(expectation, f"{process} {sources} {rules}", data)
            if problem:
                add(f"{path}.expectations[{j}]", problem)
            if data is None and (expectation.check.strip() or expectation.file.strip()):
                add(
                    f"{path}.expectations[{j}]",
                    "check and file are for real data files; without them put the value in value",
                )
        _decision_problems(case, path, add)
        _balance_problems(case, path, add)
    return requests


def _in_data(expectation: Expectation, data: ProjectData) -> bool:
    """Whether a `contains` phrase occurs in the real data (then it is not the system's wording)."""

    from awa.data import grounded

    phrase = " ".join(expectation.value.split())
    return expectation.kind == "contains" and grounded(phrase, data.profile)


def _trigger_problems(triggers: Sequence[TriggerDesign]) -> list[BlueprintIssue]:
    problems: list[BlueprintIssue] = []
    for i, trigger in enumerate(triggers):
        found = files.trigger_problems(trigger)
        if sum(other.name == trigger.name for other in triggers) > 1:
            found.append((".name", f"trigger name {trigger.name!r} is used twice"))
        problems += [BlueprintIssue("error", f"triggers[{i}]{where}", why) for where, why in found]
    return problems


def _request(case: CaseDesign, triggers: Sequence[TriggerDesign]) -> str:
    """The request a case sends: its input, or its trigger's input filled with its payload
    (empty when the trigger is unknown or the payload does not fit; reported by the checks)."""

    if not case.trigger:
        return case.input
    trigger = next((t for t in triggers if t.name == case.trigger), None)
    try:
        return files.trigger_request(trigger, case.payload) if trigger else ""
    except ValueError:
        return ""


def _name_problem(name: str) -> str:
    """Why `name` cannot name a rule or an approval step."""

    if name in files.RESERVED_NAMES:
        return f"{name!r} is a reserved name; choose another"
    return f"{name!r} is not a snake_case name"


def _approval_step_problems(
    steps: Sequence[ApprovalStepDesign], taken: set[str]
) -> list[BlueprintIssue]:
    """Invalid or taken approval step names and invalid conditions; adds valid names to `taken`."""

    found: list[tuple[str, str]] = []
    for i, step in enumerate(steps):
        path = f"approvals[{i}]"
        if not files.is_tool_name(step.name):
            found.append((f"{path}.name", _name_problem(step.name)))
            continue
        if step.name in taken:
            found.append((f"{path}.name", f"the tool name {step.name!r} is taken"))
        taken.add(step.name)
        found += [(f"{path}{where}", why) for where, why in files.approval_problems(step)]
    return [BlueprintIssue("error", where, why) for where, why in found]


def _balance_problems(case: CaseDesign, path: str, add: Callable[[str, str], None]) -> None:
    """A case the build could only pass by chance, or never: no deterministic check, more than
    one judge, a long judge rubric, or a tool both expected and refused."""

    judges = [j for j, e in enumerate(case.expectations) if e.kind == "judge"]
    if case.expectations and len(judges) == len(case.expectations):
        add(
            f"{path}.expectations",
            "give at least one deterministic expectation (contains, regex, tool_called, "
            "tool_not_called, approval, no_approval) besides the judge",
        )
    for j in judges[1:]:
        add(
            f"{path}.expectations[{j}]",
            "at most one judge expectation per case: merge the qualities into one short rubric "
            "or check them with contains, regex or tool expectations",
        )
    for j in judges:
        words = len(case.expectations[j].value.split())
        if words > _JUDGE_RUBRIC_WORDS:
            add(
                f"{path}.expectations[{j}]",
                f"the judge rubric has {words} words; state one pass/fail criterion in at most "
                f"{_JUDGE_RUBRIC_WORDS} words",
            )
    refused = {e.value.strip() for e in case.expectations if e.kind == "tool_not_called"}
    for j, expectation in enumerate(case.expectations):
        tool = expectation.value.strip()
        if expectation.kind in ("tool_called", "approval") and tool in refused:
            add(
                f"{path}.expectations[{j}]",
                f"{tool!r} is expected ({expectation.kind}) and also expected not to be called; "
                "keep one",
            )
    for k, decision in enumerate(case.decisions):
        if decision.tool.strip() in refused:
            add(
                f"{path}.decisions[{k}].tool",
                f"{decision.tool.strip()!r} is expected not to be called, so it never pauses; "
                "remove the decision or the tool_not_called expectation",
            )


def _decision_problems(case: CaseDesign, path: str, add: Callable[[str, str], None]) -> None:
    """What cannot happen: each decision answers one pause, a run with more pauses than
    decisions ends paused with no answer, and a tool cannot both wait and not wait."""

    asked = [e.value.strip() for e in case.expectations if e.kind == "approval"]
    not_asked = {e.value.strip() for e in case.expectations if e.kind == "no_approval"}
    both = sorted(set(asked) & not_asked)
    if both:
        add(f"{path}.expectations", f"{', '.join(both)} cannot both wait and not wait for a human")
    repeated: set[str] = set()
    for k, decision in enumerate(case.decisions):
        tool = decision.tool.strip()
        if tool and tool not in asked:
            add(
                f"{path}.decisions[{k}].tool",
                f"{tool!r} has no approval expectation in this case; name "
                "the tool of the pause the decision answers",
            )
        if decision.repeat and not tool:
            add(f"{path}.decisions[{k}].repeat", "a repeating decision names its tool")
        elif decision.repeat and tool in repeated:
            add(f"{path}.decisions[{k}].repeat", f"{tool!r} has two repeating decisions")
        elif decision.repeat:
            repeated.add(tool)
    # A repeating decision answers every pause of its tool, however many there are.
    decisions = sum(decision.tool.strip() not in repeated for decision in case.decisions)
    asked = [tool for tool in asked if tool not in repeated]
    if decisions > len(asked):
        add(
            f"{path}.decisions",
            f"{decisions} decision(s) but {len(asked)} approval expectation(s): each "
            "decision answers one pause; list an approval expectation for each, or remove "
            "the decisions the run never asks for",
        )
    elif len(asked) > decisions + 1:
        add(
            f"{path}.decisions",
            f"{len(asked)} approvals but {decisions} decision(s): the run ends paused at "
            f"approval {decisions + 1} ({asked[decisions]}) and never reaches the ones "
            "after it; give a decision for every approval but the last",
        )
    answer = sorted({e.kind for e in case.expectations if e.kind in _ANSWER_KINDS})
    if answer and len(asked) > decisions:
        add(
            f"{path}.expectations",
            f"the run ends paused at {asked[decisions]} without an answer to check with "
            f"{', '.join(answer)}; add a decision for that approval, or drop the answer "
            "checks",
        )


def _table_problems(system: SystemDesign, path: str, add: Callable[[str, str], None]) -> None:
    tables = [table.name for table in system.tables]
    for j, table in enumerate(system.tables):
        where = f"{path}.tables[{j}]"
        if not files.is_name(table.name) or tables.count(table.name) > 1:
            add(f"{where}.name", f"{table.name!r} is not a unique snake_case table name")
            continue
        columns = [column.name for column in table.columns]
        bad = [n for n in columns if not files.is_name(n) or columns.count(n) > 1]
        if not columns or bad:
            add(
                f"{where}.columns", f"give unique snake_case columns (problem: {bad[:1] or 'none'})"
            )
            continue
        try:
            files.table_rows(table)
        except ValueError as exc:
            add(f"{where}.rows", str(exc))


def _expectation_problem(expectation: Expectation, known: set[str], writes: set[str]) -> str:
    value = expectation.value.strip()
    if expectation.kind in ("tool_called", "tool_not_called") and value not in known:
        return f"unknown tool {value!r}; use a system tool, a rule or a builtin tool"
    if expectation.kind in ("approval", "no_approval") and not value:
        return f"a {expectation.kind} expectation names the tool"
    if expectation.kind in ("approval", "no_approval") and value not in writes:
        return (
            f"{expectation.kind} expects a writing tool of a system (writes: true) or an "
            f"approval step, got {value!r}"
        )
    if expectation.kind in ("contains", "regex", "judge") and not value:
        return f"a {expectation.kind} expectation needs a value"
    if expectation.kind == "regex":
        try:
            re.compile(value)
        except re.error as exc:
            return f"invalid regex {value!r}: {exc}"
    return ""


def _case_text(case: CaseDesign, request: str, sample: str) -> str:
    """The text a case's answer can quote: the sample data, the request and the decision notes."""

    notes = " ".join(decision.note for decision in case.decisions)
    return " ".join(f"{sample} {request} {notes}".split()).casefold()


def _wording_problem(expectation: Expectation, sources: str) -> str:
    """A `contains` phrase of three or more words that no source has: the system words it
    its own way, so the check fails on a correct answer (`ask HR to check`)."""

    value = " ".join(expectation.value.split())
    if expectation.kind != "contains" or len(value.split()) < 3:
        return ""
    if any(char.isdigit() for char in value) or value.casefold() in sources:
        return ""
    return (
        f"contains {value!r} is wording the answer may phrase differently; check for a name, "
        "id, date, amount or status word from the sample data or the request, and put the "
        "wording in a judge expectation"
    )


def _rules_text(draft: AcceptanceDesign) -> str:
    """What the rules and approval steps say: their names, descriptions, expressions and results."""

    parts: list[str] = []
    for rule in draft.rules:
        parts += [rule.name, rule.description, rule.expression, *(e.result for e in rule.examples)]
    for step in draft.approvals:
        parts += [step.name, step.description]
    return " ".join(parts)


def _code_problem(expectation: Expectation, sources: str, data: ProjectData | None) -> str:
    """A `contains` code (`on_hold`) that no source states: the system invented a vocabulary and
    the check fails a correct answer (`on_hold`, `missing_invoice_number`)."""

    value = expectation.value.strip()
    if expectation.kind != "contains" or not is_code(value) or declares(value, sources):
        return ""
    if data is not None and _in_data(expectation, data):
        return ""
    return (
        f"contains {value!r} is a code that no source states (the process, a rule's result, the "
        "data or the request); expect a plain word the process uses (hold, booked) or a value "
        "of the data, or declare the code as the result of a rule"
    )


def _case_id(text: str, index: int) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or f"case-{index + 1}"


def _cases(cases: Sequence[CaseDesign], triggers: Sequence[TriggerDesign]) -> list[EvalCase]:
    return [
        EvalCase(
            id=_case_id(case.id, index),
            input=_request(case, triggers).strip(),
            scorers=tuple(dict.fromkeys(e.spec() for e in case.expectations)),
            decisions=tuple(_decision(d) for d in case.decisions),
        )
        for index, case in enumerate(cases)
    ]


def _decision(decision: DecisionDesign) -> dict[str, Any]:
    note = " ".join(decision.note.split())
    result: dict[str, Any] = {"action": decision.action}
    if decision.tool.strip():
        result["tool"] = decision.tool.strip()
        if decision.repeat:
            result["repeat"] = True
    if note:
        result["note"] = note
    return result


def _decision_text(decision: Mapping[str, Any]) -> str:
    text = str(decision.get("action"))
    if decision.get("tool"):
        text += f" {decision['tool']}" + (" (every pause)" if decision.get("repeat") else "")
    note = decision.get("note")
    return f"{text} ({note!r})" if note else text


def _resolve_connector(system: SystemDesign, environment: EnvironmentReport) -> SystemDesign:
    """Keep `connector` only when it names a connector of the environment (and not the
    local workspace, which every system uses until it is connected)."""

    name = (system.connector or "").strip().removeprefix("connector:").partition(".")[0]
    known = {status.name for status in environment.connectors} - _LOCAL_CONNECTORS
    return system.model_copy(update={"connector": name if name in known else None})


def _connections(
    systems: Sequence[SystemDesign], environment: EnvironmentReport
) -> list[ConnectionStep]:
    statuses = {status.name: status for status in environment.connectors}
    steps: list[ConnectionStep] = []
    for system in systems:
        status = statuses.get(system.connector) if system.connector else None
        connector = GENERIC_CONNECTORS[system.kind] if status is None else status.name
        config, env_vars, login, switch = _production(system, connector)
        kind: ConnectionStatus = "map"
        if status is not None:
            login = ()
            if connector in _RENAMES:
                # One instance per system: its own variable, whatever the environment has set.
                kind = "configure"
            else:
                kind = "ready" if status.configured else "configure"
                env_vars = tuple(status.missing)
        step = ConnectionStep(
            system=" ".join(system.name.split()),
            connector=connector,
            env_vars=env_vars,
            status=kind,
            instructions="",
            tools=files.workspace_references(system),
            config=config,
            switch=switch,
            login=login,
        )
        steps.append(
            dataclasses.replace(step, instructions=files.step_instructions(step, system.key))
        )
    return steps


def _production(
    system: SystemDesign, connector: str
) -> tuple[dict[str, Any], tuple[str, ...], tuple[str, ...], dict[str, tuple[str, ...]]]:
    """The connected `connectors:` entry, its variables, its login variables and the changing references."""

    from awa.connectors import get_connector

    spec = get_connector(connector)
    own = f"{system.key.upper()}_{_SUFFIXES[connector]}" if connector in _RENAMES else ""
    config: dict[str, Any] = {"use": connector}
    if own:
        config["env"] = {_RENAMES[connector]: own}
    if "description" in spec.options:
        config["description"] = files.describe_system(system)
    if spec.configure is not None:
        spec = spec.configure(spec, system.key, config)
    renames: Mapping[str, str] = config.get("env", {})
    required = [own] if own else [renames.get(v.name, v.name) for v in spec.env if v.required]
    login: tuple[str, ...] = ()
    if spec.auth:
        first = tuple(v.name for v in spec.auth[0].variables() if v.required)
        if spec.auth_optional:
            login = first
        else:
            required += first
    reads, writes = files.workspace_operations(system)
    offered = {operation.name for operation in spec.operations}
    switch: dict[str, tuple[str, ...]] = {}
    if spec.dynamic or not set(reads) | set(writes) <= offered:
        changes = tuple(f"connector:{system.key}.{op.name}" for op in spec.writes())
        for operation in reads:
            switch[f"connector:{system.key}.{operation}"] = (f"connector:{system.key}",)
        for operation in writes:
            switch[f"connector:{system.key}.{operation}"] = () if spec.dynamic else changes
    return config, tuple(dict.fromkeys(required)), login, switch
