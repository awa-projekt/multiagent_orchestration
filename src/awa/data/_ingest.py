"""Safe file intake: type and size checks, safe names, checked zip unpacking."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import unicodedata
import zipfile
import zlib
from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO

from awa._internal.sandbox import check_zip_bounds
from awa.data._detect import detect_kind
from awa.data._errors import (
    DataError,
    TooLargeError,
    TooManyFilesError,
    TypeMismatchError,
    UnsafeArchiveError,
    UnsupportedTypeError,
)
from awa.data._model import FileKind, Limits
from awa.data._paths import PathLike, display_names, expand_paths
from awa.data.ports import UploadScanner

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE)
_DRIVE = re.compile(r"^[A-Za-z]:")
_MAX_NAME = 100
_MAX_EXTENSION = 16
_SYMLINK_MODE = 0o120000
_CHUNK = 1 << 20


@dataclass(frozen=True)
class IngestedFile:
    """One copied file.

    Attributes:
        source: The original (for a zip entry: the archive).
        name: Its safe name relative to the destination folder (posix style).
    """

    source: Path
    path: Path
    name: str
    sha256: str
    size: int
    kind: FileKind


def safe_name(name: str) -> str:
    """A file name that is safe on every platform and as a path component.

    Drops folders, replaces characters Windows forbids with `_`, prefixes reserved names (`CON`)
    with `_` and cuts to 100 characters keeping the extension; an empty result becomes `file`.
    """

    text = unicodedata.normalize("NFC", re.split(r"[\\/]", name)[-1])
    text = _UNSAFE_CHARS.sub("_", text).lstrip(". ").rstrip(". ")
    if _RESERVED.match(text):
        text = f"_{text}"
    stem, dot, extension = text.rpartition(".")
    if not (dot and stem) or len(extension) > _MAX_EXTENSION:
        stem, extension = text, ""
    suffix = f".{extension}" if extension else ""
    return (stem[: _MAX_NAME - len(suffix)] + suffix) or "file"


def ingest(
    paths: PathLike | Sequence[PathLike],
    dest: PathLike,
    limits: Limits | None = None,
    *,
    kinds: Collection[FileKind] | None = None,
    scanner: UploadScanner | None = None,
) -> list[IngestedFile]:
    """Copy files, folders and glob matches into `dest` after checking them.

    Zip archives are unpacked into a folder named like the archive, only after every entry passed
    the name, link, password, size and ratio checks (no nested archives). Names go through
    `safe_name`; taken names get `-2`, `-3`, ... Existing files are never overwritten. Whatever is
    raised, the copies made so far are removed. With upload checks switched off in the blueprint, a
    name/content mismatch and a kind outside `kinds` are accepted; the limits and `scanner` still
    apply.

    Args:
        paths: Files, folders (recursive) and glob patterns.
        limits: The file, size and zip limits; defaults to `Limits()`.
        kinds: When given, only these kinds are accepted (an upload allow-list).
        scanner: Called with every copy; it rejects the file by raising.

    Returns:
        The copies in sorted name order.

    Raises:
        TypeMismatchError: The name says one kind, the content another.
        UnsupportedTypeError: `kinds` is given and the file is not one of them.
        TooLargeError: A file or all files together are too large.
        TooManyFilesError: More files than `limits.max_files`.
        UnsafeArchiveError: A zip archive is unsafe to unpack.
        DataError: A path is missing, a link, or a folder is empty.

    Example:
        ```python
        copies = ingest(["invoices/", "bank.csv"], "project/data/files")
        print([(c.name, c.kind, c.sha256[:8]) for c in copies])
        ```
    """

    limits = limits or Limits()
    targets, _ = expand_paths(paths)
    if len(targets) > limits.max_files:
        raise TooManyFilesError.over(limits.max_files, len(targets))
    names = display_names(targets)
    from awa.plugins.switches import is_off

    run = _Run(Path(dest), limits, kinds, scanner, checks=not is_off("guard.upload_checks"))
    try:
        for source in sorted(targets, key=lambda p: names[p].lower()):
            run.take(source, _relative(names[source].split("/")))
    except BaseException:
        run.undo()
        raise
    return sorted(run.files, key=lambda f: f.name.lower())


_UNREADABLE = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError)


def check_archive(path: Path, limits: Limits, label: str) -> None:
    """Refuse a zip-based file that would unpack into far more than it claims to hold.

    Raises:
        UnsafeArchiveError: Too many entries, too large unpacked, or a zip-bomb ratio.
    """

    from awa._internal.upstream.tools import ToolException

    try:
        check_zip_bounds(
            path,
            max_bytes=limits.max_total_bytes,
            max_entries=limits.max_zip_entries,
            max_ratio=limits.max_zip_ratio,
        )
    except ToolException as error:
        raise UnsafeArchiveError(f"{label}: {error}") from error


def _entry_time(info: zipfile.ZipInfo) -> float:
    """The modification time an archive entry records (UTC; 1980-01-01 when it records none)."""

    try:
        return datetime(*info.date_time, tzinfo=UTC).timestamp()
    except ValueError:  # month 0 and the like: some tools write all zeros
        return datetime(1980, 1, 1, tzinfo=UTC).timestamp()


def _chunks(reader: IO[bytes]) -> Iterator[bytes]:
    while chunk := reader.read(_CHUNK):
        yield chunk


def _relative(parts: Iterable[str]) -> PurePosixPath:
    return PurePosixPath(*(safe_name(part) for part in parts if part not in ("", ".")))


@dataclass
class _Run:
    dest: Path
    limits: Limits
    kinds: Collection[FileKind] | None
    scanner: UploadScanner | None
    checks: bool = True
    files: list[IngestedFile] = field(default_factory=list)
    created: list[Path] = field(default_factory=list)
    folders: list[Path] = field(default_factory=list)
    taken: set[str] = field(default_factory=set)
    total: int = 0

    def take(self, source: Path, relative: PurePosixPath) -> None:
        if source.stat().st_size > self.limits.max_file_bytes:
            raise TooLargeError.file(str(relative), self.limits.max_file_bytes)
        detection = detect_kind(source)
        if detection.mismatch and self.checks:
            raise _mismatch(relative, detection.declared, detection.kind)
        if detection.kind == "zip":
            self.unpack(source, relative.with_suffix(""))
            return
        with source.open("rb") as reader:
            self.store(source, relative, _chunks(reader), source.stat().st_mtime)

    def unpack(self, archive_path: Path, folder: PurePosixPath) -> None:
        check_archive(archive_path, self.limits, f"{folder}.zip")
        try:
            with zipfile.ZipFile(archive_path) as archive:
                entries = [
                    (info, _entry_name(info, folder))
                    for info in archive.infolist()
                    if not info.is_dir()
                ]
                for info, relative in entries:
                    with archive.open(info) as reader:
                        self.store(archive_path, relative, _chunks(reader), _entry_time(info))
        except _UNREADABLE as error:
            raise UnsafeArchiveError(f"{folder}.zip: {error}") from error

    def store(
        self, source: Path, relative: PurePosixPath, chunks: Iterable[bytes], modified: float
    ) -> None:
        """Write the copy and give it the modification time of its source (`modified`), so a
        copy is as old as the original and profiles of two ingests of the same data agree."""

        limits = self.limits
        if len(self.files) >= limits.max_files:
            raise TooManyFilesError.over(limits.max_files)
        target = self._free(relative)
        digest, size = hashlib.sha256(), 0
        try:
            self._make_parent(target)
            out = target.open("xb")
        except (FileExistsError, NotADirectoryError) as error:
            raise DataError(f"{relative}: a file and a folder have the same name") from error
        self.created.append(target)
        with out:
            for chunk in chunks:
                size += len(chunk)
                if size > limits.max_file_bytes:
                    raise TooLargeError.file(str(relative), limits.max_file_bytes)
                digest.update(chunk)
                out.write(chunk)
        os.utime(target, (modified, modified))
        self.total += size
        if self.total > limits.max_total_bytes:
            raise TooLargeError.together(limits.max_total_bytes)
        kind = self._accept(target, relative)
        name = target.relative_to(self.dest).as_posix()
        self.files.append(IngestedFile(source, target, name, digest.hexdigest(), size, kind))

    def _accept(self, target: Path, relative: PurePosixPath) -> FileKind:
        detection = detect_kind(target)
        if detection.mismatch and self.checks:
            raise _mismatch(relative, detection.declared, detection.kind)
        if detection.kind == "zip":
            raise UnsafeArchiveError(f"{relative}: archives inside archives are not accepted")
        if self.checks and self.kinds is not None and detection.kind not in self.kinds:
            raise UnsupportedTypeError(
                f"{relative}: awa does not accept {detection.kind} files here "
                f"(accepted: {', '.join(sorted(self.kinds))})"
            )
        if detection.kind in ("xlsx", "docx"):
            check_archive(target, self.limits, str(relative))
        if self.scanner is not None:
            self.scanner.scan(target)
        return detection.kind

    def _free(self, relative: PurePosixPath) -> Path:
        number = 1
        while True:
            name = (
                relative
                if number == 1
                else relative.with_name(f"{relative.stem}-{number}{relative.suffix}")
            )
            if name.as_posix().lower() not in self.taken and not (self.dest / name).exists():
                self.taken.add(name.as_posix().lower())
                return self.dest / name
            number += 1

    def _make_parent(self, target: Path) -> None:
        missing = []
        folder = target.parent
        while not folder.exists():
            missing.append(folder)
            folder = folder.parent
        target.parent.mkdir(parents=True, exist_ok=True)
        self.folders += missing

    def undo(self) -> None:
        for path in self.created:
            path.unlink(missing_ok=True)
        for folder in self.folders:
            with contextlib.suppress(OSError):  # not empty: it held other files
                folder.rmdir()


def _mismatch(
    relative: PurePosixPath, declared: FileKind | None, found: FileKind
) -> TypeMismatchError:
    return TypeMismatchError(
        f"{relative}: the name says {declared} but the content is {found}; "
        "rename the file or remove it"
    )


def _entry_name(info: zipfile.ZipInfo, folder: PurePosixPath) -> PurePosixPath:
    """The safe target of an archive entry; refuses escapes, links and passwords."""

    name = info.filename
    parts = re.split(r"[\\/]", name)
    if name.startswith(("/", "\\")) or _DRIVE.match(name) or ".." in parts or "\x00" in name:
        raise UnsafeArchiveError(f"{folder}.zip: entry {name!r} leaves the target folder")
    if (info.external_attr >> 16) & 0o170000 == _SYMLINK_MODE:
        raise UnsafeArchiveError(f"{folder}.zip: entry {name!r} is a symbolic link")
    if info.flag_bits & 0x1:
        raise UnsafeArchiveError(f"{folder}.zip: entry {name!r} is password protected")
    inside = _relative(parts)
    if not inside.parts:
        raise UnsafeArchiveError(f"{folder}.zip: entry {name!r} has no file name")
    return folder / inside
