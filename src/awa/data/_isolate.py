"""Run a parser in a pooled, killable `python -m awa.data._worker` process with a memory cap,
talking JSON lines over stdin and stdout."""

from __future__ import annotations

import atexit
import contextlib
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO

from awa.data._model import FileProfile, Limits, Privacy
from awa.data._parse import ParseTimeout

ISOLATED_PARSERS: set[str] = {
    "awa.data._pdf:profile_pdf",
    "awa.data._excel:profile_xlsx",
    "awa.data._docx:profile_docx",
}
"""Parsers (`module:function`) that run in a worker process instead of the calling thread."""

_GRACE_S = 1.0
_START_TIMEOUT_S = 60.0
_WORKER_ARGS = ("-m", "awa.data._worker")
"""How the worker is started (a module constant so a test can slow the start-up down)."""
_POOL_SIZE = 2
"""Idle workers kept for the next run (more parallel runs simply start their own)."""


class IsolateError(Exception):
    """The worker failed: its message is the exception type or a short reason (no file content)."""


class Isolate:
    """A reusable worker process for isolated parsers; start-up happens on the first `run`."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._command: tuple[str, ...] = ()

    @property
    def alive(self) -> bool:
        """The worker runs and was started the way a new one would be started now."""

        return (
            self._process is not None
            and self._process.poll() is None
            and self._command == (sys.executable, *_WORKER_ARGS)
        )

    def run(
        self,
        parser: str,
        path: Path,
        file: FileProfile,
        limits: Limits,
        privacy: Privacy,
        seconds: float,
    ) -> FileProfile:
        """Run `parser` (`module:function`) on `path` in the worker and return its profile.

        Raises:
            ParseTimeout: The parser did not answer within `seconds` (the worker is killed).
            IsolateError: The parser failed or the worker died.
        """

        job = {
            "parser": parser,
            "path": str(path),
            "file": file.model_dump_json(),
            "limits": limits.model_dump_json(),
            "privacy": privacy.model_dump_json(),
            "seconds": seconds,
            "sys_path": sys.path,
        }
        process = self._worker()
        assert process.stdin is not None
        try:  # the time limit starts here: the start-up of a new worker is not the file's time
            process.stdin.write(json.dumps(job).encode("ascii") + b"\n")
            process.stdin.flush()
            reply = self._lines.get(timeout=seconds + _GRACE_S)
        except queue.Empty:
            self.close()
            raise ParseTimeout(f"parsing took longer than {seconds:g} s") from None
        except OSError:
            self.close()
            raise IsolateError("the reader process is not running") from None
        if reply is None:
            self.close()
            raise IsolateError("the reader process stopped unexpectedly")
        answer = json.loads(reply)
        if answer["status"] == "ok":
            return FileProfile.model_validate_json(answer["file"])
        if answer["status"] == "timeout":
            raise ParseTimeout(answer["message"])
        raise IsolateError(answer["message"])

    def close(self) -> None:
        """Stop the worker (killed at once; a new one starts on the next `run`)."""

        process, self._process = self._process, None
        if process is not None:
            with contextlib.suppress(OSError):
                process.kill()
            process.wait()
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()

    def _worker(self) -> subprocess.Popen[bytes]:
        if self.alive:
            assert self._process is not None
            return self._process
        self.close()
        self._lines = queue.Queue()
        self._command = (sys.executable, *_WORKER_ARGS)
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen(
            list(self._command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        assert process.stdout is not None
        threading.Thread(
            target=_forward, args=(process.stdout, self._lines), daemon=True, name="awa-data-reader"
        ).start()
        self._process = process
        try:
            ready = self._lines.get(timeout=_START_TIMEOUT_S)
        except queue.Empty:
            self.close()
            raise IsolateError("the reader process did not start") from None
        if not _is_ready(ready):
            self.close()
            raise IsolateError("the reader process could not start")
        return process


def _is_ready(line: bytes | None) -> bool:
    try:
        return bool(json.loads(line or b"null").get("status") == "ready")
    except (ValueError, AttributeError):
        return False


class _Pool:
    """Idle healthy workers between runs; stopped when the program exits."""

    def __init__(self) -> None:
        self.idle: list[Isolate] = []
        self.lock = threading.Lock()
        self.registered = False

    def close(self) -> None:
        with self.lock:
            idle, self.idle = self.idle, []
        for isolate in idle:
            isolate.close()


_POOL = _Pool()


def lease() -> Isolate:
    """An idle pooled worker, or a new one that starts on its first `run`."""

    with _POOL.lock:
        while _POOL.idle:
            isolate = _POOL.idle.pop()
            if isolate.alive:
                return isolate
            isolate.close()
    return Isolate()


def release(isolate: Isolate) -> None:
    """Give a worker back after a run: kept when healthy and the pool has room, else stopped."""

    with _POOL.lock:
        if isolate.alive and len(_POOL.idle) < _POOL_SIZE:
            if not _POOL.registered:  # on first use, not at import
                atexit.register(_POOL.close)
                _POOL.registered = True
            _POOL.idle.append(isolate)
            return
    isolate.close()


def _forward(stream: IO[bytes], lines: queue.Queue[bytes | None]) -> None:
    for line in stream:
        lines.put(line)
    lines.put(None)
