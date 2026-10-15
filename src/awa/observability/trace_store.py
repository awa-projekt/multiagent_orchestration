"""Run traces on disk: what happened in a run, as a redacted JSON Lines file.

Levels: `off`, `summary` (default; structure, tokens and short argument previews, no message
bodies) and `full` (bodies too, cut). Secrets are never stored at any level: texts are redacted,
sensitive keys masked and environment credentials removed. Files are owner-only; writing happens
on a background thread, and a trace stays readable after a crash.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import queue
import re
import string
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

from awa._internal.logging import get_logger
from awa.contracts.events import RunEvent, TextDeltaEvent
from awa.errors import AwaError, ConfigurationError
from awa.kernel.redaction import SECRETS_REDACTOR, Redactor
from awa.observability._trace_encode import (
    ERROR_CHARS,
    TraceEncoder,
    environment_secrets,
    identity_ref,
    iso,
)

if TYPE_CHECKING:
    from awa.contracts.usage import PriceTable

TraceLevel: TypeAlias = Literal["off", "summary", "full"]
"""How much a trace records (see the module documentation)."""

TRACE_LEVELS: tuple[str, ...] = ("off", "summary", "full")
"""The levels, least first."""

TRACE_FORMAT = "awa.trace/v1"
"""Format version written in the first line of every trace file."""

DEFAULT_TRACE_DIR = ".awa/traces"
"""Where traces are kept next to the local run store."""

DEFAULT_MAX_BYTES = 5_000_000
"""Longest trace of one run, in bytes; events beyond it are counted, not written."""

MIN_MAX_BYTES = 10_000
_PLAIN_STEM_CHARS = frozenset(string.ascii_lowercase + string.digits + "._-")
_logger = get_logger("trace")


def _check_max_bytes(max_bytes: int) -> None:
    if max_bytes < MIN_MAX_BYTES:
        raise ConfigurationError(f"max_bytes must be at least {MIN_MAX_BYTES}")


def resolve_level(value: str | None, *, default: TraceLevel = "summary") -> TraceLevel:
    """The trace level named by `value` (`None` or empty: `default`).

    Raises:
        ConfigurationError: If `value` is not `off`, `summary` or `full`.
    """

    if value is None or not str(value).strip():
        return default
    level = str(value).strip().lower()
    if level not in TRACE_LEVELS:
        raise ConfigurationError(
            f"Unknown trace level {value!r}; use one of {', '.join(TRACE_LEVELS)}"
        )
    return level  # type: ignore[return-value]


class _Writer:
    """One background thread that encodes and writes the events of every recorder, in order per recorder."""

    def __init__(self) -> None:
        self._jobs: queue.SimpleQueue[
            tuple[TraceRecorder | None, str, Any, threading.Event | None]
        ] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def submit(
        self, sink: TraceRecorder, op: str, payload: Any = None, done: threading.Event | None = None
    ) -> None:
        if self._thread is None or not self._thread.is_alive():
            with self._lock:
                if self._thread is None:
                    atexit.register(self.drain)
                if self._thread is None or not self._thread.is_alive():
                    self._thread = threading.Thread(
                        target=self._run, name="awa-trace-writer", daemon=True
                    )
                    self._thread.start()
        self._jobs.put((sink, op, payload, done))

    def drain(self, timeout: float = 5.0) -> None:
        """Wait until everything submitted so far is written."""

        if self._thread is None or not self._thread.is_alive():
            return
        marker = threading.Event()
        self._jobs.put((None, "barrier", None, marker))
        marker.wait(timeout)

    def _run(self) -> None:
        while True:
            batch = [self._jobs.get()]
            while len(batch) < 512:
                try:
                    batch.append(self._jobs.get_nowait())
                except queue.Empty:
                    break
            touched: dict[int, TraceRecorder] = {}
            for sink, op, payload, _ in batch:
                if sink is None:  # a barrier: it only marks a point in the queue
                    continue
                try:
                    sink._apply(op, payload)  # noqa: SLF001 - the writer drives its sinks
                except Exception:  # noqa: BLE001 - tracing never fails a run
                    _logger.warning("Writing a trace failed", exc_info=True)
                touched[id(sink)] = sink
            for sink in touched.values():
                sink._flush_file()  # noqa: SLF001
            for _, _, _, done in batch:
                if done is not None:
                    done.set()


_WRITER = _Writer()


class TraceRecorder:
    """Subscriber that writes the events of a run to a redacted JSON Lines trace file.

    Use it like any subscriber and call `close()` when the run is over.

    Args:
        path: The trace file; events are appended, so a resumed run adds to it.
        level: `summary` (default), `full` or `off`.
        root_run_id: Record only this run tree; without it, every received event is written.
        meta: Facts for the header (`automation`, `attempt`, `thread_id`, `tenant`, `actor`,
            `subject`, ...); `thread_id`, `actor` and `subject` are also kept as hashes so
            `erase_subject` finds the trace.
        redactor: Applied to every text; default `SECRETS_REDACTOR`.
        prices: Model prices, stored in the header so `awa runs trace` can show the cost.
        max_bytes: Longest trace file; later events are counted but not written.
        secrets: Further credential values to remove wherever they appear.

    Example:
        ```python
        recorder = TraceRecorder("run.trace.jsonl", level="full")
        agent = Agent("helper", hooks=[recorder])
        await agent.run("Hello")
        recorder.close()
        ```
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        level: TraceLevel = "summary",
        root_run_id: str | None = None,
        meta: Mapping[str, Any] | None = None,
        redactor: Redactor | None = None,
        prices: PriceTable | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        secrets: Iterable[str] = (),
        on_close: Callable[[], None] | None = None,
    ) -> None:
        level = resolve_level(level)
        _check_max_bytes(max_bytes)
        self.path = Path(path)
        self.level: TraceLevel = level
        self.root_run_id = root_run_id
        self._encoder = TraceEncoder(
            level,
            redactor if redactor is not None else SECRETS_REDACTOR,
            [*environment_secrets(), *(value for value in secrets if value)],
            with_root=root_run_id is None,
        )
        self._max_bytes = max_bytes
        self._on_close = on_close
        self._closed = False
        self._handle: Any = None
        self._broken = False
        self._written = 0
        self._events = 0
        self._dropped = 0
        self._truncated = False
        self._done = threading.Event()
        if level == "off":
            self._closed = True
            self._done.set()
            return
        header: dict[str, Any] = {
            "type": "trace",
            "format": TRACE_FORMAT,
            "level": level,
            "started_at": iso(datetime.now(UTC)),
            "awa_version": _version(),
            "root_run_id": root_run_id,
            "meta": self._encoder.meta(meta or {}),
        }
        if prices is not None:
            header["prices"] = prices.model_dump(mode="json", exclude_none=True)
        _WRITER.submit(self, "header", header)

    def __repr__(self) -> str:
        return f"TraceRecorder({str(self.path)!r}, level={self.level!r})"

    def __call__(self, event: RunEvent) -> None:
        """Record `event` (called on the event loop; the work happens on the writer thread)."""

        if self._closed or isinstance(event, TextDeltaEvent):
            return
        if self.root_run_id is not None and event.root_run_id != self.root_run_id:
            return
        _WRITER.submit(self, "event", event)

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until every event received so far is in the file."""

        if self._closed and self._done.is_set():
            return
        marker = threading.Event()
        _WRITER.submit(self, "flush", None, marker)
        marker.wait(timeout)

    def close(
        self,
        outcome: str | None = None,
        error: BaseException | str | None = None,
        *,
        timeout: float = 5.0,
    ) -> None:
        """Stop recording and write the closing line; calling it again does nothing.

        Args:
            outcome: How the run ended (`ok`, `paused`, `failed`, `cancelled`).
            error: Why it failed, when it did (redacted, cut).
            timeout: Longest wait for the writer, in seconds (`0`: do not wait).
        """

        if self._closed:
            return
        self._closed = True
        end: dict[str, Any] = {"type": "trace_end", "ts": iso(datetime.now(UTC))}
        if outcome:
            end["outcome"] = outcome
        if error is not None:
            text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else error
            end["error"] = self._encoder.clean(text, ERROR_CHARS, flatten=True)
        _WRITER.submit(self, "close", end, self._done)
        self._done.wait(timeout)

    def _apply(self, op: str, payload: Any) -> None:
        if op == "close":
            try:
                self._finish(payload)
            finally:  # also for a trace that broke: the store must stop counting it as open
                if self._on_close is not None:
                    self._on_close()
            return
        if self._broken:
            return
        if op == "event":
            record = self._encoder.encode(payload)
            if record is None:
                return
            self._write(record)
        elif op == "header":
            self._open()
            if self._written == 0:
                self._write(payload, always=True)

    def _finish(self, end: dict[str, Any]) -> None:
        if not self._broken:
            self._open()
            if self._dropped or self._truncated:
                end = {**end, "dropped": self._dropped}
            self._write({**end, "events": self._events}, always=True)
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._handle.close()
            self._handle = None

    def _open(self) -> None:
        if self._handle is not None or self._broken:
            return
        try:
            # A trace holds prompts and tool results: the owner alone may read it (POSIX; the
            # mode is cut by the umask, never widened). `O_BINARY` keeps Windows from adding CR.
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            descriptor = os.open(self.path, flags, 0o600)
            self._written = os.fstat(descriptor).st_size
            self._handle = os.fdopen(descriptor, "a", encoding="utf-8", newline="\n")
        except OSError as exc:
            self._broken = True
            _logger.warning("Cannot write the trace %s: %s", self.path, exc)

    def _write(self, record: dict[str, Any], *, always: bool = False) -> None:
        self._open()
        if self._handle is None:
            return
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
        size = len(line.encode("utf-8"))
        if not always and self._written + size > self._max_bytes:
            self._dropped += 1
            if not self._truncated:
                self._truncated = True
                marker = json.dumps({"type": "trace_truncated", "limit_bytes": self._max_bytes})
                self._handle.write(marker + "\n")
                self._written += len(marker) + 1
            return
        try:
            self._handle.write(line)
        except OSError as exc:
            self._broken = True
            _logger.warning("Cannot write the trace %s: %s", self.path, exc)
            return
        self._written += size
        if record.get("type") not in ("trace", "trace_end"):
            self._events += 1

    def _flush_file(self) -> None:
        if self._handle is not None:
            try:
                self._handle.flush()
            except (OSError, ValueError):
                self._broken = True


def _version() -> str:
    from awa._internal.version import __version__

    return str(__version__)


@dataclass
class TraceData:
    """A trace file read back.

    Attributes:
        header: The first line (format, level, `root_run_id`, `meta`, `prices`); empty without one.
        ends: The closing lines (one per recording session; a resumed run has several).
        truncated: The recorder stopped writing at `max_bytes`.
        dropped: How many events it did not write.
        damaged: Lines that could not be read (a cut last line after a crash counts).
    """

    path: Path
    header: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    ends: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    dropped: int = 0
    damaged: int = 0

    @property
    def meta(self) -> dict[str, Any]:
        """Facts about the run from the header."""

        meta = self.header.get("meta")
        return meta if isinstance(meta, dict) else {}

    @property
    def level(self) -> str:
        """The level the trace was recorded at (`summary` when the header is missing)."""

        return str(self.header.get("level") or "summary")

    @property
    def outcome(self) -> str | None:
        """How the last recording session ended, when it said."""

        return next((e["outcome"] for e in reversed(self.ends) if e.get("outcome")), None)

    @property
    def error(self) -> str | None:
        """The error the last recording session ended with, if any."""

        return next((e["error"] for e in reversed(self.ends) if e.get("error")), None)


def read_trace(path: str | os.PathLike[str]) -> TraceData:
    """Read a trace file; a damaged line (for example the cut last line of a crash) is skipped."""

    target = Path(path)
    data = TraceData(path=target)
    with target.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                data.damaged += 1
                continue
            if not isinstance(record, dict):
                data.damaged += 1
                continue
            kind = record.get("type")
            if kind == "trace":
                if not data.header:
                    data.header = record
            elif kind == "trace_end":
                data.ends.append(record)
                data.dropped += int(record.get("dropped") or 0)
            elif kind == "trace_truncated":
                data.truncated = True
            else:
                data.events.append(record)
    return data


class TraceDeleteError(AwaError, OSError):
    """Trace files could not be deleted: `deleted` were, `failed` were not (named in the message)."""

    def __init__(self, deleted: list[str], failed: list[str]) -> None:
        super().__init__(f"could not delete {', '.join(failed)}")
        self.deleted = deleted
        self.failed = failed


def _stem(name: str) -> str:
    """`name` as a case-safe file name: lowercase letters, digits and `._-` stay, the rest is `%XX`."""

    return "".join(
        char
        if char in _PLAIN_STEM_CHARS
        else "".join(f"%{byte:02X}" for byte in char.encode("utf-8"))
        for char in name
    )


def _age(value: timedelta | float | None, name: str) -> timedelta | None:
    if value is None:
        return None
    age = timedelta(seconds=value) if isinstance(value, int | float) else value
    if age <= timedelta(0):
        raise ConfigurationError(f"{name} must be positive, got {value!r}")
    return age


class TraceStore:
    """A folder of trace files, with the limits that keep it from growing for ever.

    Args:
        directory: The folder (created when the first trace is written).
        max_age: Traces older than this are deleted (`prune`, also hourly when a trace starts);
            default: keep them.
        max_files: Keep at most this many trace files (the newest).
        max_bytes: Longest trace of one run.

    Example:
        ```python
        store = TraceStore(".awa/traces", max_age=timedelta(days=30), max_files=2000)
        recorder = store.recorder("run_42", root_run_id="run_42", actor="u-42")
        ```
    """

    def __init__(
        self,
        directory: str | os.PathLike[str] = DEFAULT_TRACE_DIR,
        *,
        level: TraceLevel = "summary",
        max_age: timedelta | float | None = None,
        max_files: int | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        redactor: Redactor | None = None,
        prices: PriceTable | None = None,
    ) -> None:
        level = resolve_level(level)
        if max_files is not None and max_files < 1:
            raise ConfigurationError("max_files must be at least 1")
        _check_max_bytes(max_bytes)
        self.directory = Path(directory)
        self.level: TraceLevel = level
        self.max_age = _age(max_age, "max_age")
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.prices = prices
        self._redactor = redactor
        self._active: set[Path] = set()
        self._last_prune = 0.0
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"TraceStore({str(self.directory)!r}, level={self.level!r})"

    def path(self, name: str) -> Path:
        """The file of the trace called `name` (characters outside lowercase letters, digits and `._-` become `%XX`)."""

        return self.directory / f"{_stem(name)}.jsonl"

    def attempt_path(self, run_id: str, *, resume: bool = False) -> Path:
        """The file for the next attempt of process run `run_id` (`<run>.a<N>.jsonl`); `resume` continues the latest."""

        existing = self.attempts(run_id)
        if resume and existing:
            return existing[-1]
        number = _attempt_number(existing[-1]) + 1 if existing else 1
        return self.path(f"{run_id}.a{number}")

    def recorder(
        self,
        name: str | os.PathLike[str],
        *,
        root_run_id: str | None = None,
        level: TraceLevel | None = None,
        **meta: Any,
    ) -> TraceRecorder:
        """A recorder writing the trace `name` (or a file inside the folder), with `**meta` for its header."""

        self._prune_when_due()
        path = Path(name) if isinstance(name, os.PathLike) else self.path(str(name))
        if (level or self.level) != "off":
            with self._lock:
                self._active.add(path)
        try:
            return TraceRecorder(
                path,
                level=level or self.level,
                root_run_id=root_run_id,
                meta=meta,
                redactor=self._redactor,
                prices=self.prices,
                max_bytes=self.max_bytes,
                on_close=lambda: self._release(path),
            )
        except BaseException:
            self._release(path)
            raise

    def _release(self, path: Path) -> None:
        with self._lock:
            self._active.discard(path)

    def redact(self, value: Any) -> Any:
        """`value` (text or JSON-like data) with secrets and personal data removed as the traces
        of this store have them removed (its redactor, the credentials of the environment)."""

        encoder = TraceEncoder(
            "summary", self._redactor or SECRETS_REDACTOR, environment_secrets(), with_root=False
        )
        return encoder.redact(value)

    def files(self) -> list[Path]:
        """Every trace file, oldest first."""

        if not self.directory.is_dir():
            return []
        found = [p for p in self.directory.glob("*.jsonl") if p.is_file()]
        return sorted(found, key=lambda p: (_mtime(p), p.name))

    def attempts(self, run_id: str) -> list[Path]:
        """The trace files of the attempts of process run `run_id`, first attempt first."""

        pattern = re.compile(re.escape(_stem(run_id)) + r"\.a(\d+)\.jsonl")
        if not self.directory.is_dir():
            return []
        found = [
            (int(m.group(1)), p)
            for p in self.directory.iterdir()
            if (m := pattern.fullmatch(p.name)) is not None
        ]
        return [p for _, p in sorted(found)]

    def find(self, run_id: str) -> list[Path]:
        """The trace files of `run_id`: its attempts, else the single file named after it."""

        attempts = self.attempts(run_id)
        if attempts:
            return attempts
        single = self.path(run_id)
        return [single] if single.is_file() else []

    def prune(
        self,
        *,
        max_age: timedelta | float | None = None,
        max_files: int | None = None,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> list[str]:
        """Delete traces beyond `max_age` and `max_files` (not those being written); return names, oldest first.

        Raises:
            TraceDeleteError: If a file could not be deleted (the others were).
        """

        age = _age(max_age, "max_age") or self.max_age
        keep = max_files if max_files is not None else self.max_files
        if keep is not None and keep < 1:
            raise ConfigurationError("max_files must be at least 1")
        with self._lock:
            active = set(self._active)
        files = [p for p in self.files() if p not in active]
        reference = (now or datetime.now(UTC)).timestamp()
        doomed: list[Path] = []
        if age is not None:
            cutoff = reference - age.total_seconds()
            doomed = [p for p in files if _mtime(p) < cutoff]
        if keep is not None:
            remaining = [p for p in self.files() if p not in doomed]
            excess = len(remaining) - keep
            doomed += [p for p in remaining if p not in active][: max(excess, 0)]
        return [p.name for p in doomed] if dry_run else self._remove(doomed)

    def erase(
        self, subject_id: str, *, threads: Iterable[str] = (), dry_run: bool = False
    ) -> list[str]:
        """Delete the traces naming a person as `subject` or `actor`, or with a `thread_id` in `threads`.

        Identities are matched by their header hashes, so masked ids are found too.

        Raises:
            TraceDeleteError: If a file could not be deleted, for example while it is written (the
                others were deleted).
        """

        if not subject_id:
            raise ConfigurationError("subject_id must not be empty")
        person = identity_ref(subject_id)
        wanted = {identity_ref(thread) for thread in threads}
        doomed = []
        for path in self.files():
            meta = _header_meta(path)
            if person in (meta.get("subject_ref"), meta.get("actor_ref")) or (
                meta.get("thread_id_ref") in wanted
            ):
                doomed.append(path)
        return [p.name for p in doomed] if dry_run else self._remove(doomed)

    def _remove(self, doomed: list[Path]) -> list[str]:
        with self._lock:
            active = set(self._active)
        deleted: list[str] = []
        failed: list[str] = []
        for path in doomed:
            (failed if path in active or not _delete(path) else deleted).append(path.name)
        if failed:
            raise TraceDeleteError(deleted, failed)
        return deleted

    def _prune_when_due(self) -> None:
        if self.max_age is None and self.max_files is None:
            return
        moment = time.monotonic()
        if self._last_prune and moment - self._last_prune < 3600:
            return
        self._last_prune = moment
        try:
            self.prune()
        except OSError:
            _logger.warning("Pruning the traces in %s failed", self.directory, exc_info=True)


def _attempt_number(path: Path) -> int:
    match = re.search(r"\.a(\d+)\.jsonl$", path.name)
    return int(match.group(1)) if match else 0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _delete(path: Path) -> bool:
    """Delete `path`; whether it is gone."""

    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        _logger.warning("Cannot delete the trace %s", path, exc_info=True)
        return False
    return True


def _header_meta(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
        header = json.loads(first) if first.strip() else {}
    except (OSError, ValueError):
        return {}
    meta = header.get("meta") if isinstance(header, dict) else None
    return meta if isinstance(meta, dict) else {}


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TRACE_DIR",
    "TRACE_FORMAT",
    "TRACE_LEVELS",
    "TraceData",
    "TraceDeleteError",
    "TraceLevel",
    "TraceRecorder",
    "TraceStore",
    "read_trace",
    "resolve_level",
]
