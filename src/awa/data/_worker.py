"""The worker process of `awa.data._isolate`: one JSON job per stdin line, one answer per line,
with its memory capped (a Job Object on Windows, `RLIMIT_AS` elsewhere)."""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

from awa.data._model import FileProfile, Limits, Privacy
from awa.data._parse import ParseContext, ParseTimeout
from awa.runtime.clock import SystemClock

MEMORY_CAP_BYTES = 2 * 1024**3
"""What a reader process may allocate (2 GiB): room for a 100 MB file, not for a bomb."""


def cap_memory(limit: int = MEMORY_CAP_BYTES) -> bool:
    """Limit the address space of this process to `limit` bytes; True when the system accepted it."""

    try:
        if sys.platform == "win32":
            return _cap_memory_windows(limit)
        import resource

        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (limit if hard < 0 else min(limit, hard), hard))
        return True
    except Exception:  # a limit the system refuses must not stop the reader
        return False


def _cap_memory_windows(limit: int) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("r", "w", "o", "rb", "wb", "ob")]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("Basic", Basic),
            ("Io", Counters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    info = Extended()
    info.Basic.LimitFlags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.ProcessMemoryLimit = limit
    ok = kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    return bool(ok and kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()))


def _execute(job: dict[str, object]) -> dict[str, str]:
    """Run one parser and describe the outcome as plain text."""

    try:
        module, name = str(job["parser"]).split(":")
        parser = getattr(importlib.import_module(module), name)
        file = FileProfile.model_validate_json(str(job["file"]))
        seconds = float(str(job["seconds"]))
        clock = SystemClock()
        ctx = ParseContext(
            Limits.model_validate_json(str(job["limits"])),
            Privacy.model_validate_json(str(job["privacy"])),
            file.id,
            file,
            clock.monotonic() + seconds,
            lambda event: None,
            clock=clock,
        )
        result: FileProfile = parser(Path(str(job["path"])), ctx)
        return {"status": "ok", "file": result.model_dump_json()}
    except ParseTimeout as error:
        return {"status": "timeout", "message": str(error)}
    except Exception as error:
        return {"status": "error", "message": type(error).__name__}


def main() -> None:
    """Answer parse jobs from stdin, one line each, until stdin closes."""

    cap_memory()  # a runaway allocation fails in this process, not on the machine
    protocol = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)  # anything a parser prints can no longer corrupt the protocol
    protocol.write(b'{"status": "ready"}\n')  # the parent starts the file's clock after this
    for line in sys.stdin.buffer:
        job = json.loads(line)
        sys.path[:0] = [entry for entry in job["sys_path"] if entry not in sys.path]
        protocol.write(json.dumps(_execute(job)).encode("ascii") + b"\n")


if __name__ == "__main__":
    main()
