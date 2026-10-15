"""Every recipe and use case has a `process.md`: the process in plain words, as its owner tells it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
FOLDERS = sorted(
    p
    for kind in ("usecases", "recipes")
    for p in (EXAMPLES / kind).iterdir()
    if (p / "blueprint.yaml").is_file()
)


@pytest.mark.parametrize("folder", FOLDERS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_every_example_has_a_plain_process_description(folder: Path) -> None:
    path = folder / "process.md"
    assert path.is_file(), f"{folder.name} has no process.md"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    assert lines[0].startswith("# ") and len(lines[0]) > 3
    body = "\n".join(lines[1:]).strip()
    assert len(body.split()) >= 40
    assert "`" not in body, "no code or tool names in a user's description"
    assert not re.search(r"connector:|tools\.py|blueprint", body)
    assert text.endswith("\n") and not text.endswith("\n\n")
