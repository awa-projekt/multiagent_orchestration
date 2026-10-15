"""The `Skill` model and the `SKILL.md` parser."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from awa._internal.serialize import load_yaml, split_frontmatter
from awa._internal.util import is_within
from awa.errors import SkillManifestError, SkillNotFoundError, SkillReferenceNotFoundError

SKILL_FILENAME = "SKILL.md"
"""The file that holds a skill's front matter and instructions."""

MAX_NAME_LENGTH = 64
"""Longest skill name."""

MAX_DESCRIPTION_LENGTH = 1024
"""Longest skill description."""

XML_TAG_PATTERN = re.compile(r"</?[A-Za-z][^<>]*>")
"""What a skill description must not contain."""

_MAX_COMPATIBILITY_LENGTH = 500
_PROMOTED_METADATA_KEYS = ("version", "category", "tags", "risk")
_IGNORED_PARTS = frozenset({"__pycache__"})

_Risk = Literal["low", "medium", "high"]
_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Skill(BaseModel):
    """A validated skill: routing metadata, instructions and bundled resources.

    Loaded with `from_path` or built in code. Front-matter fields are `name`, `description`,
    `license`, `compatibility`, `allowed-tools` and `metadata`; `version`, `category`, `tags` and
    `risk` are read from `metadata` (or the top level); other keys are ignored.

    Attributes:
        name: 1-64 lowercase letters, digits and single hyphens, no hyphen at either end.
        description: What the skill does and when to use it (1-1024 characters, no XML tags); the
            only text a model sees before loading the skill.
        risk: Risk class of the actions the skill directs an agent to take.
        compatibility: Environment requirements (at most 500 characters).
        metadata: The front-matter `metadata` map without the keys promoted to fields.
        resources: POSIX paths of every bundled file, relative to the skill directory.

    Example:
        ```python
        skill = Skill.from_path(Path("skills/release-notes"))
        checklist = skill.read_resource("references/checklist.md")
        ```
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Annotated[
        str, StringConstraints(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=MAX_NAME_LENGTH)
    ]
    description: _Text
    instructions: _Text
    version: str = "1.0.0"
    category: str | None = None
    tags: tuple[_Text, ...] = ()
    risk: _Risk = "low"
    allowed_tools: tuple[_Text, ...] = ()
    license: str | None = None
    compatibility: str | None = Field(default=None, max_length=_MAX_COMPATIBILITY_LENGTH)
    metadata: dict[str, str] = Field(default_factory=dict)
    source_path: Path | None = None
    resources: tuple[str, ...] = ()

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        if len(value) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(
                f"skill description is {len(value)} characters long; "
                f"the maximum is {MAX_DESCRIPTION_LENGTH}"
            )
        if XML_TAG_PATTERN.search(value):
            raise ValueError("skill description must not contain XML tags")
        return value

    @field_validator("tags", "allowed_tools")
    @classmethod
    def _unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(value))

    @field_validator("tags")
    @classmethod
    def _one_word(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # SKILL.md lists tags separated by spaces: a tag of several words is joined by hyphens.
        return tuple(dict.fromkeys("-".join(tag.split()) for tag in value if tag.strip()))

    @field_validator("resources")
    @classmethod
    def _validate_resources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if _normalize_relative_path(item) != item:
                raise ValueError(f"resource {item!r} must be a normalized relative POSIX path")
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def _resources_need_a_source(self) -> Skill:
        if self.resources and self.source_path is None:
            raise ValueError("a skill with resources needs a source_path")
        return self

    @classmethod
    def from_markdown(cls, text: str, *, source_path: Path | None = None) -> Skill:
        """Parse `SKILL.md` content (YAML front matter between `---` lines, then Markdown).

        Resources are not discovered; use `from_path` for that.

        Raises:
            SkillManifestError: If the front matter is missing, not valid YAML, or a field is
                invalid.
        """

        return _parse(text, source_path=source_path)

    @classmethod
    def from_path(cls, path: Path | str) -> Skill:
        """Load a skill from its `SKILL.md` file or its directory.

        Every other non-hidden file of the directory is listed in `resources`; from a directory, the
        skill name must match the directory name.

        Raises:
            SkillNotFoundError: If `path` does not exist or the directory has no `SKILL.md`.
            SkillManifestError: If the file cannot be parsed or is invalid.
        """

        path = Path(path)
        skill_file = path / SKILL_FILENAME if path.is_dir() else path
        if not skill_file.is_file():
            raise SkillNotFoundError(f"No {SKILL_FILENAME} found at {path}")
        try:
            text = skill_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise SkillManifestError(f"{skill_file}: cannot read file: {exc}") from exc
        skill = _parse(text, source_path=skill_file, resources=_discover_resources(skill_file))
        if skill_file.name == SKILL_FILENAME and skill.name != skill_file.parent.name:
            raise SkillManifestError(
                f"{skill_file}: skill name {skill.name!r} must match its directory name "
                f"{skill_file.parent.name!r}"
            )
        return skill

    def read_resource(self, relative_path: str) -> str:
        """Read one bundled resource file, as listed in `resources`, as UTF-8 text.

        Raises:
            SkillReferenceNotFoundError: If the path is absolute, escapes the skill directory, is
                not a listed resource, or is not readable UTF-8 text.
        """

        normalized = _normalize_relative_path(relative_path)
        if normalized is None:
            raise SkillReferenceNotFoundError(
                f"Resource path {relative_path!r} must be relative to the directory of "
                f"skill {self.name!r} and must not contain '..'"
            )
        if normalized not in self.resources or self.source_path is None:
            available = ", ".join(self.resources) or "none"
            raise SkillReferenceNotFoundError(
                f"Skill {self.name!r} has no resource {relative_path!r}. "
                f"Available resources: {available}"
            )
        base = self.source_path.parent.resolve()
        target = (base / normalized).resolve()
        if not is_within(target, base):
            raise SkillReferenceNotFoundError(
                f"Resource {relative_path!r} of skill {self.name!r} resolves outside the "
                "skill directory"
            )
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SkillReferenceNotFoundError(
                f"Resource {relative_path!r} of skill {self.name!r} is not UTF-8 text"
            ) from exc
        except OSError as exc:
            raise SkillReferenceNotFoundError(
                f"Resource {relative_path!r} of skill {self.name!r} cannot be read: {exc}"
            ) from exc

    def render(self) -> str:
        """The skill as a prompt section: `"## Skill: <name>"` followed by the instructions."""

        return f"## Skill: {self.name}\n{self.instructions}"


def skill_fields(
    fields: Mapping[str, Any],
    body: str,
    where: str,
    *,
    source_path: Path | None = None,
    resources: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The `Skill` keyword arguments of a front-matter mapping and a body.

    Raises:
        SkillManifestError: If a field has the wrong type; `where` names the file.
    """

    raw_metadata = fields.get("metadata") or {}
    if not isinstance(raw_metadata, Mapping):
        raise SkillManifestError(f"{where}: 'metadata' must be a mapping")
    metadata: dict[str, Any] = {str(key): value for key, value in raw_metadata.items()}
    promoted = {
        key: metadata.pop(key) if key in metadata else fields.get(key)
        for key in _PROMOTED_METADATA_KEYS
    }

    arguments: dict[str, Any] = {
        "name": _scalar_string(fields["name"], "name", where),
        "description": _scalar_string(fields["description"], "description", where),
        "instructions": body,
        "category": _scalar_string(promoted["category"], "category", where),
        "tags": _string_list(promoted["tags"], "tags", where),
        "allowed_tools": _string_list(fields.get("allowed-tools"), "allowed-tools", where),
        "license": _scalar_string(fields.get("license"), "license", where),
        "compatibility": _scalar_string(fields.get("compatibility"), "compatibility", where),
        "metadata": {
            key: _scalar_string(value, f"metadata.{key}", where) or ""
            for key, value in metadata.items()
        },
        "source_path": source_path,
        "resources": resources,
    }
    for key in ("version", "risk"):
        value = _scalar_string(promoted[key], key, where)
        if value is not None:
            arguments[key] = value
    return arguments


def _normalize_relative_path(value: str) -> str | None:
    """Return `value` as a normalized relative POSIX path, or None if it is unsafe."""

    candidate = value.strip().replace("\\", "/")
    if not candidate or candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate):
        return None
    parts = [part for part in PurePosixPath(candidate).parts if part != "."]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _discover_resources(skill_file: Path) -> tuple[str, ...]:
    base = skill_file.parent
    resources: list[str] = []
    for candidate in base.rglob("*"):
        if candidate == skill_file or not candidate.is_file():
            continue
        relative = candidate.relative_to(base)
        if any(part.startswith(".") or part in _IGNORED_PARTS for part in relative.parts):
            continue
        if not is_within(candidate, base):
            continue
        resources.append(relative.as_posix())
    return tuple(sorted(resources))


def _split_list(value: str) -> tuple[str, ...]:
    """Split on whitespace and commas that are not inside parentheses."""

    items: list[str] = []
    current: list[str] = []
    depth = 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        if depth == 0 and (char.isspace() or char == ","):
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    items.append("".join(current))
    return tuple(item for item in items if item)


def _string_list(value: Any, field: str, where: str) -> tuple[str, ...]:
    """Accept a space- or comma-separated string, or a YAML list of strings."""

    if value is None:
        return ()
    if isinstance(value, str):
        return _split_list(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise SkillManifestError(f"{where}: '{field}' must be a string or a list of strings")


def _scalar_string(value: Any, field: str, where: str) -> str | None:
    """Return a YAML scalar as a string (YAML may read `1.0` as a float)."""

    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    raise SkillManifestError(f"{where}: '{field}' must be a string")


def _parse(text: str, *, source_path: Path | None, resources: tuple[str, ...] = ()) -> Skill:
    where = str(source_path) if source_path is not None else "<SKILL.md>"
    try:
        frontmatter, body = split_frontmatter(text)
    except ValueError:
        raise SkillManifestError(
            f"{where}: SKILL.md frontmatter block is not closed with '---'"
        ) from None
    if frontmatter is None:
        raise SkillManifestError(f"{where}: SKILL.md must start with a '---' frontmatter block")
    try:
        data = load_yaml(frontmatter, what="skill front matter")
    except ValueError as exc:
        raise SkillManifestError(f"{where}: frontmatter is not valid YAML: {exc}") from None
    if not isinstance(data, Mapping):
        raise SkillManifestError(f"{where}: frontmatter must be a YAML mapping")
    fields: dict[str, Any] = {str(key): value for key, value in data.items()}

    missing = [key for key in ("name", "description") if not fields.get(key)]
    if missing:
        raise SkillManifestError(f"{where}: SKILL.md is missing frontmatter fields: {missing}")

    arguments = skill_fields(fields, body, where, source_path=source_path, resources=resources)
    try:
        return Skill(**arguments)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'skill'}: {error['msg']}"
            for error in exc.errors()
        )
        raise SkillManifestError(f"{where}: invalid skill: {problems}") from exc
