"""Skills as plain tools: `load_skill` reads a skill's instructions, `read_skill_resource` one of
the files bundled with it.

A skill is a folder `skills/<name>/` with a `SKILL.md` (YAML frontmatter, then the Markdown
instructions) and optional resource files. The system prompt names the skills; the model loads
the full text only when it needs it. Errors reach the model as tool text, never as exceptions.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, create_model

MAX_RESOURCE_CHARS = 50_000
"""Longest resource text returned; a longer file is cut with a notice."""


def _body(text: str) -> str:
    """The Markdown after the frontmatter of a `SKILL.md`."""

    if text.startswith("---"):
        _, _, rest = text[3:].partition("\n---")
        text = rest.partition("\n")[2]
    return text.strip()


def _resources(folder: Path) -> list[str]:
    return sorted(
        relative.as_posix()
        for path in folder.rglob("*")
        if path.is_file()
        and (relative := path.relative_to(folder)).as_posix() != "SKILL.md"
        and not any(part.startswith(".") or part == "__pycache__" for part in relative.parts)
    )


def skill_tools(root: Path, names: Sequence[str], *, load: bool = True) -> list[BaseTool]:
    """The tools that load the skills `names` from the folders under `root`.

    Args:
        root: The folder that holds one folder per skill.
        names: The skills the agent has.
        load: Add `load_skill`; off when the instructions are already in the prompt.

    `read_skill_resource` is added only when a skill bundles files besides `SKILL.md`.
    """

    allowed: Any = Literal[tuple(names)]
    arguments: dict[str, type[BaseModel]] = {
        "load": create_model("LoadSkillArgs", name=(allowed, Field(description="Skill name."))),
        "read": create_model(
            "ReadSkillResourceArgs",
            name=(allowed, Field(description="Skill name.")),
            path=(str, Field(description="Resource path inside the skill folder.")),
        ),
    }

    def load_skill(name: str) -> str:
        """Load the full instructions of a skill before you apply it."""

        return _body((root / name / "SKILL.md").read_text(encoding="utf-8-sig"))

    def read_skill_resource(name: str, path: str) -> str:
        """Read a file bundled with a skill, as the skill's instructions name it."""

        folder = (root / name).resolve()
        target = (folder / path).resolve()
        if target == folder or folder not in target.parents or not target.is_file():
            return f"Unknown resource {path!r} of skill {name!r}."
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > MAX_RESOURCE_CHARS:
            return text[:MAX_RESOURCE_CHARS] + "\n[truncated]"
        return text

    tools: list[BaseTool] = (
        [StructuredTool.from_function(load_skill, args_schema=arguments["load"])] if load else []
    )
    if any(_resources(root / name) for name in names):
        tools.append(
            StructuredTool.from_function(read_skill_resource, args_schema=arguments["read"])
        )
    return tools
