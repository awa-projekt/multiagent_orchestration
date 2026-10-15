"""`awa show dry-run <blueprint>` and `awa.blueprints.dry_run`: the path a run would take with
given values (checks, special cases, retries, pauses), free and the same every time."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from awa.blueprints import Blueprint, dry_run
from awa.cli import main
from awa.persistence import StateInMemorySaver

ROOT = Path(__file__).resolve().parents[2]
INVOICE_CHECK = ROOT / "examples" / "processes" / "invoice-check" / "blueprint.yaml"

ROUTE = """\
name: invoice-route
agents:
  - name: reader
    description: Reads one invoice.
    output_schema:
      type: object
      properties: {supplier: {type: string}, amount_eur: {type: number}}
      required: [supplier, amount_eur]
  - name: board
    description: Decides on very large invoices.
  - name: archivist
    description: Files the invoice.
process:
  - name: extract
    agent: reader
    retry: {max_attempts: 3, initial: 10s}
    after_last_try: ask
    checks:
      - {kind: rule, fail_when: "amount_eur <= 0", message: No amount found.}
  - {name: board_review, agent: board, when: "amount_eur >= 50000"}
  - name: archive
    agent: archivist
special_cases:
  extract:
    cases:
      - {if: "supplier in ['Blocked Supplies Ltd']", then: reject, message: Blocked supplier.}
      - {if: [{field: amount_eur, op: ">=", value: 5000}], then: ask, by: finance}
      - {if: [{field: amount_eur, op: "<", value: 5000}], then: approve}
    else: ask
"""


@pytest.fixture
def route(tmp_path: Path) -> Path:
    path = tmp_path / "route.yaml"
    path.write_text(ROUTE, encoding="utf-8")
    return path


def show(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    code = main(["show", "dry-run", *args])
    return code, capsys.readouterr().out


def test_the_example_process_goes_through_with_sample_values(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out = show(capsys, str(INVOICE_CHECK))

    assert code == 0
    assert "dry run of invoice-check: 3 step(s), no model" in out
    assert out.count("passed its checks.") == 3
    assert "result: completed" in out


def test_every_repository_example_blueprint_dry_runs(capsys: pytest.CaptureFixture[str]) -> None:
    examples = sorted(ROOT.glob("examples/**/blueprint.yaml"))
    assert examples
    for path in examples:
        code, out = show(capsys, str(path))
        assert code == 0, (path, out)
        assert "no process" in out or "result: completed" in out, (path, out)


def test_retries_waits_and_a_rule_row_are_on_the_path(
    route: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = show(
        capsys,
        str(route),
        "--input",
        '{"supplier": "Acme Tools", "amount_eur": 1200}',
        "--fail",
        "extract=2",
    )

    assert code == 0
    assert "Try 1 of 3 failed: ModelProviderError: simulated failure of try 1 (dry run). " in out
    assert "A real run waits 10 s, then tries again." in out
    assert "A real run waits 20 s, then tries again." in out
    assert (
        "passed its checks (try 3). Special case row 3 (If amount is less than 5,000 EUR: "
        "approved without asking a person)." in out
    )
    assert "board_review  Skipped: amount_eur >= 50000 is false." in out
    assert "result: completed, 30 s of waits between tries" in out


def test_a_pause_is_shown_and_answered_as_chosen(
    route: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    values = '{"supplier": "Acme Tools", "amount_eur": 6200}'
    code, out = show(capsys, str(route), "--input", values, "--decide", "reject")

    assert code == 0
    assert (
        "PAUSE: a person is asked: Special case: row 2: a person decides (finance). "
        "The dry run answers reject." in out
    )
    assert "result: rejected, 1 question(s) to a person" in out


def test_a_reject_row_ends_the_path_and_a_failed_step_exits_with_one(
    route: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    values = tmp_path / "blocked.yaml"
    values.write_text("supplier: Blocked Supplies Ltd\namount_eur: 300\n", encoding="utf-8")
    code, out = show(capsys, str(route), "--input", str(values))
    assert code == 0 and "REJECTED by special case row 1" in out

    code, out = show(capsys, str(INVOICE_CHECK), "--input", '{"total": 0}')
    assert code == 1 and "result: failed" in out


def test_after_the_last_try_the_path_asks_a_person(
    route: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = show(capsys, str(route), "--input", '{"amount_eur": 10}', "--fail", "extract=3")

    assert code == 0
    assert "PAUSE: a person is asked: All 3 tries failed" in out


def test_wrong_options_say_the_fix(route: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show", "dry-run", str(route), "--fail", "nowhere"]) == 1
    assert "--fail names nowhere, which the process does not have" in capsys.readouterr().err
    assert main(["show", "dry-run", str(route), "--input", "[1, 2]"]) == 1
    assert "--input must be an object" in capsys.readouterr().err


async def test_the_dry_run_is_deterministic_and_fast() -> None:
    blueprint = Blueprint.from_yaml_text(ROUTE)
    values = {"supplier": "Acme Tools", "amount_eur": 1200}

    started = time.monotonic()
    first = await dry_run(blueprint, values, checkpointer=StateInMemorySaver(), fail={"extract": 2})
    second = await dry_run(
        blueprint, values, checkpointer=StateInMemorySaver(), fail={"extract": 2}
    )

    assert first.lines == second.lines and first.waits == second.waits == [10.0, 20.0]
    assert time.monotonic() - started < 5.0
    assert first.notes[0] == "No model was asked, no tool was called and no document was read."


async def test_two_steps_of_one_agent_keep_their_own_values_and_failures() -> None:
    blueprint = Blueprint.from_yaml_text(
        """\
name: twice
agents:
  - name: writer
    description: Writes a line.
    output_schema: {type: object, properties: {text: {type: string}}}
process:
  - {name: draft, agent: writer}
  - {name: polish, agent: writer, retry: {max_attempts: 2, initial: 1s}}
"""
    )
    values = {"draft": {"text": "first"}, "polish": {"text": "second"}}

    path = await dry_run(blueprint, values, checkpointer=StateInMemorySaver(), fail={"polish": 1})

    assert path.outcome == "completed"
    assert path.waits == [1.0]
    assert [(line.step, line.attempt) for line in path.lines if line.status == "retrying"] == [
        ("polish", 1)
    ]


def test_awa_show_lists_the_branches_of_each_step(
    route: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["show", str(route)]) == 0
    out = capsys.readouterr().out
    assert "tries: up to 3, again after a failed error and a failed check" in out
    assert "after the last try: ask" in out
    assert "runs only when: amount_eur >= 50000" in out
    assert "special case: If amount is 5,000 EUR or more: a person decides (finance)" in out
