"""`awa show cases <blueprint>`: special cases in words and their boundary tests, and `awa show
dry-run <blueprint>`: the path a run would take with given values; both without a model."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from awa.cli._common import EXIT_ERROR, EXIT_OK, print_table

if TYPE_CHECKING:
    from awa.blueprints import Blueprint


def show_cases(blueprint: Blueprint) -> int:
    """Print every special-case table of `blueprint`: its rows in words, warnings and notes,
    then the boundary table (row, probe, input, expected outcome)."""

    from awa.workflows import boundary_cases, case_notes

    if not blueprint.special_cases:
        print(f"{blueprint.name}: no special cases")
        return EXIT_OK
    for position, (name, table) in enumerate(blueprint.special_cases.items()):
        if position:
            print()
        print(f"special cases {name}: {len(table.cases)} row(s), first match wins")
        lines = table.describe()
        for number, line in enumerate(lines[: len(table.cases)], 1):
            print(f"  {number}. {line}")
        for line in lines[len(table.cases) :]:
            print(f"  {line}")
        for warning in table.warnings:
            print(f"  warning: {warning}")
        for note in case_notes(table):
            print(f"  note: {note.text}")
        print()
        rows = [("row", "probe", "input", "expected")]
        rows += [
            (str(case.row), case.probe, _input(case.values), case.expected.describe())
            for case in boundary_cases(table)
        ]
        print_table(rows, free_last=True)
    return EXIT_OK


def show_dry_run(
    blueprint: Blueprint, values: str | None, fails: Sequence[str], decide: str
) -> int:
    """Print the path a run of `blueprint`'s process would take with `values`, without a model.

    Exit code 0 when the run would go through or end as rejected, 1 when a step would fail.

    Raises:
        BlueprintError: If the values, a `--fail` entry or the process are not usable.
    """

    from awa._internal.aio import run
    from awa._internal.upstream.checkpoint import StateInMemorySaver
    from awa.blueprints import dry_run

    if blueprint.process is None:
        print(f"{blueprint.name}: no process, so there is no path to walk")
        return EXIT_OK
    path = run(
        dry_run(
            blueprint,
            _values(values),
            checkpointer=StateInMemorySaver(),
            fail=_fails(fails),
            decide="reject" if decide == "reject" else "approve",
        )
    )
    print(f"dry run of {blueprint.name}: {len(blueprint.process.steps)} step(s), no model")
    rows = [("step", "what happens")]
    rows += [(line.step, line.text) for line in path.lines]
    print_table(rows, free_last=True)
    print()
    asked = f", {path.pauses} question(s) to a person" if path.pauses else ""
    waited = f", {sum(path.waits):g} s of waits between tries" if path.waits else ""
    print(f"result: {path.outcome}{asked}{waited}")
    if path.message:
        print(f"  {path.message}")
    for note in path.notes:
        print(f"note: {note}")
    return EXIT_ERROR if path.outcome == "failed" else EXIT_OK


def _values(text: str | None) -> dict[str, Any]:
    """`--input`: a JSON object, or a .json/.yaml/.yml file holding one."""

    from awa.blueprints.errors import BlueprintError

    if not text:
        return {}
    source = text
    candidate = Path(text)
    if candidate.suffix.lower() in (".json", ".yaml", ".yml"):
        if not candidate.is_file():
            raise BlueprintError(f"--input file {text!r} does not exist")
        source = candidate.read_text(encoding="utf-8")
    try:
        loaded = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise BlueprintError(f"--input is not JSON or YAML: {exc}") from None
    if not isinstance(loaded, dict):
        raise BlueprintError('--input must be an object of field values, such as {"total": 120}')
    return {str(key): value for key, value in loaded.items()}


def _fails(entries: Sequence[str]) -> dict[str, int]:
    """`--fail STEP[=TRIES]` entries by step."""

    from awa.blueprints.errors import BlueprintError

    fails: dict[str, int] = {}
    for entry in entries:
        step, _, count = entry.partition("=")
        if not step.strip() or (count and not count.strip().isdigit()):
            raise BlueprintError(f"--fail {entry!r}: write STEP or STEP=TRIES, such as extract=2")
        fails[step.strip()] = int(count) if count else 1
    return fails


def _input(values: dict[str, Any]) -> str:
    """The probe's values on one line: `amount_eur=4999, supplier="Acme"`."""

    return (
        ", ".join(
            f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in values.items()
        )
        or "(nothing)"
    )
