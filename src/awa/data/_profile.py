"""`profile()`: find, identify and parse the files, collect one `DataProfile`."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import itertools
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from awa.data._detect import detect_kind
from awa.data._errors import DataError, TooLargeError, TooManyFilesError, UnsafeArchiveError
from awa.data._ingest import check_archive, safe_name
from awa.data._isolate import ISOLATED_PARSERS, Isolate, IsolateError, lease, release
from awa.data._model import DataProfile, FileProfile, Limits, Privacy, ProfileEvent, make_flag
from awa.data._parse import ParseContext, ParseTimeout, parser_for
from awa.data._paths import PathLike, display_names, expand_paths, file_sha256
from awa.data._relations import with_relations
from awa.data.ports import Parser
from awa.runtime.clock import Clock, SystemClock

_MAX_EMBEDDED_DEPTH = 3
_HARD_TIMEOUT_GRACE_S = 0.5
_READ_AHEAD_THREADS = 8  # files opened side by side before their turn
_READ_AHEAD_BYTES = 4096
_READ_AHEAD_JOIN_S = 1.0  # a file that is still not open after the run is left alone


def profile(
    paths: PathLike | Sequence[PathLike],
    *,
    limits: Limits | None = None,
    privacy: Privacy | None = None,
    on_progress: Callable[[ProfileEvent], None] | None = None,
    root: PathLike | None = None,
    created: str | None = None,
    clock: Clock | None = None,
) -> DataProfile:
    """Profile files, folders (recursively) and globs into one `DataProfile`.

    Deterministic and offline. A bad file never fails the run: it is in the result with status
    `error` or `unsupported` and a flag. Symbolic links are not followed.

    Args:
        paths: One path or a list of files, folders and glob patterns (`reports/*.csv`).
        limits: What the run may spend; defaults to `Limits()`. A file over
            `limits.parse_timeout_s` gets `DF-E-TIMEOUT`.
        privacy: Recorded in the profile; `render_for_model` applies it.
        root: When given, every path must lie inside it, and names are relative to it.
        created: The ISO 8601 `created` time to record, for reproducible output; default now.

    Raises:
        DataError: No path, a missing path or one outside `root`, or more files or bytes than
            the limits allow.

    Example:
        ```python
        p = profile(["bank.csv", "invoices/"])
        for file in p.files:
            print(file.path, file.kind, file.status, [flag.code for flag in file.flags])
        ```
    """

    return _run(paths, limits, privacy, on_progress, root, created, None, clock)


async def aprofile(
    paths: PathLike | Sequence[PathLike],
    *,
    limits: Limits | None = None,
    privacy: Privacy | None = None,
    on_progress: Callable[[ProfileEvent], None] | None = None,
    root: PathLike | None = None,
    created: str | None = None,
    clock: Clock | None = None,
) -> DataProfile:
    """`profile()` in a worker thread; cancelling stops the run before the next file.

    `on_progress` is called from that worker thread.
    """

    import asyncio  # imported on use: `import awa.data` stays light

    cancel = threading.Event()
    try:
        return await asyncio.to_thread(
            _run, paths, limits, privacy, on_progress, root, created, cancel, clock
        )
    except asyncio.CancelledError:
        cancel.set()
        raise


@dataclass
class _Run:
    """What the files of one run share."""

    limits: Limits
    privacy: Privacy
    emit: Callable[[ProfileEvent], None]
    cancel: threading.Event | None = None
    isolate: Isolate = field(default_factory=lease)
    clock: Clock = field(default_factory=SystemClock)


def _run(
    paths: PathLike | Sequence[PathLike],
    limits: Limits | None,
    privacy: Privacy | None,
    on_progress: Callable[[ProfileEvent], None] | None,
    root: PathLike | None,
    created: str | None,
    cancel: threading.Event | None,
    clock: Clock | None,
) -> DataProfile:
    run = _Run(
        limits or Limits(),
        privacy or Privacy(),
        on_progress or (lambda _event: None),
        cancel,
        clock=clock or SystemClock(),
    )
    base = Path(root).resolve() if root is not None else None
    targets, skipped = expand_paths(paths, base)
    if len(targets) > run.limits.max_files:
        raise TooManyFilesError.over(run.limits.max_files, len(targets))
    if sum(path.stat().st_size for path in targets) > run.limits.max_total_bytes:
        raise TooLargeError.together(run.limits.max_total_bytes)
    names = display_names(targets, base)
    run.emit(ProfileEvent(stage="start", message=f"{len(targets)} files"))
    ordered = sorted(targets, key=lambda p: (names[p].lower(), names[p]))
    reading_ahead = _read_ahead(ordered)
    try:
        files = []
        for number, path in enumerate(ordered, 1):
            if cancel is not None and cancel.is_set():
                raise DataError("profiling was cancelled")
            files.append(_profile_file(run, path, names[path], f"f{number}"))
    finally:
        release(run.isolate)  # a healthy worker serves the next run
        for thread in reading_ahead:
            thread.join(_READ_AHEAD_JOIN_S)
    from awa._internal.version import __version__

    run.emit(ProfileEvent(stage="done", message=f"{len(files)} files"))
    draft = DataProfile(
        awa_version=__version__,
        created=created or run.clock.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        limits=run.limits,
        privacy=run.privacy,
        files=files,
        flags=skipped,
    )
    return with_relations(draft)


def _read_ahead(paths: Sequence[Path]) -> list[threading.Thread]:
    """Open every file once in a few threads so slow first opens overlap."""

    count = min(_READ_AHEAD_THREADS, len(paths))
    if count < 2:
        return []

    def touch(batch: Sequence[Path]) -> None:
        for path in batch:
            with contextlib.suppress(OSError), path.open("rb") as handle:
                handle.read(_READ_AHEAD_BYTES)

    threads = [
        threading.Thread(
            target=touch, args=(paths[i::count],), daemon=True, name="awa-data-read-ahead"
        )
        for i in range(count)
    ]
    for thread in threads:
        thread.start()
    return threads


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _identify(
    path: Path, name: str, file_id: str, source: str | None, *, embedded: bool
) -> tuple[FileProfile, bool]:
    """The identity of a file (hash, size, kind) and whether its name contradicts its content.

    Never raises: an unreadable file comes back with status `error` and an empty hash.
    """

    shown = source if source is not None else str(path)
    size = 0
    try:
        stat = path.stat()
        size = stat.st_size
        detection = detect_kind(path)
        digest = file_sha256(path)
    except Exception as error:
        code, what = (
            ("DF-E-UNREADABLE", "the file cannot be read")
            if isinstance(error, OSError)
            else ("DF-E-PARSE-FAILED", "could not identify the file")
        )
        failed = FileProfile(
            id=file_id,
            path=name,
            source=shown,
            sha256="",
            size=size,
            kind="unknown",
            status="error",
        )
        failed.flags.append(make_flag(code, f"{what} ({type(error).__name__})", file_id))
        return failed, False
    file = FileProfile(
        id=file_id,
        path=name,
        source=shown,
        sha256=digest,
        size=size,
        kind=detection.kind,
        detected_by=detection.detected_by,
        mtime=None if embedded else _stamp(stat.st_mtime),
    )
    if detection.note:
        file.flags.append(make_flag("DF-W-DETECTION-LIMITED", detection.note, file_id))
    return file, detection.mismatch


def _profile_file(
    run: _Run,
    path: Path,
    name: str,
    file_id: str,
    *,
    depth: int = 0,
    source: str | None = None,
    parent: ParseContext | None = None,
) -> FileProfile:
    run.emit(ProfileEvent(file=file_id, stage="start", path=name))
    file, mismatch = _identify(path, name, file_id, source, embedded=depth > 0)
    run.emit(ProfileEvent(file=file_id, stage="detect", path=name, kind=file.kind))
    if file.status == "error":
        run.emit(ProfileEvent(file=file_id, stage="error", path=name, status="error", flags=1))
        return file
    numbers = itertools.count(1)
    with contextlib.ExitStack() as stack:

        def child(child_name: str, data: bytes) -> FileProfile:
            child_id = f"{file_id}.a{next(numbers)}"
            return _embedded(run, stack, child_id, child_name, data, depth, ctx)

        ctx = ParseContext(  # an embedded file shares the time and file budget of its top file
            run.limits,
            run.privacy,
            file_id,
            file,
            parent.deadline
            if parent is not None
            else run.clock.monotonic() + run.limits.parse_timeout_s,
            run.emit,
            child,
            run.clock,
            parent.embedded_budget if parent is not None else None,
        )
        file = _parse(run, path, mismatch, ctx)
    run.emit(
        ProfileEvent(
            file=file_id,
            stage="error" if file.status == "error" else "done",
            path=name,
            kind=file.kind,
            status=file.status,
            flags=len(file.flags),
        )
    )
    return file


def _parse(run: _Run, path: Path, mismatch: bool, ctx: ParseContext) -> FileProfile:
    file = ctx.profile
    if mismatch:
        ctx.flag("DF-W-TYPE-MISMATCH", f"the name of {file.path} does not match its content")
    if file.size > run.limits.max_file_bytes:
        return ctx.fail(
            "DF-E-TOO-LARGE", f"{file.size} bytes; the limit is {run.limits.max_file_bytes}"
        )
    parser = parser_for(file.kind)
    if parser is None:
        file.status = "unsupported"
        hint = "; unpack archives with ingest() first" if file.kind == "zip" else ""
        ctx.flag(
            "DF-W-UNSUPPORTED-KIND",
            f"awa cannot read {file.kind} files ({path.suffix or 'no extension'}){hint}",
        )
        return file
    if file.kind in ("xlsx", "docx"):
        try:
            check_archive(path, run.limits, file.path)
        except UnsafeArchiveError as error:
            return ctx.fail("DF-E-ZIP-UNSAFE", str(error))
    ctx.emit(ProfileEvent(file=file.id, stage="parse", path=file.path, kind=file.kind))
    identity = file.model_copy(deep=True)  # what a result is built from if the parser is abandoned
    try:
        return _call(run, parser, path, ctx)
    except ParseTimeout as error:
        run.isolate.close()  # an abandoned parser thread may still be using the worker
        return dataclasses.replace(ctx, profile=identity).fail("DF-E-TIMEOUT", str(error))
    except IsolateError as error:
        return ctx.fail("DF-E-PARSE-FAILED", f"could not read the file ({error})")
    except Exception as error:  # a parser bug or a hostile file must not fail the run
        return ctx.fail("DF-E-PARSE-FAILED", f"could not read the file ({type(error).__name__})")


def _call(run: _Run, parser: Parser, path: Path, ctx: ParseContext) -> FileProfile:
    """Run the parser in the worker process (isolated kinds) or in a thread with a hard time limit."""

    seconds = run.limits.parse_timeout_s
    name = f"{getattr(parser, '__module__', '')}:{getattr(parser, '__qualname__', '')}"
    if name in ISOLATED_PARSERS:
        return run.isolate.run(name, path, ctx.profile, run.limits, run.privacy, seconds)
    outcome: list[FileProfile | BaseException] = []

    def work() -> None:
        try:
            outcome.append(parser(path, ctx))
        except BaseException as error:  # handed to the caller below
            outcome.append(error)

    thread = threading.Thread(target=work, daemon=True, name="awa-data-parse")
    thread.start()
    thread.join(seconds + _HARD_TIMEOUT_GRACE_S)
    if not outcome:  # a parser that never checks its deadline: abandon its thread
        raise ParseTimeout(f"parsing took longer than {seconds:g} s")
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result


def _embedded(
    run: _Run,
    stack: contextlib.ExitStack,
    child_id: str,
    name: str,
    data: bytes,
    depth: int,
    parent: ParseContext,
) -> FileProfile:
    safe = safe_name(name)
    if depth >= _MAX_EMBEDDED_DEPTH:
        child = FileProfile(
            id=child_id,
            path=safe,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            kind="unknown",
            status="unsupported",
            flags=[make_flag("DF-W-NESTED-TOO-DEEP", "embedded files nest too deeply")],
        )
        return child
    folder = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="awa-data-")))
    target = folder / safe
    target.write_bytes(data)
    return _profile_file(
        run, target, safe, child_id, depth=depth + 1, source=f"#{safe}", parent=parent
    )
