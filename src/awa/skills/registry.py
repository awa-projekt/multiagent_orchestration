"""`SkillRegistry`, a named collection of skills, and prompt composition."""

from __future__ import annotations

import difflib
import functools
from collections.abc import Iterable, Sequence
from pathlib import Path

from awa._internal.util import name_taken
from awa.errors import SkillAlreadyRegisteredError, SkillNotFoundError
from awa.skills.skill import SKILL_FILENAME, Skill

_BUILTIN_DIR = Path(__file__).parent / "builtin"


class SkillRegistry:
    """A mutable collection of skills addressed by name; listings are sorted by name."""

    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        """Create a registry from skills with unique names.

        Raises:
            SkillAlreadyRegisteredError: If two skills share a name.
        """

        self._skills: dict[str, Skill] = {}
        for skill in skills:
            self.add(skill)

    @classmethod
    def builtin(cls) -> SkillRegistry:
        """A new registry with every shipped skill the blueprint does not switch off."""

        from awa.plugins.switches import is_off

        return cls(skill for skill in _builtin_skills() if not is_off(f"skill.{skill.name}"))

    @classmethod
    def from_directory(cls, *paths: Path | str, include_builtin: bool = False) -> SkillRegistry:
        """Load skills from skill folders, `SKILL.md` files, or roots whose subfolders are skills.

        A later path replaces an earlier skill of the same name, and every path wins over builtin
        skills.

        Raises:
            SkillNotFoundError: If a path does not exist.
            SkillManifestError: If a `SKILL.md` is invalid.

        Example:
            ```python
            registry = SkillRegistry.from_directory("skills", include_builtin=True)
            ```
        """

        registry = cls.builtin() if include_builtin else cls()
        for raw_path in paths:
            for skill in _load_location(Path(raw_path)):
                registry.add(skill, replace=True)
        return registry

    def add(self, skill: Skill, *, replace: bool = False) -> None:
        """Register a skill.

        Raises:
            SkillAlreadyRegisteredError: If the name is taken and `replace` is false.
        """

        if skill.name in self._skills and not replace:
            raise SkillAlreadyRegisteredError(name_taken("skill", skill.name))
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill:
        """The skill registered under `name`.

        Raises:
            SkillNotFoundError: If no skill has that name; the message suggests close matches.
        """

        try:
            return self._skills[name]
        except KeyError:
            raise SkillNotFoundError(self._unknown_message(name)) from None

    def __contains__(self, name: object) -> bool:
        return name in self._skills

    def __len__(self) -> int:
        return len(self._skills)

    def names(self) -> list[str]:
        """Return all skill names, sorted."""

        return sorted(self._skills)

    def all(self) -> list[Skill]:
        """Return all skills, sorted by name."""

        return [self._skills[name] for name in self.names()]

    def index(self) -> list[tuple[str, str]]:
        """`(name, description)` pairs sorted by name, enough to pick a skill."""

        return [(skill.name, skill.description) for skill in self.all()]

    def render_index(self, names: Sequence[str] | None = None) -> str:
        """A skill catalog for a system prompt that tells the model to call `load_skill` first.

        Args:
            names: Skills to list, in this order; `None` lists every skill.

        Returns:
            The catalog, or an empty string when there is nothing to list.

        Raises:
            SkillNotFoundError: If a name is not registered.
            TypeError: If `names` is a single string.
        """

        if isinstance(names, str):
            raise TypeError("names must be a sequence of skill names, not a single string")
        skills = self.all() if names is None else [self.get(name) for name in dict.fromkeys(names)]
        if not skills:
            return ""
        lines = [
            "## Skills",
            "",
            "Each skill below is a set of instructions for one kind of work. Before you "
            "apply a skill, call `load_skill` with its name to read its full instructions. "
            "If your instructions give an output format or a process, follow them; a skill "
            "only adds know-how."
            + (
                " Call `read_skill_resource` only for files those instructions refer to."
                if any(skill.resources for skill in skills)
                else ""
            ),
            "",
        ]
        lines.extend(f"- `{skill.name}`: {skill.description}" for skill in skills)
        return "\n".join(lines)

    def _unknown_message(self, name: str) -> str:
        message = f"Unknown skill: {name!r}."
        close = difflib.get_close_matches(name, self._skills, n=3)
        if close:
            return f"{message} Did you mean: {', '.join(close)}?"
        return f"{message} Registered skills: {', '.join(self.names()) or 'none'}."


def compose_prompt(
    base_prompt: str, skills: Sequence[str | Skill], registry: SkillRegistry | None = None
) -> str:
    """Append the rendered sections of `skills` to a system prompt.

    The base prompt stays first, a stable prefix for prompt caching; duplicates render once.

    Args:
        skills: Skill names (resolved through `registry`) or `Skill` objects.
        registry: Resolves names; without one, names are ignored and only `Skill` objects render.

    Raises:
        SkillNotFoundError: If a name is not registered in `registry`.
        TypeError: If `skills` is a single string.

    Example:
        ```python
        prompt = compose_prompt("You review pull requests.", ["code-review"], registry)
        ```
    """

    if isinstance(skills, str):
        raise TypeError("skills must be a sequence of skill names or Skill objects")
    resolved: dict[str, Skill] = {}
    for item in skills:
        if isinstance(item, Skill):
            resolved.setdefault(item.name, item)
        elif registry is not None:
            resolved.setdefault(item, registry.get(item))
    if not resolved:
        return base_prompt
    sections = [base_prompt] if base_prompt.strip() else []
    sections.extend(skill.render() for skill in resolved.values())
    return "\n\n".join(sections)


def builtin_skill_rows() -> list[tuple[str, Skill, str]]:
    """The built-in skills as `(name, skill, summary)` rows, the provider of the plugin registry."""

    return [
        (skill.name, skill, skill.description.split(". ")[0][:160]) for skill in _builtin_skills()
    ]


@functools.cache
def _builtin_skills() -> tuple[Skill, ...]:
    return tuple(_load_location(_BUILTIN_DIR))


def _load_location(path: Path) -> list[Skill]:
    if not path.exists():
        raise SkillNotFoundError(f"Skill location not found: {path}")
    if path.is_file() or (path / SKILL_FILENAME).is_file():
        return [Skill.from_path(path)]
    return [
        Skill.from_path(child)
        for child in sorted(path.iterdir())
        if child.is_dir() and (child / SKILL_FILENAME).is_file()
    ]
