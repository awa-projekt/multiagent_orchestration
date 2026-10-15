"""`awa export`: eject a project into a standalone LangChain and LangGraph project (exit 4 when
something cannot be carried over)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from awa.cli._common import EXIT_OK, EXIT_UNSUPPORTED, Commands, add_command, print_json


def add_export(commands: Commands) -> None:
    command = add_command(commands, "export")
    command.add_argument(
        "project", nargs="?", default=".", help="project folder or blueprint file (default .)"
    )
    command.add_argument("--out", metavar="DIR", help="folder to write; must not exist yet")
    command.add_argument(
        "--check",
        action="store_true",
        help="render and import the project in a temporary folder; write nothing",
    )
    command.add_argument(
        "--force",
        action="store_true",
        help="replace an earlier export in --out (never other files)",
    )
    command.add_argument("--json", action="store_true", help="print the result as JSON")
    command.set_defaults(handler=_export)


def _export(args: argparse.Namespace) -> int:
    from awa.exporter import ExportError, ExportUnsupportedError, export

    if args.out is None and not args.check:
        raise ExportError("give the folder to write with --out, or --check to try without writing")
    out = Path(args.out or "export-check")
    try:
        report = export(args.project, out, check=args.check, force=args.force)
    except ExportUnsupportedError as exc:
        if args.json:
            print_json({"unsupported": [vars(finding) for finding in exc.findings]})
        else:
            print("\n".join(exc.lines()), file=sys.stderr)
            print(
                f"nothing written: {len(exc.findings)} feature(s) cannot be exported; "
                "see docs/develop/export.md",
                file=sys.stderr,
            )
        return EXIT_UNSUPPORTED
    if args.json:
        print_json(
            {
                "out": str(report.out) if report.written else None,
                "package": report.package,
                "files": list(report.files),
                "warnings": list(report.warnings),
                "written": report.written,
            }
        )
        return EXIT_OK
    if report.written:
        print(f"exported to {report.out}: {len(report.files)} files, package {report.package}")
        print(f'next: cd {report.out}, then see README.md (python run.py "your task")')
    else:
        print(
            f"check passed: {len(report.files)} files render and the graph imports; nothing written"
        )
    for warning in report.warnings:
        print(f"note: {warning}")
    return EXIT_OK
