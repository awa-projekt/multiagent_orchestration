"""Fixtures of the export tests: the sample projects written into a temporary folder."""

from __future__ import annotations

from pathlib import Path

import pytest

from _helpers.export_projects import (
    DESK_BLUEPRINT,
    DESK_TOOLS,
    SKILL,
    STRUCTURED_BLUEPRINT,
    write_project,
)


@pytest.fixture
def desk(tmp_path: Path) -> Path:
    """The desk project: tools, a conditional and an unconditional approval, a denied tool,
    all four limits and a skill with a resource."""

    root = write_project(tmp_path / "desk", DESK_BLUEPRINT, DESK_TOOLS)
    skill = root / "skills" / "refund-policy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (skill / "reference.md").write_text("Limit: 500 EUR.", encoding="utf-8")
    return root


@pytest.fixture
def structured(tmp_path: Path) -> Path:
    """A project with an output schema and no tools."""

    return write_project(tmp_path / "classifier", STRUCTURED_BLUEPRINT)
