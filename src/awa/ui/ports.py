"""What the pages of the web app need from the outside world, as small backend protocols.

A `FileStore` raises `awa.data.DataError` (or a subclass) for a file it refuses or a name that is
not stored; the backend is `awa.ui._data_folder.LocalFileStore`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class StoredFile:
    """A file kept by a `FileStore`.

    Attributes:
        name: Its name relative to the store, with `/` between folders.
        kind: `csv`, `xlsx`, `pdf`, `docx`, `mail`, `json`, `sqlite` or `text`.
    """

    name: str
    size: int
    kind: str
    sha256: str


class FileStore(Protocol):
    """Private copies of the user's files (the originals are never touched).

    Attributes:
        root: The folder the copies are in (the profile reads them there).
    """

    root: Path
    max_file_bytes: int
    max_total_bytes: int
    max_files: int

    @property
    def kinds(self) -> Collection[str]: ...

    async def receive(self, name: str, chunks: AsyncIterator[bytes]) -> list[StoredFile]:
        """Store `chunks` as `name` (a zip is unpacked) and return what was stored.

        Nothing is overwritten (a taken name gets `-2`); a refused file leaves nothing behind.
        """
        ...

    def listing(self) -> list[StoredFile]:
        """Every stored file, sorted by name."""
        ...

    def remove(self, name: str) -> None:
        """Delete the stored file `name` (and its folder when that stays empty)."""
        ...

    def used(self) -> int:
        """The bytes all stored files take."""
        ...


__all__ = ["FileStore", "StoredFile"]
