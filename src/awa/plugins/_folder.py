"""The project folder as the first plugin source, trusted like a blueprint's code; a file is
read again when its modification time or size changes."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

from awa._internal.upstream.tools import BaseTool
from awa.plugins._kinds import Entry, Kind, PluginError, _import
from awa.plugins._registry import project_source

_LOCK = threading.RLock()


class _Cache:
    """The entries read from each folder file, by file and modification signature."""

    def __init__(self) -> None:
        self.found: dict[str, tuple[tuple[int, int], list[Entry]]] = {}


_CACHE = _Cache()


def entries(project: Path, kind: Kind | None = None) -> list[Entry]:
    """The entries the folder of `project` provides for `kind`; imports its `.py` files."""

    root = Path(project).resolve()
    found: list[Entry] = []
    with _LOCK:
        for path, read in _files(root, kind):
            found += _cached(root, path, read)
    return found


def _files(root: Path, kind: Kind | None) -> list[tuple[Path, Any]]:
    files: list[tuple[Path, Any]] = []
    if kind in (None, "tools"):
        if (root / "tools.py").is_file():
            files.append((root / "tools.py", _tools_entries))
        files += [(p, _tools_entries) for p in sorted((root / "tools").glob("*.py")) if p.is_file()]
    if kind in (None, "connectors"):
        files += [
            (p, _connector_entries)
            for p in sorted((root / "connectors").glob("*.py"))
            if p.is_file()
        ]
    if kind in (None, "skills"):
        files += [(p, _skill_entries) for p in sorted((root / "skills").glob("*/SKILL.md"))]
    if (root / "plugins.py").is_file():
        files.append((root / "plugins.py", _plugins_entries))
    return files


def _cached(root: Path, path: Path, read: Any) -> list[Entry]:
    stat = path.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    key = str(path)
    cached = _CACHE.found.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    found: list[Entry] = read(root, path)
    _CACHE.found[key] = (signature, found)
    return found


def _module(root: Path, path: Path) -> ModuleType:
    digest = hashlib.sha1(str(path).encode()).hexdigest()[:10]  # noqa: S324 - a module name only
    name = f"_awa_project_{digest}_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"{path} cannot be imported")
    module = importlib.util.module_from_spec(spec)
    folder = str(root)
    added = folder not in sys.path
    if added:
        sys.path.insert(0, folder)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise PluginError(f"importing {path} failed: {type(exc).__name__}: {exc}") from exc
    finally:
        if added and folder in sys.path:
            sys.path.remove(folder)
    return module


def _entry(kind: Kind, name: str, path: Path, obj: Any, summary: str = "") -> Entry:
    return Entry(kind, name, "project", str(path), lambda: obj, summary=summary)


def _tools_entries(root: Path, path: Path) -> list[Entry]:
    module = _module(root, path)
    tools: dict[str, Any] = {
        value.name: value for value in vars(module).values() if isinstance(value, BaseTool)
    }
    for attribute in getattr(module, "__all__", ()):
        value = getattr(module, attribute, None)
        if not (inspect.isfunction(value) and inspect.getdoc(value)):
            continue
        if value.__name__ not in tools:
            tools[value.__name__] = value
    return [
        _entry("tools", name, path, tool, _first_line(getattr(tool, "description", "")))
        for name, tool in tools.items()
    ]


def _connector_entries(root: Path, path: Path) -> list[Entry]:
    module = _module(root, path)
    return [
        _entry("connectors", value.name, path, value, getattr(value, "summary", ""))
        for value in vars(module).values()
        if type(value).__name__ == "ConnectorSpec"
    ]


def _skill_entries(root: Path, path: Path) -> list[Entry]:
    folder = path.parent
    return [
        Entry(
            "skills",
            folder.name,
            "project",
            str(folder),
            lambda: _import("awa.skills:Skill").from_path(folder),
            summary="a skill of this project",
        )
    ]


def _plugins_entries(root: Path, path: Path) -> list[Entry]:
    sink: list[Entry] = []
    with project_source(sink):
        _module(root, path)
    return sink


def _first_line(text: str) -> str:
    return (text or "").strip().split("\n", 1)[0][:160]
