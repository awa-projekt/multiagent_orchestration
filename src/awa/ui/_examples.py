"""Open a shipped example (a use case or a recipe) as a new project, the way a user starts one."""

from __future__ import annotations

import contextlib
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from awa.core.dotenv import copy_env_file
from awa.errors import ConfigurationError

PROCESS_FILE = "process.md"
"""The project's process description, as the Describe step shows it."""
_FOLDERS = (("usecases", "use case"), ("recipes", "recipe"))
_SKIPPED = frozenset({"outbox", "__pycache__", ".awa"})
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
_PROCESS = re.compile(r"\*\*Process\.\*\*(.*?)(?:\n\n|\Z)", re.S)
_KEPT = frozenset({".awa", ".env"})  # what a fresh `awa ui` folder may already hold


def find_examples(start: str | Path | None = None) -> Path | None:
    """The examples folder: `start` when given, else the one of a source checkout of awa, else
    `./examples`; `None` when none of them holds `usecases/` or `recipes/`."""

    import awa

    candidates = (
        [Path(start)]
        if start is not None
        else [Path(awa.__file__).resolve().parents[2] / "examples", Path.cwd() / "examples"]
    )
    for candidate in candidates:
        if any((candidate / folder).is_dir() for folder, _ in _FOLDERS):
            return candidate.resolve()
    return None


def _folder(examples: Path, example_id: str) -> Path | None:
    for folder, _ in _FOLDERS:
        directory = examples / folder / example_id
        if (directory / "blueprint.yaml").is_file() and (directory / "README.md").is_file():
            return directory
    return None


def _data_files(directory: Path) -> list[Path]:
    """The example's data files, relative to its `data/` folder (outputs and dot files left
    out)."""

    root = directory / "data"
    if not root.is_dir():
        return []
    return sorted(
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and not any(
            part in _SKIPPED or part.startswith(".") for part in path.relative_to(root).parts
        )
    )


def process_text(markdown: str) -> str:
    """The description in `markdown` without its leading `# title` line."""

    lines = markdown.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).strip()


def example_process(directory: Path) -> str:
    """The process description of the example in `directory`: its `process.md`, else the
    `**Process.**` paragraph of its README."""

    described = directory / PROCESS_FILE
    if described.is_file():
        return process_text(described.read_text(encoding="utf-8"))
    found = _PROCESS.search((directory / "README.md").read_text(encoding="utf-8"))
    return " ".join(found.group(1).split()) if found else ""


def list_examples(examples: str | Path | None = None) -> list[dict[str, Any]]:
    """The examples that can be opened (`id`, `kind`, `title`, `description`, `files`)."""

    from awa.blueprints._catalog import _entry, catalog

    folder = find_examples(examples)
    if folder is None:
        return []
    known: Mapping[str, Mapping[str, Any]] = {str(e["id"]): e for e in catalog()}
    found: list[dict[str, Any]] = []
    for sub, kind in _FOLDERS:
        base = folder / sub
        if not base.is_dir():
            continue
        for directory in sorted(base.iterdir()):
            if not _ID.match(directory.name) or _folder(folder, directory.name) != directory:
                continue
            entry = known.get(directory.name) or _entry(directory, kind)
            title = str(entry["title"])
            found.append(
                {
                    "id": directory.name,
                    "kind": kind,
                    "title": title[:1].upper() + title[1:],
                    "description": str(entry["description"]),
                    "files": len(_data_files(directory)),
                }
            )
    return found


def _fresh(target: Path) -> bool:
    return not target.exists() or (
        target.is_dir() and all(child.name in _KEPT for child in target.iterdir())
    )


def example_target(base: str | Path, example_id: str, *, beside: bool) -> Path:
    """Where to open `example_id`: `base` itself when it holds no project yet, else a new
    folder named after the example (`<id>`, `<id>-2`, ...) next to `base` (`beside`) or in it."""

    base = Path(base).expanduser().resolve()
    if _fresh(base):
        return base
    parent = base.parent if beside else base
    target, number = parent / example_id, 1
    while not _fresh(target) or target == base:
        number += 1
        target = parent / f"{example_id}-{number}"
    return target


def open_example(
    example_id: str,
    target: str | Path,
    *,
    examples: str | Path | None = None,
    settings_from: str | Path | None = None,
) -> dict[str, Any]:
    """Copy the example `example_id` into the project folder `target`.

    The project gets the data under `data/files`, `process.md`, `evals/cases.yaml` and the
    reference design under `reference/`; the reference tools are not copied, the user builds the
    system.

    Args:
        target: The new project folder: missing, empty, or holding only `.awa`/`.env`.
        settings_from: A project folder whose `.env` is copied as a file when `target` has none;
            the copy is readable by the owner only.

    Raises:
        ConfigurationError: If there is no examples folder, no such example, or `target`
            already holds a project.
    """

    folder = find_examples(examples)
    if folder is None:
        raise ConfigurationError(
            "The examples are not installed here; start awa from its source folder, or pass "
            "the examples folder (awa ui --examples DIR)"
        )
    directory = _folder(folder, example_id) if _ID.match(example_id) else None
    if directory is None:
        names = ", ".join(e["id"] for e in list_examples(folder)) or "none"
        raise ConfigurationError(f"There is no example {example_id!r}; the examples are: {names}")
    project = Path(target).expanduser().resolve()
    if not _fresh(project):
        raise ConfigurationError(
            f"{project} already holds a project; open the example in a new folder"
        )
    files = _data_files(directory)
    project.mkdir(parents=True, exist_ok=True)
    data = project / "data" / "files"
    for relative in files:
        destination = data / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(directory / "data" / relative, destination)
    process = example_process(directory)
    title = _title(directory)
    (project / PROCESS_FILE).write_text(f"# {title}\n\n{process}\n", encoding="utf-8", newline="\n")
    cases = directory / "cases.yaml"
    if cases.is_file():
        (project / "evals").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cases, project / "evals" / "cases.yaml")
    reference = project / "reference"
    reference.mkdir(parents=True, exist_ok=True)
    for name in ("README.md", "blueprint.yaml"):
        shutil.copyfile(directory / name, reference / name)
    if settings_from is not None:
        source = Path(settings_from).expanduser().resolve() / ".env"
        if source.is_file():
            with contextlib.suppress(FileExistsError):
                copy_env_file(source, project / ".env")
    return {
        "folder": str(project),
        "id": example_id,
        "title": title,
        "process": process,
        "files": [relative.as_posix() for relative in files],
        "cases": cases.is_file(),
    }


def _title(directory: Path) -> str:
    described = directory / PROCESS_FILE
    if described.is_file():
        first = described.read_text(encoding="utf-8").strip().splitlines()[:1]
        if first and first[0].startswith("# "):
            return first[0][2:].strip()
    heading = (directory / "README.md").read_text(encoding="utf-8").splitlines()[0]
    title = heading.partition(":")[2].strip() or directory.name
    return title[:1].upper() + title[1:]


def project_process(directory: Path) -> str:
    """The process description kept in the project folder (empty when there is none)."""

    path = directory / PROCESS_FILE
    return process_text(path.read_text(encoding="utf-8")) if path.is_file() else ""
