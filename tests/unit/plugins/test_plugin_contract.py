"""The plugin contract and plugin governance."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from _helpers.polling import SLOW_SUBPROCESS_TIMEOUT
from awa.blueprints import Blueprint
from awa.cli import main
from awa.cli._plugins import warn_security_off
from awa.plugins import list_plugins
from awa.plugins.switches import override
from awa.testing.plugin_contract import builtin_object, suite_of, verify


def test_every_built_in_passes_its_own_contract() -> None:
    keys = [
        row.key
        for row in list_plugins()
        if row.source == "builtin" and row.key and suite_of(row.key)
    ]
    assert {key.partition(".")[0] for key in keys} == {"parser", "connector", "tool", "guard"}
    for key in keys:
        result = verify(key, builtin_object(key))
        assert result.passed, result.summary()


def _naive_guard(statement: str, dialect: str | None = None) -> None:
    text = statement.strip().lower()
    if not text.startswith("select") or ";" in text:
        raise ValueError("not a select")
    if any(word in text for word in ("insert", "update", "delete", "drop")):
        raise ValueError("changes data")


def test_a_naive_sql_guard_does_not_pass_the_suite() -> None:
    result = verify("guard.sql_guard", _naive_guard)
    assert not result.passed
    failed = {case.name for case in result.cases if case.status == "failed"}
    assert "refuses: SELECT * INTO copy FROM orders" in failed
    assert "refuses: SELECT pg_terminate_backend(1)" in failed
    assert "refuses: SELECT pg_read_file('/etc/passwd')" in failed


def _raises(path: Path, ctx: object) -> None:
    raise ValueError("cannot read")


@pytest.mark.parametrize(
    ("key", "candidate", "case"),
    [
        ("parser.csv", _raises, "profiles_a_valid_sample"),
        ("parser.csv", "not callable", "is_a_function_of_path_and_context"),
        ("guard.sql_guard", "not callable", "is_a_function_of_statement_and_dialect"),
    ],
)
def test_a_failing_candidate_names_the_first_failed_case(
    key: str, candidate: object, case: str
) -> None:
    result = verify(key, candidate)
    assert not result.passed
    assert result.failed_case is not None and result.failed_case.name == case


def test_the_suites_run_without_importing_pytest() -> None:
    code = (
        "import sys\n"
        "from awa.testing.plugin_contract import verify, builtin_object\n"
        "result = verify('parser.csv', builtin_object('parser.csv'))\n"
        "assert result.passed, result.summary()\n"
        "assert 'pytest' not in sys.modules\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=SLOW_SUBPROCESS_TIMEOUT,
        check=False,
    )
    assert done.returncode == 0, done.stderr


BLUEPRINT = """\
name: demo
agents:
  - name: helper
    instructions: Help.
    model: test
components:
  guard:
    sql_guard: off
"""


def test_validate_reports_a_guard_switched_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "blueprint.yaml"
    path.write_text(BLUEPRINT, encoding="utf-8")
    findings = [i for i in Blueprint.from_yaml_text(BLUEPRINT).check() if "governance" in str(i)]
    assert findings and findings[0].severity == "warning"
    assert "guard.sql_guard" in str(findings[0])
    assert main(["validate", str(path)]) == 0
    assert "governance" in capsys.readouterr().out


def test_serve_and_worker_still_print_the_security_off_banner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with override({"guard.sql_guard": "off"}), caplog.at_level(logging.WARNING):
        assert warn_security_off("serve") == ["guard.sql_guard"]
    assert "guard.sql_guard" in caplog.text
    assert "awa serve starts" in caplog.text
    assert warn_security_off("worker") == []
