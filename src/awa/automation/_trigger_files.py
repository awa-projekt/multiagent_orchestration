"""`FileDrop`: files appearing in a directory, and the bounded text of a received document."""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from awa._internal.logging import get_logger
from awa._internal.util import DurationLike
from awa.automation._time import as_utc, require_timedelta
from awa.automation._trigger_base import PollingTrigger, RunEndedError, TriggerContext
from awa.automation.models import TriggerEvent
from awa.automation.templates import TemplateLike
from awa.errors import ConfigurationError

if TYPE_CHECKING:
    from awa.automation.models import ProcessRun

DEFAULT_MAX_TEXT_CHARS = 100_000
"""Characters of a document's text kept in a payload."""

_logger = get_logger("automation.triggers")

_MAX_PDF_PAGES = 200
_TEXT_SUFFIXES = frozenset(
    {".txt", ".csv", ".tsv", ".md", ".json", ".xml", ".html", ".htm", ".yaml", ".yml", ".log"}
)
_LOCK_RETRIES = (0.2, 0.5, 1.0, 2.0, 4.0)
"""Seconds to wait before each new try to move a file another program still holds open."""

_WINDOWS = sys.platform == "win32"
_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"{port}{n}" for port in ("COM", "LPT") for n in range(1, 10))}
)


@dataclass
class _Candidate:
    size: int
    mtime_ns: int
    since: datetime


class FileDrop(PollingTrigger):
    """Start a run for every file that appears in a directory.

    A file is picked up once unchanged for `stable_for` and then moves to `in_progress_dir`, where
    its run reads it; it moves on to `processed_dir` on success, and to `failed_dir` with a
    `<name>.error.txt` when the run fails or is refused (existing names get a suffix). After a
    restart, files in `in_progress_dir` are offered again and find their run by dedup key. Links
    and Windows device names are ignored. The dedup key is the name plus the content's SHA-256.

    Events carry `name`, `path`, `processed_path`, `size`, `modified_at`, `sha256` and, per `read`:
    `"auto"` the text of PDF, Word, Excel and text files (`text`, `text_truncated`, `text_error`),
    `"text"` the decoded text, `"bytes"` `base64`, `"none"` nothing.

    Args:
        pattern: Glob pattern of file names, for example `"*.pdf"`.
        processed_dir: Where handled files go; relative to `directory`.
        failed_dir: Where the files of failed or refused runs go; relative to `directory`.
        stable_for: How long a file must stay unchanged.
        max_bytes: Larger files are moved to `failed_dir` without a run.

    Example:
        ```python
        FileDrop("inbox/invoices", "*.xml", input="Book this invoice:\\n{payload.text}")
        ```
    """

    def __init__(
        self,
        directory: str | os.PathLike[str],
        pattern: str = "*",
        *,
        name: str = "files",
        processed_dir: str | os.PathLike[str] = "processed",
        failed_dir: str | os.PathLike[str] = "failed",
        stable_for: DurationLike = timedelta(seconds=2),
        poll_interval: DurationLike = timedelta(seconds=5),
        read: Literal["auto", "text", "bytes", "none"] = "auto",
        encoding: str = "utf-8",
        max_bytes: int = 10 * 1024 * 1024,
        max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
        input: TemplateLike | None = None,
        thread_id: TemplateLike | None = None,
        dedup: TemplateLike | None = None,
    ) -> None:
        super().__init__(
            name, poll_interval=poll_interval, input=input, thread_id=thread_id, dedup=dedup
        )
        if read not in ("auto", "text", "bytes", "none"):
            raise ConfigurationError(
                f"read must be 'auto', 'text', 'bytes' or 'none', got {read!r}"
            )
        self.directory = Path(directory)
        self.pattern = pattern
        self.processed_dir = self.directory / processed_dir
        self.failed_dir = self.directory / failed_dir
        self.in_progress_dir = self.directory / "in-progress"
        self.stable_for = require_timedelta(stable_for, what="stable_for")
        self.read = read
        self.encoding = encoding
        self.max_bytes = max_bytes
        self.max_text_chars = max_text_chars
        self._candidates: dict[Path, _Candidate] = {}
        self._pending: set[Path] = set()
        self._moved: dict[Path, Path] = {}  # where the file of a waiting run is now

    def describe(self) -> dict[str, Any]:
        """See `BaseTrigger.describe`."""

        return {**super().describe(), "directory": str(self.directory), "pattern": self.pattern}

    async def poll(self, now: datetime) -> list[TriggerEvent]:
        """Return events for the files that are ready (see the class documentation)."""

        return await asyncio.to_thread(self._scan, as_utc(now))

    async def ready(self, context: TriggerContext) -> list[TriggerEvent]:
        """The files ready now; files seen for the first time are looked at again after
        `stable_for`, so a one-off pass picks up every file that stopped changing."""

        found = await self.poll(as_utc(context.clock.now()))
        if set(self._candidates) - self._pending:
            await context.sleep(self.stable_for.total_seconds())
            found += await self.poll(as_utc(context.clock.now()))
        return found

    async def ack(self, event: TriggerEvent, error: BaseException | None) -> None:
        """Move the file to `processed_dir`, or to `failed_dir` with a `.error.txt` when
        `error` is set."""

        original = Path(str(event.metadata.get("path", "")))
        path = self._moved.pop(original, original)
        self._pending.discard(original)
        self._candidates.pop(original, None)
        if not path.is_file():
            return
        for wait in (*_LOCK_RETRIES, None):
            try:
                if error is not None:
                    moved = await asyncio.to_thread(_move_unique, path, self.failed_dir)
                    await asyncio.to_thread(_write_reason, moved, error)
                else:
                    target = Path(str(event.metadata.get("processed_path") or self.processed_dir))
                    await asyncio.to_thread(_move_to, path, target)
                return
            except PermissionError:  # still open in another program (Windows): wait, try again
                if wait is None:
                    raise
                await asyncio.sleep(wait)

    async def settle(self, event: TriggerEvent, run: ProcessRun) -> None:
        """Move the file by the run's outcome (see the class documentation)."""

        if run.status != "waiting":
            await super().settle(event, run)
            return
        original = Path(str(event.metadata.get("path", "")))
        path = self._moved.get(original, original)
        if path.is_file() and path.parent != self.in_progress_dir:
            target = self.in_progress_dir / path.name
            self._moved[original] = await asyncio.to_thread(_move_to, path, target)

    def _scan(self, now: datetime) -> list[TriggerEvent]:
        self.directory.mkdir(parents=True, exist_ok=True)
        ready = self._waiting_files(now)
        present: set[Path] = set()
        for path in sorted(self.directory.iterdir()):
            if (
                path.is_symlink()  # a link could point at any file on the machine
                or not path.is_file()
                or not fnmatch.fnmatch(path.name, self.pattern)
                or (_WINDOWS and is_device_name(path.name))
            ):
                continue
            present.add(path)
            if path in self._pending:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            candidate = self._candidates.get(path)
            if candidate is None or (candidate.size, candidate.mtime_ns) != (
                stat.st_size,
                stat.st_mtime_ns,
            ):
                self._candidates[path] = _Candidate(stat.st_size, stat.st_mtime_ns, now)
                if self.stable_for > timedelta(0):
                    continue
                candidate = self._candidates[path]
            if now - candidate.since < self.stable_for:
                continue
            event = self._event(path, stat.st_size, stat.st_mtime_ns, now, claim=True)
            if event is not None:
                self._pending.add(Path(str(event.metadata["path"])))
                self._candidates.pop(path, None)
                ready.append(event)
        for gone in set(self._candidates) - present:
            self._candidates.pop(gone, None)
        return ready

    def _waiting_files(self, now: datetime) -> list[TriggerEvent]:
        """Files of runs that waited when the worker stopped: offered again; their dedup key
        finds the run, and the worker moves them on when the run ends."""

        if not self.in_progress_dir.is_dir():
            return []
        tracked = set(self._moved.values())
        ready = []
        for path in sorted(self.in_progress_dir.iterdir()):
            if path in self._pending or path in tracked or path.is_symlink():
                continue
            if not path.is_file() or not fnmatch.fnmatch(path.name, self.pattern):
                continue
            stat = path.stat()
            event = self._event(path, stat.st_size, stat.st_mtime_ns, now)
            if event is not None:
                self._pending.add(path)
                ready.append(event)
        return ready

    def _event(
        self, path: Path, size: int, mtime_ns: int, now: datetime, *, claim: bool = False
    ) -> TriggerEvent | None:
        if size > self.max_bytes:
            _logger.warning(
                "FileDrop %s: %s is larger than max_bytes; moved aside", self.name, path
            )
            moved = _move_unique(path, self.failed_dir)
            _write_reason(moved, ValueError(f"larger than max_bytes ({self.max_bytes} bytes)"))
            self._candidates.pop(path, None)
            return None
        try:
            data = path.read_bytes()
        except OSError:  # still locked by the writer (Windows): try again next poll
            return None
        digest = hashlib.sha256(data).hexdigest()
        processed = _free_name(self.processed_dir, path.name, digest)
        if claim:
            # The run reads the file in in_progress_dir from start to end, one place that does
            # not change while it waits or after a restart; it moves on when the run ends.
            try:
                path = _move_to(path, _free_name(self.in_progress_dir, path.name, digest))
            except OSError:  # still locked by the writer (Windows): try again next poll
                return None
        payload: dict[str, Any] = {
            "name": path.name,
            "path": str(path),
            "processed_path": str(processed),
            "size": size,
            "modified_at": datetime.fromtimestamp(mtime_ns / 1e9, tz=now.tzinfo).isoformat(),
            "sha256": digest,
        }
        if self.read == "auto":
            payload.update(document_text(path, data, max_chars=self.max_text_chars))
        elif self.read == "text":
            payload["text"] = data.decode(self.encoding, errors="replace")
        elif self.read == "bytes":
            payload["base64"] = base64.b64encode(data).decode("ascii")
        return TriggerEvent(
            source=self.name,
            payload=payload,
            received_at=now,
            dedup_key=f"{self.name}:{path.name}:{digest[:32]}",
            metadata={"path": str(path), "processed_path": str(processed)},
        )


def is_device_name(name: str) -> bool:
    """Whether a file name is a Windows device name (`NUL`, `com1.txt`, ...), which cannot safely be read or moved."""

    return name.split(".")[0].rstrip(" ").upper() in _DEVICE_NAMES


def document_text(
    path: Path, data: bytes, *, max_chars: int = DEFAULT_MAX_TEXT_CHARS
) -> dict[str, Any]:
    """Payload fields with the text of the file at `path` (its content is `data`).

    Returns:
        `text` (cut at `max_chars`, then `text_truncated: true`), nothing for another binary file,
        or `text_error` when the file could not be read.
    """

    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            from awa.toolkits.documents import pdf_text

            pages = pdf_text(data, max_pages=_MAX_PDF_PAGES, max_chars=max_chars)
            text: str | None = "\n\n".join(page for page in pages if page)
        elif suffix == ".docx":
            from awa.toolkits.documents import docx_text

            text = docx_text(path)
        elif suffix in (".xlsx", ".xlsm"):
            text = _workbook_text(path, max_chars)
        else:
            text = _decoded(data, suffix)
    except Exception as exc:  # noqa: BLE001 - the run still starts, with the reason
        return {"text_error": f"{type(exc).__name__}: {exc}"[:500]}
    if text is None:
        return {}
    if len(text) > max_chars:
        return {"text": text[:max_chars], "text_truncated": True}
    return {"text": text}


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            sha.update(block)
    return sha.hexdigest()


def _free_name(directory: Path, name: str, digest: str) -> Path:
    """Where a file goes in `directory`: its own name, or that name with the start of
    its digest when the name is taken (the same content lands on the same path)."""

    target = directory / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    return directory / f"{stem}.{digest[:12]}{suffix}"


def _move_to(path: Path, target: Path) -> Path:
    """Move `path` to `target`, or next to it under a free name if something else took it."""

    if target.exists() and (
        target.stat().st_size != path.stat().st_size or _digest(target) != _digest(path)
    ):
        return _move_unique(path, target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(path, target)
    return target


def _write_reason(path: Path, error: BaseException) -> None:
    """Write `<name>.error.txt` next to a failed file: what happened and what to do."""

    from awa.kernel.redaction import redact_secrets

    run = error.run if isinstance(error, RunEndedError) else None
    status = run.status if run is not None else "refused"
    detail = (run.error if run is not None else None) or str(error) or type(error).__name__
    detail = redact_secrets(" ".join(detail.split()))[:500]
    lines = [
        f"file: {path.name}",
        f"time: {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"status: {status}",
        f"error: {detail}",
    ]
    if run is not None:
        lines += [
            f"run: {run.id}",
            f"details: awa runs show {run.id}",
            f"fix: correct the cause, then awa runs retry {run.id} (or put the file back)",
        ]
    else:
        lines.append("fix: correct the cause (see the worker log), then put the file back")
    reason = path.with_name(f"{path.name}.error.txt")
    reason.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _move_unique(path: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / path.name
    counter = 1
    while target.exists():
        target = directory / f"{path.stem}.{counter}{path.suffix}"
        counter += 1
    shutil.move(str(path), str(target))
    return target


def _decoded(data: bytes, suffix: str) -> str | None:
    if suffix not in _TEXT_SUFFIXES and b"\x00" in data[:4096]:
        return None  # binary content of a type without a reader
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _workbook_text(path: Path, max_chars: int) -> str:
    """Every sheet of a workbook as JSON rows (the data toolkit's `read_excel`)."""

    from awa.toolkits.data import data_toolkit

    [read_excel] = data_toolkit(path.parent, include=["read_excel"], frame_output=False)
    sheets: list[str] = []
    names: list[str] | None = None
    while names is None or len(sheets) < len(names):
        sheet = None if names is None else names[len(sheets)]
        result = json.loads(read_excel.invoke({"path": path.name, "sheet": sheet, "limit": 500}))
        names = names or list(result["sheets"])
        sheets.append(f"# {result['sheet']}\n{json.dumps(result, ensure_ascii=False)}")
        if sum(map(len, sheets)) > max_chars:
            break
    return "\n\n".join(sheets)
