"""Safe file access: a folder the file tools cannot leave, zip-bomb bounds, text encodings."""

from __future__ import annotations

import codecs
import os
import re
import zipfile
from pathlib import Path, PurePosixPath

from awa._internal.upstream.tools import ToolException
from awa.errors import ConfigurationError

_CHUNK = 1 << 20
_WINDOWS_RESERVED = re.compile(
    r"^(con|prn|aux|nul|com[0-9]|lpt[0-9]|conin\$|conout\$)(\..*)?$", re.IGNORECASE
)


class Sandbox:
    """Resolves paths the model supplies inside one root directory.

    A path is refused when it is absolute, names a drive, contains `..`, NUL, `:` or a reserved
    Windows device name, or resolves (following links and junctions) outside the root or, unless
    `allow_hidden`, to a hidden entry.

    Raises:
        ConfigurationError: If `root` is not an existing directory.

    Example:
        ```python
        path = Sandbox("./data").resolve("reports/2026.csv")
        ```
    """

    def __init__(self, root: str | os.PathLike[str], *, allow_hidden: bool = False) -> None:
        self.allow_hidden = allow_hidden
        if not str(root).strip():  # `Path("")` would quietly mean the working directory
            raise ConfigurationError("toolkit root is empty; name the directory the tools may use")
        candidate = Path(root).expanduser()
        if not candidate.is_dir():
            raise ConfigurationError(f"toolkit root {str(root)!r} is not an existing directory")
        self.root = candidate.resolve(strict=True)

    def resolve(self, path: str, *, must_exist: bool = False) -> Path:
        """Return the absolute path of `path` inside the root.

        Raises:
            ToolException: If `path` is not allowed or leaves the root, or (with `must_exist`) does not
                exist.
        """

        parts = self._parts(path)
        target = self.root.joinpath(*parts)
        resolved = target.resolve(strict=False)
        if not self.contains(resolved):
            raise ToolException(f"path {path!r} is outside the allowed directory")
        if self.hidden(resolved):  # a link to a hidden entry
            raise ToolException(f"path {path!r} names a hidden file or directory")
        if must_exist and not resolved.exists():
            raise ToolException(f"{path!r} does not exist{self._hint(resolved)}")
        return resolved

    def _hint(self, missing: Path) -> str:
        """` (did you mean ...)` for files next to `missing` that start with its name."""

        folder = missing.parent
        if not folder.is_dir() or not self.contains(folder):
            return ""
        names = sorted(
            entry.name
            for entry in folder.iterdir()
            if entry.name.lower().startswith(missing.name.lower())
            and (self.allow_hidden or not entry.name.startswith("."))
        )
        return f" (did you mean {', '.join(repr(name) for name in names[:5])}?)" if names else ""

    def contains(self, path: Path) -> bool:
        """Return whether the resolved `path` lies inside the root (or is the root)."""

        return path == self.root or path.is_relative_to(self.root)

    def hidden(self, path: Path) -> bool:
        """Return whether `path` (inside the root) is hidden and hidden entries are refused."""

        if self.allow_hidden:
            return False
        return any(part.startswith(".") for part in path.relative_to(self.root).parts)

    def relative(self, path: Path) -> str:
        """Return `path` relative to the root, with `/` separators."""

        relative = path.relative_to(self.root).as_posix()
        return relative or "."

    def _parts(self, path: str) -> list[str]:
        if not isinstance(path, str) or "\x00" in path:
            raise ToolException("path contains a NUL byte")
        text = path.strip().replace("\\", "/")
        if text in ("", "."):
            return []
        if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or text.startswith("~"):
            raise ToolException(f"path {path!r} must be relative to the allowed directory")
        parts = [part for part in PurePosixPath(text).parts if part not in ("", ".")]
        for part in parts:
            if part == "..":
                raise ToolException(f"path {path!r} must not contain '..'")
            if part.startswith(".") and not self.allow_hidden:
                raise ToolException(f"path {path!r} names a hidden file or directory")
            if ":" in part:
                raise ToolException(f"path {path!r} must not contain ':'")
            if _WINDOWS_RESERVED.match(part) or part.endswith((" ", ".")):
                raise ToolException(f"path {path!r} contains a reserved file name")
        return parts


def check_zip_bounds(
    path: str | os.PathLike[str],
    *,
    max_bytes: int = 200_000_000,
    max_entries: int = 20_000,
    max_ratio: int = 200,
) -> None:
    """Refuse an Office file (a zip) that would unpack into far more than it claims to hold.

    Raises:
        ToolException: If it has too many entries, unpacks to more than `max_bytes`, or an
            entry compresses by more than `max_ratio` (a zip bomb).
    """

    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
    except (zipfile.BadZipFile, OSError):
        return  # not a zip: the reading library reports it
    total = sum(entry.file_size for entry in entries)
    bomb = any(
        e.file_size > 1_000_000 and e.file_size > max_ratio * e.compress_size for e in entries
    )
    if len(entries) > max_entries or total > max_bytes or bomb:
        raise ToolException("the file unpacks to an unreasonable size and is refused")


_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def file_encoding(path: str | os.PathLike[str]) -> tuple[str, bool]:
    """`(encoding, has_bom)`: a BOM decides, else UTF-8 if the whole file decodes, else cp1252."""

    with open(path, "rb") as handle:
        start = handle.read(4)
    for bom, name in _BOMS:
        if start.startswith(bom):
            return name, True
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(_CHUNK):
                decoder.decode(chunk)
            decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return "cp1252", False
    return "utf-8", False
