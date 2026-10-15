"""The session check that a test run leaves no new untracked file in the checkout."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from _helpers.clean_tree import untracked_files

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git_repository(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    return root


def test_a_new_file_is_reported_and_caches_and_ignored_files_are_not(tmp_path: Path) -> None:
    root = _git_repository(tmp_path / "repo")
    before = untracked_files(root)
    for name in ("left/over.txt", "pkg/__pycache__/m.pyc", ".pytest_cache/v", "ignored/out.txt"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x", encoding="utf-8")

    after = untracked_files(root)

    assert before is not None and after is not None
    assert after - before == {"left/over.txt"}


def test_a_folder_without_git_is_not_checked(tmp_path: Path) -> None:
    assert untracked_files(tmp_path) is None
