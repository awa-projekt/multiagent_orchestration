"""`awa plugins list|show|check|switch`: the plugins and switchable parts of awa."""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict
from pathlib import Path

from awa.cli._common import EXIT_ERROR, EXIT_OK, Commands, add_command, print_json, print_table
from awa.plugins import PluginInfo, list_plugins


def add_plugins(commands: Commands) -> None:
    parser = add_command(
        commands,
        "plugins",
        description="Plugins add tools, connectors, skills, models, parsers and erasure "
        "handlers. They come from the project folder, from installed packages (entry points "
        "awa.<kind>) and from awa itself. Parts that are only switches (guard.*, cache.*, "
        "tracer.*) are listed too.",
    )
    actions = parser.add_subparsers(dest="plugins_command", required=True, metavar="action")
    listing = actions.add_parser("list", help="list every plugin and part")
    listing.add_argument("--kind", help="only this kind (tools, parsers, skill, guard, ...)")
    listing.add_argument("--json", action="store_true", help="print the rows as JSON")
    listing.set_defaults(handler=_list)
    show = actions.add_parser("show", help="show one plugin or part")
    show.add_argument("name", metavar="KIND.NAME", help="as awa plugins list shows it")
    show.add_argument("--json", action="store_true", help="print the rows as JSON")
    show.set_defaults(handler=_show)
    check = actions.add_parser("check", help="run the contract test of a part (offline)")
    check.add_argument("name", metavar="KIND.NAME", help="a parser, connector, tool or sql_guard")
    check.add_argument("--target", metavar="module:Object", help="test this replacement instead")
    check.set_defaults(handler=_check)
    switch = actions.add_parser(
        "switch", help="switch a part on or off in the blueprint's components: section"
    )
    switch.add_argument("name", metavar="KIND.NAME", help="the part, for example parser.pdf")
    switch.add_argument("state", choices=["on", "off", "default"], help="the new state")
    switch.add_argument("--blueprint", metavar="PATH", help="the blueprint to edit")
    switch.add_argument("--dry-run", action="store_true", help="print the result, write nothing")
    switch.set_defaults(handler=_switch)


def warn_security_off(command: str) -> list[str]:
    """Log one WARNING when `awa serve` or `awa worker` starts with a security part off."""

    from awa._internal.logging import get_logger
    from awa.plugins.switches import security_off

    off = security_off()
    if off:
        get_logger("components").warning(
            "awa %s starts with security parts switched off or replaced: %s "
            "(remove them from the blueprint's components: to restore)",
            command,
            ", ".join(off),
        )
    return off


def _rows(kind: str | None) -> list[PluginInfo]:
    return list_plugins(kind, project=Path.cwd())


def _failed(exc: Exception) -> int:
    print(f"problem: {exc}", file=sys.stderr)
    return EXIT_ERROR


def _list(args: argparse.Namespace) -> int:
    from awa.plugins import PluginError

    try:
        rows = _rows(args.kind)
    except PluginError as exc:
        return _failed(exc)
    if args.json:
        print_json([asdict(row) for row in rows])
        return EXIT_OK
    table = [["KIND", "NAME", "SOURCE", "STATUS", "SWITCH", "DISTRIBUTION"]]
    for row in rows:
        dist = f"{row.dist or ''} {row.version or ''}".strip()
        status = row.status if row.enabled else "off"
        table.append([row.kind, row.name, row.source, status, row.key or "", dist])
    print_table(table)
    return EXIT_OK


def _find(name: str) -> list[PluginInfo]:
    from awa.plugins.switches import PREFIXES, split_key

    kind, _, plain = name.partition(".")
    if kind in PREFIXES:
        prefix, plain = split_key(name)
        return [r for r in _rows(None) if r.key == f"{prefix}.{plain}"]
    return [r for r in _rows(kind) if r.name == plain]


def _show(args: argparse.Namespace) -> int:
    from awa.plugins import PluginError
    from awa.plugins.switches import ComponentError

    try:
        rows = _find(args.name)
    except (PluginError, ComponentError) as exc:
        return _failed(exc)
    if not rows:
        return _unknown(args.name)
    if args.json:
        print_json([asdict(row) for row in rows])
        return EXIT_OK
    for row in rows:
        for field, value in asdict(row).items():
            if value not in (None, "", False):
                print(f"{field}: {value}")
        print()
    return EXIT_OK


def _unknown(name: str) -> int:
    import difflib

    known = sorted({r.key or f"{r.kind}.{r.name}" for r in _rows(None)})
    close = difflib.get_close_matches(name, known, n=1)
    hint = f" (did you mean {close[0]!r}?)" if close else ""
    print(
        f"problem: no plugin or part named {name!r}{hint}; see `awa plugins list`", file=sys.stderr
    )
    return EXIT_ERROR


def _check(args: argparse.Namespace) -> int:
    from awa.plugins._kinds import _import
    from awa.plugins.switches import ComponentError
    from awa.testing.plugin_contract import builtin_object, suite_of, verify

    try:
        if suite_of(args.name) is None:
            print(f"problem: {args.name} has no contract suite", file=sys.stderr)
            return EXIT_ERROR
        sys.path.insert(0, str(Path.cwd()))
        try:
            candidate = _import(args.target) if args.target else builtin_object(args.name)
        finally:
            sys.path.remove(str(Path.cwd()))
        result = verify(args.name, candidate)
    except (ComponentError, ImportError, AttributeError, ValueError) as exc:
        return _failed(exc)
    print(result.summary())
    return EXIT_OK if result.passed else EXIT_ERROR


def _switch(args: argparse.Namespace) -> int:
    from awa.blueprints.components import find_blueprint, set_component
    from awa.errors import AwaError
    from awa.plugins.switches import ComponentError

    try:
        if args.state != "default" and not _find(args.name):
            return _unknown(args.name)
        path = find_blueprint(Path.cwd(), args.blueprint)
        text = set_component(path, args.name, args.state, dry_run=args.dry_run)
    except (AwaError, ComponentError) as exc:
        return _failed(exc)
    if args.dry_run:
        print(text, end="")
        return EXIT_OK
    print(f"{args.name}: {args.state} ({path.name})")
    return EXIT_OK
