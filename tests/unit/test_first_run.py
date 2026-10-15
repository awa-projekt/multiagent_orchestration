"""The first-run check: its rules run by default, the real wheel build is marked `first_run`.

Run the real check (builds a wheel, creates an environment, needs uv and a package index or
cache, a few minutes): `python -m pytest -m first_run tests/unit/test_first_run.py`. With
`AWA_TEST_WHEEL` set it installs that wheel instead of building one.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from _helpers.wheel import WHEEL_VARIABLE, prebuilt_wheel

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "first_run_check", ROOT / "scripts" / "first_run_check.py"
)
assert _SPEC is not None and _SPEC.loader is not None
check = importlib.util.module_from_spec(_SPEC)
sys.modules["first_run_check"] = check
_SPEC.loader.exec_module(check)


def test_a_traceback_is_a_problem() -> None:
    text = "Traceback (most recent call last):\n  File x"
    assert check.output_problems(text) == ["prints a traceback"]


def test_a_warning_needs_a_fix_line() -> None:
    bad = "  WARN  credentials  absent\n  OK    thing\n"
    good = "  WARN  credentials  absent\n        fix: set the key\n"
    assert check.output_problems(bad) == ["message without a fix: WARN  credentials  absent"]
    assert check.output_problems(good) == []
    assert check.output_problems("error: nope") != []
    assert check.output_problems("Result: usable") == []


def test_clean_environment_has_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("AWA_HOME", "x")
    assert not [k for k in check.clean_environment() if k.startswith(("ANTHROPIC", "AWA_"))]


def test_a_work_folder_inside_the_repo_is_refused(tmp_path: Path) -> None:
    report = check.run_check(tmp_path, tmp_path / "inside")
    assert not report.ok
    assert report.commands == 0


def test_more_than_five_commands_fail() -> None:
    report = check.Report(steps=[check.Step(f"c{n}", 0.1, 0) for n in range(6)])
    assert not check.judge(report).ok
    assert "limit is 5" in report.failures[0]
    five = check.Report(steps=[check.Step(f"c{n}", 0.1, 0) for n in range(5)])
    assert check.judge(five).ok


def test_the_install_tests_take_the_wheel_the_variable_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(WHEEL_VARIABLE, raising=False)
    assert prebuilt_wheel() is None
    wheel = tmp_path / "awa_agents-1.0-py3-none-any.whl"
    monkeypatch.setenv(WHEEL_VARIABLE, str(wheel))
    with pytest.raises(FileNotFoundError, match=WHEEL_VARIABLE):
        prebuilt_wheel()
    wheel.write_bytes(b"")
    assert prebuilt_wheel() == wheel


@pytest.mark.first_run
def test_a_fresh_install_follows_the_keyless_path() -> None:
    # A short temp folder: the pytest tmp_path is deep, and a deep Windows path breaks imports.
    workdir = Path(tempfile.mkdtemp(prefix="awa-fr-"))
    try:
        report = check.run_check(ROOT, workdir, wheel=prebuilt_wheel())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    assert report.ok, check.render(report)
    assert report.commands <= check.MAX_COMMANDS
