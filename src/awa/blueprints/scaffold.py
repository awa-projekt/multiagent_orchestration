"""Scaffolds: a runnable starter project (`awa init`), a tool, a toolbox or a skill (`awa new`).

Files come from the templates in `awa/templates/`, rendered with a `{{name}}` substitution;
tool and toolbox scaffolds ship with a passing contract test. To start from a process
description instead, use `awa.blueprints.build_project` (`awa build`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from awa.blueprints.schema import Blueprint
from awa.contracts.cards import AGENT_NAME_PATTERN
from awa.errors import ScaffoldError


@dataclass(frozen=True)
class ProjectTemplate:
    """A project template shipped with awa, named as in `awa init --template`."""

    name: str
    summary: str
    example_request: str


TEMPLATES: dict[str, ProjectTemplate] = {
    template.name: template
    for template in (
        ProjectTemplate(
            "minimal",
            "One agent that answers account questions and opens follow-up tickets; "
            "every ticket waits for a human.",
            "Which plan is account A-100 on?",
        ),
        ProjectTemplate(
            "team",
            "An orchestrator that delegates research and writing to team members; publishing a "
            "brief waits for a human.",
            "Write a short brief on heat pumps for the sales team.",
        ),
        ProjectTemplate(
            "workflow",
            "A fixed review workflow: intake, parallel policy and risk checks, and a "
            "structured verdict.",
            "Review document DOC-7.",
        ),
        ProjectTemplate(
            "network",
            "A lead that plans tasks on a board and teammates that work on them "
            "concurrently; scheduling a post waits for a human.",
            "Launch the analytics add-on for small businesses.",
        ),
        ProjectTemplate(
            "support",
            "A customer-support desk: triage delegates order questions and hands refunds "
            "to a refunds agent; every refund waits for a human.",
            "Where is my order 1002?",
        ),
    )
}
"""The available templates by name."""

DEFAULT_TEMPLATE = "minimal"
"""The template `awa init` uses without `--template`."""

PROJECT_FILES: tuple[str, ...] = (
    "blueprint.yaml",
    "tools.py",
    "tests/conftest.py",
    "tests/test_blueprint.py",
    "evals/cases.yaml",
    ".env.example",
    ".gitignore",
    "README.md",
    "pyproject.toml",
)
"""Every file a project consists of, relative to its directory."""

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_MODEL_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]*$")
_RESERVED = ("team", "workflow")
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

PARSER_KINDS = ("csv", "xlsx", "pdf", "docx", "mail", "json", "sqlite", "text", "image", "zip")
"""The file kinds an own parser can be written for (what awa detects)."""


def render(text: str, values: Mapping[str, str]) -> str:
    """Replace every `{{key}}` in `text`; raises `ScaffoldError` for a key missing from `values`."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise ScaffoldError(f"Template placeholder {{{{{key}}}}} has no value")
        return values[key]

    return _PLACEHOLDER.sub(replace, text)


def create_project(
    directory: str | Path,
    *,
    template: str = DEFAULT_TEMPLATE,
    model: str = "default",
    force: bool = False,
) -> list[Path]:
    """Write a new project from a template into `directory` and return the written files.

    Args:
        directory: Created if missing; its last component names the project and blueprint.
        model: `defaults.model` of the blueprint: a profile name or `provider:model`.
        force: Overwrite project files that already exist.

    Raises:
        ScaffoldError: If the template, name or model is invalid, or files exist without `force`.

    Example:
        ```python
        written = create_project("my-desk", template="support")
        ```
    """

    if template not in TEMPLATES:
        raise ScaffoldError(f"Unknown template {template!r}; choose one of: {', '.join(TEMPLATES)}")
    name = _project_name(directory)
    _validate_model(model)
    _refuse_overwrite(directory, force)
    info = TEMPLATES[template]
    values = _values(name, info.summary, info.example_request, model)
    files = {**_template_files("_common", values), **_template_files(template, values)}
    _require_valid_blueprint(files["blueprint.yaml"])
    root = Path(directory)
    written: list[Path] = []
    for relative in PROJECT_FILES:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(files[relative], encoding="utf-8", newline="\n")
        written.append(path)
    return written


def _project_name(directory: str | Path) -> str:
    """The project name for `directory` (its last path component), validated."""

    name = Path(directory).resolve().name
    if not AGENT_NAME_PATTERN.fullmatch(name):
        raise ScaffoldError(
            f"Invalid project name {name!r}: use letters, digits, '_' and '-', start with a "
            "letter, at most 48 characters (e.g. 'my-desk')"
        )
    if name.lower() in _RESERVED:
        raise ScaffoldError(f"Invalid project name {name!r}: 'team' and 'workflow' are reserved")
    return name


def _validate_model(model: str) -> None:
    if not _MODEL_REFERENCE.fullmatch(model):
        raise ScaffoldError(
            f"Invalid model {model!r}: use a profile name (default, fast, strong) or "
            "'provider:model'"
        )


def _values(name: str, summary: str, example_request: str, model: str) -> dict[str, str]:
    return {
        "name": name,
        "summary": summary,
        "summary_line": summary.replace("\\", "\\\\").replace('"', '\\"'),
        "example_request": example_request.replace('"', "'"),
        "model": model,
    }


def _template_files(template: str, values: Mapping[str, str]) -> dict[str, str]:
    """Render every file of a template directory, keyed by output path."""

    root = resources.files("awa").joinpath("templates", template)
    if not root.is_dir():
        raise ScaffoldError(f"Template {template!r} is missing from the installed package")
    files: dict[str, str] = {}
    for relative, entry in _walk(root, ""):
        # `dot_env.example.tmpl` is written as `.env.example`.
        output = "/".join(
            f".{part[4:]}" if part.startswith("dot_") else part
            for part in relative.removesuffix(".tmpl").split("/")
        )
        files[output] = render(entry.read_text(encoding="utf-8"), values)
    return files


def _walk(directory: Traversable, prefix: str) -> list[tuple[str, Traversable]]:
    found: list[tuple[str, Traversable]] = []
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        relative = f"{prefix}{entry.name}"
        if entry.is_dir():
            if entry.name != "__pycache__":
                found.extend(_walk(entry, f"{relative}/"))
        elif entry.name.endswith(".tmpl"):
            found.append((relative, entry))
    return found


def _refuse_overwrite(directory: str | Path, force: bool) -> None:
    root = Path(directory)
    if root.exists() and not root.is_dir():
        raise ScaffoldError(f"{str(root)!r} exists and is not a directory")
    if force:
        return
    clashes = [root / relative for relative in PROJECT_FILES if (root / relative).exists()]
    if clashes:
        listing = ", ".join(path.relative_to(root).as_posix() for path in clashes)
        raise ScaffoldError(f"{str(root)!r} already contains {listing}; pass --force to overwrite")


def _require_valid_blueprint(text: str) -> None:
    errors = [
        issue for issue in Blueprint.from_yaml_text(text).check() if issue.severity == "error"
    ]
    if errors:
        details = "; ".join(str(issue) for issue in errors)
        raise ScaffoldError(f"The generated blueprint is invalid: {details}")


def create_tool(
    name: str,
    directory: str | Path = ".",
    *,
    is_async: bool = False,
    approval: bool = False,
    force: bool = False,
) -> list[Path]:
    """Write `<directory>/<name>.py` (a tool) and its test; raises `ScaffoldError` as `create_project`."""

    name = _tool_name(name)
    options = 'approval=True, risk=["external_action"], timeout=10' if approval else "timeout=10"
    approve_arg = ", approve=True" if approval else ""
    if approval:
        approval_tests = (
            "\n\ndef test_every_call_waits_for_a_human() -> None:\n"
            '    contract.expect_approval("always")\n'
            '    payload = contract.expect_approval_request({"record_id": "R-1"})\n'
            f'    assert payload["tool"] == "{name}"\n'
            '    assert "rejected" in contract.call({"record_id": "R-1"}, approve=False)'
        )
    else:
        approval_tests = (
            "\n\ndef test_runs_without_approval() -> None:\n    contract.expect_approval(False)"
        )
    values = {
        "name": name,
        "class_name": _class_name(name),
        "options": options,
        "async_prefix": "async " if is_async else "",
        "approve_arg": approve_arg,
        "approval_tests": approval_tests,
    }
    root = Path(directory)
    files = {
        root / f"{name}.py": _render_authoring("tool.py.tmpl", values),
        root / "tests" / f"test_{name}.py": _render_authoring("test_tool.py.tmpl", values),
    }
    return _write(files, force)


def create_toolbox(name: str, directory: str | Path = ".", *, force: bool = False) -> list[Path]:
    """Write `<directory>/<name>.py` (a `Toolbox`) and its test; raises `ScaffoldError` as `create_project`."""

    name = _tool_name(name)
    values = {"name": name, "class_name": _class_name(name)}
    root = Path(directory)
    files = {
        root / f"{name}.py": _render_authoring("toolbox.py.tmpl", values),
        root / "tests" / f"test_{name}.py": _render_authoring("test_toolbox.py.tmpl", values),
    }
    return _write(files, force)


def create_skill(
    name: str, directory: str | Path = "skills", *, category: str = "general", force: bool = False
) -> list[Path]:
    """Write `<directory>/<name>/SKILL.md` and `references/guide.md`.

    Raises:
        ScaffoldError: If the name or category is invalid, or a file exists without `force`.
    """

    name = name.strip().lower().replace("_", "-")
    if len(name) > 64 or not _SKILL_NAME.fullmatch(name):
        raise ScaffoldError(
            f"Invalid skill name {name!r}: use lowercase letters, digits and single hyphens "
            "(at most 64 characters), for example 'refund-policy'"
        )
    if not re.fullmatch(r"[a-z][a-z0-9-]*", category):
        raise ScaffoldError(f"Invalid category {category!r}: use lowercase letters and hyphens")
    words = name.split("-")
    values = {
        "name": name,
        "title": " ".join(words),
        "heading": " ".join(word.capitalize() for word in words),
        "category": category,
        "tags": " ".join(dict.fromkeys([*words, category, "custom"])),
    }
    root = Path(directory) / name
    files = {
        root / "SKILL.md": _render_authoring("skill/SKILL.md.tmpl", values),
        root / "references" / "guide.md": _render_authoring(
            "skill/references/guide.md.tmpl", values
        ),
    }
    return _write(files, force)


def create_parser(kind: str, directory: str | Path = ".", *, force: bool = False) -> list[Path]:
    """Write `<directory>/<kind>_parser.py` and its test; the parser passes its contract as written.

    Raises:
        ScaffoldError: If `kind` is not a file kind awa detects, or a file exists without `force`.
    """

    if kind not in PARSER_KINDS:
        raise ScaffoldError(
            f"Invalid file kind {kind!r}: a parser reads one of {', '.join(PARSER_KINDS)}"
        )
    root = Path(directory)
    values = {"kind": kind, **(_TABLE_PARSER if kind == "csv" else _TEXT_PARSER)}
    files = {
        root / f"{kind}_parser.py": _render_authoring("parser.py.tmpl", values),
        root / "tests" / f"test_{kind}_parser.py": _render_authoring("test_parser.py.tmpl", values),
    }
    return _write(files, force)


_TEXT_PARSER = {
    "imports": "from pathlib import Path\n\nfrom awa.data import FileProfile, ParseContext, TextInfo\n",
    "reads": "lines of text: how many, and the first few",
    "tables": "",
}
_TABLE_PARSER = {
    "imports": (
        "import csv\nfrom pathlib import Path\n\n"
        "from awa.data import ColumnProfile, FileProfile, ParseContext, TableProfile, TextInfo, "
        "column_key\n"
    ),
    "reads": "one table whose first line names the columns, and its lines of text",
    "tables": (
        "    if lines:\n"
        "        header = next(csv.reader(lines[:1]))\n"
        "        columns = [\n"
        '            ColumnProfile(name=name[:limit], key=f"{column_key(name)}_{index}", index=index)\n'
        "            for index, name in enumerate(header)\n"
        "        ]\n"
        "        ctx.profile.tables = [\n"
        '            TableProfile(id=f"{ctx.profile.id}.t1", rows=len(lines) - 1, columns=columns)\n'
        "        ]\n"
    ),
}
"""What `parser.py.tmpl` fills in for a text kind and for the tabular kind (`csv`)."""


def create_connector(name: str, directory: str | Path = ".", *, force: bool = False) -> list[Path]:
    """Write `<directory>/<name>_connector.py` (a `ConnectorSpec`) and its contract test.

    Raises:
        ScaffoldError: If the name is invalid or a file exists without `force`.
    """

    name = _tool_name(name)
    values = {
        "name": name,
        "title": " ".join(part.capitalize() for part in name.split("_")),
        "env": name.upper(),
    }
    root = Path(directory)
    files = {
        root / f"{name}_connector.py": _render_authoring("connector.py.tmpl", values),
        root / "tests" / f"test_{name}_connector.py": _render_authoring(
            "test_connector.py.tmpl", values
        ),
    }
    return _write(files, force)


def _tool_name(name: str) -> str:
    candidate = name.strip().replace("-", "_")
    if not _TOOL_NAME.fullmatch(candidate) or candidate.startswith("test"):
        raise ScaffoldError(
            f"Invalid tool name {name!r}: use lowercase letters, digits and '_', start with a "
            "letter (not 'test'), for example 'lookup_record'"
        )
    return candidate


def _class_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_") if part)


def _render_authoring(template: str, values: Mapping[str, str]) -> str:
    entry = resources.files("awa").joinpath("templates", "_authoring", *template.split("/"))
    return render(entry.read_text(encoding="utf-8"), values)


def _write(files: Mapping[Path, str], force: bool) -> list[Path]:
    clashes = [path for path in files if path.exists()]
    if clashes and not force:
        listing = ", ".join(str(path) for path in clashes)
        raise ScaffoldError(f"{listing} already exist(s); pass --force to overwrite")
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return list(files)


__all__ = [
    "DEFAULT_TEMPLATE",
    "PROJECT_FILES",
    "TEMPLATES",
    "ProjectTemplate",
    "PARSER_KINDS",
    "create_connector",
    "create_parser",
    "create_project",
    "create_skill",
    "create_tool",
    "create_toolbox",
    "render",
]
