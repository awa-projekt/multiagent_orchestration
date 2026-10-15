"""`awa data add-system|remove-system`: declare and test the external systems of a
project."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from awa.cli._common import EXIT_ERROR, EXIT_OK

if TYPE_CHECKING:
    from awa.blueprints._declare import Declared, Setting
    from awa.blueprints._systems import DeclaredSystem


def add_data_subcommands(actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add `awa data add-system` and `awa data remove-system`."""

    add = actions.add_parser(
        "add-system",
        help="declare a database or API the process uses: test it read-only, save its settings "
        "in .env and read its structure",
    )
    add.add_argument("project", help="project folder (created when missing)")
    add.add_argument(
        "--type",
        required=True,
        metavar="KIND",
        help="the kind of system: sql (SQLite, PostgreSQL, MySQL/MariaDB, SQL Server), openapi "
        "(a REST API with an OpenAPI description), or another connector of `awa connectors list`",
    )
    add.add_argument("--name", required=True, help='your name for it, e.g. "Invoice ledger"')
    add.add_argument("--key", help="its short id in blueprint.yaml (default: from the name)")
    add.add_argument(
        "--samples",
        action="store_true",
        help="also read up to 3 example rows per table for the design (masked like your files)",
    )
    add.add_argument(
        "--no-input",
        action="store_true",
        help="never ask: use only settings already in DIR/.env or the environment",
    )
    add.set_defaults(handler=_add)

    remove = actions.add_parser(
        "remove-system", help="forget a declared system (its settings stay in .env)"
    )
    remove.add_argument("project", help="project folder")
    remove.add_argument("key", help="the system's id (shown by `awa data inspect DIR`)")
    remove.set_defaults(handler=_remove)


def _add(args: argparse.Namespace) -> int:
    from awa.blueprints._declare import (
        declare_system,
        merged_env,
        read_declared_systems,
        system_config,
        system_settings,
    )
    from awa.blueprints._systems import system_key

    root = Path(args.project)
    root.mkdir(parents=True, exist_ok=True)
    declared = read_declared_systems(root)
    existing = next((s for s in declared if s.name == " ".join(args.name.split())), None)
    key = args.key or (
        existing.key if existing else system_key(args.name, {s.key for s in declared})
    )
    config = system_config(args.type, key)
    asking = not args.no_input and sys.stdin.isatty()
    entered: dict[str, str] = {}
    asked: set[str] = set()
    for _round in range(2):  # an API's login settings are known once its document is
        env = merged_env(os.environ, root / ".env", entered)
        settings = system_settings(key, config, env)
        for setting in settings:
            if setting.name in asked:
                continue
            asked.add(setting.name)
            if env.get(setting.name):
                print(f"  {setting.name}: already set")
                continue
            value = _ask(setting, getpass.getpass if setting.secret else input) if asking else ""
            if value:
                entered[setting.name] = value
            elif setting.required:
                print(f"  {setting.name}: missing ({setting.description})")
    outcome = declare_system(
        root,
        connector=args.type,
        name=args.name,
        values=entered,
        environ=os.environ,
        samples=args.samples,
        key=key,
    )
    _report(outcome, root)
    return EXIT_OK if outcome.system is not None else EXIT_ERROR


def _ask(setting: Setting, read: Callable[[str], str]) -> str:
    optional = "" if setting.required else ", optional: Enter skips it"
    hidden = ", hidden" if setting.secret else ""
    example = f" (for example {setting.example})" if setting.example and not setting.secret else ""
    try:
        return read(f"  {setting.name}{example}{hidden}{optional}: ").strip()
    except EOFError:
        return ""


def _report(outcome: Declared, root: Path) -> None:
    if not outcome.ok:
        print(f"The connection test failed: {outcome.message}")
        print(f"What to do: {outcome.advice}")
        print("Nothing was saved.")
        return
    system = outcome.system
    assert system is not None
    print(f"Test passed: {outcome.message}")
    for warning in outcome.warnings:
        print(f"Note: {warning}")
    if outcome.saved:
        print(f"Saved {', '.join(outcome.saved)} in {root / '.env'} (never shown again)")
    print(
        f"Declared {system.name} ({system.kind}) as `{system.key}` in {root / 'data/SYSTEMS.json'}"
    )
    print(*system_lines(system), sep="\n")
    print(f'Next: awa build "<your process>" --out {root}  (the design reads it live, read-only)')


def system_lines(system: DeclaredSystem) -> list[str]:
    """A declared system for a terminal: what it is, its tables or operations, its tools."""

    from awa.blueprints._systems import declared_tools

    lines = [f"{system.name} ({system.kind}), id {system.key}: live, read-only"]
    for table in system.tables[:30]:
        rows = f", about {table.rows:,} rows" if table.rows is not None else ""
        lines.append(f"  {table.kind} {table.name}{rows}: {', '.join(table.columns)}")
    if len(system.tables) > 30:
        lines.append(f"  ... and {len(system.tables) - 30} more")
    reads = [op for op in system.operations if not op.write]
    for op in reads[:30]:
        where = f" ({op.method} {op.path})" if op.path else ""
        lines.append(f"  operation {op.name}{where}")
    if system.operations and len(reads) < len(system.operations):
        lines.append(
            f"  {len(system.operations) - len(reads)} operation(s) that change data are left out"
        )
    lines.append(f"  tools: {', '.join(declared_tools([system]))}")
    lines += [f"  note: {warning}" for warning in system.warnings]
    return lines


def _remove(args: argparse.Namespace) -> int:
    from awa.blueprints._declare import remove_declared_system

    try:
        remove_declared_system(args.project, args.key)
    except ValueError as exc:
        print(exc)
        return EXIT_ERROR
    print(f"Removed {args.key}; its settings stay in {Path(args.project) / '.env'}.")
    return EXIT_OK
