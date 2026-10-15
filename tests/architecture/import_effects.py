"""Import every awa module under a PEP 578 audit hook and print the effects as one JSON line.

Run by the guard GT6 (`measures.import_effects`) in a child process: an import must define names
only (rule R19). Detected: write-mode `open`, file removal and creation, sockets, process and
thread start, and `atexit` registrations (counted, as `atexit` raises no audit event). Reading the
environment raises no audit event: GT10 covers it by reading the source. A module whose optional
extra is missing cannot be imported and is skipped.

    PYTHONPATH=src python tests/architecture/import_effects.py
"""

from __future__ import annotations

import atexit
import importlib
import json
import os
import sys
import warnings

EVENTS = {
    "os.remove", "os.rename", "os.mkdir", "os.rmdir", "subprocess.Popen", "os.system",
    "_thread.start_joinable_thread", "_thread.start_new_thread", "socket.connect",
    "socket.getaddrinfo", "socket.bind", "_winapi.CreateProcess",
}  # fmt: skip

current = ["<startup>"]
found: dict[str, list[str]] = {}


def hook(event: str, args: tuple[object, ...]) -> None:
    if event == "open":
        mode = args[1] if len(args) > 1 and isinstance(args[1], str) else ""
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        write = any(c in mode for c in "wax+") or bool(
            flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
        )
        path = str(args[0])
        if write and not isinstance(args[0], int) and "__pycache__" not in path:
            found.setdefault(current[0], []).append(f"open-write {path}")
    elif event in EVENTS and "__pycache__" not in str(args):
        found.setdefault(current[0], []).append(f"{event} {str(args)[:80]}")


def module_names(root: str) -> list[str]:
    names = ["awa"]
    # Walk the file system: walking packages (`pkgutil`) would import them.
    for directory, _folders, files in os.walk(root):
        if "__pycache__" in directory:
            continue
        package = os.path.relpath(directory, os.path.dirname(root)).replace(os.sep, ".")
        names += [
            package if f == "__init__.py" else f"{package}.{f[:-3]}"
            for f in files
            if f.endswith(".py")
        ]
    return sorted(set(names))


def main() -> None:
    sys.addaudithook(hook)
    import awa  # the facade first, as a user imports it

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # The upstream seam goes first: importing LangChain registers hooks and binds a socket once,
        # and the measure's allowed list names the seam module that pays for it.
        names = module_names(os.path.dirname(awa.__file__))
        names.sort(key=lambda name: name != "awa._internal.upstream.agents")
        for name in names:
            if name.endswith("__main__"):
                continue
            current[0] = name
            before = atexit._ncallbacks()  # type: ignore[attr-defined]
            try:
                importlib.import_module(name)
            except Exception:  # noqa: BLE001 - an optional extra that is not installed
                continue
            if (added := atexit._ncallbacks() - before) > 0:  # type: ignore[attr-defined]
                found.setdefault(name, []).append(f"atexit.register x{added}")
    print(json.dumps(found))


if __name__ == "__main__":
    main()
