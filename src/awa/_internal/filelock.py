"""A cross-process lock file next to a file that several writers update."""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

_LOCK_SECONDS = 10.0


@contextlib.contextmanager
def locked_file(file: Path) -> Iterator[None]:
    """Hold `.<name>.lock` next to `file`, so two writers never lose each other's change."""

    lock = file.with_name(f".{file.name}.lock")
    deadline = time.monotonic() + _LOCK_SECONDS
    while True:
        try:
            os.close(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            break
        except (FileExistsError, PermissionError) as exc:
            # Windows refuses (PermissionError) a lock file that another writer is still deleting.
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > _LOCK_SECONDS:
                    lock.unlink()  # left by a writer that died
                    continue
            if time.monotonic() > deadline:
                if isinstance(exc, PermissionError):
                    raise
                raise OSError(f"{file} is being written by another awa process") from None
            time.sleep(0.05)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink()
