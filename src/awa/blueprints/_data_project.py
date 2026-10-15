"""Check the `data:` section of a project folder against the files on disk (never outside it, never through links).

Profiles come from `data/PROFILE.json` when its sha256 values match, else are made live.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from awa.blueprints._check_data import check_data
from awa.blueprints._project_data import FILES, PROFILE_FILE
from awa.blueprints.errors import BlueprintError, BlueprintIssue
from awa.blueprints.schema import Blueprint
from awa.data import DataError, DataProfile, DataReader, FileProfile, Limits
from awa.data import profile as profile_files
from awa.data._paths import file_sha256
from awa.data._validate import listing

BLUEPRINT_NAMES = ("blueprint.yaml", "blueprint.yml", "blueprint.json")

_GLOB = re.compile(r"[*?\[]")
_MAX_FILES = Limits().max_files

NO_DATA_SECTION = "the blueprint has no `data:` section, so there is nothing to check"
"""The one issue `check_project_data` reports for a blueprint without `data:`."""


def check_project(
    blueprint: Blueprint, base: Path | None = None, *, reader: DataReader | None = None
) -> list[BlueprintIssue]:
    """Check the `data:` section of `blueprint` against the files it names on disk.

    Besides the issues of `check_data`: a warning for an outdated `data/PROFILE.json`, an error
    for more than 500 matched files, and a warning for files under `data/files` no entry names.

    Args:
        base: Folder the `path:` entries are relative to; default the blueprint's folder.
    """

    if not blueprint.data:
        return []
    root = (base or (blueprint.source.parent if blueprint.source else Path.cwd())).resolve()
    inputs = {i: e.path for i, e in enumerate(blueprint.data) if e.role != "output"}
    found = {i: disk_files(root, pattern) for i, pattern in inputs.items()}
    paths = sorted({p for files in found.values() for p in files})
    if len(paths) > _MAX_FILES:
        message = f"the data entries match more than {_MAX_FILES} files; narrow them"
        return [BlueprintIssue("error", "data", message)]
    profile, notes = project_profile(root, paths, found)
    issues = [*notes, *check_data(blueprint, profile, reader=reader)]
    return [*issues, *_unlisted(root, {Path(f.source) for f in profile.files if f.source})]


def check_project_data(target: str | Path) -> list[BlueprintIssue]:
    """The data checks of a project folder or blueprint file (`awa data check`).

    Raises:
        BlueprintError: If `target` holds no blueprint or the blueprint cannot be read.
    """

    path = Path(target)
    if path.is_dir():
        found = [path / name for name in BLUEPRINT_NAMES if (path / name).is_file()]
        if not found:
            raise BlueprintError(f"{path} holds no blueprint (expected {BLUEPRINT_NAMES[0]})")
        path = found[0]
    blueprint = Blueprint.from_yaml_file(path)
    if not blueprint.data:
        return [BlueprintIssue("warning", "data", NO_DATA_SECTION)]
    return check_project(blueprint, path.parent)


def _inside(root: Path, path: Path) -> bool:
    return path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root)


def disk_files(root: Path, pattern: str) -> list[Path]:
    """Files an entry names, confined to the project folder; links are not followed."""

    text = pattern.replace("\\", "/")
    candidates: Iterable[Path]
    if _GLOB.search(text):
        candidates = root.glob(text)
    else:
        target = root / text
        candidates = target.rglob("*") if target.is_dir() else [target]
    found: list[Path] = []
    for path in candidates:
        if _inside(root, path):
            found.append(path.resolve())
        if len(found) > _MAX_FILES:  # far too many: the caller reports it, stop reading
            break
    return sorted(found)


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _saved_profile(root: Path) -> tuple[DataProfile | None, list[BlueprintIssue]]:
    path = root / PROFILE_FILE
    if not path.is_file():
        return None, []
    try:
        return DataProfile.from_json(path.read_bytes()), []
    except (DataError, ValueError, OSError) as error:
        message = f"{PROFILE_FILE} cannot be read ({error}); profiled the files"
        return None, [BlueprintIssue("warning", "data", message)]


def project_profile(
    root: Path, paths: list[Path], found: dict[int, list[Path]]
) -> tuple[DataProfile, list[BlueprintIssue]]:
    """Profiles of `paths` from `data/PROFILE.json` where the sha256 matches, else live; paths relative to the project."""

    saved, notes = _saved_profile(root)
    by_sha = {f.sha256: f for f in saved.files} if saved else {}
    files: dict[Path, FileProfile] = {}
    missing: set[Path] = set()
    for path in paths:
        hit = by_sha.get(file_sha256(path))
        if hit is None:
            missing.add(path)
        else:
            files[path] = hit.model_copy(update={"path": _rel(root, path), "source": str(path)})
    if saved is not None:
        for index, entry_files in found.items():
            if changed := sorted(_rel(root, p) for p in entry_files if p in missing):
                message = (
                    f"data changed since the profile was written: {listing(changed)} "
                    f"{'differs' if len(changed) == 1 else 'differ'} from {PROFILE_FILE}; "
                    "the files were profiled again"
                )
                notes.append(BlueprintIssue("warning", f"data[{index}].path", message))
    if missing:
        try:
            for file in profile_files(sorted(missing), root=root, created="").files:
                files[root / file.path] = file
        except DataError as error:
            message = f"the data files cannot be profiled: {error}"
            notes.append(BlueprintIssue("error", "data", message))
    profile = DataProfile(
        awa_version=saved.awa_version if saved else "",
        created=saved.created if saved else "",
        files=sorted(files.values(), key=lambda f: f.path),
    )
    return profile, notes


def _unlisted(root: Path, listed: set[Path]) -> list[BlueprintIssue]:
    """A warning for the files under `data/files` that no `data:` entry names."""

    folder = root / FILES
    if not folder.is_dir():
        return []
    extra = sorted(
        _rel(root, p.resolve())
        for p in folder.rglob("*")
        if _inside(root, p) and p.resolve() not in listed
    )
    if not extra:
        return []
    message = f"{len(extra)} file(s) under {FILES} have no `data:` entry: {listing(extra)}"
    return [BlueprintIssue("warning", "data", message)]
