"""Import `SKILL.md` folders and Markdown prompts as skills; nothing imported is executed."""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import yaml

from awa._internal.serialize import load_yaml, split_frontmatter
from awa.errors import AdapterError, SkillManifestError, SkillNotFoundError
from awa.skills import Skill, SkillRegistry
from awa.skills.skill import (
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    SKILL_FILENAME,
    XML_TAG_PATTERN,
    skill_fields,
)

SkillImportKind = Literal["skill", "markdown"]
"""Where an imported skill came from: a skill folder or a Markdown prompt."""

REVIEW_METADATA_KEY = "needs-review"
"""Skill metadata key set to `"true"` on skills whose name or description was generated."""

SOURCE_METADATA_KEY = "imported-from"
"""Skill metadata key naming the file or object a skill was imported from."""

_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
_SKIPPED_FILES = frozenset({"readme", "changelog", "license", "licence", "contributing"})
_IGNORED_PARTS = frozenset({"__pycache__", ".git", "node_modules"})
_MAX_PROMPT_BYTES = 1_000_000
_FRONT_MATTER_KEYS = frozenset(
    {
        "name", "description", "metadata", "tags", "license", "version", "category", "risk",
        "compatibility", "allowed-tools",
    }
)  # fmt: skip
_NAME_CHARS = re.compile(r"[^a-z0-9]+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_MARKER = re.compile(r"^(?:[-*+>]\s+)+")
"""List and quote markers at the start of a line (emphasis such as `**` stays)."""


@dataclass(frozen=True)
class ImportedSkill:
    """One skill `import_skills` produced.

    Attributes:
        source: The file, folder or object it came from.
        needs_review: Whether its name or description was generated and should be checked.
        written_to: The `SKILL.md` written, when importing into a directory.
    """

    skill: Skill
    kind: SkillImportKind
    source: str
    needs_review: bool = False
    notes: tuple[str, ...] = ()
    written_to: Path | None = None


@dataclass(frozen=True)
class SkillImportIssue:
    """A source `import_skills` skipped (`source`: path or type name) and why (`message`)."""

    source: str
    message: str


@dataclass(frozen=True)
class SkillImportReport:
    """What `import_skills` imported, what needs review and what failed.

    Attributes:
        dry_run: Whether nothing was written or registered.
    """

    imported: tuple[ImportedSkill, ...] = ()
    issues: tuple[SkillImportIssue, ...] = ()
    dry_run: bool = False

    @property
    def skills(self) -> list[Skill]:
        """The imported skills."""

        return [entry.skill for entry in self.imported]

    @property
    def needs_review(self) -> list[ImportedSkill]:
        """The imported skills whose name or description was generated."""

        return [entry for entry in self.imported if entry.needs_review]

    @property
    def ok(self) -> bool:
        """Whether every source was imported."""

        return not self.issues

    def render(self) -> str:
        """Render the report as readable text."""

        verb = "would import" if self.dry_run else "imported"
        lines = [f"{verb} {len(self.imported)} skill(s), {len(self.issues)} issue(s)"]
        for entry in self.imported:
            where = f" -> {entry.written_to}" if entry.written_to is not None else ""
            review = "  [review]" if entry.needs_review else ""
            lines.append(f"  {entry.skill.name} ({entry.kind}: {entry.source}){where}{review}")
            lines.extend(f"    note: {note}" for note in entry.notes)
        for issue in self.issues:
            lines.append(f"  skipped {issue.source}: {issue.message}")
        return "\n".join(lines)


def import_skills(
    *sources: Any,
    into: SkillRegistry | str | Path | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> SkillImportReport:
    """Import skills from skill folders and Markdown prompts.

    Skill folders are validated and copied with their resources; prompts become skills whose derived
    names and descriptions are flagged for review. File references are not followed.

    Args:
        *sources: Paths (a skill folder, a folder of skills and prompts, a `SKILL.md`, a Markdown
            prompt) or `Skill` objects. Folders are searched recursively for skill folders; prompt
            files only at their top level (`README.md` and similar are skipped).
        into: A `SkillRegistry` to add to, or a directory to write `<name>/SKILL.md` folders to;
            `None` only returns them.
        overwrite: Replace existing skills of the same name instead of reporting an issue.
        dry_run: Parse and validate only.

    Raises:
        AdapterError: If a source path does not exist, or `into` is a file.

    Example:
        ```python
        report = import_skills("prompts/", into="skills/")
        print(report.render())      # what was created, what needs review
        ```
    """

    target_dir: Path | None = None
    if isinstance(into, str | Path):
        target_dir = Path(into)
        if target_dir.exists() and not target_dir.is_dir():
            raise AdapterError(f"Cannot import skills into {target_dir}: it is a file")
    imported: list[ImportedSkill] = []
    issues: list[SkillImportIssue] = []
    for source in sources:
        imported.extend(_read_source(source, issues))
    seen: set[str] = set()
    unique: list[ImportedSkill] = []
    for entry in imported:
        name = entry.skill.name
        if name in seen:
            issues.append(
                SkillImportIssue(entry.source, f"a skill named {name!r} is imported twice")
            )
        else:
            seen.add(name)
            unique.append(entry)
    placed: list[ImportedSkill] = []
    for entry in unique:
        result = _place(entry, into, target_dir, overwrite=overwrite, dry_run=dry_run)
        if isinstance(result, SkillImportIssue):
            issues.append(result)
        else:
            placed.append(result)
    return SkillImportReport(tuple(placed), tuple(issues), dry_run)


def render_skill_markdown(skill: Skill) -> str:
    """Render `skill` as `SKILL.md` text (front matter plus instructions)."""

    front: dict[str, Any] = {"name": skill.name, "description": skill.description}
    if skill.license:
        front["license"] = skill.license
    if skill.compatibility:
        front["compatibility"] = skill.compatibility
    if skill.allowed_tools:
        front["allowed-tools"] = " ".join(skill.allowed_tools)
    metadata: dict[str, str] = {"version": skill.version}
    if skill.category:
        metadata["category"] = skill.category
    if skill.tags:
        metadata["tags"] = " ".join(skill.tags)
    if skill.risk != "low":
        metadata["risk"] = skill.risk
    metadata.update(skill.metadata)
    front["metadata"] = metadata
    header = yaml.safe_dump(front, sort_keys=False, allow_unicode=True, width=100)
    return f"---\n{header}---\n{skill.instructions}\n"


def _read_source(source: Any, issues: list[SkillImportIssue]) -> Iterable[ImportedSkill]:
    if isinstance(source, Skill):
        return [ImportedSkill(source, "skill", source.name)]
    if not isinstance(source, str | Path):
        issues.append(
            SkillImportIssue(type(source).__name__, "not a path or a Skill: nothing to import")
        )
        return []
    path = Path(source)
    if not path.exists():
        raise AdapterError(f"Skill source not found: {path}")
    if path.is_file():
        entry = _read_file(path, issues)
        return [entry] if entry is not None else []
    if (path / SKILL_FILENAME).is_file():
        entry = _read_skill_folder(path, issues)
        return [entry] if entry is not None else []
    entries: list[ImportedSkill] = []
    for skill_file in sorted(path.rglob(SKILL_FILENAME)):
        relative = skill_file.relative_to(path).parts
        if any(part.startswith(".") or part in _IGNORED_PARTS for part in relative):
            continue
        if _inside_other_skill(skill_file.parent, path):
            continue
        entry = _read_skill_folder(skill_file.parent, issues)
        if entry is not None:
            entries.append(entry)
    for child in sorted(path.iterdir()):
        if child.is_file() and _is_prompt_file(child):
            entry = _read_file(child, issues)
            if entry is not None:
                entries.append(entry)
    return entries


def _inside_other_skill(folder: Path, root: Path) -> bool:
    """Whether a folder between `root` (exclusive) and `folder` is itself a skill."""

    parent = folder.parent
    while parent != root and root in parent.parents:
        if (parent / SKILL_FILENAME).is_file():
            return True
        parent = parent.parent
    return False


def _is_prompt_file(path: Path) -> bool:
    if path.name.startswith(".") or path.stem.lower() in _SKIPPED_FILES:
        return False
    return path.suffix.lower() in _MARKDOWN_SUFFIXES


def _read_skill_folder(folder: Path, issues: list[SkillImportIssue]) -> ImportedSkill | None:
    try:
        skill = Skill.from_path(folder)
    except (SkillManifestError, SkillNotFoundError) as exc:
        issues.append(SkillImportIssue(str(folder), str(exc)))
        return None
    return ImportedSkill(skill, "skill", str(folder))


def _read_file(path: Path, issues: list[SkillImportIssue]) -> ImportedSkill | None:
    if path.name == SKILL_FILENAME:
        return _read_skill_folder(path.parent, issues)
    try:
        if path.stat().st_size > _MAX_PROMPT_BYTES:
            raise AdapterError(f"larger than {_MAX_PROMPT_BYTES:,} bytes")
        if path.suffix.lower() not in _MARKDOWN_SUFFIXES:
            raise AdapterError(f"unsupported file type {path.suffix!r}: expected Markdown or text")
        text = path.read_text(encoding="utf-8-sig")
        return _markdown_skill(text, path)
    except (AdapterError, SkillManifestError, OSError, UnicodeDecodeError, ValueError) as exc:
        issues.append(SkillImportIssue(str(path), str(exc)))
        return None


def _markdown_skill(text: str, path: Path) -> ImportedSkill:
    front, body = _split_front_matter(text, path)
    notes: list[str] = []
    raw_name = _string(front.get("name"))
    heading = None if raw_name else _first_heading(body)
    name = _skill_name(raw_name or heading or path.stem)
    generated_name = raw_name is None or name != raw_name
    if generated_name:
        origin = "first heading" if heading else "file name"
        if raw_name:
            origin = "front-matter name"
        notes.append(f"name {name!r} derived from the {origin}")
    description = _string(front.get("description"))
    generated_description = description is None
    if description is None:
        description = _derive_description(body, name)
        notes.append("description generated from the first paragraph")
    if not body.strip():
        raise AdapterError("the prompt has no text")
    ignored = sorted(set(front) - _FRONT_MATTER_KEYS)
    if ignored:
        notes.append(f"front matter keys ignored: {', '.join(ignored)}")
    review = generated_name or generated_description
    skill = _build_skill(
        {**front, "name": name, "description": description},
        body,
        where=str(path),
        source=path.name,
        review=review,
    )
    return ImportedSkill(skill, "markdown", str(path), review, tuple(notes))


def _place(
    entry: ImportedSkill,
    into: SkillRegistry | str | Path | None,
    target_dir: Path | None,
    *,
    overwrite: bool,
    dry_run: bool,
) -> ImportedSkill | SkillImportIssue:
    name = entry.skill.name
    if isinstance(into, SkillRegistry):
        if name in into and not overwrite:
            return SkillImportIssue(entry.source, f"skill {name!r} is already registered")
        if not dry_run:
            into.add(entry.skill, replace=overwrite)
        return entry
    if target_dir is None:
        return entry
    folder = target_dir / name
    source_file = entry.skill.source_path
    source_folder = None
    if entry.kind == "skill" and source_file is not None and source_file.name == SKILL_FILENAME:
        source_folder = source_file.parent
    if source_folder is not None and source_folder.resolve() == folder.resolve():
        return replace(entry, written_to=source_file)
    if folder.exists() and not overwrite:
        return SkillImportIssue(entry.source, f"{folder} already exists (pass overwrite)")
    if dry_run:
        return replace(entry, written_to=folder / SKILL_FILENAME)
    if folder.exists():
        shutil.rmtree(folder)
    if source_folder is not None:
        shutil.copytree(source_folder, folder, ignore=_not_copied)
    else:
        folder.mkdir(parents=True)
        (folder / SKILL_FILENAME).write_text(render_skill_markdown(entry.skill), "utf-8")
    return replace(entry, skill=Skill.from_path(folder), written_to=folder / SKILL_FILENAME)


def _not_copied(directory: str, names: list[str]) -> set[str]:
    """Hidden files, tool folders and symbolic links, which could point outside the skill."""

    base = Path(directory)
    return {
        name
        for name in names
        if name.startswith(".") or name in _IGNORED_PARTS or (base / name).is_symlink()
    }


def _build_skill(
    fields: Mapping[str, Any], instructions: str, *, where: str, source: str, review: bool
) -> Skill:
    arguments = skill_fields(fields, instructions, where)
    arguments["metadata"][SOURCE_METADATA_KEY] = source
    if review:
        arguments["metadata"][REVIEW_METADATA_KEY] = "true"
    try:
        return Skill(**arguments)
    except ValueError as exc:
        raise SkillManifestError(f"invalid skill {arguments['name']!r}: {exc}") from None


def _split_front_matter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    try:
        front, body = split_frontmatter(text)
    except ValueError:
        raise AdapterError(f"{path.name}: front matter is not closed with '---'") from None
    if front is None:
        return {}, text.strip()
    try:
        data = load_yaml(front, what="front matter") or {}
    except ValueError as exc:
        raise AdapterError(f"front matter is not valid YAML: {exc}") from None
    if not isinstance(data, Mapping):
        raise AdapterError("front matter must be a YAML mapping")
    return {str(k): v for k, v in data.items()}, body.strip()


def _first_heading(body: str) -> str | None:
    for line in body.splitlines():
        match = _HEADING.match(line)
        if match:
            return match.group(1)
    return None


def _derive_description(body: str, name: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    for paragraph in paragraphs:
        lines = [line for line in paragraph.splitlines() if not _HEADING.match(line)]
        text = " ".join(_MARKER.sub("", line.strip()) for line in lines)
        text = XML_TAG_PATTERN.sub("", text).strip()
        if text:
            if len(text) > MAX_DESCRIPTION_LENGTH:
                text = text[: MAX_DESCRIPTION_LENGTH - 3].rstrip() + "..."
            return text
    return f"Instructions imported as the {name} skill."


def _skill_name(value: str) -> str:
    name = _NAME_CHARS.sub("-", value.lower()).strip("-")[:MAX_NAME_LENGTH].strip("-")
    return name or "imported-skill"


def _string(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return None


__all__ = [
    "REVIEW_METADATA_KEY",
    "SOURCE_METADATA_KEY",
    "ImportedSkill",
    "SkillImportIssue",
    "SkillImportKind",
    "SkillImportReport",
    "import_skills",
    "render_skill_markdown",
]
