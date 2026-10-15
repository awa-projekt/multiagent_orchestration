"""The skill matrix page is generated from the builtin skills and must not drift."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

from awa.skills import SkillRegistry

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gen_skill_matrix.py"
PAGE = ROOT / "docs" / "reference" / "skills.md"

_spec = importlib.util.spec_from_file_location("gen_skill_matrix_for_drift", SCRIPT)
assert _spec and _spec.loader
matrix = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = matrix
_spec.loader.exec_module(matrix)


def test_matrix_page_matches_the_builtin_skills() -> None:
    assert PAGE.read_text(encoding="utf-8").replace("\r\n", "\n") == matrix.render(), (
        "run: python scripts/gen_skill_matrix.py"
    )


def test_matrix_has_one_row_per_skill_with_no_empty_cell() -> None:
    rows = [line for line in matrix.render().splitlines() if line.startswith("| `")]
    cells = [re.split(r"(?<!\\)\|", row)[1:-1] for row in rows]

    assert [row[0].strip(" `") for row in cells] == sorted(SkillRegistry.builtin().names())
    assert all(len(row) == 8 and all(cell.strip() for cell in row) for row in cells)


def test_matrix_check_mode_passes_on_the_current_page() -> None:
    ok = subprocess.run([sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr


def test_matrix_row_mentions_bundled_files_and_named_skills() -> None:
    page = matrix.render()

    assert "`references/match-tolerances.md`" in page
    for name in ("email-triage", "quotation-drafting", "data-subject-requests"):
        assert f"| `{name}` |" in page
