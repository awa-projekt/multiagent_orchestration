"""Part switches: which built-in parts of awa are on or off, and the one question the runtime asks.

A part `KIND.NAME` (`parser.csv`, `guard.sql_guard`) is decided by the first layer that names
it: Python in this process (`override`, replacement registrations), the blueprint's
`components:` section, else on. It imports nothing above `awa.core`.
"""

from __future__ import annotations

import contextlib
import difflib
import os
import re
import threading
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

from awa._internal.util import bound
from awa.errors import ConfigurationError

Prefix = Literal["parser", "connector", "tool", "toolkit", "skill", "guard", "cache", "tracer"]
PREFIXES: tuple[Prefix, ...] = get_args(Prefix)
"""The prefixes of a part key, in listing order."""

PARTS: dict[str, str] = {
    "guard.redaction": "Masks personal data (email, IBAN, card, phone, IP address) in what awa "
    "sends to Langfuse and OpenTelemetry; credentials are always masked, also when it is off.",
    "guard.approvals": "Makes tool calls that need a person's approval wait for one.",
    "guard.sql_guard": "Refuses SQL that could change data before sql_query sends it (the "
    "database's own read-only rights always apply).",
    "guard.upload_checks": "Refuses uploaded files whose name and content disagree, or whose kind "
    "is not accepted (size and archive limits always apply).",
    "cache.prompt": "Provider prompt caching (Claude cache marks, OpenAI and Azure cache keys).",
    "cache.tools": "Reuse of read-only tool results.",
    "cache.steps": "Reuse of deterministic process steps while their files are unchanged.",
    "tracer.langfuse": "Langfuse tracing of the awa commands (sends only with "
    "AWA_LANGFUSE_TRACING=true and the LANGFUSE_* keys).",
    "tracer.otel": "OpenTelemetry export of runs (when your code adds an OpenTelemetrySubscriber).",
}
"""The parts that are only switches (no object behind them), with what each does."""

SECURITY: frozenset[str] = frozenset(key for key in PARTS if key.startswith("guard."))
"""The security parts: switching one off is reported as a governance finding."""

State = Literal["on", "off", "replaced"]
SetBy = Literal["default", "blueprint", "python"]

_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_LOCK = threading.Lock()


class _Memo:
    """The blueprint files already read, by file and modification signature."""

    def __init__(self) -> None:
        self.blueprints: dict[str, tuple[tuple[int, int], dict[str, Entry], list[str]]] = {}


_MEMO = _Memo()


class ComponentError(ConfigurationError):
    """A part name or switch that cannot be used; the message says the fix."""


@dataclass(frozen=True)
class Entry:
    """What one layer says about one part.

    Attributes:
        state: `on`, `off`, or `None` when only `use` is given.
        use: `python:<name>` for an object registered in Python in this process.
    """

    state: Literal["on", "off"] | None = None
    use: str | None = None


@dataclass(frozen=True)
class Choice:
    """The switch of one part as the runtime sees it.

    Attributes:
        state: `on`, `off` or `replaced`.
        entry: The deciding layer's entry (`None` for the default).
    """

    key: str
    state: State
    set_by: SetBy
    entry: Entry | None = None

    @property
    def is_off(self) -> bool:
        """Whether the part is switched off."""

        return self.state == "off"


def _suggest(word: str, candidates: Any) -> str:
    match = difflib.get_close_matches(word, list(candidates), n=1)
    return f" (did you mean {match[0]!r}?)" if match else ""


def split_key(key: str) -> tuple[Prefix, str]:
    """`"parser.csv"` as `("parser", "csv")`.

    Raises:
        ComponentError: If the prefix is unknown or the name is malformed.
    """

    prefix, dot, name = str(key).strip().partition(".")
    if prefix not in PREFIXES:
        raise ComponentError(
            f"unknown part kind {prefix!r} in {key!r}{_suggest(prefix, PREFIXES)} "
            f"(kinds: {', '.join(PREFIXES)}); name a part as KIND.NAME, for example parser.csv"
        )
    if not dot or not _NAME.fullmatch(name):
        raise ComponentError(
            f"invalid part name {key!r}: use KIND.NAME with a lower-case name of letters, "
            "digits, '_', '-' or '.', for example tool.web_search"
        )
    return prefix, name


def parse_section(data: Any, *, where: str) -> tuple[dict[str, Entry], list[str]]:
    """Parse a blueprint's `components:` into valid entries by `KIND.NAME` and one message per problem."""

    entries: dict[str, Entry] = {}
    problems: list[str] = []
    if not isinstance(data, Mapping):
        return {}, [f"{where}: components must be a mapping of kinds"]
    for kind, names in data.items():
        if kind not in PREFIXES:
            problems.append(f"unknown kind {kind!r}{_suggest(str(kind), PREFIXES)}")
        elif names is not None and not isinstance(names, Mapping):
            problems.append(f"{kind}: must be a mapping of names")
        else:
            for name, raw in (names or {}).items():
                key = f"{kind}.{name}"
                if not isinstance(name, str) or not _NAME.fullmatch(name):
                    problems.append(f"{key}: invalid name")
                elif isinstance(raw, bool) or raw in ("on", "off"):
                    entries[key] = Entry(state="on" if raw in (True, "on") else "off")
                else:
                    problems.append(
                        f"{key}: must be on or off (to replace a part, register your own "
                        "object with replace=True in plugins.py)"
                    )
    return entries, problems


_BLUEPRINT: ContextVar[dict[str, Entry] | None] = ContextVar(
    "awa_components_blueprint", default=None
)


@contextlib.contextmanager
def using_blueprint(entries: Mapping[str, Entry]) -> Iterator[None]:
    """Use `entries` as the blueprint layer inside the block (the blueprint being run)."""

    with bound(_BLUEPRINT, dict(entries)):
        yield


def blueprint_entries(project: Path) -> tuple[dict[str, Entry], list[str]]:
    """The `components:` section of the project's `blueprint.yaml`, and its problems."""

    path = project / "blueprint.yaml"
    try:
        stat = path.stat()
    except OSError:
        return {}, []
    signature = (stat.st_mtime_ns, stat.st_size)
    key = os.path.normcase(str(path))
    with _LOCK:
        cached = _MEMO.blueprints.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1], cached[2]
    from awa._internal.serialize import load_yaml

    try:
        data = load_yaml(path.read_bytes(), what="blueprint")
    except (OSError, ValueError):
        data = None  # the blueprint loader reports a broken file
    section = data.get("components") if isinstance(data, dict) else None
    entries, problems = (
        parse_section(section, where=f"{path} components") if section is not None else ({}, [])
    )
    with _LOCK:
        _MEMO.blueprints[key] = (signature, entries, problems)
    return entries, problems


class _PythonLayer:
    """Switches made in this process (registrations and `override`), with registered objects."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.entries: dict[str, Entry] = {}
        self.objects: dict[str, Any] = {}


_PYTHON = _PythonLayer()


def set_python(key: str, entry: Entry | None, obj: Any = None) -> None:
    """Record a switch made in Python for `key` (`None` removes it)."""

    split_key(key)
    with _PYTHON.lock:
        if entry is None:
            _PYTHON.entries.pop(key, None)
        else:
            _PYTHON.entries[key] = entry
        if entry is not None and obj is not None:
            _PYTHON.objects[key] = obj
        else:
            _PYTHON.objects.pop(key, None)


def python_entries() -> dict[str, Entry]:
    """The switches made in Python in this process."""

    with _PYTHON.lock:
        return dict(_PYTHON.entries)


def python_object(key: str) -> Any:
    """The object registered in Python for `key`, or `None`."""

    with _PYTHON.lock:
        return _PYTHON.objects.get(key)


@contextlib.contextmanager
def override(changes: Mapping[str, str | bool]) -> Iterator[None]:
    """Switch parts on or off in this process for the `with` block, then restore only those parts.

    Args:
        changes: `KIND.NAME` to `"on"`/`"off"` (or `True`/`False`).

    Raises:
        ComponentError: A name is malformed or a state is neither on nor off.

    Example:
        ```python
        with override({"parser.csv": "off"}):
            profile("orders.csv")   # the CSV file is reported as unsupported
        ```
    """

    planned: dict[str, Entry] = {}
    for key, value in changes.items():
        name = ".".join(map(str, split_key(key)))
        state = ("on" if value else "off") if isinstance(value, bool) else value
        if state not in ("on", "off"):
            raise ComponentError(f"{key}: the state must be on or off, not {value!r}")
        planned[name] = Entry(state="on" if state == "on" else "off")
    with _PYTHON.lock:
        saved = {key: (_PYTHON.entries.get(key), _PYTHON.objects.get(key)) for key in planned}
        for key, entry in planned.items():
            set_python(key, entry)
    try:
        yield
    finally:
        for key, (before, obj) in saved.items():
            set_python(key, before, obj)


def layers(project: Path | None = None) -> list[tuple[SetBy, dict[str, Entry]]]:
    """The layers, highest first: Python, blueprint."""

    from awa.core.config import current

    blueprint = _BLUEPRINT.get()
    if blueprint is None:
        blueprint = blueprint_entries(project if project is not None else current().project_dir)[0]
    return [("python", python_entries()), ("blueprint", blueprint)]


def choice(
    key: str,
    *,
    project: Path | None = None,
    found: list[tuple[SetBy, dict[str, Entry]]] | None = None,
) -> Choice:
    """The switch of `key`: the first layer that says something about it, else the default.

    `found` passes the `layers` already read, to decide several keys from one read.
    """

    for set_by, entries in found if found is not None else layers(project):
        entry = entries.get(key)
        if entry is not None:
            if entry.state == "off":
                return Choice(key, "off", set_by, entry)
            return Choice(key, "replaced" if entry.use else "on", set_by, entry)
    return Choice(key, "on", "default")


def is_off(key: str, *, project: Path | None = None) -> bool:
    """Whether `key` is switched off."""

    return choice(key, project=project).is_off


def security_off(project: Path | None = None) -> list[str]:
    """The security parts that are switched off or replaced, sorted (for doctor, `/health`, runs)."""

    read = layers(project)
    found = []
    for key in sorted(SECURITY):
        picked = choice(key, project=project, found=read)
        if picked.state != "on":
            found.append(key if picked.state == "off" else f"{key} (replaced)")
    return found


def in_effect(project: Path | None = None) -> dict[str, list[str]]:
    """The switches that differ from the defaults: `{"disabled", "replaced", "security_off"}`, sorted."""

    read = layers(project)
    picks = [
        choice(key, project=project, found=read)
        for key in sorted({key for _, entries in read for key in entries})
    ]
    off = [p.key for p in picks if p.state == "off"]
    replaced = {p.key for p in picks if p.state == "replaced"}
    return {"disabled": off, "replaced": sorted(replaced), "security_off": security_off(project)}


def replacement(picked: Choice) -> Any:
    """The object a `replaced` choice registered in Python, or `None` (the built-in stays)."""

    if picked.state != "replaced" or picked.set_by != "python":
        return None
    return python_object(picked.key)


__all__ = [
    "PARTS",
    "PREFIXES",
    "SECURITY",
    "Choice",
    "ComponentError",
    "Entry",
    "Prefix",
    "SetBy",
    "State",
    "blueprint_entries",
    "choice",
    "in_effect",
    "is_off",
    "layers",
    "override",
    "parse_section",
    "python_entries",
    "python_object",
    "replacement",
    "security_off",
    "set_python",
    "split_key",
    "using_blueprint",
]
