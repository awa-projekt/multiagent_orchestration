"""`awa data run`: run a project built with `awa build --data` on its input files, each on a
fresh copy of `data/`."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from awa.cli._common import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_PAUSED,
    add_limit_options,
    add_tools_options,
    blueprint_path,
    limits_from_args,
    print_json,
)

if TYPE_CHECKING:
    from awa.blueprints import RunOutcome


def add_data_subcommands(actions: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add `awa data run` (called by `awa data` for every module listed in `_EXTENSIONS`)."""

    run = actions.add_parser(
        "run", help="run a project built with --data on its input files; results go to data/output/"
    )
    run.add_argument("project", help="project folder (or its blueprint.yaml)")
    which = run.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--file",
        action="append",
        metavar="NAME",
        help="an input file or glob (matched against the name below data/files/), repeatable",
    )
    which.add_argument(
        "--all",
        action="store_true",
        help="run on every input file (refused when several files exist and none is marked "
        "role: reference: name them with --file)",
    )
    run.add_argument("--model", help="model profile or provider:model for every agent")
    run.add_argument("--json", action="store_true", help="print the outcomes as JSON")
    run.add_argument(
        "--no-input",
        action="store_true",
        help="never ask for approvals: a run that needs one ends paused (exit code 2)",
    )
    add_tools_options(run)
    add_limit_options(run, "run")
    run.set_defaults(handler=_run)


def _run(args: argparse.Namespace) -> int:
    from awa._internal import aio as asyncio_compat
    from awa.blueprints import Blueprint, LocalRunFiles, run_project
    from awa.blueprints._resolve import load_tools, skill_registry, tools_files
    from awa.blueprints._run_data import unlabelled_inputs
    from awa.cli._common import ask_decision
    from awa.cli._preflight import preflight
    from awa.errors import ConfigurationError

    path = blueprint_path(args.project)
    preflight(path)
    blueprint = Blueprint.from_yaml(path)
    if args.all and (names := unlabelled_inputs(blueprint, LocalRunFiles(path.parent))):
        raise ConfigurationError(
            f"--all would run all {len(names)} files as inputs ({', '.join(names)}), because the "
            "blueprint marks no file as a lookup file (`role: reference` in its data: section). "
            "Name the files to run with --file (a glob such as --file '*' takes every file), or "
            "mark the lookup files `role: reference` in blueprint.yaml"
        )
    tools = load_tools(tools_files(args.tools, path))
    asking = sys.stdin.isatty() and not args.no_input

    def show(outcome: RunOutcome) -> None:
        if not args.json:
            wrote = f"; wrote {', '.join(outcome.outputs)}" if outcome.outputs else ""
            print(f"{outcome.file}: {outcome.status}{wrote} ({outcome.report})")

    outcomes = asyncio_compat.run(
        run_project(
            blueprint,
            LocalRunFiles(path.parent),
            select=None if args.all else args.file,
            tools=tools,
            skills=skill_registry(args.skills, path),
            model=args.model,
            usage_limits=limits_from_args(args, blueprint),
            decide=ask_decision if asking else None,
            allow_imports=True,
            on_outcome=show,
        )
    )
    if args.json:
        print_json([outcome.to_dict() for outcome in outcomes])
    statuses = {outcome.status for outcome in outcomes}
    if "failed" in statuses:
        for outcome in outcomes:
            if outcome.status == "failed":
                print(f"{outcome.file}: {outcome.error}", file=sys.stderr)
        return EXIT_ERROR
    if "paused" in statuses:
        print(
            "A run is paused for an approval; run this command in a terminal to decide.",
            file=sys.stderr,
        )
        return EXIT_PAUSED
    return EXIT_OK
