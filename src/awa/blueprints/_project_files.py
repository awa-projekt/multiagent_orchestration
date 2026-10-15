"""The files of a designed project, rendered deterministically from a `ProjectDesign`.

Code is generated only from validated names, `ast`-checked rule expressions and quoted
literals, so no model text becomes code; approval conditions stay expressions in the blueprint.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import keyword
import re
import sqlite3
import types
from collections.abc import Iterable, Iterator, Sequence
from contextlib import closing, contextmanager, suppress
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

from awa._internal.upstream.tools import BaseTool
from awa.blueprints.errors import BlueprintError

if TYPE_CHECKING:
    from awa.blueprints._project import (
        ApprovalStepDesign,
        ColumnDesign,
        ConnectionStep,
        ProjectDesign,
        RuleDesign,
        SystemDesign,
        TableDesign,
        TriggerDesign,
    )
    from awa.blueprints.schema import Blueprint
    from awa.evals.report import EvalReport

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_PYTHON_TYPES = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}
_SQL_TYPES = {"text": "TEXT", "integer": "INTEGER", "real": "REAL", "boolean": "INTEGER"}
_TRUE, _FALSE = {"true", "yes", "1"}, {"false", "no", "0"}
_STEP_MARKER = re.compile(r"<!-- awa:step (\{.*?\}) -->")
_STATUS_TITLES = {
    "ready": "connector ready",
    "configure": "connector needs settings",
    "map": "generic connector needs settings",
}
_MAX_EXPRESSION = 500
_RULE_FUNCTIONS = frozenset({"abs", "min", "max", "round"})
_RULE_NODES: tuple[type[ast.AST], ...] = (
    (ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.IfExp, ast.Call)
    + (ast.Name, ast.Load, ast.Constant, ast.Tuple, ast.List)
    + (ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd)
    + (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod)
    + (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn)
)

RESERVED_NAMES = frozenset({"items", "json", "tool", "one", "each", "fits", "any"})
"""Names a rule, approval step or argument may not take: the generated `tools.py` uses them."""

_DATA_FOLDER = re.compile(r"^(?:\./)?data(?:/.*)?$")

_TRIGGER_NAME = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_DEFAULT_MAIL_HOST = "imap.example.com"


def is_name(name: str) -> bool:
    """Whether `name` is a snake_case identifier of at most 48 characters (not a keyword)."""

    return bool(_NAME.fullmatch(name)) and not keyword.iskeyword(name)


def is_tool_name(name: str) -> bool:
    """Whether `name` may name a rule, an approval step or one of their arguments."""

    return is_name(name) and name not in RESERVED_NAMES


def _check_names(names: Iterable[str], *, tools: bool = False) -> None:
    """Raise `BlueprintError` for the first non-snake_case name (with `tools`, also `RESERVED_NAMES`)."""

    valid = is_tool_name if tools else is_name
    invalid = next((name for name in names if not valid(name)), None)
    if invalid is not None:
        if invalid in RESERVED_NAMES:
            raise BlueprintError(f"{invalid!r} is a reserved name; choose another")
        raise BlueprintError(f"{invalid!r} is not a snake_case name")


def workspace_operations(system: SystemDesign) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The workspace connector operations of `system`: `(reads, writes)`."""

    if system.kind == "files":
        return ("list_files", "read_csv", "query_csv"), ("write_csv",) if system.writes else ()
    return ("schema", "query"), ("execute",) if system.writes else ()


def workspace_references(system: SystemDesign) -> tuple[str, ...]:
    """The blueprint references of `system`'s workspace tools (`connector:orders.query`)."""

    reads, writes = workspace_operations(system)
    return tuple(f"connector:{system.key}.{operation}" for operation in (*reads, *writes))


def tool_name(reference: str) -> str:
    """The tool name of a connector reference: `connector:orders.query` -> `orders_query`."""

    return reference.removeprefix("connector:").replace(".", "_")


def is_outbox(system: SystemDesign) -> bool:
    """Whether `system` is only sent to: a database without a single sample row (a mail server)."""

    return system.kind != "files" and not any(
        line.strip() for table in system.tables for line in table.rows
    )


def table_statement(table: TableDesign) -> str:
    """The `CREATE TABLE` statement of `table` (names are checked before they are used)."""

    columns = ", ".join(f'"{c.name}" {_SQL_TYPES[c.type]}' for c in table.columns)
    return f'CREATE TABLE "{table.name}" ({columns})'


def workspace_config(system: SystemDesign, *, on_files: bool = False) -> dict[str, Any]:
    """The `connectors:` entry of `system` on its local workspace.

    With the user's files (`on_files`) a system that is only sent to gets its database in
    `data/output/` on first use, so nothing is invented next to the files.
    """

    if system.kind == "files":
        return {"use": "workspace", "root": f"data/{system.key}"}
    config: dict[str, Any] = {
        "use": "workspace",
        "root": "data",
        "database": f"{system.key}.db",
        "description": describe_system(system),
    }
    if on_files and is_outbox(system):
        _check_names(_names(system))
        config |= {"root": "data/output", "schema": [table_statement(t) for t in system.tables]}
    return config


def describe_system(system: SystemDesign) -> str:
    """What a system's data holds (tables and columns), for the model."""

    tables = "; ".join(
        f"{table.name} ({_line(table.description)}): "
        + ", ".join(f"{c.name} {c.type}" for c in table.columns)
        for table in system.tables
    )
    return f"{_line(system.name)}: {_line(system.description)} Tables: {tables}."


def cell(value: str, column: ColumnDesign) -> Any:
    """One CSV value as the column's type (`None` when empty); raises `ValueError` if it does not fit."""

    text = value.strip()
    if not text:
        return None
    if column.type == "integer":
        return int(text)
    if column.type == "real":
        return float(text)
    if column.type == "boolean":
        lowered = text.lower()
        if lowered not in _TRUE | _FALSE:
            raise ValueError(f"{text!r} is not true or false")
        return int(lowered in _TRUE)
    return text


def table_rows(table: TableDesign) -> list[list[Any]]:
    """The typed sample rows of `table`; raises `ValueError` for a wrong row length or value type."""

    rows: list[list[Any]] = []
    for number, line in enumerate(table.rows, start=1):
        if not line.strip():
            continue
        try:
            values = next(csv.reader([line], skipinitialspace=True))
        except csv.Error as exc:
            raise ValueError(f"row {number} is not a CSV line: {exc}") from None
        if len(values) != len(table.columns):
            raise ValueError(
                f"row {number} has {len(values)} value(s), the table has "
                f"{len(table.columns)} column(s)"
            )
        try:
            rows.append([cell(v, c) for v, c in zip(values, table.columns, strict=True)])
        except ValueError as exc:
            raise ValueError(f"row {number}: {exc}") from None
    return rows


def _names(system: SystemDesign) -> Iterator[str]:
    """The names of a system that end up in SQL and file names."""

    yield system.key
    for table in system.tables:
        yield table.name
        yield from (column.name for column in table.columns)


def write_workspace(directory: Path, systems: Sequence[SystemDesign]) -> None:
    """Write every system's sample data into `directory`; raises `BlueprintError` for a non-snake_case name."""

    _check_names(name for system in systems for name in _names(system))
    directory.mkdir(parents=True, exist_ok=True)
    for system in systems:
        if system.kind == "files":
            folder = directory / system.key
            folder.mkdir(parents=True, exist_ok=True)
            for table in system.tables:
                with (folder / f"{table.name}.csv").open("w", encoding="utf-8", newline="") as out:
                    writer = csv.writer(out)
                    writer.writerow([column.name for column in table.columns])
                    writer.writerows(table_rows(table))
            continue
        path = directory / f"{system.key}.db"
        path.unlink(missing_ok=True)
        with closing(sqlite3.connect(path)) as connection:
            for table in system.tables:
                connection.execute(table_statement(table))
                marks = ", ".join("?" for _ in table.columns)
                connection.executemany(
                    f'INSERT INTO "{table.name}" VALUES ({marks})', table_rows(table)
                )
            connection.commit()


def in_data_folder(value: str) -> bool:
    """Whether `value` is the project's `data` folder or a path below it (no `..`, no backslash)."""

    return bool(_DATA_FOLDER.match(value)) and ".." not in value.split("/") and "\\" not in value


def on_workspace(blueprint: Blueprint, root: Path) -> Blueprint:
    """`blueprint` with every workspace instance's folder, and every toolkit folder or database
    below `data/`, made absolute under `root`."""

    connectors = {
        name: {
            **options,
            "root": str(root / options.get("root", "data")),
            **({"sources": str(root / options["sources"])} if options.get("sources") else {}),
        }
        if options.get("use") == "workspace"
        else options
        for name, options in blueprint.connectors.items()
    }
    toolkits = {
        name: {
            option: str(root / value)
            if option in ("root", "path", "database")
            and isinstance(value, str)
            and in_data_folder(value)
            else value
            for option, value in options.items()
        }
        for name, options in blueprint.toolkits.items()
    }
    return blueprint.model_copy(update={"connectors": connectors, "toolkits": toolkits})


@contextmanager
def workspace(systems: Sequence[SystemDesign]) -> Iterator[Path]:
    """A temporary project folder holding the workspace data in `data/`."""

    with TemporaryDirectory(prefix="awa-workspace-", ignore_cleanup_errors=True) as folder:
        root = Path(folder)
        write_workspace(root / "data", systems)
        yield root


def rule_problem(rule: RuleDesign) -> str:
    """Why the rule's expression is not allowed, or `""`."""

    text = rule.expression.strip()
    if not text:
        return "the rule needs an expression"
    if len(text) > _MAX_EXPRESSION:
        return f"the expression is longer than {_MAX_EXPRESSION} characters"
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        return f"invalid expression: {exc.msg}"
    names = {argument.name for argument in rule.arguments}
    numbers = {argument.name for argument in rule.arguments if argument.type != "string"}
    clash = sorted(names & _RULE_FUNCTIONS)
    if clash:
        return f"the argument {clash[0]!r} hides the function of that name; rename it ({clash[0]}_value)"
    for node in ast.walk(tree):
        if not isinstance(node, _RULE_NODES):
            return f"{type(node).__name__} is not allowed in a rule expression"
        # `[0] * 10**9` or `max('a', 'b') * 10**9` would exhaust memory.
        if isinstance(node, ast.BinOp) and not (
            _numeric(node.left, numbers) and _numeric(node.right, numbers)
        ):
            return "arithmetic on text or lists is not allowed; compare them with == or in"
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _RULE_FUNCTIONS:
                return f"only {', '.join(sorted(_RULE_FUNCTIONS))} may be called"
            if node.keywords:
                return "function calls take positional arguments only"
            # `round(amount, -10**15)` on an integer computes 10**(10**15).
            if node.func.id == "round" and len(node.args) > 1 and not _digits(node.args[1]):
                return "round takes its digits as a whole number from -15 to 15"
        elif isinstance(node, ast.Name) and node.id not in names | _RULE_FUNCTIONS:
            if node.id not in ("True", "False", "None"):
                return f"unknown name {node.id!r}; use the rule's arguments"
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str) and len(node.value) > 200:
                return "text constants may have at most 200 characters"
            if isinstance(node.value, int | float) and abs(node.value) > 1e15:
                return "numbers may be at most 1e15"
    return ""


def _numeric(node: ast.expr, numbers: set[str]) -> bool:
    """Whether `node` always gives a number: number constants and arguments, and arithmetic,
    functions and conditionals of them."""

    if isinstance(node, ast.Constant):
        return isinstance(node.value, int | float)
    if isinstance(node, ast.Name):
        return node.id in numbers
    if isinstance(node, ast.UnaryOp):
        return isinstance(node.op, ast.USub | ast.UAdd) and _numeric(node.operand, numbers)
    if isinstance(node, ast.BinOp):
        return _numeric(node.left, numbers) and _numeric(node.right, numbers)
    if isinstance(node, ast.IfExp):
        return _numeric(node.body, numbers) and _numeric(node.orelse, numbers)
    if isinstance(node, ast.Call):
        return bool(node.args) and all(_numeric(arg, numbers) for arg in node.args)
    return False


def _digits(node: ast.expr) -> bool:
    """Whether `node` is a whole-number constant from -15 to 15 (`2`, `-1`)."""

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        node = node.operand
    value = node.value if isinstance(node, ast.Constant) else None
    return type(value) is int and abs(value) <= 15


def approval_problems(step: ApprovalStepDesign) -> list[tuple[str, str]]:
    """What is wrong with an approval step as `(path suffix, why)` pairs, empty when it is valid.

    The condition may read only the step's arguments, and every example must be decided as it
    says (with a condition, at least one example on each side).
    """

    from awa.errors import ConfigurationError
    from awa.tools.approval import ApprovalCondition

    arguments = [argument.name for argument in step.arguments]
    bad = [name for name in arguments if not is_tool_name(name) or arguments.count(name) > 1]
    if bad:
        return [(".arguments", f"argument {bad[0]!r} is not a unique snake_case name")]
    if not step.when.strip():
        wrong = [i for i, example in enumerate(step.examples) if not example.asks]
        if wrong:
            return [
                (
                    f".examples[{wrong[0]}]",
                    "without a condition the person is always asked; set when or asks: true",
                )
            ]
        return []
    if len(step.when) > _MAX_EXPRESSION:
        return [(".when", f"the condition is longer than {_MAX_EXPRESSION} characters")]
    try:
        condition = ApprovalCondition(step.when, arguments=arguments)
    except ConfigurationError as exc:
        return [(".when", str(exc))]
    problems: list[tuple[str, str]] = []
    for index, example in enumerate(step.examples):
        try:
            values = _json(example.arguments or "{}")
        except ValueError as exc:
            problems.append((f".examples[{index}].arguments", f"not JSON: {exc}"))
            continue
        if not isinstance(values, dict):
            problems.append((f".examples[{index}].arguments", "give a JSON object"))
            continue
        if condition(values) is not example.asks:
            verdict = "asks" if example.asks else "does not ask"
            problems.append(
                (f".examples[{index}]", f"the example says it {verdict}, the condition disagrees")
            )
    if not problems and {example.asks for example in step.examples} != {True, False}:
        problems.append(
            (".examples", "give at least one example where the person is asked and one where not")
        )
    return problems


def trigger_entry(design: TriggerDesign) -> dict[str, Any]:
    """The valid `triggers:` entry of `design`; raises `ConfigurationError` if the settings are invalid."""

    from awa.blueprints.triggers import trigger_entry as entry

    options: dict[str, Any] = {
        "cron": design.cron.strip() or None,
        "every": design.every.strip() or None,
        "timezone": design.timezone.strip() or None,
        "directory": design.directory.strip() or "inbox",
        "pattern": design.pattern.strip() or "*",
        "host": design.host.strip() or _DEFAULT_MAIL_HOST,
        "folder": design.folder.strip() or "INBOX",
    }
    return entry(design.kind, design.name, input=design.input.strip(), **options)


def trigger_request(design: TriggerDesign, payload: str) -> str:
    """The request an event with the JSON object `payload` sends.

    Raises:
        ValueError: If `payload` is not a JSON object or the input needs a field it lacks.
    """

    from awa.automation.templates import Template
    from awa.errors import TemplateError

    values = _json(payload or "{}")
    if not isinstance(values, dict):
        raise ValueError("the payload must be a JSON object")
    context = {"payload": values, "trigger": design.name, "today": date.today().isoformat()}
    try:
        return Template(design.input).render(context)
    except TemplateError as exc:
        raise ValueError(str(exc)) from None


def trigger_problems(design: TriggerDesign) -> list[tuple[str, str]]:
    """What is wrong with a trigger: `(path suffix, why)` pairs, empty when it is valid."""

    from awa.blueprints.triggers import check_trigger
    from awa.errors import ConfigurationError

    if not _TRIGGER_NAME.match(design.name):
        return [(".name", f"{design.name!r} is not a kebab-case name")]
    folder = design.directory.strip() or "inbox"
    if design.kind == "file-drop" and (
        folder.startswith(("/", "\\")) or ":" in folder or ".." in re.split(r"[\\/]", folder)
    ):
        return [(".directory", "give a folder inside the project, e.g. 'inbox'")]
    if design.kind == "schedule" and not (design.cron.strip() or design.every.strip()):
        return [(".cron", "a schedule needs cron or every")]
    try:
        check_trigger(trigger_entry(design))
        trigger_request(design, design.sample_payload)
    except (ConfigurationError, ValueError) as exc:
        where = ".sample_payload" if "payload" in str(exc) else ""
        return [(where, str(exc))]
    return []


def rules_module(rules: Sequence[RuleDesign], approvals: Sequence[ApprovalStepDesign] = ()) -> str:
    """The source of `tools.py`: one tool per rule and per approval step.

    An approval step returns `{"approved": true, ...}`; the wait comes from its approval in the
    blueprint.

    Raises:
        BlueprintError: If a name is not an identifier or an expression is not allowed.
    """

    _check_names(
        [
            *(r.name for r in rules),
            *(a.name for r in rules for a in r.arguments),
            *(s.name for s in approvals),
            *(a.name for s in approvals for a in s.arguments),
        ],
        tools=True,
    )
    problems = [f"{r.name}: {p}" for r in rules if (p := rule_problem(r))]
    if problems:
        raise BlueprintError(f"Invalid rule {problems[0]}")
    title = "The rules of this process: its exact calculations and decisions, as tools."
    tested = "tests/test_tools.py checks each rule against the examples of the design."
    if approvals:
        title = "The rules and approval steps of this process, as tools."
        tested = (
            "tests/test_tools.py checks each rule against the examples of the design, and each "
            "approval condition in blueprint.yaml against its examples."
        )
    lines = [
        f'"""{title}',
        "",
        tested,
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "import json",
        "from typing import Any",
        "",
        "from awa import tool",
    ]
    if any(rule.arguments for rule in rules):
        lines += _BATCH_HELPERS.split("\n")
    for rule in rules:
        expression = ast.unparse(ast.parse(rule.expression.strip(), mode="eval"))
        if not rule.arguments:
            lines += _function(rule.name, rule.description, rule.arguments)
            lines += ['    """', "", f"    return json.dumps({expression})"]
            continue
        names = ", ".join(a.name for a in rule.arguments)
        typed = ", ".join(f"{a.name}: Any" for a in rule.arguments)
        signature = f"{_names_tuple(rule)}, {_kinds_tuple(rule)}"
        lines += ["", "", f"def _{rule.name}({typed}) -> Any:", f"    return {expression}"]
        lines += _function(rule.name, rule.description, rule.arguments, _BATCH_NOTE, batch=True)
        lines += [
            '    """',
            "",
            "    if items is not None:",
            f"        return json.dumps(_each(_{rule.name}, {signature}, items))",
            f"    return json.dumps(_one(_{rule.name}, {signature}, [{names}]))",
        ]
    for step in approvals:
        condition = " ".join(step.when.split())
        when = f"when {_line(condition)}" if condition else "every time"
        note = [
            "",
            f"    An approval step: a call waits for the person's decision {when} (its",
            "    approval in blueprint.yaml) and returns once approved; a rejection comes back",
            "    as the reviewer's note instead.",
        ]
        lines += _function(step.name, step.description, step.arguments, note)
        lines += ['    """', ""]
        fields = "".join(f", {a.name!r}: {a.name}" for a in step.arguments)
        lines.append(f"    return json.dumps({{'approved': True{fields}}})")
    everything = sorted([*(r.name for r in rules), *(s.name for s in approvals)])
    listing = ", ".join(f'"{name}"' for name in everything)
    lines += ["", "", f"__all__ = [{listing}]"]
    return "\n".join(lines) + "\n"


_BATCH_HELPERS = '''

_KINDS: dict[str, tuple[type, ...]] = {
    "str": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
}


def _fits(value: Any, kind: str) -> bool:
    """Whether `value` has the argument type `kind` (a bool is no number)."""

    if isinstance(value, bool) and kind != "bool":
        return False
    return isinstance(value, _KINDS[kind])


def _one(rule: Any, names: tuple[str, ...], kinds: tuple[str, ...], values: list[Any]) -> Any:
    """One case: a clear error instead of a crash when a value is missing or wrong."""

    missing = [name for name, value in zip(names, values, strict=True) if value is None]
    if missing:
        return {"error": "missing " + ", ".join(missing)}
    wrong = [
        f"{name} must be {kind}"
        for name, kind, value in zip(names, kinds, values, strict=True)
        if not _fits(value, kind)
    ]
    if wrong:
        return {"error": "; ".join(wrong)}
    try:
        return rule(*values)
    except (ArithmeticError, TypeError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _each(
    rule: Any, names: tuple[str, ...], kinds: tuple[str, ...], items: list[dict[str, Any]]
) -> list[Any]:
    """Many cases, one result each; other keys of an item (an id to match it by) are
    ignored, and one bad item does not fail the others."""

    return [_one(rule, names, kinds, [item.get(n) for n in names]) for item in items]'''
"""Helpers of every generated `tools.py` whose rules take arguments."""


def _names_tuple(rule: RuleDesign) -> str:
    """The rule's argument names as Python tuple source, e.g. `('amount',)`."""

    return "(" + ", ".join(repr(a.name) for a in rule.arguments) + ",)"


def _kinds_tuple(rule: RuleDesign) -> str:
    """The rule's argument types as Python tuple source, e.g. `('float',)`."""

    return "(" + ", ".join(repr(_PYTHON_TYPES[a.type]) for a in rule.arguments) + ",)"


_BATCH_NOTE = (
    "",
    "    Decide many cases in ONE call: pass items, a list of objects with the arguments",
    "    below, and get a JSON list of the results in the same order.",
)
"""Docstring lines of a rule tool that also takes a list (one call instead of one per item)."""


def _function(
    name: str,
    description: str,
    arguments: Sequence[Any],
    note: Sequence[str] = (),
    *,
    batch: bool = False,
) -> list[str]:
    """The decorator, signature and docstring start of a tool; with `batch`, an `items` list takes many cases."""

    if batch:
        parameters = [f"{a.name}: {_PYTHON_TYPES[a.type]} | None = None" for a in arguments]
        parameters.append("items: list[dict[str, Any]] | None = None")
        signature = [f"def {name}(", *(f"    {p}," for p in parameters), ") -> str:"]
    else:
        typed = ", ".join(f"{a.name}: {_PYTHON_TYPES[a.type]}" for a in arguments)
        signature = [f"def {name}({typed}) -> str:"]
    lines = ["", "", "@tool", *signature, f'    """{_line(description)}']
    lines += note
    if arguments:
        lines += ["", "    Args:"]
        lines += [f"        {a.name}: {_line(a.description) or a.name}" for a in arguments]
        if batch:
            lines += [
                "        items: Many cases at once, each an object with the arguments above;",
                "            use it instead of one call per case.",
            ]
    return lines


def exec_generated_tools(source: str) -> list[BaseTool]:
    """Execute a generated `tools.py` in a fresh module and return its tools."""

    digest = hashlib.sha1(source.encode(), usedforsecurity=False).hexdigest()[:10]
    module = types.ModuleType(f"awa_project_tools_{digest}")
    exec(compile(source, "tools.py", "exec"), module.__dict__)  # generated from validated data
    return [module.__dict__[name] for name in module.__dict__["__all__"]]


def rule_tools(
    rules: Sequence[RuleDesign], approvals: Sequence[ApprovalStepDesign] = ()
) -> list[BaseTool]:
    """The rules and approval steps as tools (none without either)."""

    return exec_generated_tools(rules_module(rules, approvals)) if rules or approvals else []


def rule_failures(rules: Sequence[RuleDesign]) -> list[tuple[str, int, str]]:
    """Examples the generated rule tools get wrong: `(rule, example index, why)`."""

    tools = {tool.name: tool for tool in rule_tools(rules)}
    failures: list[tuple[str, int, str]] = []
    for rule in rules:
        for index, example in enumerate(rule.examples):
            try:
                arguments = _json(example.arguments or "{}")
                expected = _json(example.result)
                actual = json.loads(str(tools[rule.name].invoke(arguments)))
            except Exception as exc:  # noqa: BLE001 - reported back to the designer
                failures.append((rule.name, index, f"{type(exc).__name__}: {exc}"))
                continue
            if not same(actual, expected):
                why = f"expected {expected!r}, the rule gives {actual!r}"
                failures.append((rule.name, index, why))
        if rule.arguments and rule.examples:
            try:
                items = [_json(example.arguments or "{}") for example in rule.examples]
                batch = json.loads(str(tools[rule.name].invoke({"items": items})))
                single = [json.loads(str(tools[rule.name].invoke(item))) for item in items]
            except Exception as exc:  # noqa: BLE001 - reported back to the designer
                failures.append((rule.name, -1, f"items: {type(exc).__name__}: {exc}"))
                continue
            if len(batch) != len(single) or not all(map(same, batch, single)):
                failures.append((rule.name, -1, "items: the list gives other results"))
    return failures


def _json(text: str) -> Any:
    """Parse JSON, refusing `NaN` and `Infinity`: the generated tests could not write them."""

    def refuse(name: str) -> Any:
        raise ValueError(f"{name} is not a JSON value")

    return json.loads(text, parse_constant=refuse)


def same(actual: Any, expected: Any) -> bool:
    """Equal, numbers within 1e-9 (booleans compared exactly)."""

    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual is expected
    if isinstance(actual, int | float) and isinstance(expected, int | float):
        return abs(actual - expected) <= 1e-9 * max(1.0, abs(expected))
    return bool(actual == expected)


def _tests_module(rules: Sequence[RuleDesign], approvals: Sequence[ApprovalStepDesign]) -> str:
    """The source of `tests/test_tools.py`: the design's examples as pytest cases."""

    cases = [
        (rule.name, _json(example.arguments or "{}"), _json(example.result))
        for rule in rules
        for example in rule.examples
    ]
    steps = [
        (step.name, _json(example.arguments or "{}"), example.asks)
        for step in approvals
        for example in step.examples
    ]
    tested = " and ".join(
        what for what, given in (("rules", cases), ("approval steps", steps)) if given
    )
    text = (
        f'"""Tests of the {tested} in tools.py, from the examples of the design."""\n\n'
        "from __future__ import annotations\n\n"
        "import json\nimport sys\nfrom pathlib import Path\nfrom typing import Any\n\n"
        "import pytest\n\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n\n"
        "import tools  # noqa: E402\n"
    )
    if cases:
        listing = "\n".join(f"    {case!r}," for case in cases)
        text += (
            f"\nCASES = [\n{listing}\n]\n\n\n"
            "def same(actual: Any, expected: Any) -> bool:\n"
            "    if isinstance(actual, bool) or isinstance(expected, bool):\n"
            "        return actual is expected\n"
            "    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):\n"
            "        return abs(actual - expected) <= 1e-9 * max(1.0, abs(expected))\n"
            "    return bool(actual == expected)\n\n\n"
            '@pytest.mark.parametrize(("rule", "arguments", "expected"), CASES)\n'
            "def test_rule(rule: str, arguments: dict[str, Any], expected: Any) -> None:\n"
            "    assert same(json.loads(getattr(tools, rule).invoke(arguments)), expected)\n\n\n"
            "def test_rules_decide_many_cases_in_one_call() -> None:\n"
            "    for rule in sorted({name for name, arguments, _ in CASES if arguments}):\n"
            "        cases = [(a, e) for name, a, e in CASES if name == rule and a]\n"
            '        items = {"items": [arguments for arguments, _ in cases]}\n'
            "        results = json.loads(getattr(tools, rule).invoke(items))\n"
            "        assert len(results) == len(cases)\n"
            "        assert all(same(r, e) for r, (_, e) in zip(results, cases, strict=True))\n"
        )
    if steps:
        listing = "\n".join(f"    {step!r}," for step in steps)
        text += (
            "\nfrom awa.blueprints import Blueprint  # noqa: E402\n"
            "from awa.tools import ApprovalCondition  # noqa: E402\n\n"
            'BLUEPRINT = Path(__file__).resolve().parents[1] / "blueprint.yaml"\n\n'
            f"APPROVALS = [\n{listing}\n]\n\n\n"
            '@pytest.mark.parametrize(("step", "arguments", "asks"), APPROVALS)\n'
            "def test_approval(step: str, arguments: dict[str, Any], asks: bool) -> None:\n"
            "    blueprint = Blueprint.from_yaml(BLUEPRINT)\n"
            "    condition = next(a for a in blueprint.approvals if a.tool == step).condition\n"
            "    assert (condition is None or ApprovalCondition(condition)(arguments)) is asks\n"
            '    assert json.loads(getattr(tools, step).invoke(arguments))["approved"] is True\n'
        )
    return text


def _line(text: str) -> str:
    """`text` on one line, safe inside a docstring or comment."""

    return " ".join(text.split()).replace("\\", "/").replace('"', "'")


def step_instructions(step: ConnectionStep, key: str) -> str:
    """Plain-language steps to connect one system."""

    names = ", ".join(f"`{name}`" for name in step.env_vars)
    lines = [
        f"Until it is connected, the agents work on the sample data in the local workspace "
        f"(`connectors: {key}: use: workspace` in blueprint.yaml).",
        "",
    ]
    if step.status == "map":
        lines.append(
            f"- No ready-made connector knows {step.system}; it is linked through the generic "
            f"`{step.connector}` connector. Ask the person who manages {step.system} for the "
            "values below."
        )
    lines.append(
        f"- Settings to set: {names}. Run `awa connect .` in this folder and enter each value "
        "when asked; they are saved in `.env` on this computer only (never share or commit it)."
        if step.env_vars
        else "- The settings are already in place."
    )
    if step.login:
        login = " and ".join(f"`{name}`" for name in step.login)
        lines.append(f"- If {step.system} asks for a login, also set {login}.")
    test = f"`awa connectors test {step.connector}`" if step.status != "map" else "`awa connect .`"
    lines.append(f"- Test the connection: {test} makes one small call to {step.system}.")
    shown = {name: value for name, value in step.config.items() if name != "description"}
    change = f"`connectors: {key}:` becomes `{json.dumps(shown)}`"
    if step.switch:
        swaps = "; ".join(
            f"`{name}` -> {', '.join(f'`{ref}`' for ref in refs) or 'nothing yet'}"
            for name, refs in step.switch.items()
        )
        change += f", and the agents' tools change: {swaps}"
    else:
        change += "; the agents keep their tools"
    lines.append(f"- Once the test passes, `awa connect .` switches blueprint.yaml: {change}.")
    open_tools = [name for name, refs in step.switch.items() if not refs]
    if open_tools:
        lines.append(
            f"- {', '.join(f'`{name}`' for name in open_tools)} then needs the operation that "
            f"changes {step.system}: list them with `awa connectors show {step.connector}` and "
            "put the one the agents need in blueprint.yaml."
        )
    lines.append(
        "- Adjust the expected values in `evals/cases.yaml` to your real data and run "
        "`awa eval` again (see README.md)."
    )
    return "\n".join(lines)


def parse_steps(text: str) -> list[dict[str, Any]]:
    """The machine-readable steps embedded in a `CONNECT.md`, in order (a marker someone
    edited into something that is not a JSON object is skipped)."""

    steps: list[dict[str, Any]] = []
    for match in _STEP_MARKER.findall(text):
        with suppress(ValueError):
            step = json.loads(match)
            if isinstance(step, dict):
                steps.append(step)
    return steps


def render(project: ProjectDesign, report: EvalReport | None) -> dict[str, str]:
    """Every text file of the project folder, by relative path (the workspace data aside)."""

    files = {
        "blueprint.yaml": _blueprint_file(project),
        "evals/cases.yaml": project.evals.to_yaml(),
        "CONNECT.md": _connect_file(project),
        ".env.example": _env_file(project),
        "README.md": _readme(project, report),
    }
    if project.data is not None:
        files["data/PROFILE.json"] = project.data.profile_json()
        files["data/PROFILE.md"] = project.data.profile_md(project.blueprint.data)
    if project.rules or project.approvals:
        files["tools.py"] = rules_module(project.rules, project.approvals)
    if any(rule.examples for rule in project.rules) or any(
        step.examples for step in project.approvals
    ):
        files["tests/test_tools.py"] = _tests_module(project.rules, project.approvals)
    return files


def _blueprint_file(project: ProjectDesign) -> str:
    quoted = "\n".join(
        f"#   {line.strip()}" for line in project.process.splitlines() if line.strip()
    )
    return (
        f"# {project.blueprint.name}: designed by awa from this process description:\n"
        f"{quoted}\n#\n"
        "#   awa validate blueprint.yaml\n"
        "#   awa eval blueprint.yaml evals/cases.yaml\n"
        "#   awa chat blueprint.yaml\n"
    ) + project.blueprint.to_yaml()


def _connect_file(project: ProjectDesign) -> str:
    lines = [f"# Connecting {project.blueprint.name} to its external systems", ""]
    lines += ["## Where the data comes from", "", *project.data_map().lines(), ""]
    if not project.connections:
        lines += ["There is nothing to connect: no system works on a stand-in.", ""]
        return "\n".join(lines)
    lines += [
        "## Connecting the stand-ins",
        "",
        "A stand-in is a local database or folder in `data/` with sample records awa wrote from "
        "your description; it stands in for a system until that system is connected. Connect "
        "the external systems one by one; `awa connect .` walks you through the steps below. "
        "Connecting changes settings in `.env` and `blueprint.yaml`, never code.",
        "",
    ]
    keys = {system.name: system.key for system in project.systems}
    for number, step in enumerate(project.connections, start=1):
        key = keys.get(step.system, step.system)
        marker = {
            "system": step.system,
            "key": key,
            "status": step.status,
            "connector": step.connector,
            "env_vars": list(step.env_vars),
            "login": list(step.login),
            "config": dict(step.config),
            "switch": {name: list(refs) for name, refs in step.switch.items()},
        }
        lines += [
            f"### {number}. {step.system}: {_STATUS_TITLES[step.status]}",
            "",
            f"<!-- awa:step {json.dumps(marker, ensure_ascii=False)} -->",
            step.instructions,
            "",
        ]
    return "\n".join(lines)


def _env_file(project: ProjectDesign) -> str:
    lines = [
        f"# Settings for {project.blueprint.name}. Copy to .env and fill in the values, or run",
        "# `awa connect .`, which asks for them. Never commit or share .env.",
        "",
        "# The model: a profile or provider:model, and the credentials of that provider",
        "# (`awa doctor` lists which ones it needs).",
        "# AWA_DEFAULT_MODEL=",
    ]
    for system in project.declared:
        if system.variables:
            lines += ["", f"# {system.name} (declared under Your data; live, read-only)"]
            lines += [f"{name}=" for name in system.variables]
    for number, step in enumerate(project.connections, start=1):
        if step.env_vars or step.login:
            lines += ["", f"# {step.system} (CONNECT.md step {number})"]
            lines += [f"{name}=" for name in step.env_vars]
            if step.login:
                lines.append("# only if it asks for a login:")
                lines += [f"{name}=" for name in step.login]
    return "\n".join(lines) + "\n"


def _readme(project: ProjectDesign, report: EvalReport | None) -> str:
    blueprint = project.blueprint
    quoted = "\n".join(f"> {line.strip()}" for line in project.process.splitlines() if line.strip())
    lines = [f"# {blueprint.name}", ""]
    if blueprint.description:
        lines += [blueprint.description, ""]
    lines += ["Designed by awa from this process description:", "", quoted, "", "## Status", ""]
    if report is not None and report.runs:
        passed = sum(result.status == "passed" for result in report.runs)
        lines.append(
            f"- Evaluation: {passed} of {len(report.runs)} cases pass "
            f"({report.pass_rate:.0%}) on the local workspace."
        )
    found = project.data_map()
    if any(source.state == "stand-in" for source in found.sources):
        lines.append("- Some systems work on a stand-in until they are connected (CONNECT.md).")
    lines += ["", "## Where the data comes from", "", *found.lines(), ""]
    lines += ["## Acceptance criteria", "", *(f"- {c}" for c in project.criteria), ""]
    if assumptions := _assumptions(project):
        lines += ["## Assumptions made", "", *(f"- {a}" for a in assumptions), ""]
    if project.data is not None:
        lines += project.data.readme(blueprint.data)
    lines += [
        "## Files",
        "",
        "- `blueprint.yaml`: the agents, how they work together, the connectors and the steps "
        "that need approval.",
        "- `evals/cases.yaml`: the eval cases, written before the design.",
        "- `data/`: your files (`data/files/`, `data/output/`, `data/PROFILE.json`) and the "
        "sample data of the outside systems; what the process sends to a system without sample "
        "data (an outbox) is written to `data/output/` too."
        if project.data is not None
        else "- `data/`: the local workspace, sample data of every system.",
        "- `CONNECT.md`: how to connect each external system, step by step.",
        "- `.env.example`: the names of the settings; the values go in `.env`.",
    ]
    if project.rules or project.approvals:
        if not project.approvals:
            what = "the rules"
        elif not project.rules:
            what = "the approval steps"
        else:
            what = "the rules and approval steps"
        lines.append(f"- `tools.py`: {what} of the process; `tests/test_tools.py` tests them.")
    on_files = project.data is not None
    lines += [
        "",
        "## Use it",
        "",
        "```console",
        "$ awa eval blueprint.yaml evals/cases.yaml",
        "$ awa chat blueprint.yaml",
        *(
            ["$ awa data run . --all        # once for each of your files; results in data/output/"]
            * on_files
        ),
        "$ awa connect .                  # connect the external systems (CONNECT.md)",
        "$ awa serve blueprint.yaml       # an HTTP API (needs the server extra)",
        "```",
        "",
        "Run the commands in this folder (the workspace paths are relative to blueprint.yaml; "
        "`tools.py` is found by itself). "
        + (
            "The eval cases were written against your real files in `data/files/`; when "
            "the files change, run `awa validate blueprint.yaml` to see what no longer fits."
            if on_files
            else "The eval cases were written against the sample data; once a system is "
            "connected, adjust the expected values in `evals/cases.yaml` to your real data."
        ),
        "",
        *_automation_section(project),
    ]
    return "\n".join(lines) + "\n"


def _assumptions(project: ProjectDesign) -> list[str]:
    """What the designer assumed, plus what the build decided by itself: gated writes, the
    placeholder mailbox host."""

    found = list(project.assumptions)
    gated = sorted({a.tool for a in project.blueprint.approvals})
    if gated:
        found.append(f"A person decides before these run: {', '.join(gated)}.")
    if any(t.kind == "mail" and not t.host.strip() for t in project.triggers):
        found.append(
            f"The mailbox server is {_DEFAULT_MAIL_HOST}, a placeholder: set `host` of the mail "
            "trigger in `blueprint.yaml` (and the mailbox login in `.env`)."
        )
    return found


def _automation_section(project: ProjectDesign) -> list[str]:
    """The README section "Run it automatically": the triggers, how to run them, approvals."""

    from awa.blueprints.triggers import trigger_steps

    payloads = {
        t.name: json.dumps(_json(t.sample_payload), ensure_ascii=False) for t in project.triggers
    }
    steps = trigger_steps(project.blueprint, payloads=payloads)
    lines = [
        "## Run it automatically",
        "",
        "A process rarely starts with someone typing. Say what should start it (a web request, "
        "a schedule, a file in a folder, an email or a queue message); the command asks and "
        "writes the trigger into `blueprint.yaml`:",
        "",
        "```console",
        "$ awa add trigger blueprint.yaml",
        "$ awa serve blueprint.yaml           # runs every trigger (awa worker: without a web server)",
        "$ awa runs list --status waiting     # runs that wait for an approval",
        '$ awa runs decide RUN_ID --approve   # RUN_ID from the list; or --reject --note "why"; also on the Approvals page of `awa ui`',
        "```",
    ]
    if steps:
        lines += ["", "This system already has triggers:", ""]
        lines += [
            f"- {step.title}: `{step.command}`" + (f" ({step.note})" if step.note else "")
            for step in steps
        ]
        if secrets := _secrets(project):
            lines += ["", f"Set in `.env` (names only here): {', '.join(secrets)}."]
    return lines


def _secrets(project: ProjectDesign) -> list[str]:
    """The environment variables the triggers read, in order and without repeats."""

    names: list[str] = []
    for item in project.blueprint.triggers:
        if item.webhook is not None and item.webhook.secret_env:
            names.append(item.webhook.secret_env)
        if item.mailbox is not None:
            names += [item.mailbox.username_env, item.mailbox.password_env]
    return list(dict.fromkeys(names))
