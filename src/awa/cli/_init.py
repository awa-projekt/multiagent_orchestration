"""`awa init <directory> [--template T]`: a runnable starter project from a template."""

from __future__ import annotations

import argparse
from pathlib import Path

from awa.cli._common import EXIT_OK, Commands, add_command

NEXT_STEPS: tuple[tuple[str, str], ...] = (
    ("cd {directory}", ""),
    ("pip install pytest", "if it is missing"),
    ("pytest", "offline tests, no key"),
    ("awa setup", "set your provider's key in .env (or: awa ui, then Set up)"),
    ("awa chat blueprint.yaml", "tools.py next to the blueprint is loaded by itself"),
)
"""What `awa init` prints after the files: the keyless tests first, then the key, then a chat."""


def add_init(commands: Commands) -> None:
    from awa.blueprints.scaffold import DEFAULT_TEMPLATE, TEMPLATES

    init = add_command(
        commands,
        "init",
        description="Create a starter project from a template: blueprint.yaml, tools.py, "
        "offline tests (tests/), eval cases (evals/cases.yaml), .env.example, .gitignore, "
        "README.md and pyproject.toml. Its tests run offline with pytest and need no key; chat, "
        "run and eval need a model key (awa setup).",
    )
    init.add_argument("directory", help="project directory; its name is the project name")
    init.add_argument(
        "--template", choices=list(TEMPLATES), help=f"project template (default {DEFAULT_TEMPLATE})"
    )
    init.add_argument(
        "--model", help="model profile or provider:model for the agents (default: default)"
    )
    init.add_argument("--force", action="store_true", help="overwrite existing project files")
    init.set_defaults(handler=_init)


def _init(args: argparse.Namespace) -> int:
    from awa.blueprints.scaffold import DEFAULT_TEMPLATE, create_project

    directory = Path(args.directory)
    template = args.template or DEFAULT_TEMPLATE
    written = create_project(
        directory, template=template, model=args.model or "default", force=args.force
    )
    print(f"created {directory} (template {template}):")
    for path in written:
        print(f"  {path.relative_to(directory).as_posix()}")
    print()
    print("next:")
    for command, comment in NEXT_STEPS:
        command = command.format(directory=directory)
        print(f"  {command:<26} {comment}".rstrip())
    return EXIT_OK
