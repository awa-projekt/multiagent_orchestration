"""`awa data inspect|check`: look at files the way awa does, and test a blueprint's `data:`."""

from __future__ import annotations

import argparse
import importlib
import sys
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING

from awa.cli._common import EXIT_ERROR, EXIT_OK, Commands, add_command, print_json, print_table

if TYPE_CHECKING:
    from awa.blueprints._systems import DeclaredSystem
    from awa.data import ColumnProfile, DataProfile, FileProfile

_EXTENSIONS = ("_data_run", "_data_systems")
"""Modules of this package that add their own `awa data` subcommand with `add_data_subcommands`."""


def add_data(commands: Commands) -> None:
    command = add_command(commands, "data")
    actions = command.add_subparsers(dest="data_command", required=True, metavar="action")

    inspect = actions.add_parser(
        "inspect", help="show what is in your files and what awa made of them"
    )
    inspect.add_argument(
        "files",
        nargs="+",
        help="files, folders (recursive) and glob patterns; a project folder also shows its "
        "declared external systems",
    )
    shown = inspect.add_mutually_exclusive_group()
    shown.add_argument("--json", action="store_true", help="print the profile as JSON")
    shown.add_argument(
        "--model-view",
        action="store_true",
        help="print exactly what a model would see (a short, budgeted digest)",
    )
    inspect.add_argument("--out", metavar="FILE", help="also write the profile JSON to FILE")
    inspect.add_argument("--force", action="store_true", help="overwrite an existing --out file")
    inspect.add_argument(
        "--mask",
        choices=("none", "placeholders", "drop"),
        default="none",
        help="what the model view shows of personal data: nothing hidden (default), consistent "
        "placeholders like <IBAN_1>, or no sample values; `drop` also strips the JSON",
    )
    inspect.add_argument(
        "--budget",
        type=int,
        default=6000,
        metavar="N",
        help="size of the model view in tokens (default 6000)",
    )
    inspect.add_argument(
        "--no-samples",
        action="store_true",
        help="leave out every value taken from the files (rows, examples, excerpts)",
    )
    inspect.add_argument(
        "--max-file-mb",
        type=float,
        default=100.0,
        metavar="MB",
        help="larger files are reported as errors (default 100)",
    )
    inspect.add_argument(
        "--strict", action="store_true", help="exit with code 1 on any warning, not only on errors"
    )
    inspect.set_defaults(handler=_inspect)

    check = actions.add_parser(
        "check", help="test a project's `data:` section against the files on disk"
    )
    check.add_argument(
        "project",
        help="a project folder (with blueprint.yaml) or a blueprint file with a `data:` section",
    )
    check.add_argument("--json", action="store_true", help="print the result as JSON")
    check.set_defaults(handler=_check)

    for name in _EXTENSIONS:
        if find_spec(f"awa.cli.{name}") is not None:
            importlib.import_module(f"awa.cli.{name}").add_data_subcommands(actions)


def _inspect(args: argparse.Namespace) -> int:
    from awa.data import Limits, Privacy, profile

    if args.out and Path(args.out).exists() and not args.force:
        print(f"error: {args.out} exists; add --force to overwrite it", file=sys.stderr)
        return EXIT_ERROR
    limits = Limits(max_file_bytes=max(1, int(args.max_file_mb * 1_000_000)))
    privacy = Privacy(mask=args.mask, include_samples=not args.no_samples)
    systems = _declared(args.files)
    paths = [_files_of(path) for path in args.files]
    if systems and all(path is None for path in paths):
        _print_systems(systems, args.json)
        return EXIT_OK
    result = profile([p for p in paths if p is not None], limits=limits, privacy=privacy)
    stripped = result.without_samples() if args.no_samples or args.mask == "drop" else result
    if args.out:
        Path(args.out).write_text(stripped.to_json(), encoding="utf-8")
    if args.model_view:
        from awa.data import render_for_model

        print(render_for_model(result, args.budget, privacy=privacy))
    elif args.json:
        if systems:
            import json

            data = json.loads(stripped.to_json())
            data["systems"] = [s.model_dump(mode="json") for s in systems]
            print_json(data)
        else:
            print(stripped.to_json())
    else:
        _print_report(stripped, verbose=bool(getattr(args, "verbose", False)))
        if systems:
            print()
            _print_systems(systems, False)
    failed = result.summary.errors > 0 or (args.strict and result.summary.warnings > 0)
    return EXIT_ERROR if failed else EXIT_OK


def _declared(paths: list[str]) -> list[DeclaredSystem]:
    """The systems declared in the project folders among `paths`."""

    from awa.blueprints import read_declared_systems
    from awa.blueprints._systems import SYSTEMS_FILE

    found: list[DeclaredSystem] = []
    for path in paths:
        if (Path(path) / SYSTEMS_FILE).is_file():
            found += read_declared_systems(path)
    return found


def _files_of(path: str) -> str | None:
    """What to profile of `path`: a project folder's `data/files` (`None` when it has none)."""

    from awa.blueprints._systems import SYSTEMS_FILE

    folder = Path(path)
    if not (folder / SYSTEMS_FILE).is_file():
        return path
    files = folder / "data" / "files"
    return str(files) if files.is_dir() else None


def _print_systems(systems: list[DeclaredSystem], as_json: bool) -> None:
    if as_json:
        print_json({"systems": [s.model_dump(mode="json") for s in systems]})
        return
    from awa.cli._data_systems import system_lines

    print(f"External systems ({len(systems)}):")
    for system in systems:
        print("\n".join(system_lines(system)))


def _check(args: argparse.Namespace) -> int:
    from awa.blueprints._data_project import NO_DATA_SECTION, check_project_data

    issues = check_project_data(args.project)
    if not args.json and [issue.message for issue in issues] == [NO_DATA_SECTION]:
        print(f"{args.project}: no data: section; nothing to check")
        return EXIT_OK
    errors = sum(issue.severity == "error" for issue in issues)
    warnings = len(issues) - errors
    if args.json:
        print_json(
            {
                "project": args.project,
                "ok": not errors,
                "errors": errors,
                "warnings": warnings,
                "issues": [
                    {"severity": i.severity, "path": i.path, "message": i.message} for i in issues
                ],
            }
        )
    else:
        for issue in issues:
            print(issue)
        verdict = f"{_count(errors, 'error')}, {_count(warnings, 'warning')}"
        print(
            f"{args.project}: {verdict}" if errors else f"{args.project}: data matches ({verdict})"
        )
    return EXIT_ERROR if errors else EXIT_OK


def _print_report(result: DataProfile, *, verbose: bool = False) -> None:
    summary = result.summary
    print(
        f"{_count(summary.files, 'file')}, {_count(summary.tables, 'table')}, "
        f"{_count(summary.rows, 'row')}, {_count(summary.warnings, 'warning')}, "
        f"{_count(summary.errors, 'error')}\n"
    )
    print_table(
        [("file", "kind", "content", "status", "flags")]
        + [
            (file.path, file.kind, _extent(file), file.status, _flag_counts(file))
            for file in result.files
        ]
    )
    for file in result.files:
        print()
        print("\n".join(_describe(file, verbose=verbose)))


def _extent(file: FileProfile) -> str:
    if file.pdf is not None:
        return f"{_count(file.pdf.pages, 'page')} ({file.pdf.document_class.replace('_', ' ')})"
    if file.docx is not None:
        return _count(file.docx.paragraphs, "paragraph")
    if file.mail is not None:
        return _count(file.mail.messages_total, "message")
    if file.json_doc is not None and file.json_doc.records is not None:
        return _count(file.json_doc.records, "record")
    if file.tables:
        rows = sum(table.rows for table in file.tables)
        approximate = "" if all(table.rows_exact for table in file.tables) else "~"
        return f"{approximate}{_count(rows, 'row')}"
    if file.text is not None and file.text.lines is not None:
        return _count(file.text.lines, "line")
    return "-"


def _flag_counts(file: FileProfile) -> str:
    errors = sum(flag.severity == "error" for flag in file.flags)
    warnings = sum(flag.severity == "warning" for flag in file.flags)
    parts = [_count(errors, "error")] if errors else []
    parts += [_count(warnings, "warning")] if warnings else []
    return ", ".join(parts) or "-"


def _count(number: int, noun: str) -> str:
    return f"{number:,} {noun}{'' if number == 1 else 's'}"


def _describe(file: FileProfile, *, verbose: bool = False) -> list[str]:
    """The details of one file: how it was read, its tables and columns, its flags (with their
    codes, such as `DF-W-...`, only with `verbose`)."""

    lines = [f"{file.path}  ({file.kind}, {file.size:,} bytes, {file.status})"]
    if file.text is not None and file.text.encoding is not None:
        parts = [f"encoding {file.text.encoding}"]
        if file.text.delimiter:
            parts.append(f"delimiter {file.text.delimiter!r}")
        if file.text.newline:
            parts.append(f"line ends {file.text.newline}")
        lines.append("  " + ", ".join(parts))
    for number, table in enumerate(file.tables, 1):
        # A table is named by its sheet or name; the profile's internal id (`f1.t1`) is not shown.
        named = table.sheet or table.name
        where = f" {named!r}" if named else (f" {number}" if len(file.tables) > 1 else "")
        header = f", header row {table.header_row}" if table.header_row else ", no header row"
        exact = "" if table.rows_exact else " (estimate)"
        lines.append(f"  table{where}: {_count(table.rows, 'row')}{exact}{header}")
        if table.totals_rows:
            lines.append(f"    totals rows left out: {', '.join(map(str, table.totals_rows))}")
        for column in table.columns:
            lines.append("    " + _column_line(column))
    if file.pdf is not None:
        lines.append(
            f"  {_count(file.pdf.pages, 'page')}, {file.pdf.document_class.replace('_', ' ')}"
            + (", encrypted" if file.pdf.encrypted else "")
        )
    for flag in file.flags:
        code = f" {flag.code}" if verbose else ""
        lines.append(f"  [{flag.severity}]{code} {flag.message}")
    return lines


def _column_line(column: ColumnProfile) -> str:
    parts = [f"{column.name:<22} {column.type:<9}"]
    if column.format is not None and column.format.decimal:
        thousands = f", thousands {column.format.thousands!r}" if column.format.thousands else ""
        parts.append(f"decimal {column.format.decimal!r}{thousands}")
    if column.date_format:
        parts.append(f"date {column.date_format}" + (" (ambiguous)" if column.ambiguous else ""))
    if column.unique:
        parts.append("unique")
    if column.pii.kinds:
        parts.append("personal data: " + ", ".join(column.pii.kinds))
    if column.min is not None and column.max is not None:
        parts.append(f"{column.min} to {column.max}")
    if column.examples:
        parts.append("e.g. " + ", ".join(column.examples[:3]))
    return "  ".join(parts)
