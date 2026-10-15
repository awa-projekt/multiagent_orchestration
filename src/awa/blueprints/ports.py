"""Backend protocols of the build: how `build_project` and `design_project` reach the user's files.

`StagedFiles` are private copies owned by a backend (`LocalFiles`: a temporary folder);
a `DataStager` turns paths into a `ProjectData` (`prepare_data` works on the local file
system). Both are structural protocols; refused files, a note that matches no file and a
staged copy that is gone raise `awa.data.DataError`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from awa.blueprints._fit import FitVerdict
    from awa.blueprints._project_data import ProjectData
    from awa.data import Limits, ProfileEvent

Mask = Literal["none", "placeholders", "drop"]
"""What the model sees of personal data in the digest of the files (see `awa.data.Privacy`)."""


class StagedFiles(Protocol):
    """Private copies of the user's files, laid out as `files/<name>` (read-only for the build)."""

    def materialize(self, dest: Path, *, share: bool = True, replace: bool = False) -> None:
        """Put the files into `dest/files` and create an empty `dest/output`.

        Args:
            share: Allow hard links where no tool can change a file (a database is always copied).
            replace: Remove an existing `dest/files` first.
        """
        ...


class DataStager(Protocol):
    """Copy files and profile the copies."""

    def __call__(
        self,
        paths: Sequence[str | os.PathLike[str]],
        *,
        notes: Mapping[str, str],
        mask: Mask,
        limits: Limits | None,
        on_progress: Callable[[ProfileEvent], None] | None,
    ) -> ProjectData:
        """Stage `paths` (files, folders, globs); `notes` maps a file name, path or glob to what must happen with it."""
        ...


class RunFiles(Protocol):
    """The files of a built project that `run_project` reads and writes.

    Results go only below `data/output/` and run reports below `runs/`; an implementation
    refuses anything else. A missing project folder raises `awa.data.DataError`.
    """

    def inputs(self, pattern: str) -> list[str]:
        """The names (relative to `data/files/`, sorted) of the files a `data:` entry's `path` matches."""
        ...

    def digest(self, name: str) -> str:
        """The sha256 of the input file `name`."""
        ...

    def workspace(self) -> AbstractContextManager[Path]:
        """A fresh copy of the project's `data/` in a temporary project folder (removed at exit)."""
        ...

    def collect(self, workspace: Path, name: str) -> list[str]:
        """Copy what a run wrote to `workspace/output` into `data/output/<name>/`; the paths
        (relative to the project) of the copies."""
        ...

    def record(self, run_id: str, report: Mapping[str, Any]) -> str:
        """Write the report of a run as `runs/<run_id>.json`; its path relative to the project."""
        ...


class FitJudge(Protocol):
    """The optional model call of `fit_check`: description, digest and findings in, `FitVerdict` out."""

    async def __call__(
        self, description: str, digest: str, findings: Sequence[str]
    ) -> FitVerdict: ...


__all__ = ["DataStager", "FitJudge", "Mask", "RunFiles", "StagedFiles"]
