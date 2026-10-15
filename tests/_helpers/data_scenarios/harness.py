"""What the data-first scenario tests share: the input files of a scenario copied into a folder,
the check of a profile fact, and the scripted fake models.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from awa.data import DataProfile, column_key, profile
from awa.data._model import FileProfile
from awa.testing import FunctionChatModel, ModelCall, structured_output, tool_call

from .scenarios import Case, Fact, Scenario, Script

REPO_ROOT = Path(__file__).resolve().parents[3]
CREATED = "2026-10-05T00:00:00Z"
# Where a built project keeps the user's files below the toolkit root `data`.
FILES_PREFIX = "files/"
READ_TOOLS = frozenset(
    [
        "extract_pdf_text",
        "extract_docx_text",
        "read_excel",
        "query_excel",
        "read_csv",
        "query_csv",
        "json_query",
        "read_file",
        "sql_query",
    ]
)
GATED_TOOLS = frozenset({"write_file", "sql_execute", "send_email", "warehouse_execute"})
# The database system's tool and the builtin it equals.
DATABASE_READ = {"warehouse_query": "sql_query"}

# ------------------------------------------------------------------------ the input files


def source_of(spec: str, generated: Path) -> Path:
    """The file a scenario names: `repo:<path from the repository root>` or `gen:<generated name>`."""

    kind, _, relative = spec.partition(":")
    return (REPO_ROOT if kind == "repo" else generated) / relative


def materialize(scenario: Scenario, generated: Path, target: Path) -> Path:
    """Copy the scenario's input files flat into `target/in` (so the display names are the file
    names); the repository's own files are never handed to code that might write."""

    folder = target / "in"
    folder.mkdir(parents=True)
    for spec in scenario.files:
        shutil.copy2(source_of(spec, generated), folder / spec.rpartition("/")[2])
    return folder


def digests(folder: Path) -> dict[str, tuple[str, int]]:
    """sha256 and modification time (ns) of every file below `folder`."""

    return {
        p.relative_to(folder).as_posix(): (
            hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_mtime_ns,
        )
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def profile_of(folder: Path) -> DataProfile:
    return profile(folder, created=CREATED)


# ------------------------------------------------------------------------ facts


def walk_files(data: DataProfile) -> Iterator[FileProfile]:
    stack = list(reversed(data.files))
    while stack:
        item = stack.pop()
        yield item
        stack.extend(reversed(item.children))


def find_file(data: DataProfile, name: str) -> FileProfile | None:
    return next((f for f in walk_files(data) if f.path.rpartition("/")[2] == name), None)


def _named(item: object, token: str) -> bool:
    wanted = token.casefold()
    for attribute in ("name", "sheet", "key", "path", "code"):
        value = getattr(item, attribute, None)
        if isinstance(value, str) and (
            value.casefold() == wanted
            or (attribute == "name" and column_key(value) == column_key(token))
        ):
            return True
    return False


def _step(obj: Any, token: str) -> Any:
    if isinstance(obj, list):
        if token.isdigit():
            return obj[int(token)]
        for item in obj:
            if _named(item, token):
                return item
        raise LookupError(f"nothing called {token!r} in a list of {len(obj)}")
    if isinstance(obj, dict):
        return obj[token]
    if obj is None:
        raise LookupError("empty")
    return getattr(obj, token)


def lookup(obj: Any, path: str) -> Any:
    for token in path.split("."):
        obj = _step(obj, token)
    return obj


def _contains(actual: Any, value: object) -> bool:
    if isinstance(actual, str):
        return str(value) in actual
    return any(item == value or _named(item, str(value)) for item in actual)


def check_fact(data: DataProfile, fact: Fact) -> str | None:
    """None when the profile shows what the fact says, else why not."""

    file = find_file(data, fact.file)
    if file is None:
        return f"no file {fact.file!r} in the profile ({[f.path for f in data.files]})"
    if fact.op in ("flag", "not_flag", "flag_at"):
        codes = [f.code for f in file.flags]
        if fact.op == "flag_at":
            hit = any((f.where or "").endswith("." + str(fact.value)) for f in file.flags)
        else:
            hit = fact.value in codes
        wanted = fact.op != "not_flag"
        return None if hit == wanted else f"{fact.op} {fact.value!r}: the flags are {codes}"
    try:
        actual = lookup(file, fact.path)
    except (LookupError, AttributeError, IndexError, KeyError) as exc:
        return f"{fact.file}: {fact.path} not found ({exc})"
    ok = {
        "eq": lambda: actual == fact.value,
        "in": lambda: actual in fact.value,  # type: ignore[operator]
        "ge": lambda: actual >= fact.value,  # type: ignore[operator]
        "has": lambda: _contains(actual, fact.value),
        "len": lambda: len(actual) == fact.value,
        "len_ge": lambda: len(actual) >= fact.value,  # type: ignore[operator]
        "not_contains": lambda: str(fact.value) not in actual,
    }[fact.op]()
    return (
        None if ok else f"{fact.file}: {fact.path} is {actual!r}, expected {fact.op} {fact.value!r}"
    )


def sample_values(data: DataProfile) -> set[str]:
    """Raw cell values a profile holds (examples, top values, sample rows) that a digest in
    `drop` mode must not show: at least 5 characters with a letter, and not a name the digest
    shows as structure anyway (files, sheets, columns, the keys of a JSON file)."""

    names: set[str] = set()
    values: set[str] = set()
    for file in walk_files(data):
        names |= {file.path, file.path.rpartition("/")[2]}
        names |= {part for part in re.split(r"[\W_]+", file.path) if part}  # `support-hours.md`
        if file.json_doc is not None:  # `support.sla_hours.urgent`: every key is structure
            for field in file.json_doc.paths:
                names |= {part for part in re.split(r"\[\]|\.", field.path) if part}
        for table in file.tables:
            names |= {table.sheet or "", table.name or ""}
            for column in table.columns:
                names |= {column.name, column.key}
                values |= set(column.examples) | {top.value for top in column.top}
            values |= {str(cell) for row in table.sample_rows for cell in row if cell is not None}
    return {v for v in values if len(v) >= 5 and re.search("[A-Za-z]", v) and v not in names}


def shows(text: str, value: str) -> bool:
    """True when `value` stands in `text` as a word or phrase of its own: the column name
    `Lieferantennr` does not show the cell value `Lieferant`, `mail x@y.de.` shows `x@y.de`."""

    return re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", text) is not None


def leaked_values(data: DataProfile, text: str) -> list[str]:
    """The raw sample values of `data` that `text` (a digest in `drop` mode) shows."""

    return sorted(v for v in sample_values(data) if shows(text, v))


# ------------------------------------------------------------------------ scripted designs


def acceptance_dict(scenario: Scenario) -> dict[str, Any]:
    """The fake test designer's answer: no systems (the files are the systems), rules, approval
    steps and the cases of the script."""

    script = scenario.script
    assert script is not None
    return {
        "criteria": [scenario.description],
        "systems": [],
        "rules": list(script.rules),
        "approvals": list(script.approvals),
        "cases": [
            {
                "id": case.id,
                "input": case.request,
                "expectations": [{"kind": k, "value": v} for k, v in case.expect],
                "decisions": [{"action": a, "tool": t} for a, t in case.decisions],
            }
            for case in script.cases
        ],
    }


def blueprint_dict(scenario: Scenario, *, wrong: bool = False) -> dict[str, Any]:
    """The fake architect's answer: one agent; `wrong` gives the first draft with the mistake."""

    script = scenario.script
    assert script is not None
    return {
        "name": scenario.slug.replace("-", "_"),
        "description": "Works on the files of the project as the instructions say.",
        "entrypoint": "agent",
        "agents": [
            {
                "name": script.agent,
                "description": scenario.shape[:200],
                "instructions": (script.wrong_instructions or script.instructions)
                if wrong
                else script.instructions,
                "tools": list(script.tools),
            }
        ],
        "toolkits": [{"toolkit": t, "option": o, "value": v} for t, o, v in script.toolkits],
    }


def gated(script: Script) -> set[str]:
    """Tools whose calls can pause for a person: the approval steps and the gated builtins."""

    return {str(step["name"]) for step in script.approvals} | GATED_TOOLS


def pauses(script: Script, case: Case) -> int:
    """How many calls of the case wait for a decision."""

    from awa.kernel.conditions import predicate

    count = 0
    steps = {str(step["name"]): step for step in script.approvals}
    for call in case.calls:
        if call.tool in GATED_TOOLS:
            count += 1
        elif call.tool in steps:
            when = str(steps[call.tool]["when"])
            count += not when or bool(predicate(when).evaluate(lambda name, a=call.args: a[name]))  # type: ignore[arg-type]
    return count


def system_prompt(call: ModelCall) -> str:
    first = call.messages[0]
    return str(first.content) if isinstance(first, SystemMessage) else ""


def role_of(call: ModelCall) -> str:
    """`designer`, `architect` or `agent`: which model of the build is asking."""

    from awa.blueprints._project import TEST_DESIGNER_INSTRUCTIONS
    from awa.blueprints.architect import ARCHITECT_INSTRUCTIONS

    system = system_prompt(call)
    if system.startswith(TEST_DESIGNER_INSTRUCTIONS[:40]):
        return "designer"
    if system.startswith(ARCHITECT_INSTRUCTIONS[:40]):
        return "architect"
    return "agent"


class ScriptedBuild:
    """One fake model for the whole build of a scenario: the test designer and the architect
    return the scripted design (the architect first with the mistake of the script, then, after
    its repair message, the right one); the designed agent makes the calls of the case whose
    request it was given, then answers."""

    def __init__(
        self, scenario: Scenario, *, prefix: str = FILES_PREFIX, fit: dict[str, Any] | None = None
    ) -> None:
        assert scenario.script is not None
        self.scenario, self.script, self.prefix, self.fit = scenario, scenario.script, prefix, fit
        self.roles: list[str] = []
        self.architect_prompts: list[str] = []
        self.model = FunctionChatModel(self._respond)

    def _respond(self, call: ModelCall) -> Any:
        from awa.blueprints import AcceptanceDesign, BlueprintDesign

        role = role_of(call)
        self.roles.append(role)
        if role == "designer":
            return structured_output(AcceptanceDesign, acceptance_dict(self.scenario))
        if role == "architect":
            asked = [str(m.content) for m in call.messages if not isinstance(m, SystemMessage)]
            self.architect_prompts.append("\n".join(asked))
            first = len(self.architect_prompts) == 1
            return structured_output(
                BlueprintDesign,
                blueprint_dict(self.scenario, wrong=first and bool(self.script.wrong_instructions)),
            )
        request = next(str(m.content) for m in call.messages if isinstance(m, HumanMessage))
        case = next((c for c in self.script.cases if c.request == request), None)
        if case is None:  # not a case of the script: the fit check of the description asks
            return structured_output("FitVerdict", self.fit) if self.fit else "unknown request"
        done = sum(isinstance(m, ToolMessage) for m in call.messages)
        if done < len(case.calls):
            step = case.calls[done]
            args = dict(step.args)
            if step.file:
                args["path"] = self.prefix + step.file
            return tool_call(step.tool, args)
        return case.answer

    def count(self, role: str) -> int:
        return self.roles.count(role)


# ------------------------------------------------------------------------ the real tools

_DATA_ANSWER = re.compile(r"[a-z ]+")


def real_tools(folder: Path) -> dict[str, Any]:
    """The read-only builtin tools on `folder`, the ones the scripted agents call."""

    from awa.toolkits import data_toolkit, document_toolkit, file_toolkit, sql_toolkit

    tools = [
        *data_toolkit(folder, requires_approval=False),
        *document_toolkit(folder),
        *file_toolkit(folder, read_only=True),
    ]
    database = next(iter(sorted(folder.glob("*.db"))), None)
    if database is not None:
        tools += [t for t in sql_toolkit(database) if t.name == "sql_query"]
    return {t.name: t for t in tools}


def run_real_calls(case: Case, tools: dict[str, Any]) -> list[str]:
    """Run the case's calls of read-only builtin tools on the real files; return their outputs."""

    outputs = []
    for call in case.calls:
        name = DATABASE_READ.get(call.tool, call.tool)
        if name in READ_TOOLS:
            args = {**call.args, **({"path": call.file} if call.file else {})}
            outputs.append(str(tools[name].invoke(args)))
    return outputs


def data_like(value: str) -> bool:
    """A value that claims to be data (an id, an amount, a name) and not a plain word."""

    return not _DATA_ANSWER.fullmatch(value)
