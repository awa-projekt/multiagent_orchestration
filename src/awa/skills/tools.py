"""Model-facing `load_skill` and `read_skill_resource` tools for loading skills on demand."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

from pydantic import Field, create_model

from awa._internal.upstream.tools import BaseTool, StructuredTool
from awa.errors import SkillNotFoundError, SkillReferenceNotFoundError
from awa.skills.registry import SkillRegistry
from awa.skills.skill import Skill

DEFAULT_MAX_RESOURCE_CHARS = 50_000
"""Default number of characters `read_skill_resource` returns before truncating."""


def create_skill_tools(
    registry: SkillRegistry,
    *,
    allowed: Sequence[str] | None = None,
    max_resource_chars: int = DEFAULT_MAX_RESOURCE_CHARS,
) -> list[BaseTool]:
    """Create the `load_skill` and `read_skill_resource` tools; pair them with `render_index`.

    Errors (unknown skill, unknown or unsafe resource path) are returned to the model as text.

    Args:
        registry: Registry the tools read from; without `allowed`, later additions are reachable.
        allowed: Restrict to these skill names (an enum in the schema); empty yields no tools.
        max_resource_chars: Longest resource text returned; longer files are cut with a notice.

    Raises:
        SkillNotFoundError: If a name in `allowed` is not registered.
        TypeError: If `allowed` is a single string.
        ValueError: If `max_resource_chars` is smaller than 1.

    Example:
        ```python
        tools = create_skill_tools(registry, allowed=["code-review", "security-review"])
        ```
    """

    if max_resource_chars < 1:
        raise ValueError("max_resource_chars must be at least 1")
    permitted: tuple[str, ...] | None = None
    if allowed is not None:
        if isinstance(allowed, str):
            raise TypeError("allowed must be a sequence of skill names, not a single string")
        permitted = tuple(dict.fromkeys(allowed))
        if not permitted:
            return []
        for name in permitted:
            registry.get(name)

    name_type: Any = str if permitted is None else Literal[permitted]
    name_help = "Name of the skill, exactly as listed in the skill catalog."
    load_args = create_model("LoadSkillArgs", name=(name_type, Field(description=name_help)))
    resource_args = create_model(
        "ReadSkillResourceArgs",
        name=(name_type, Field(description=name_help)),
        path=(
            str,
            Field(
                description=(
                    "Resource path relative to the skill directory, as listed by "
                    "`load_skill` (for example 'references/checklist.md')."
                )
            ),
        ),
    )

    def lookup(name: str) -> Skill:
        if permitted is not None and name not in permitted:
            raise SkillNotFoundError(
                f"Skill {name!r} is not available. Available skills: {', '.join(permitted)}"
            )
        return registry.get(name)

    def load_skill(name: str) -> str:
        try:
            return _render_loaded(lookup(name))
        except SkillNotFoundError as exc:
            return f"Error: {exc}"

    def read_skill_resource(name: str, path: str) -> str:
        try:
            content = lookup(name).read_resource(path)
        except (SkillNotFoundError, SkillReferenceNotFoundError) as exc:
            return f"Error: {exc}"
        if len(content) <= max_resource_chars:
            return content
        return (
            f"{content[:max_resource_chars]}\n\n[Truncated: showing the first "
            f"{max_resource_chars} of {len(content)} characters.]"
        )

    async def aload_skill(name: str) -> str:
        return load_skill(name)

    return [
        StructuredTool.from_function(
            func=load_skill,
            coroutine=aload_skill,
            name="load_skill",
            description=(
                "Load the full instructions of a skill from the skill catalog, plus the "
                "list of files bundled with it. Call this before applying a skill."
            ),
            args_schema=load_args,
        ),
        StructuredTool.from_function(
            # No coroutine: LangChain runs the file read in a worker thread, off the event loop.
            func=read_skill_resource,
            name="read_skill_resource",
            description=(
                "Read one file bundled with a skill (a reference, template or script), "
                "using a path listed by `load_skill`."
            ),
            args_schema=resource_args,
        ),
    ]


def _render_loaded(skill: Skill) -> str:
    sections = [
        f"# Skill: {skill.name} (v{skill.version})",
        "If your instructions give an output format or a process, follow them; use the "
        "formats and templates below only where they do not.",
        skill.instructions,
    ]
    if skill.allowed_tools:
        sections.append(f"Tools this skill expects: {', '.join(skill.allowed_tools)}")
    if skill.resources:
        listing = "\n".join(f"- {resource}" for resource in skill.resources)
        sections.append(
            "Bundled resources (read with `read_skill_resource` when the instructions "
            f"refer to them):\n{listing}"
        )
    return "\n\n".join(sections)
