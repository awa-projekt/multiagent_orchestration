"""`awa new tool|toolbox|parser|connector|skill|process` and `awa tools list|call`: write and
try your own parts."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from awa.cli._common import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_PAUSED,
    Commands,
    add_command,
    blueprint_argument,
    json_argument,
    preview,
    print_json,
    print_table,
)

if TYPE_CHECKING:
    from awa._internal.upstream.tools import BaseTool


_SOURCE = "a .py file, a module (dotted name), or builtin (the tools awa ships)"


def add_new(commands: Commands) -> None:
    from awa.blueprints.wizard import STRUCTURES

    new = add_command(commands, "new")
    kinds = new.add_subparsers(dest="new_command", required=True, metavar="action")

    new_tool = kinds.add_parser("tool", help="a typed, documented tool and its test")
    new_tool.add_argument("name", help="tool name, for example lookup_record")
    new_tool.add_argument("--async", dest="is_async", action="store_true", help="async function")
    new_tool.add_argument("--approval", action="store_true", help="every call waits for a human")
    _add_output_options(new_tool, default=".")
    new_tool.set_defaults(handler=_new_tool)

    new_toolbox = kinds.add_parser("toolbox", help="a configurable connector and its test")
    new_toolbox.add_argument("name", help="toolbox name and tool prefix, for example crm")
    _add_output_options(new_toolbox, default=".")
    new_toolbox.set_defaults(handler=_new_toolbox)

    new_parser = kinds.add_parser(
        "parser", help="an own reader of one file kind and its contract test"
    )
    new_parser.add_argument("name", metavar="KIND", help="file kind, for example text or image")
    _add_output_options(new_parser, default=".")
    new_parser.set_defaults(handler=_new_parser)

    new_connector = kinds.add_parser(
        "connector", help="an HTTP connector (ConnectorSpec) and its contract test"
    )
    new_connector.add_argument("name", help="connector name, for example inventory")
    _add_output_options(new_connector, default=".")
    new_connector.set_defaults(handler=_new_connector)

    new_skill = kinds.add_parser("skill", help="a SKILL.md with metadata and references/")
    new_skill.add_argument("name", help="skill name, for example refund-policy")
    new_skill.add_argument("--category", default="general", help="metadata category")
    _add_output_options(new_skill, default="skills")
    new_skill.set_defaults(handler=_new_skill)

    process = kinds.add_parser(
        "process",
        help="a blueprint for a process, from agents and their tasks (asks when run bare)",
        description="Write a blueprint from a process name, agents with their tasks, a "
        "structure and a model. Missing answers are asked for in a terminal.",
    )
    process.add_argument("name", nargs="?", help='process name, for example "Invoice processing"')
    process.add_argument(
        "--agent",
        action="append",
        default=[],
        metavar="NAME=TASK",
        help="an agent and its task (repeatable, in order)",
    )
    process.add_argument(
        "--structure", choices=STRUCTURES, help="how the agents work together (default team)"
    )
    process.add_argument("--lead", help="team orchestrator or network lead (default: lead)")
    process.add_argument("--model", help="model profile or provider:model (default: default)")
    process.add_argument("-o", "--output", help="blueprint file (default: <name>.yaml)")
    process.add_argument(
        "--no-auto", dest="auto", action="store_false", help="no auto-matching of skills and tools"
    )
    process.add_argument("--force", action="store_true", help="overwrite an existing file")
    process.add_argument(
        "--no-input", action="store_true", help="never ask; fail when an answer is missing"
    )
    process.set_defaults(handler=_new_process)


def add_tools(commands: Commands) -> None:
    tools = add_command(commands, "tools")
    actions = tools.add_subparsers(dest="tools_command", required=True, metavar="action")

    listing = actions.add_parser("list", help="name, risk, approval and description of each tool")
    listing.add_argument("source", help=_SOURCE)
    listing.add_argument(
        "--blueprint",
        type=blueprint_argument,
        metavar="BLUEPRINT",
        help="also show the approvals this blueprint (or project folder) adds",
    )
    listing.add_argument("--json", action="store_true", help="print JSON")
    listing.set_defaults(handler=_tools_list)

    call = actions.add_parser("call", help="run one tool and print its result")
    call.add_argument("source", help=f"{_SOURCE}; or builtin:<name> alone")
    call.add_argument("tool", nargs="?", help="tool name (left out with builtin:<name>)")
    call.add_argument(
        "--args", default="{}", help='arguments: a JSON object, or "@file.json" (default {})'
    )
    call.add_argument(
        "--deps",
        default=None,
        help='RunContext deps: JSON, "@file.json", or the name of a module attribute (called '
        "if callable)",
    )
    call.add_argument("--approve", action="store_true", help="approve an approval pause")
    call.add_argument("--json", action="store_true", help="print status and content as JSON")
    call.set_defaults(handler=_tools_call)


def _add_output_options(parser: argparse.ArgumentParser, *, default: str) -> None:
    parser.add_argument("--dir", default=default, help=f"output directory (default {default})")
    parser.add_argument("--force", action="store_true", help="overwrite existing files")


def _new_tool(args: argparse.Namespace) -> int:
    from awa.blueprints.scaffold import create_tool

    written = create_tool(
        args.name, args.dir, is_async=args.is_async, approval=args.approval, force=args.force
    )
    return _report(written, f"pytest {written[1]}")


def _new_toolbox(args: argparse.Namespace) -> int:
    from awa.blueprints.scaffold import create_toolbox

    written = create_toolbox(args.name, args.dir, force=args.force)
    return _report(written, f"pytest {written[1]}")


def _new_parser(args: argparse.Namespace) -> int:
    from awa.blueprints.scaffold import create_parser

    written = create_parser(args.name, args.dir, force=args.force)
    module = written[0].stem
    return _report(
        written,
        f'pytest {written[1]}, then add register("parsers", "{args.name}", '
        f"{module}.profile_{args.name}, replace=True) to plugins.py",
    )


def _new_connector(args: argparse.Namespace) -> int:
    from awa.blueprints.scaffold import create_connector

    written = create_connector(args.name, args.dir, force=args.force)
    return _report(
        written,
        f'pytest {written[1]}, then add register("connectors", '
        f'"{written[0].stem.removesuffix("_connector")}", {written[0].stem}.CONNECTOR, '
        "replace=True) to plugins.py",
    )


def _new_skill(args: argparse.Namespace) -> int:
    from awa.blueprints.scaffold import create_skill

    written = create_skill(args.name, args.dir, category=args.category, force=args.force)
    return _report(written, f"awa skills list --skills {Path(args.dir)}")


def _new_process(args: argparse.Namespace) -> int:
    from awa.blueprints.wizard import run_wizard

    interactive = not args.no_input and sys.stdin.isatty()
    return run_wizard(args, ask=input if interactive else None, out=sys.stdout)


def _report(written: Sequence[Path], next_step: str) -> int:
    for path in written:
        print(f"wrote {path}")
    print(f"next: {next_step}")
    return EXIT_OK


def _tools(source: str) -> list[BaseTool]:
    """The tools of a module or file, documented functions turned into awa tools."""

    from awa._internal.upstream.tools import BaseTool
    from awa.blueprints._resolve import load_tools
    from awa.tools import tool

    return [item if isinstance(item, BaseTool) else tool(item) for item in load_tools(source)]


def _is_builtin(source: str) -> bool:
    return source == "builtin" or source.startswith("builtin:")


def _builtin_rows() -> list[dict[str, Any]]:
    """The builtin catalog: what each tool does, whether it asks, what it needs."""

    from awa.toolkits import catalog

    rows = []
    for entry in catalog():
        needs = [f"toolkits.{entry.toolkit.name}.{' or '.join(names)}" for names in entry.required]
        needs += [f"the {entry.extra} extra"] if entry.extra and not entry.installed else []
        rows.append(
            {
                "name": entry.reference,
                "description": entry.summary,
                "risk": list(entry.risk.flags()),
                "approval": "always" if entry.requires_approval else "never",
                "needs": needs,
            }
        )
    return rows


def _blueprint_approvals(path: str | None) -> dict[str, str]:
    """Tool name -> the approval a blueprint adds (`always`, or `when <condition>`)."""

    if path is None:
        return {}
    from awa.blueprints import Blueprint

    return {
        approval.tool: f"when {approval.when}" if approval.when else "always"
        for approval in Blueprint.from_yaml(Path(path)).approvals
    }


def _tool_row(item: BaseTool) -> dict[str, Any]:
    from awa.tools import infer_tool_risk, tool_spec

    spec = tool_spec(item)
    return {
        "name": item.name,
        "description": " ".join((item.description or "").split()),
        "risk": list(infer_tool_risk(item).flags()),
        "approval": spec.approval if spec is not None else "unknown",
    }


def _tools_list(args: argparse.Namespace) -> int:
    if _is_builtin(args.source):
        rows = _builtin_rows()
    else:
        rows = [_tool_row(item) for item in _tools(args.source)]
    gated = _blueprint_approvals(getattr(args, "blueprint", None))
    for row in rows:
        added = gated.get(row["name"]) or gated.get(row["name"].removeprefix("builtin:"))
        if added and row["approval"] == "never":
            row["approval"] = f"{added} (blueprint)"
    if args.json:
        print_json(rows)
        return EXIT_OK
    table = [("NAME", "RISK", "APPROVAL", "DESCRIPTION")]
    for row in rows:
        risk = ",".join(row["risk"]) or "-"
        needs = f" (needs {', '.join(row['needs'])})" if row.get("needs") else ""
        table.append((row["name"], risk, row["approval"], preview(row["description"] + needs, 90)))
    print_table(table, free_last=True)
    if _is_builtin(args.source):
        print(f"\n{len(rows)} builtin tools; use one in a blueprint as builtin:<name>.")
    return EXIT_OK


def _called_tool(args: argparse.Namespace) -> tuple[BaseTool, ModuleType | None]:
    """The tool `awa tools call` runs, and the module it comes from (`None` for a builtin)."""

    from awa.blueprints._resolve import allow_local_imports, import_module_entry
    from awa.errors import ConfigurationError

    if _is_builtin(args.source):
        from awa.toolkits import catalog

        name = args.source.removeprefix("builtin").removeprefix(":") or args.tool or ""
        if not name:
            raise ConfigurationError("name the builtin tool: awa tools call builtin:<name>")
        entry = catalog().get(name)
        problems = entry.problems({})
        if problems:
            raise ConfigurationError(
                f"{entry.reference} cannot run without configuration: {'; '.join(problems)}. "
                "Use it in a blueprint with that toolkits: section"
            )
        return entry.create({}), None
    if not args.tool:
        raise ConfigurationError(f"name the tool to call: awa tools call {args.source} <tool>")
    allow_local_imports()
    module = import_module_entry(args.source)
    tools = {item.name: item for item in _tools(args.source)}
    if args.tool not in tools:
        raise ConfigurationError(
            f"No tool {args.tool!r} in {args.source}; it has: {', '.join(sorted(tools))}"
        )
    return tools[args.tool], module


def _signature(item: BaseTool) -> str:
    """`name(a: string, b: number = 1)`: the arguments a tool takes, from its schema."""

    fields = []
    schema = item.args or {}
    for name, field in schema.items():
        kind = field.get("type", "any") if isinstance(field, dict) else "any"
        default = (
            f" = {field['default']!r}" if isinstance(field, dict) and "default" in field else ""
        )
        fields.append(f"{name}: {kind}{default}")
    return f"{item.name}({', '.join(fields)})"


def _argument_problems(item: BaseTool, arguments: dict[str, Any]) -> str | None:
    """Why `arguments` do not fit the tool, in words; `None` when they do."""

    from pydantic import ValidationError

    schema = getattr(item, "args_schema", None)
    validate = getattr(schema, "model_validate", None)
    if not callable(validate):
        return None
    try:
        validate(arguments)
    except ValidationError as exc:
        missing = [str(e["loc"][0]) for e in exc.errors() if e["type"] == "missing" and e["loc"]]
        other = [
            f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}"
            for e in exc.errors()
            if e["type"] != "missing"
        ]
        parts = [f"missing arguments: {', '.join(missing)}"] if missing else []
        parts += other
        return f"{'; '.join(parts)}. The tool takes {_signature(item)}"
    return None


def _tools_call(args: argparse.Namespace) -> int:
    from awa._internal import aio as asyncio_compat
    from awa.errors import ConfigurationError
    from awa.testing.tool_contract import ToolApprovalRequested, ToolContract

    arguments = _json_object(args.args, "--args")
    item, module = _called_tool(args)
    deps = _deps(module, args.deps)
    problem = _argument_problems(item, arguments)
    if problem is not None:
        raise ConfigurationError(problem)
    contract = ToolContract(item, deps=deps, agent="awa-cli")
    try:
        message = asyncio_compat.run(
            contract.acall_message(arguments, approve=True if args.approve else None)
        )
    except ToolApprovalRequested as request:
        print(
            f"error: {item.name} needs approval for this call; rerun with --approve to run it",
            file=sys.stderr,
        )
        shown = {key: value for key, value in request.payload.items() if key != "instructions"}
        print(json.dumps(shown, indent=2, default=str), file=sys.stderr)
        return EXIT_PAUSED
    status = message.status or "success"
    content = message.content if isinstance(message.content, str) else message.text
    if args.json:
        print_json({"tool": item.name, "status": status, "content": content})
    else:
        from awa.observability.events import tool_output_text

        print(tool_output_text(content) if isinstance(content, str) else content)
    return EXIT_ERROR if status == "error" else EXIT_OK


def _deps(module: ModuleType | None, value: str | None) -> Any:
    """`--deps`: JSON, or the name of an attribute of the module (called if callable)."""

    from awa.errors import ConfigurationError

    if value is None:
        return None
    if value.startswith("@"):
        return json_argument(value, "--deps")
    with contextlib.suppress(json.JSONDecodeError):
        return json.loads(value)
    if module is None or not value.isidentifier() or not hasattr(module, value):
        where = f"an attribute of {module.__name__}" if module is not None else "JSON"
        raise ConfigurationError(f"--deps {value!r} is not {where}")
    attribute = getattr(module, value)
    return attribute() if callable(attribute) else attribute


def _json_object(text: str, option: str) -> dict[str, Any]:
    from awa.errors import ConfigurationError

    value = json_argument(text, option)
    if not isinstance(value, dict):
        raise ConfigurationError(f"{option} must be a JSON object")
    return value
