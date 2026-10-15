"""Which files does a list of paths mean, and what are they called? Shared by profile and ingest."""

from __future__ import annotations

import glob
import hashlib
import os
import re
from collections.abc import Sequence
from pathlib import Path

from awa.data._errors import DataError, PathNotAllowedError
from awa.data._model import Flag, make_flag

PathLike = str | os.PathLike[str]
_GLOB_CHARS = re.compile(r"[*?\[]")


def file_sha256(path: Path) -> str:
    """The sha256 (hex) of the file at `path`, read in chunks."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expand_paths(
    paths: PathLike | Sequence[PathLike], root: Path | None = None
) -> tuple[list[Path], list[Flag]]:
    """The files that `paths` (files, folders, glob patterns) stand for, resolved and unique.

    Folders are walked recursively; a link inside a folder is skipped with a flag, a link given
    directly raises.

    Raises:
        DataError: No path, an empty folder, a missing path or pattern, a link, or a file outside
            `root`.
    """

    entries = [paths] if isinstance(paths, str | os.PathLike) else list(paths)
    if not entries:
        raise DataError("no path was given; pass a file, a folder or a glob pattern")
    found: dict[Path, None] = {}
    skipped: list[Flag] = []
    for entry in entries:
        path = Path(entry)
        if path.is_symlink():
            raise PathNotAllowedError(f"{path} is a symbolic link; awa never follows links")
        if path.is_dir():
            items = sorted(path.rglob("*"))
            links = [item for item in items if item.is_symlink()]
            skipped += [
                make_flag("DF-W-SYMLINK-SKIPPED", f"{link.name} is a link") for link in links
            ]
            files = [item for item in items if item.is_file() and item not in links]
            if not files:
                raise DataError(f"the folder {path} holds no files")
            found.update(dict.fromkeys(item.resolve() for item in files))
        elif path.is_file():
            found[path.resolve()] = None
        elif _GLOB_CHARS.search(str(path)):
            matches = [Path(match) for match in sorted(glob.glob(str(path), recursive=True))]
            files = [m for m in matches if m.is_file() and not m.is_symlink()]
            if not files:
                raise PathNotAllowedError(f"the pattern {path} matches no file")
            found.update(dict.fromkeys(m.resolve() for m in files))
        else:
            raise PathNotAllowedError(f"{path} does not exist")
    if root is not None:
        outside = [p for p in found if not p.is_relative_to(root)]
        if outside:
            raise PathNotAllowedError(f"{outside[0]} is outside the allowed folder {root}")
    return list(found), skipped


def display_names(paths: Sequence[Path], root: Path | None = None) -> dict[Path, str]:
    """Names relative to `root`, else to the closest folder that holds all files (posix style)."""

    try:
        base = root or Path(os.path.commonpath([p.parent for p in paths]))
    except ValueError:  # files on different drives
        return {p: p.as_posix() for p in paths}
    return {p: p.relative_to(base).as_posix() for p in paths}
