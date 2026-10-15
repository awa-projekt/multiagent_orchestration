"""`awa add tool|skills|graph|trigger`: import tools, skills and graphs, or add a trigger;
`--dry-run` prints the changes without writing (rewritten blueprints lose YAML comments)."""

from __future__ import annotations

import argparse
import ast
import json
import keyword
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from awa._internal.serialize import dump_yaml, load_yaml
from awa.cli._common import EXIT_ERROR, EXIT_OK, Commands, add_blueprint_argument, add_command
from awa.errors import AdapterError

DEFAULT_MODULE = "tools_imported.py"
"""File `awa add tool` and `awa add graph` write their code to."""

_MODULE_HEADER = '''"""Tools and graphs imported with `awa add`. Edit freely; `awa add` appends to this file.

Blueprints reference these objects as `{module}:<name>`; `awa run` imports
them from the working directory.
"""

from awa.agents.graph_agent import from_langgraph  # noqa: F401
from awa.tools.importing import from_langchain, from_langchain_tool  # noqa: F401
'''
_IDENTIFIER = re.compile(r"[^0-9A-Za-z_]+")
_SETTINGS = (
    "input", "secret_env", "scheme", "cron", "every", "timezone", "directory", "pattern",
    "host", "username_env", "password_env", "folder", "queue",
)  # fmt: skip


def add_add(commands: Commands) -> None:
    from awa.blueprints.triggers import KINDS

    add = add_command(commands, "add")
    kinds = add.add_subparsers(dest="add_command", required=True, metavar="action")

    tool = kinds.add_parser("tool", help="import a LangChain tool, toolkit or tool factory")
    tool.add_argument("path", help="import path, e.g. langchain_tavily:TavilySearch")
    _add_target_options(tool)
    approval = tool.add_mutually_exclusive_group()
    approval.add_argument(
        "--approval",
        dest="approval",
        action="store_const",
        const=True,
        help="gate every imported tool with human approval",
    )
    approval.add_argument(
        "--no-approval",
        dest="approval",
        action="store_const",
        const=False,
        help="never add approval, not even for inferred external actions",
    )
    tool.add_argument(
        "--select", action="append", default=[], help="only this tool of a toolkit (repeatable)"
    )
    tool.add_argument("--prefix", default="", help="prefix for the tool names, e.g. gh_")
    tool.set_defaults(handler=_add_tool, approval=None)

    graph = kinds.add_parser("graph", help="add a compiled LangGraph graph as a delegate")
    graph.add_argument("path", help="import path of the compiled graph, e.g. billing.graph:graph")
    graph.add_argument("--name", required=True, help="agent name of the graph")
    graph.add_argument("--description", default="", help="what the graph is for")
    graph.add_argument(
        "--input-key", default="messages", help="messages channel (default messages)"
    )
    _add_target_options(graph)
    graph.set_defaults(handler=_add_graph)

    skills = kinds.add_parser("skills", help="import skill folders or Markdown prompts")
    skills.add_argument("path", nargs="+", help="skill folder, prompt file or folder of them")
    skills.add_argument("--into", default="skills", help="skills directory (default ./skills)")
    skills.add_argument("--overwrite", action="store_true", help="replace existing skill folders")
    skills.add_argument("--dry-run", action="store_true", help="show what would change")
    skills.set_defaults(handler=_add_skills)

    trigger = kinds.add_parser(
        "trigger",
        help="start the system automatically: web request, schedule, file, email or queue",
        description="Add a trigger to a blueprint and print the commands that run it. Without "
        "--kind, in a terminal, it asks what it needs. Secrets are never written: the "
        "blueprint only names the environment variables that hold them.",
    )
    add_blueprint_argument(trigger)
    trigger.add_argument(
        "--kind",
        choices=(*KINDS, "file_drop", "mailbox"),
        metavar="KIND",
        help="what starts a run: webhook, schedule, file-drop (or file_drop), mail (or mailbox) "
        "or queue (asks when omitted)",
    )
    trigger.add_argument("--name", help="name of the trigger (default: the kind)")
    trigger.add_argument("--input", help="what the system does per event; {payload.x} fills in")
    trigger.add_argument("--dry-run", action="store_true", help="show what would change")
    trigger.add_argument("--secret-env", metavar="VAR", help="webhook: variable holding the secret")
    trigger.add_argument(
        "--scheme", default="timestamped", help="webhook: timestamped (default), body or token"
    )
    trigger.add_argument("--cron", help="schedule: cron expression, e.g. '0 7 * * MON-FRI'")
    trigger.add_argument("--every", help="schedule: an interval instead, e.g. 15m")
    trigger.add_argument("--timezone", help="schedule: time zone, e.g. Europe/Berlin")
    trigger.add_argument(
        "--directory", default="inbox", help="file-drop: folder to watch (default inbox)"
    )
    trigger.add_argument(
        "--pattern", default="*", help="file-drop: file names, e.g. '*.pdf' (default: all)"
    )
    trigger.add_argument("--host", help="mail: IMAP server")
    trigger.add_argument(
        "--username-env",
        default="AWA_IMAP_USER",
        metavar="VAR",
        help="mail: variable holding the user name (default AWA_IMAP_USER)",
    )
    trigger.add_argument(
        "--password-env",
        default="AWA_IMAP_PASSWORD",
        metavar="VAR",
        help="mail: variable holding the password (default AWA_IMAP_PASSWORD)",
    )
    trigger.add_argument("--folder", default="INBOX", help="mail: folder to read (default INBOX)")
    trigger.add_argument("--queue", help="queue: the queue's name (default: the trigger name)")
    trigger.set_defaults(handler=_add_trigger)


def _add_target_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--blueprint", help="blueprint to register the import in")
    parser.add_argument("--agent", help="agent of the blueprint that gets the tool")
    parser.add_argument(
        "--module",
        help=f"Python file for the generated code (default {DEFAULT_MODULE} next to the "
        "blueprint, or in the working directory)",
    )
    parser.add_argument("--dry-run", action="store_true", help="show what would change")


def _add_tool(args: argparse.Namespace) -> int:
    from awa.tools.importing import import_langchain_tools

    blueprint_path, module = _paths(args)
    report = import_langchain_tools(
        args.path,
        requires_approval=args.approval,
        select=args.select or None,
        prefix=args.prefix or None,
    )
    if not report.entries:
        raise AdapterError(f"{args.path!r} provides no tools")
    print(report.render())
    names = [_variable(entry.name) for entry in report.entries]
    taken = sorted(set(names) & _defined_names(module))
    if taken:
        raise AdapterError(
            f"{module} already defines {', '.join(taken)}; pass --prefix to import under "
            "other names"
        )
    # With a blueprint, approvals are registered there instead of in the code.
    code_approval = False if blueprint_path is not None else args.approval
    block = _tool_block(args.path, report, names, code_approval, args.prefix, args.select)
    changes: list[str] = []
    data: dict[str, Any] | None = None
    if blueprint_path is not None:
        data = _load_blueprint_data(blueprint_path)
        agent = _target_agent(data, args.agent)
        for entry, name in zip(report.entries, names, strict=True):
            reference = f"{module.stem}:{name}"
            changes += _register_tool(agent, reference)
            changes += _register_risk(data, reference, entry.risk.model_dump())
            if entry.requires_approval:
                changes += _register_approval(data, reference)
    _apply(module, block, blueprint_path, data, changes, dry_run=args.dry_run)
    if blueprint_path is None:
        print(f"use them: from {module.stem} import {', '.join(names)}")
    else:
        _print_check(blueprint_path, data, dry_run=args.dry_run)
    return EXIT_OK


def _add_graph(args: argparse.Namespace) -> int:
    from awa.agents.graph_agent import from_langgraph

    blueprint_path, module = _paths(args)
    agent = from_langgraph(
        args.path, name=args.name, description=args.description, input_key=args.input_key
    )
    print(
        f"{agent.name}: graph {agent.graph.get_name()!r}, tools: {', '.join(agent.card.tools) or '-'}"
    )
    variable = _variable(args.name)
    tool_variable = f"{variable}_tool"
    taken = sorted({variable, tool_variable} & _defined_names(module))
    if taken:
        raise AdapterError(f"{module} already defines {', '.join(taken)}; pick another --name")
    options = [f"name={args.name!r}"]
    if args.description:
        options.append(f"description={args.description!r}")
    if args.input_key != "messages":
        options.append(f"input_key={args.input_key!r}")
    block = (
        f"# awa add graph {args.path}\n"
        f"{variable} = from_langgraph({args.path!r}, {', '.join(options)})\n"
        f"{tool_variable} = {variable}.as_tool()\n"
    )
    changes: list[str] = []
    data: dict[str, Any] | None = None
    if blueprint_path is not None:
        data = _load_blueprint_data(blueprint_path)
        target = _target_agent(data, args.agent)
        changes += _register_tool(target, f"{module.stem}:{tool_variable}")
    _apply(module, block, blueprint_path, data, changes, dry_run=args.dry_run)
    print(
        f"use it: from {module.stem} import {variable}  "
        "(a GraphAgent: Team member, workflow step or delegate)"
    )
    if blueprint_path is not None:
        _print_check(blueprint_path, data, dry_run=args.dry_run)
    return EXIT_OK


def _add_skills(args: argparse.Namespace) -> int:
    from awa.skills.importing import import_skills

    report = import_skills(
        *args.path, into=Path(args.into), overwrite=args.overwrite, dry_run=args.dry_run
    )
    print(report.render())
    if report.needs_review:
        names = ", ".join(entry.skill.name for entry in report.needs_review)
        print(f"review the generated names and descriptions of: {names}")
    if report.imported and not args.dry_run:
        print(f"use them: awa run <blueprint> <task> --skills {args.into}")
    return EXIT_OK if report.ok else EXIT_ERROR


def _add_trigger(args: argparse.Namespace) -> int:
    from awa.blueprints import Blueprint
    from awa.blueprints.triggers import add_trigger, ask_trigger, trigger_entry, trigger_steps
    from awa.errors import ConfigurationError

    path = Path(args.blueprint)
    blueprint = Blueprint.from_yaml_file(path)
    if args.kind is not None:
        entry = trigger_entry(
            args.kind, args.name, **{key: getattr(args, key) for key in _SETTINGS}
        )
    elif sys.stdin.isatty():
        section = blueprint.automation_section()
        taken = {item.name for item in section.triggers} if section else set()
        asked = ask_trigger(input, sys.stdout, taken=taken)
        if asked is None:
            print("nothing added")
            return EXIT_OK
        entry = asked
    else:
        raise ConfigurationError("Give --kind (webhook, schedule, file-drop, mail or queue)")
    original = path.read_text(encoding="utf-8-sig")
    text = add_trigger(path, entry, dry_run=args.dry_run)
    comments = [line.strip() for line in original.splitlines() if line.lstrip().startswith("#")]
    if any(comment not in text for comment in comments):
        print("note: the blueprint is rewritten from its data; its YAML comments are dropped")
    print(f"{'would add' if args.dry_run else 'added'} trigger {entry['name']!r} to {path}:")
    print("  " + dump_yaml([entry]).replace("\n", "\n  ").rstrip())
    if blueprint.limits is None:
        print(
            "note: the blueprint has no `limits:`; add e.g. `limits: {max_requests: 100, "
            "max_total_tokens: 200000}` so a triggered run cannot use as much as it likes"
        )
    if not args.dry_run:
        for step in trigger_steps(Blueprint.from_yaml_file(path), str(path), only=entry["name"]):
            print(
                f"\n{step.title}:\n  {step.command}" + (f"\n  ({step.note})" if step.note else "")
            )
    return EXIT_OK


def _tool_block(
    path: str,
    report: Any,
    names: Sequence[str],
    approval: bool | None,
    prefix: str,
    select: Sequence[str],
) -> str:
    options = ""
    if approval is not None:
        options += f", requires_approval={approval}"
    if prefix:
        options += f", prefix={prefix!r}"
    lines = [f"# awa add tool {path}"]
    entries = report.entries
    if len(entries) == 1:
        pick = f", name={entries[0].original_name!r}" if select else ""
        lines.append(f"{names[0]} = from_langchain_tool({path!r}{pick}{options})")
    else:
        originals = [entry.original_name for entry in entries]
        lines.append(f"{', '.join(names)} = from_langchain(")
        lines.append(f"    {path!r},")
        lines.append(f"    select={originals!r}{options},")
        lines.append(")")
    return "\n".join(lines) + "\n"


def _variable(name: str) -> str:
    variable = _IDENTIFIER.sub("_", name).strip("_") or "tool"
    if variable[0].isdigit() or keyword.iskeyword(variable):
        variable = f"tool_{variable}"
    return variable


def _paths(args: argparse.Namespace) -> tuple[Path | None, Path]:
    """The `--blueprint` and the module to write; makes the module importable."""

    blueprint = Path(args.blueprint) if args.blueprint else None
    if args.module:
        module = Path(args.module)
    else:
        module = (blueprint.parent if blueprint is not None else Path.cwd()) / DEFAULT_MODULE
    if module.suffix != ".py" or not module.stem.isidentifier() or keyword.iskeyword(module.stem):
        raise AdapterError(f"--module must be a .py file with a valid module name, got {module}")
    for directory in (Path.cwd(), module.parent):
        entry = str(directory.resolve())
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return blueprint, module


def _defined_names(module: Path) -> set[str]:
    """Names assigned at the top level of `module` (parsed, never executed)."""

    if not module.is_file():
        return set()
    try:
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    except SyntaxError as exc:
        raise AdapterError(f"{module} is not valid Python: {exc}") from None
    names: set[str] = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        for target in targets:
            for item in target.elts if isinstance(target, ast.Tuple) else [target]:
                if isinstance(item, ast.Name):
                    names.add(item.id)
    return names


def _load_blueprint_data(path: Path) -> dict[str, Any]:
    from awa.blueprints import Blueprint

    Blueprint.from_yaml_file(path)  # validates, with the usual error messages
    data = load_yaml(path.read_bytes(), what="blueprint")
    if not isinstance(data, dict):
        raise AdapterError(f"{path} is not a blueprint mapping")
    return data


def _target_agent(data: dict[str, Any], name: str | None) -> tuple[str, dict[str, Any]]:
    """The name and editable entry of the agent that gets the tool (a short-form task becomes its
    `description:`)."""

    raw = data.get("agents") or []
    entries = (
        {str(key): value for key, value in raw.items()}
        if isinstance(raw, dict)
        else {str(agent.get("name")): agent for agent in raw if isinstance(agent, dict)}
    )
    names = list(entries)
    if name is None:
        team = data.get("team")
        lead = team if isinstance(team, str) else (team or {}).get("orchestrator")
        if isinstance(team, str | dict) and lead:
            name = str(lead)
        elif len(names) == 1:
            name = names[0]
        else:
            raise AdapterError(f"Pass --agent: the blueprint has agents {', '.join(names)}")
    if name not in entries:
        raise AdapterError(f"The blueprint has no agent {name!r} (agents: {', '.join(names)})")
    entry = entries[name]
    if isinstance(raw, dict) and not isinstance(entry, dict):
        entry = raw[name] = {} if entry is None else {"description": entry}
    return name, entry


def _register_tool(agent: tuple[str, dict[str, Any]], reference: str) -> list[str]:
    name, entry = agent
    tools = entry.setdefault("tools", [])
    if reference in tools:
        return []
    tools.append(reference)
    return [f"agents[{name}].tools += {reference}"]


def _register_risk(data: dict[str, Any], reference: str, risk: dict[str, Any]) -> list[str]:
    risks = data.setdefault("tool_risks", {})
    flags = {name: bool(value) for name, value in risk.items()}
    if risks.get(reference) == flags:
        return []
    risks[reference] = flags
    shown = ", ".join(name for name, value in flags.items() if value) or "none"
    return [f"tool_risks[{reference}] = {shown}"]


def _register_approval(data: dict[str, Any], reference: str) -> list[str]:
    approvals = data.setdefault("approvals", [])
    if any(isinstance(a, dict) and a.get("tool") == reference for a in approvals):
        return []
    approvals.append({"tool": reference})
    return [f"approvals += {reference}"]


def _dump_blueprint(path: Path, data: dict[str, Any]) -> str:
    if path.suffix.lower() == ".json":
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return dump_yaml(data)


def _apply(
    module: Path,
    block: str,
    blueprint: Path | None,
    data: dict[str, Any] | None,
    changes: Sequence[str],
    *,
    dry_run: bool,
) -> None:
    from awa.blueprints import Blueprint

    prefix = "would " if dry_run else ""
    text = ""
    if blueprint is not None and data is not None:
        text = _dump_blueprint(blueprint, data)
        Blueprint.from_dict(data)  # the updated blueprint must still be valid
    exists = module.is_file()
    print(f"{prefix}{'append to' if exists else 'create'} {module}:")
    for line in block.rstrip().splitlines():
        print(f"  + {line}")
    if blueprint is not None:
        print(f"{prefix}update {blueprint}:" if changes else f"{blueprint}: already up to date")
        for change in changes:
            print(f"  {change}")
        original = blueprint.read_text(encoding="utf-8-sig")
        if changes and any(line.lstrip().startswith("#") for line in original.splitlines()):
            print("  note: the blueprint is rewritten from its data; its YAML comments are dropped")
    if dry_run:
        return
    current = (
        module.read_text(encoding="utf-8") if exists else _MODULE_HEADER.format(module=module.stem)
    )
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(f"{current}{_blank_line_after(current)}{block}", encoding="utf-8")
    if blueprint is not None and changes:
        blueprint.write_text(text, encoding="utf-8")


def _blank_line_after(text: str) -> str:
    if text.endswith("\n\n"):
        return ""
    if text.endswith("\n"):
        return "\n"
    return "\n\n"


def _print_check(blueprint: Path, data: dict[str, Any] | None, *, dry_run: bool) -> None:
    from awa.blueprints import Blueprint

    if data is None:
        return
    issues = Blueprint.from_dict(data).check()
    for issue in issues:
        print(f"  {issue}")
    errors = sum(issue.severity == "error" for issue in issues)
    state = "would be" if dry_run else "is"
    verdict = f"has {errors} error(s)" if errors else "valid"
    print(f"{blueprint} {state} {verdict}; run it from {blueprint.parent.resolve()}")
