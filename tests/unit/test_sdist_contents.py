"""The source archive ships every top-level file and folder the tests read."""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"


def _tracked_top_level() -> set[str]:
    try:
        listed = subprocess.run(
            ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("needs a git checkout")
    return {line.split("/", 1)[0] for line in listed.splitlines() if line}


def test_the_sdist_includes_every_top_level_path_the_tests_name() -> None:
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    included = {
        path.strip("/").split("/", 1)[0]
        for path in config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    }
    text = "\n".join(path.read_text(encoding="utf-8") for path in TESTS.rglob("*.py"))
    named = {
        name
        for name in _tracked_top_level()
        if re.search(rf"""(?:ROOT|REPO) / ["']{re.escape(name)}["/']""", text)
    }
    assert named - included == set()


def test_every_path_the_sdist_includes_exists() -> None:
    config = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]
    missing = [p for p in patterns if not any(REPO.glob(p.strip("/")))]

    assert "/CHANGELOG.md" in patterns
    assert missing == []
