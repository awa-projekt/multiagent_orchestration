"""The untracked files of a git checkout, for the check that a test run leaves none behind."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

CACHE_PARTS = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", ".cache"}
)
"""Path parts that mark a cache; files under them are never reported."""


def untracked_files(root: Path) -> frozenset[str] | None:
    """The untracked, not ignored files under `root` (outside caches), or None without git."""

    git = shutil.which("git")
    if git is None or not (root / ".git").exists():
        return None
    completed = subprocess.run(
        [git, "status", "--porcelain", "--untracked-files=all", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    paths = (
        entry[3:].decode("utf-8", "replace")
        for entry in completed.stdout.split(b"\0")
        if entry.startswith(b"?? ")
    )
    return frozenset(path for path in paths if CACHE_PARTS.isdisjoint(path.split("/")))
