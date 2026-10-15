"""One `.env` loader for every awa command, and where each value comes from.

A value comes from the process environment, else the project `.env`, else the registry default.
The project folder is `--project` (or `AWA_PROJECT_DIR`), else the nearest folder with
`blueprint.yaml`, `[tool.awa]` or `.awa/`, searched up to the home folder or repository root.
Library use reads `.env` without changing `os.environ`; the command line exports known names
(never those on `DENYLIST`).
"""

from __future__ import annotations

import difflib
import os
import re
import sys
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TextIO
from urllib.parse import urlsplit, urlunsplit

from awa.core._envparse import parse_env_file
from awa.core.settings_registry import (
    DOTENV_TYPO_PREFIXES,
    PROJECT_ENV,
    REGISTRY,
    STRICT_ENV,
    TYPO_PREFIXES,
    DeclaredEnv,
    SettingSpec,
    declared_env,
    is_denied,
    is_secret_name,
    known_names,
    spec,
)
from awa.kernel.redaction import redact_secrets

Origin = Literal["env", "env_empty", "dotenv", "default"]
"""Where a value comes from: the process environment (set, or set but empty), `.env`, or the
registry default."""


_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class ValueSource:
    """Where a value comes from.

    Attributes:
        origin: `env`, `env_empty`, `dotenv` or `default`.
        path: The `.env` file, relative to the project folder or as `~/...` (for `dotenv`).
    """

    origin: Origin
    path: str | None = None
    line: int | None = None

    @property
    def label(self) -> str:
        """`env`, `env (empty)`, `.env:12` or `default`."""

        if self.origin == "dotenv":
            return f"{self.path}:{self.line}" if self.line else str(self.path)
        return "env (empty)" if self.origin == "env_empty" else self.origin


@dataclass(frozen=True)
class UnknownName:
    """An `AWA_*` (or provider) name no setting has: where it is and the closest known name."""

    name: str
    where: Literal["env", "dotenv"]
    suggestion: str | None = None

    def message(self) -> str:
        """`AWA_X is not a known setting (did you mean AWA_Y?)`."""

        place = " in .env" if self.where == "dotenv" else ""
        hint = f" (did you mean {self.suggestion}?)" if self.suggestion else ""
        return f"{self.name}{place} is not a known setting{hint}"


@dataclass
class LoadedEnvironment:
    """The project folder, its `.env` and what loading it did.

    Attributes:
        exported: Names this loader put into `os.environ` (command line only).
        shadowed: Names in `.env` that the process environment overrides.
        not_exported: Names in `.env` that were not exported (unknown or denied).
        declared: Names the project's blueprints declare in `*_env` keys.
    """

    project_dir: Path
    dotenv_path: Path | None
    exported: frozenset[str] = frozenset()
    shadowed: frozenset[str] = frozenset()
    not_exported: frozenset[str] = frozenset()
    unknown: list[UnknownName] = field(default_factory=list)
    declared: dict[str, DeclaredEnv] = field(default_factory=dict)
    skip_preflight: bool = False
    _values: dict[str, str] = field(default_factory=dict, repr=False)
    _lines: dict[str, int] = field(default_factory=dict, repr=False)
    _stamp: float | None = field(default=None, repr=False)
    _project_dotenv: bool = field(default=True, repr=False)

    def dotenv_file(self) -> Path | None:
        """The `.env` file now (also one created after loading), or `None`."""

        self._refresh()
        return self.dotenv_path

    def get(self, name: str) -> str | None:
        """The value of `name` (or an alias): process environment, then `.env`; no registry default."""

        names = _names(name)
        for candidate in names:
            if candidate in os.environ:
                return os.environ[candidate]
        self._refresh()
        for candidate in names:
            value = self._values.get(candidate)
            if value is not None and value.strip():
                return value
        return None

    def source_of(self, name: str) -> ValueSource:
        """Where the value of `name` comes from."""

        names = _names(name)
        for candidate in names:
            if candidate in os.environ and candidate not in self.exported:
                empty = not os.environ[candidate].strip()
                return ValueSource("env_empty" if empty else "env")
        self._refresh()
        for candidate in names:
            if self._values.get(candidate, "").strip():
                return ValueSource("dotenv", self._shown_path(), self._lines.get(candidate))
        return ValueSource("default")

    def _shown_path(self) -> str | None:
        if self.dotenv_path is None:
            return None
        return shown_path(self.dotenv_path, self.project_dir)

    def _refresh(self) -> None:
        """Re-read `.env` when it changed (library mode: values only, nothing exported)."""

        path = self.dotenv_path
        if path is None and self._project_dotenv:
            # A `.env` written after loading (`awa setup`, `awa env set`) counts from now on.
            path = _dotenv_path(self.project_dir, None)
            self.dotenv_path = path
        if path is None:
            return
        stamp = _mtime(path)
        if stamp != self._stamp:
            self._values, self._lines = parse_env_file(path)
            self._stamp = stamp


def shown_path(path: Path, project: Path) -> str:
    """`path` relative to `project`, or under the home folder as `~/...`, else as it is."""

    resolved = _resolve(path)
    for base, prefix in ((_resolve(project), ""), (_resolve(Path.home()), "~/")):
        try:
            return prefix + resolved.relative_to(base).as_posix()
        except ValueError:
            continue
    return str(path)


def _names(name: str) -> tuple[str, ...]:
    found = spec(name)
    if found is None:
        return (name,)
    return tuple(dict.fromkeys((name, found.name, *found.aliases)))


def _resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.absolute()


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(str(_resolve(a))) == os.path.normcase(str(_resolve(b)))


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _is_project(folder: Path) -> bool:
    if (folder / "blueprint.yaml").is_file() or (folder / ".awa").is_dir():
        return True
    pyproject = folder / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        return re.search(r"^\s*\[tool\.awa(?:\.|\])", text, re.MULTILINE) is not None
    return False


def find_project(start: str | Path | None = None, *, project: str | Path | None = None) -> Path:
    """The resolved project folder; `project` (`--project`) wins over the search from `start`."""

    explicit = project if project is not None else os.environ.get(PROJECT_ENV, "").strip()
    if explicit:
        return _resolve(Path(explicit))
    begin = _resolve(Path(start) if start is not None else Path.cwd())
    if begin.is_file() or (not begin.exists() and begin.suffix):
        begin = begin.parent
    home = _resolve(Path.home())
    if not _same(begin, home) and ((begin / ".env").is_file() or _is_project(begin)):
        return begin
    folder = begin
    while not _same(folder, home):
        if _is_project(folder):
            return folder
        if (folder / ".git").exists() or folder.parent == folder:
            break
        folder = folder.parent
    return begin


class _State:
    """What this process loaded: one object, so tests and `unload_environment` reset it whole."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.loaded: LoadedEnvironment | None = None  # set by an explicit `load_environment`
        self.key: tuple[object, ...] | None = None
        self.exported: dict[str, str] = {}  # what this loader put into os.environ
        self.implicit: dict[tuple[str, str | None], LoadedEnvironment] = {}
        self.warned: set[str] = set()
        self.skip_preflight = False


_STATE = _State()


def load_environment(
    start: str | Path | None = None,
    *,
    project: str | Path | None = None,
    dotenv: str | Path | None = None,
    export: bool = False,
    files: Iterable[str | Path] = (),
    allow: Iterable[str] = (),
) -> LoadedEnvironment:
    """Find the project, read its `.env` and (with `export`) export the names awa knows.

    Calling it again does nothing unless the `.env` changed. A load without `export` keeps the
    names an earlier load exported; `unload_environment` removes them. The result is also returned by
    `current()` from now on.

    Args:
        start: Where the project search starts (a blueprint file or a folder).
        dotenv: Another `.env` file instead of the project's.
        export: Export the allowed names the process environment lacks (the command line does).
        files: Further blueprint files whose `*_env` names may be exported.
        allow: More names that may be exported.

    Example:
        ```python
        loaded = load_environment()
        print(source_of("AWA_DEFAULT_MODEL").label)   # "env", ".env:3" or "default"
        ```
    """

    extra = frozenset(allow)
    with _STATE.lock:
        folder = find_project(start, project=project)
        path = _dotenv_path(folder, dotenv)
        declared = declared_env(folder, files=files)
        key = (
            os.path.normcase(str(folder)),
            os.path.normcase(str(_resolve(path))) if path is not None else None,
            _mtime(path) if path is not None else None,
            export,
            frozenset(declared),
            extra,
        )
        if _STATE.loaded is not None and key == _STATE.key:
            return _STATE.loaded
        if export:
            _unexport()
        values, lines = parse_env_file(path) if path is not None else ({}, {})
        shadowed = frozenset(name for name in values if name in os.environ)
        exported: frozenset[str] = frozenset()
        not_exported: frozenset[str] = frozenset()
        if export:
            allowed = known_names() | set(declared) | extra
            exported, not_exported = _export(values, allowed)
        loaded = LoadedEnvironment(
            project_dir=folder,
            dotenv_path=path,
            exported=exported,
            shadowed=shadowed,
            not_exported=not_exported,
            unknown=_unknown(values, declared),
            declared=declared,
            skip_preflight=_STATE.skip_preflight,
            _values=values,
            _lines=lines,
            _stamp=_mtime(path) if path is not None else None,
            _project_dotenv=dotenv is None,
        )
        _STATE.loaded = loaded
        _STATE.key = key
        _STATE.implicit.clear()
        return loaded


def _export(
    values: Mapping[str, str], allowed: frozenset[str]
) -> tuple[frozenset[str], frozenset[str]]:
    """Put the allowed, non-empty `.env` values the process lacks into `os.environ`.

    Returns the names exported and the names refused (unknown or denied).
    """

    exported: set[str] = set()
    refused: set[str] = set()
    for name, value in values.items():
        if name in os.environ:
            continue
        if is_denied(name) or name not in allowed:
            refused.add(name)
        elif value.strip():
            os.environ[name] = value
            _STATE.exported[name] = value
            exported.add(name)
    return frozenset(exported), frozenset(refused)


def _dotenv_path(folder: Path, dotenv: str | Path | None) -> Path | None:
    if dotenv is not None:
        return Path(dotenv)
    if _same(folder, Path.home()):
        return None  # the home folder's .env belongs to no project
    candidate = folder / ".env"
    return candidate if candidate.is_file() else None


def _unexport() -> None:
    for name, value in list(_STATE.exported.items()):
        if os.environ.get(name) == value:
            os.environ.pop(name, None)
    _STATE.exported.clear()


def unload_environment() -> None:
    """Forget the loaded environment and remove the names it exported from `os.environ`."""

    with _STATE.lock:
        _unexport()
        _STATE.loaded = None
        _STATE.key = None
        _STATE.implicit.clear()
        _STATE.warned.clear()
        _STATE.skip_preflight = False


def current() -> LoadedEnvironment:
    """The environment `load_environment` loaded, else the one of the working directory
    (read, never exported)."""

    with _STATE.lock:
        if _STATE.loaded is not None:
            return _STATE.loaded
        folder = find_project()
        path = _dotenv_path(folder, None)
        key = (os.path.normcase(str(folder)), str(path) if path else None)
        loaded = _STATE.implicit.get(key)
        if loaded is None:
            loaded = LoadedEnvironment(project_dir=folder, dotenv_path=path)
            _STATE.implicit.clear()
            _STATE.implicit[key] = loaded
        return loaded


def env_value(name: str) -> str | None:
    """The value of `name`: the process environment, else the project `.env`, else `None`."""

    return current().get(name)


def source_of(name: str) -> ValueSource:
    """Where the value of `name` comes from (see `ValueSource`)."""

    return current().source_of(name)


def set_skip_preflight(skipped: bool) -> None:
    """Record that `--skip-preflight` was given, for `awa doctor` and `/health`."""

    with _STATE.lock:
        _STATE.skip_preflight = skipped
        if _STATE.loaded is not None:
            _STATE.loaded.skip_preflight = skipped


def preflight_skipped() -> bool:
    """Whether `--skip-preflight` was given in this process."""

    return _STATE.skip_preflight


def strict() -> bool:
    """Whether `AWA_STRICT_SETTINGS` is on."""

    return (env_value(STRICT_ENV) or "").strip().lower() in _TRUE


def _unknown(values: Mapping[str, str], declared: Mapping[str, DeclaredEnv]) -> list[UnknownName]:
    known = known_names() | set(declared)
    found: list[UnknownName] = []
    seen: set[str] = set()
    candidates = [(name, "env") for name in os.environ if name.startswith(TYPO_PREFIXES)]
    candidates += [(name, "dotenv") for name in values if name.startswith(DOTENV_TYPO_PREFIXES)]
    for name, where in candidates:
        if name in known or name in seen:
            continue
        seen.add(name)
        match = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.8)
        found.append(UnknownName(name, where, match[0] if match else None))  # type: ignore[arg-type]  # where is a literal
    return sorted(found, key=lambda item: item.name)


def warn_unknown(loaded: LoadedEnvironment, stream: TextIO | None = None) -> list[str]:
    """Print one warning line per unknown name, once per process; return the lines printed."""

    out = sys.stderr if stream is None else stream
    printed = []
    for item in loaded.unknown:
        if item.name in _STATE.warned:
            continue
        _STATE.warned.add(item.name)
        line = f"warning: {item.message()}"
        print(line, file=out)
        printed.append(line)
    return printed


def mask(name: str, value: str | None) -> str:
    """A value as it may be shown: `set`/`not set` for secrets, URLs without credentials or query."""

    if value is None or not value.strip():
        return "not set"
    found = spec(name)
    secret = found.is_secret if found is not None else is_secret_name(name)
    if secret or name in current().declared:
        return "set"
    return redact_secrets(_mask_userinfo(value))


def _mask_userinfo(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.scheme or not parts.netloc:
        return value
    host = parts.netloc.rsplit("@", 1)[1] if "@" in parts.netloc else parts.netloc
    netloc = f"***@{host}" if "@" in parts.netloc else host
    query = "***" if parts.query else ""
    return urlunsplit((parts.scheme, netloc, parts.path, query, ""))


def registry() -> Mapping[str, SettingSpec]:
    """Every setting awa knows, by name (`awa.core.settings_registry.REGISTRY`)."""

    return REGISTRY


__all__ = [
    "PROJECT_ENV",
    "STRICT_ENV",
    "LoadedEnvironment",
    "Origin",
    "UnknownName",
    "ValueSource",
    "current",
    "env_value",
    "find_project",
    "load_environment",
    "mask",
    "preflight_skipped",
    "registry",
    "set_skip_preflight",
    "shown_path",
    "source_of",
    "strict",
    "unload_environment",
    "warn_unknown",
]
