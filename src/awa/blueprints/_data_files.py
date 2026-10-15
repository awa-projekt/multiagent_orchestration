"""The local file system backend of the build's data protocols (`StagedFiles`, `DataStager`).

Copies are read-only and live in a private temporary folder, removed with the `LocalFiles`.
A read-only file can be hard-linked into a run; a database, which tools change, is copied.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from awa.blueprints._project_data import FILES, ProjectData, resolve_notes
from awa.blueprints.ports import Mask
from awa.data import DataError, Limits, Privacy, ProfileEvent, safe_name
from awa.data import ingest as ingest_files
from awa.data import profile as profile_files
from awa.data._paths import file_sha256

SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
"""File suffixes of a SQLite database."""


def _remove(folder: Path) -> None:
    """Delete `folder`; only on Windows are read-only files made writable first (a hard-linked copy must stay read-only)."""

    if os.name == "nt":
        for item in folder.rglob("*"):
            with suppress(OSError):
                item.chmod(0o666 if item.is_file() else 0o777)
    shutil.rmtree(folder, ignore_errors=True)


class LocalFiles:
    """Staged copies in a temporary folder (`root/files/<name>`), see `StagedFiles`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        weakref.finalize(self, _remove, root)

    def materialize(self, dest: Path, *, share: bool = True, replace: bool = False) -> None:
        """Put the files into `dest/files` and create an empty `dest/output`.

        Raises:
            DataError: `replace` and `dest/files` is a link (it is not followed or removed).
        """

        target = dest / "files"
        if replace and target.is_symlink():
            raise DataError(f"{str(target)!r} is a link; it is not replaced by the copies")
        if replace and target.exists():
            shutil.rmtree(target)
        base = self.root / "files"
        for source in sorted(p for p in base.rglob("*") if p.is_file()):
            database = source.suffix.lower() in SQLITE_SUFFIXES
            copy = (dest if database else target) / source.relative_to(base)
            copy.parent.mkdir(parents=True, exist_ok=True)
            linked = False
            # Not on Windows: a link shares the read-only attribute, which any program may clear.
            if share and not database and os.name != "nt":
                with suppress(OSError):  # another volume, or a file system without links
                    os.link(source, copy)
                    linked = True
            if not linked:
                shutil.copyfile(source, copy)
                if share and not database:
                    copy.chmod(0o444)  # read-only as a link would be: tools only read inputs
        (dest / "output").mkdir(parents=True, exist_ok=True)


def prepare_data(
    paths: str | os.PathLike[str] | Sequence[str | os.PathLike[str]],
    *,
    notes: Mapping[str, str] | None = None,
    mask: Mask = "none",
    limits: Limits | None = None,
    on_progress: Callable[[ProfileEvent], None] | None = None,
) -> ProjectData:
    """Copy the files into a private folder and profile the copies (a `DataStager`); originals are never touched.

    Args:
        paths: Files, folders (recursive) and glob patterns; zip archives are unpacked safely.
        notes: A file name, path or glob to what must happen with it.
        mask: What the model sees of personal data in the digest of the files.
        on_progress: Called with a `ProfileEvent` for every stage of every file.

    Raises:
        DataError: A path does not exist, a limit is exceeded, a note matches no file, or a
            file is not safe to take (`UnsafeArchiveError`, `TypeMismatchError`, ...).

    Example:
        ```python
        data = prepare_data(["bank.csv", "invoices/"], notes={"bank.csv": "match to invoices"})
        ```
    """

    if isinstance(paths, str | os.PathLike):
        paths = [paths]
    root = Path(tempfile.mkdtemp(prefix="awa-data-"))
    try:
        copies = ingest_files(paths, root / "files", limits)
        profile = profile_files(
            root / "files",
            limits=limits,
            privacy=Privacy(mask=mask),
            on_progress=on_progress,
            root=root / "files",
        )
        for file in (root / "files").rglob("*"):
            if file.is_file():
                file.chmod(0o444)
        resolved = resolve_notes(profile, notes or {})
    except BaseException:
        _remove(root)
        raise
    return ProjectData(
        profile=profile,
        files=LocalFiles(root),
        notes=resolved,
        originals={copy.name: str(copy.source) for copy in copies},
        mask=mask,
    )


class LocalRunFiles:
    """The files of a built project folder for `run_project` (see `RunFiles`)."""

    def __init__(self, project: str | os.PathLike[str]) -> None:
        self.project = Path(project).resolve()
        if not (self.project / FILES).is_dir():
            raise DataError(
                f"{str(project)!r} holds no {FILES}/ folder; it was not built with `--data`"
            )

    def _inside(self, path: Path) -> bool:
        base = (self.project / FILES).resolve()
        return path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(base)

    def inputs(self, pattern: str) -> list[str]:
        """The files below `data/files/` that a `data:` entry's `path` names (a file, folder or glob)."""

        text = pattern.replace("\\", "/").removeprefix("./").rstrip("/")
        if text != FILES and not text.startswith(f"{FILES}/"):
            return []
        base = self.project / FILES
        relative = text.removeprefix(FILES).lstrip("/")
        found: list[Path]
        if any(ch in relative for ch in "*?["):
            found = list(base.glob(relative))
        elif (base / relative).is_dir():
            found = list((base / relative).rglob("*"))
        else:
            found = [base / relative]
        return sorted(p.relative_to(base).as_posix() for p in found if self._inside(p))

    def digest(self, name: str) -> str:
        """The sha256 of the input file `name`."""

        path = self.project / FILES / name
        if not self._inside(path):
            raise DataError(f"{name!r} is not a file of {FILES}/")
        return file_sha256(path)

    @contextmanager
    def workspace(self) -> Iterator[Path]:
        """A temporary project folder with a copy of `data/` and an empty `data/output/`."""

        data = self.project / "data"

        def without_output(directory: str, names: list[str]) -> list[str]:
            return ["output"] if Path(directory) == data else []

        with tempfile.TemporaryDirectory(prefix="awa-run-", ignore_cleanup_errors=True) as folder:
            shutil.copytree(data, Path(folder) / "data", ignore=without_output)
            (Path(folder) / "data" / "output").mkdir()
            yield Path(folder)

    def collect(self, workspace: Path, name: str) -> list[str]:
        """Copy the regular files a run wrote to `workspace/data/output` into `data/output/<name>/`; return their paths.

        An earlier result of the same input is replaced.
        """

        source = workspace / "data" / "output"
        # The whole input name, extension included: `orders.csv` and `orders.xlsx` stay apart.
        flat = PurePosixPath(name.replace("\\", "/")).as_posix().replace("/", "_")
        target = self.project / "data" / "output" / safe_name(flat)
        written = [p for p in sorted(source.rglob("*")) if p.is_file() and not p.is_symlink()]
        if target.exists():
            shutil.rmtree(target)
        copies = []
        for item in written:
            copy = target / item.relative_to(source)
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, copy)
            copies.append(copy.relative_to(self.project).as_posix())
        return copies

    def record(self, run_id: str, report: Mapping[str, Any]) -> str:
        """Write `runs/<run_id>.json`."""

        path = self.project / "runs" / f"{safe_name(run_id)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path.relative_to(self.project).as_posix()


def system_clock() -> datetime:
    """The current time, timezone aware (the default clock of `run_project`)."""

    return datetime.now(UTC)
