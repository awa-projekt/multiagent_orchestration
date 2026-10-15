"""`awa plugins list|show|check|switch`."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from awa.cli import main

BLUEPRINT = "name: demo\nagents:\n  - name: helper\n    instructions: Help.\n    model: test\n"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("AWA_PROJECT_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "blueprint.yaml").write_text(BLUEPRINT, encoding="utf-8")
    yield tmp_path


def test_list_json_and_kind_filters(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["plugins", "list", "--kind", "skills", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows and {row["kind"] for row in rows} == {"skills"}
    assert main(["plugins", "show", "parser.csv"]) == 0
    assert "name: csv" in capsys.readouterr().out


def test_check_passes_for_a_builtin_and_fails_for_a_bad_replacement(project: Path) -> None:
    (project / "bad_parser.py").write_text(
        "def profile_csv(path, ctx):\n    return ctx.profile\n", encoding="utf-8"
    )
    assert main(["plugins", "check", "parser.csv"]) == 0
    assert main(["plugins", "check", "parser.csv", "--target", "bad_parser:profile_csv"]) == 1


def test_switch_writes_the_blueprint_and_dry_run_writes_nothing(project: Path) -> None:
    assert main(["plugins", "switch", "parser.pdf", "off", "--dry-run"]) == 0
    assert "components" not in (project / "blueprint.yaml").read_text(encoding="utf-8")
    assert main(["plugins", "switch", "parser.pdf", "off"]) == 0
    assert "pdf: false" in (project / "blueprint.yaml").read_text(encoding="utf-8")


def test_switch_without_a_blueprint_or_with_several_exits_1(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "blueprint.yaml").rename(project / "one.yaml")
    assert main(["plugins", "switch", "parser.pdf", "off"]) == 1
    assert "create blueprint.yaml or pass --blueprint" in capsys.readouterr().err
    (project / "blueprint_a.yaml").write_text(BLUEPRINT, encoding="utf-8")
    (project / "blueprint_b.yaml").write_text(BLUEPRINT, encoding="utf-8")
    assert main(["plugins", "switch", "parser.pdf", "off"]) == 1
    assert "several blueprints" in capsys.readouterr().err


def test_an_unknown_name_exits_1_with_a_suggestion(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["plugins", "show", "tool.web_serch"]) == 1
    assert "tool.web_search" in capsys.readouterr().err


def test_switch_survives_an_empty_kind_key(project: Path) -> None:
    path = project / "blueprint.yaml"
    path.write_text(BLUEPRINT + "components:\n  parser:\n", encoding="utf-8")
    assert main(["plugins", "switch", "parser.pdf", "off"]) == 0
    assert "pdf: false" in path.read_text(encoding="utf-8")


def test_check_finds_a_target_next_to_the_command(project: Path) -> None:
    (project / "csv_ok.py").write_text(
        "from awa.data._parse import builtin_parsers\n"
        "profile_csv = {k: p for k, p, _ in builtin_parsers()}['csv']\n",
        encoding="utf-8",
    )
    assert main(["plugins", "check", "parser.csv", "--target", "csv_ok:profile_csv"]) == 0
