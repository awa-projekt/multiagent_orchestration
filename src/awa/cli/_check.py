"""`awa check`: the setup checks as green or red lines, each red one with its fix."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from awa.checks._render import print_line, print_summary
from awa.cli._common import Commands, add_command, print_json


def add_check(commands: Commands) -> None:
    command = add_command(
        commands,
        "check",
        description="Check the Microsoft 365 setup with small read-only calls and say "
        "how to fix what is red.",
    )
    command.add_argument(
        "groups",
        nargs="*",
        metavar="GROUP",
        help="microsoft or all (default all; both run the Microsoft 365 checks)",
    )
    command.add_argument(
        "--allow-post",
        action="append",
        default=[],
        choices=["teams"],
        help="allow ONE test message (teams: a card through the webhook or to the pinned channel)",
    )
    command.add_argument(
        "--no-model", action="store_true", help="check the Azure OpenAI route without calling it"
    )
    command.add_argument(
        "--project", default=".", metavar="DIR", help="project folder with .env (default .)"
    )
    command.add_argument("--json", action="store_true", help="print the report as JSON")
    command.set_defaults(handler=_check)


def _check(args: argparse.Namespace) -> int:
    from awa.checks import EXIT_CONFIGURATION, run_groups, save_report
    from awa.checks._env import project_env

    project = Path(args.project)
    allow = set(args.allow_post)
    if (project_env(project.resolve())("AWA_CHECK_ALLOW_POST") or "").strip().lower() == "teams":
        allow.add("teams")
    if not args.json:
        print("awa check: live, read-only calls")
    try:
        report = run_groups(
            args.groups,
            allow_post=allow,
            no_model=args.no_model,
            project=project,
            on_result=None if args.json else print_line,
        )
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_CONFIGURATION
    saved = save_report(report, project.resolve())
    if args.json:
        print_json({**report.to_dict(), "saved": str(saved)})
    else:
        for note in report.notes:
            print(f"note: {note}")
        print_summary(report)
        print(f"report saved: {saved}")
    return report.exit_code
