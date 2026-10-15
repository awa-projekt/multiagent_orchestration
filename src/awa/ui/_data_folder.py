"""The file store of the Data step: a folder of the project (`data/files`)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import threading
import unicodedata
import uuid
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath

from awa.data import (
    DataError,
    Limits,
    TooLargeError,
    TooManyFilesError,
    detect_kind,
    ingest,
    safe_name,
)
from awa.data._model import FileKind
from awa.ui.ports import StoredFile

_MAX_DEPTH = 8
_DRIVE = re.compile(r"^[A-Za-z]:")
UPLOAD_LIMITS = Limits(max_files=200, max_file_bytes=50_000_000, max_total_bytes=500_000_000)
"""What the Data step accepts: 200 files, 50 MB each, 500 MB in all."""
ACCEPTED_KINDS: frozenset[FileKind] = frozenset(
    {"csv", "xlsx", "pdf", "docx", "mail", "json", "sqlite", "text", "zip"}
)
"""The kinds the Data step accepts (a zip archive only as a container of the others)."""


def megabytes(size: int) -> str:
    """`size` bytes as text for a person: `3.5 MB`, `120 KB`, `12 bytes`."""

    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.0f} KB"
    return f"{size} bytes"


class LocalFileStore:
    """The user's files in a folder of the project, behind the `FileStore` backend protocol.

    Uploads stream into a scratch folder, then `awa.data.ingest` copies them in (kind by content,
    zip entries checked first, nothing overwritten); the scratch copy is always removed.

    Args:
        limits: `max_files`, `max_file_bytes` and `max_total_bytes` of the store.
    """

    def __init__(
        self, root: Path, scratch: Path, limits: Limits, kinds: frozenset[FileKind] = ACCEPTED_KINDS
    ) -> None:
        self.root = root
        self.max_file_bytes = limits.max_file_bytes
        self.max_total_bytes = limits.max_total_bytes
        self.max_files = limits.max_files
        self.kinds = kinds
        self._scratch = scratch
        self._limits = limits
        self._lock = threading.Lock()  # one copy into the store at a time
        self._hashes: dict[tuple[str, int, int], str] = {}

    def sweep(self) -> None:
        """Remove what an upload that was cut short left in the scratch folder."""

        shutil.rmtree(self._scratch, ignore_errors=True)

    @staticmethod
    def _relative(name: str) -> PurePosixPath:
        """The safe relative name of an uploaded `name`: folders kept, every part made safe."""

        name = "".join(c for c in name if unicodedata.category(c) != "Cf")  # no direction tricks
        if name.startswith(("/", "\\")) or _DRIVE.match(name):
            raise DataError(f"{name!r}: a file name must not start with / or a drive letter")
        parts = [part for part in re.split(r"[\\/]", name) if part not in ("", ".")]
        if not parts or ".." in parts or len(parts) > _MAX_DEPTH:
            raise DataError(
                f"{name!r}: not a usable file name (no .., at most {_MAX_DEPTH} folders)"
            )
        return PurePosixPath(*(safe_name(part) for part in parts))

    async def receive(self, name: str, chunks: AsyncIterator[bytes]) -> list[StoredFile]:
        relative = self._relative(name)
        scratch = self._scratch / uuid.uuid4().hex
        target = scratch.joinpath(*relative.parts)
        try:
            size = await self._write(relative, target, chunks)
            return await asyncio.to_thread(self._take, relative, target, size)
        finally:
            await asyncio.to_thread(shutil.rmtree, scratch, True)

    async def _write(
        self, relative: PurePosixPath, target: Path, chunks: AsyncIterator[bytes]
    ) -> int:
        """Stream the body into `target`; stop at the first byte over the limit."""

        target.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with target.open("wb") as out:
            async for chunk in chunks:
                size += len(chunk)
                if size > self.max_file_bytes:
                    raise TooLargeError(
                        f"{relative}: bigger than {megabytes(self.max_file_bytes)}, "
                        "the limit for one file"
                    )
                await asyncio.to_thread(out.write, chunk)
        if size == 0:
            raise DataError(f"{relative}: the file is empty")
        return size

    def _take(self, relative: PurePosixPath, target: Path, size: int) -> list[StoredFile]:
        """Copy the scratch file into the store (one call at a time)."""

        with self._lock:
            return self._copy(relative, target, size)

    def _copy(self, relative: PurePosixPath, target: Path, size: int) -> list[StoredFile]:
        if self.used() + size > self.max_total_bytes:
            raise TooLargeError(
                f"{relative}: the files would take more than {megabytes(self.max_total_bytes)} "
                "together; remove files you no longer need"
            )
        if len(self.listing()) >= self.max_files:
            raise TooManyFilesError(f"The project holds {self.max_files} files already")
        folder = relative.parent
        dest = self.root.joinpath(*folder.parts)
        copies = ingest([target], dest, self._limits, kinds=self.kinds)
        stored = [
            StoredFile((folder / copy.name).as_posix(), copy.size, copy.kind, copy.sha256)
            for copy in copies
        ]
        # An archive unpacks to more than its upload: check the limits again.
        problem: DataError | None = None
        if len(self.listing()) > self.max_files:
            problem = TooManyFilesError(f"The project may hold at most {self.max_files} files")
        elif self.used() > self.max_total_bytes:
            problem = TooLargeError(
                f"{relative}: unpacked, the files would take more than "
                f"{megabytes(self.max_total_bytes)} together; remove files you no longer need"
            )
        if problem is not None:
            for copy in copies:
                copy.path.unlink(missing_ok=True)
            raise problem
        return stored

    def listing(self) -> list[StoredFile]:
        files: list[StoredFile] = []
        if not self.root.is_dir():
            return files
        for path in sorted(self.root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            info = path.stat()
            name = path.relative_to(self.root).as_posix()
            files.append(
                StoredFile(name, info.st_size, detect_kind(path).kind, self._sha(path, name, info))
            )
        return files

    def _sha(self, path: Path, name: str, info: os.stat_result) -> str:
        """The hash of a file, remembered while its size and time stay the same."""

        key = (name, info.st_size, info.st_mtime_ns)
        if key not in self._hashes:
            digest = hashlib.sha256()
            with path.open("rb") as reader:
                while chunk := reader.read(1 << 20):
                    digest.update(chunk)
            self._hashes[key] = digest.hexdigest()
        return self._hashes[key]

    def used(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(
            p.stat().st_size for p in self.root.rglob("*") if p.is_file() and not p.is_symlink()
        )

    def remove(self, name: str) -> None:
        path = self.root.joinpath(*[p for p in re.split(r"[\\/]", name) if p])
        resolved = path.resolve()
        if (
            ".." in re.split(r"[\\/]", name)
            or path.is_symlink()
            or not resolved.is_relative_to(self.root.resolve())
            or not resolved.is_file()
        ):
            raise DataError(f"{name!r}: there is no such file in your data")
        resolved.unlink()
        folder = resolved.parent
        root = self.root.resolve()
        while folder != root and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent
