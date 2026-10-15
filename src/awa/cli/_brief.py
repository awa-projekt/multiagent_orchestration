"""`awa brief`: a brief for a coding assistant, written from the project folder."""

from __future__ import annotations

import argparse
from pathlib import Path

from awa.cli._common import EXIT_OK, Commands, add_command


def add_brief(commands: Commands) -> None:
    brief = add_command(
        commands,
        "brief",
        description="Write a brief for a coding assistant from the "
        "project folder: the process, the pattern, ready prompts, templates, how to attach MCP "
        "servers and what to run to verify. It reads the folder only and never the values in "
        ".env. Prints the brief, or writes it with --out. Exit code 1 when the folder has no "
        "blueprint.yaml.",
    )
    brief.add_argument(
        "--project",
        dest="brief_project",
        metavar="DIR",
        help="project folder (default: the nearest project folder)",
    )
    brief.add_argument(
        "--out",
        metavar="FILE",
        help="write the brief to FILE instead of printing it (BRIEF.md in the folder is the "
        "name the web app uses)",
    )
    brief.add_argument("--force", action="store_true", help="replace FILE when it exists")
    brief.set_defaults(handler=_brief)


def _brief(args: argparse.Namespace) -> int:
    from awa.blueprints import assistant_brief, write_brief
    from awa.core.config import find_project

    folder = find_project(project=args.brief_project or getattr(args, "project_folder", None))
    if args.out is None:
        print(assistant_brief(folder), end="")
        return EXIT_OK
    written = write_brief(folder, out=Path(args.out), force=args.force)
    print(f"wrote {written}")
    return EXIT_OK
