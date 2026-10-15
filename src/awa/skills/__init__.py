"""Agent Skills: reusable instruction sets loaded from `SKILL.md` files.

A skill is disclosed progressively: name and description form a catalog (`render_index`), the
instructions go into a prompt (`compose_prompt`) or are loaded on demand (`create_skill_tools`),
and resource files are read only when needed (`Skill.read_resource`).
"""

from awa.skills.registry import SkillRegistry, compose_prompt
from awa.skills.skill import Skill
from awa.skills.tools import DEFAULT_MAX_RESOURCE_CHARS, create_skill_tools

__all__ = [
    "DEFAULT_MAX_RESOURCE_CHARS",
    "Skill",
    "SkillRegistry",
    "compose_prompt",
    "create_skill_tools",
]
