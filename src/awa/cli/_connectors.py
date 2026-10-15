"""`awa connectors list|show|test`: the connector catalog and connection tests."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from awa.cli._common import EXIT_ERROR, EXIT_OK, Commands, add_command, print_json, print_table


def add_connectors(commands: Commands) -> None:
    command = add_command(commands, "connectors")
    actions = command.add_subparsers(dest="connectors_command", required=True, metavar="action")

    listing = actions.add_parser("list", help="list the connectors and whether they are set up")
    listing.add_argument("--category", help="only this category")
    listing.add_argument("--configured", action="store_true", help="only configured connectors")
    listing.add_argument("--json", action="store_true", help="print JSON")
    listing.set_defaults(handler=_list)

    show = actions.add_parser("show", help="show a connector's variables and tools")
    show.add_argument("name", help="connector or instance name")
    show.add_argument("--blueprint", help="take the instance's options from this blueprint")
    show.add_argument("--json", action="store_true", help="print JSON")
    show.set_defaults(handler=_show)

    test = actions.add_parser("test", help="check connections with one small real call each")
    test.add_argument("names", nargs="*", help="connector or instance names")
    test.add_argument("--all", action="store_true", help="every configured connector")
    test.add_argument("--blueprint", help="take the instances' options from this blueprint")
    test.add_argument("--json", action="store_true", help="print JSON")
    test.set_defaults(handler=_test)


def _blueprint_connectors(path: str | None) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """The blueprint's `connectors:` options (paths resolved) and the instances its agents use."""

    if path is None:
        return {}, []
    from awa.blueprints import Blueprint
    from awa.blueprints._resolve import connector_options
    from awa.connectors._catalog import split_reference

    blueprint = Blueprint.from_yaml_file(path)
    base = Path(path).resolve().parent
    used = list(
        dict.fromkeys(
            split_reference(reference)[0]
            for spec in blueprint.agents
            for reference in spec.tools
            if reference.startswith("connector:")
        )
    )
    options = {
        name: connector_options(name, blueprint.connectors, base)
        for name in {*blueprint.connectors, *used}
    }
    return options, used


def _list(args: argparse.Namespace) -> int:
    from awa.connectors import discover
    from awa.connectors._catalog import CORE_CONNECTORS, list_connectors

    report = {status.name: status for status in discover().connectors}
    rows: list[dict[str, Any]] = []
    for spec in list_connectors():
        status = report[spec.name]
        if args.category and spec.category != args.category:
            continue
        if args.configured and not status.configured:
            continue
        rows.append(
            {
                "name": spec.name,
                "category": spec.category,
                "stability": "core" if spec.name in CORE_CONNECTORS else "preview",
                "title": spec.title,
                "configured": status.configured,
                "missing": list(status.missing),
                "reads": len(spec.reads()),
                "writes": len(spec.writes()),
                "dynamic": spec.dynamic,
            }
        )
    if args.json:
        print_json(rows)
        return EXIT_OK
    table: list[tuple[str, ...]] = [("NAME", "CATEGORY", "STABILITY", "SETTINGS", "TOOLS", "TITLE")]
    for row in rows:
        tools = (
            "listed by system" if row["dynamic"] else f"{row['reads']} read, {row['writes']} write"
        )
        missing = len(row["missing"])
        state = "set" if row["configured"] else f"{missing} missing"
        table.append(
            (
                str(row["name"]),
                str(row["category"]),
                str(row["stability"]),
                state,
                tools,
                str(row["title"]),
            )
        )
    print_table(table)
    print(
        "\nset: the settings are present (not tested; awa connectors test NAME checks them). "
        "preview: no stability promise; tested against fake connections, the Microsoft 365 "
        "connectors also read live on one tenant."
    )
    return EXIT_OK


def _show(args: argparse.Namespace) -> int:
    from awa.connectors._catalog import connector_context

    options, _ = _blueprint_connectors(args.blueprint)
    ctx = connector_context(args.name, config=options.get(args.name))
    spec = ctx.spec
    own = {variable.name for variable in spec.env}
    variables = [
        {
            "name": ctx.env.name(variable.name),
            "description": variable.description,
            "required": variable.required,
            "auth": variable.name not in own,
            "secret": variable.secret,
            "set": ctx.env.is_set(variable.name),
            "default": variable.default,
        }
        for variable in spec.variables()
    ]
    operations = [
        {
            "reference": f"connector:{ctx.name}.{op.name}",
            "write": op.write,
            "approval": ctx.requires_approval(op),
            "description": op.description,
        }
        for op in ctx.operations()
    ]
    missing = ctx.missing()
    if args.json:
        print_json(
            {
                "name": ctx.name,
                "connector": spec.name,
                "title": spec.title,
                "summary": spec.summary,
                "configured": not missing,
                "missing": list(missing),
                "variables": variables,
                "auth": [[ctx.env.name(v.name) for v in a.variables()] for a in spec.auth],
                "operations": operations,
                "docs": spec.docs,
                "setup": spec.setup,
            }
        )
        return EXIT_OK
    status = "variables set (not tested)" if not missing else "missing " + ", ".join(missing)
    print(f"{ctx.name}: {spec.title} ({spec.category})")
    print(f"  {spec.summary}")
    print(f"  status: {status}")
    if variables:
        print("  variables:")
        for variable in variables:
            print("    " + _variable_line(variable))
    if len(spec.auth) > 1:
        alternatives = [
            " + ".join(ctx.env.name(v.name) for v in a.variables() if v.required) for a in spec.auth
        ]
        print("  authentication (first complete one is used): " + " | ".join(alternatives))
    if operations:
        print("  tools:")
        for op in operations:
            print(f"    {op['reference']}{_operation_mark(op)}  {op['description']}")
    elif spec.dynamic:
        print(f"  tools: listed by the system (connector:{ctx.name})")
    if spec.setup:
        print(f"  setup: {spec.setup}")
    if spec.docs:
        print(f"  docs: {spec.docs}")
    return EXIT_OK


def _variable_line(variable: dict[str, Any]) -> str:
    state = "SET" if variable["set"] else "not set"
    if not variable["set"] and variable["default"]:
        state += f" (default {variable['default']})"
    if not variable["required"]:
        need = "optional"
    elif variable["auth"]:
        need = "auth"
    else:
        need = "required"
    return f"{variable['name']:<28} {state:<10} {need}  {variable['description']}"


def _operation_mark(operation: dict[str, Any]) -> str:
    if operation["approval"]:
        return " [approval]"
    if operation["write"]:
        return " [write]"
    return ""


def _test(args: argparse.Namespace) -> int:
    from awa.connectors import discover
    from awa.connectors._catalog import check_connector
    from awa.errors import ConfigurationError

    options, used = _blueprint_connectors(args.blueprint)
    names: list[str] = list(args.names)
    if args.all:
        names += used or [s.name for s in discover().connectors if s.configured]
    names = list(dict.fromkeys(names))
    if not names:
        raise ConfigurationError("name one or more connectors, or use --all")
    results = [check_connector(name, config=options.get(name)) for name in names]
    if args.json:
        print_json(
            [
                {
                    "name": r.name,
                    "ok": r.ok,
                    "message": r.message,
                    "missing": list(r.missing),
                    "seconds": round(r.seconds, 3),
                }
                for r in results
            ]
        )
    else:
        for result in results:
            print(f"{'OK  ' if result.ok else 'FAIL'} {result.name}: {result.message}")
    return EXIT_OK if all(result.ok for result in results) else EXIT_ERROR
