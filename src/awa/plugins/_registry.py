"""The registry: entries of every kind from three sources (`project` > `plugin` > `builtin`)."""

from __future__ import annotations

import difflib
import threading
import warnings
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awa._internal.util import bound, name_taken
from awa.plugins._kinds import (
    KINDS,
    PLUGIN_PREFIX,
    PROVIDERS,
    SOURCE_ORDER,
    Entry,
    Kind,
    PluginError,
    Source,
    _import,
    normalize_dist,
    scan,
)

_SOURCE: ContextVar[Source] = ContextVar("awa_plugin_source", default="builtin")
_SINK: ContextVar[list[Entry] | None] = ContextVar("awa_plugin_sink", default=None)
Key = tuple[Kind, str, Source, str]


def _constant(obj: Any) -> Callable[[], Any]:
    return lambda: obj


def _describe(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    module = getattr(obj, "__module__", None)
    name = getattr(obj, "__qualname__", None) or type(obj).__name__
    return f"{module}:{name}" if module else str(name)


def current_project() -> Path:
    """The folder of the current project (where `plugins.py`, `tools.py` and the like live)."""

    from awa.core.config import current

    return current().project_dir


@contextmanager
def project_source(sink: list[Entry]) -> Iterator[None]:
    """Entries registered inside the block belong to the project and are collected in `sink`."""

    with bound(_SOURCE, "project"), bound(_SINK, sink):
        yield


@dataclass(frozen=True)
class PluginInfo:
    """One row of the listing; building it imports nothing.

    Attributes:
        kind: The kind (or the part family `guard`, `cache`, `tracer` for switch-only parts).
        status: `active`, `shadowed`, `replaced`, `clash` or `shadowed (replace unknown until load)`.
        key: Its switch key (`tool.name`) when it can be switched in `components:`.
        enabled: False when the blueprint's `components:` switches it off.
        reference: The `plugin:<dist>/<name>` reference of an installed tool or skill.
        security: A security part (`guard.*`): switching it off raises a governance finding.
    """

    kind: str
    name: str
    source: str
    target: str = ""
    summary: str = ""
    status: str = "active"
    dist: str | None = None
    version: str | None = None
    key: str | None = None
    enabled: bool = True
    reference: str | None = None
    security: bool = False


class Registry:
    """Entries by kind, built-ins filled in once per kind, plugins read from metadata."""

    def __init__(self, *, providers: Mapping[Kind, Sequence[str]] | None = None) -> None:
        self._lock = threading.Lock()
        self._entries: dict[Key, Entry] = {}
        self._builtins: dict[Kind, tuple[Entry, ...]] = {}
        self._providers = PROVIDERS if providers is None else providers
        self._boot_lock = threading.RLock()
        self._ready: set[Kind] = set()

    def _boot(self, kind: Kind) -> None:
        # Re-entrant: a provider that registers while its kind loads finds the kind ready;
        # another thread waits on the lock until loading is done.
        with self._boot_lock:
            if kind in self._ready:
                return
            self._ready.add(kind)
            try:
                found: list[Entry] = []
                with bound(_SOURCE, "builtin"):
                    for provider in self._providers.get(kind, ()):
                        try:
                            rows = _import(provider)()
                        except Exception as exc:
                            raise PluginError(
                                f"built-in provider {provider} failed: {exc}"
                            ) from exc
                        found += [
                            Entry(kind, n, "builtin", provider, _constant(o), summary=s)
                            for n, o, s in rows
                        ]
                shipped = tuple(found)
                found += scan(KINDS[kind])
            except BaseException:
                self._ready.discard(kind)
                raise
            with self._lock:
                self._builtins[kind] = shipped
                for entry in found:
                    key = (kind, entry.name, entry.source, entry.dist or "")
                    self._entries.setdefault(key, entry)

    def register(
        self, kind: Kind, name: str, obj: Any, *, replace: bool = False, summary: str = ""
    ) -> Any:
        """Add an entry; `obj` may be a `module:attribute` string (imported at load).

        Raises:
            PluginError: If the kind is unknown, or the name is taken and `replace` is false.
        """

        if kind not in KINDS:
            raise PluginError(f"unknown plugin kind {kind!r}; the kinds are {', '.join(KINDS)}")
        source = _SOURCE.get()
        entry = Entry(
            kind,
            name,
            source,
            _describe(obj),
            (lambda: _import(obj)) if isinstance(obj, str) else _constant(obj),
            replace,
            summary=summary,
        )
        taken = name_taken(KINDS[kind].noun, name)
        if (sink := _SINK.get()) is not None:
            if any(e.kind == kind and e.name == name for e in sink) and not replace:
                raise PluginError(taken)
            sink.append(entry)
            return obj
        self._boot(kind)
        key = (kind, name, source, "")
        with self._lock:
            if key in self._entries and not replace:
                raise PluginError(taken)
            self._entries[key] = entry
        return obj

    def unregister(self, kind: Kind, name: str) -> None:
        """Remove every entry named `name` of `kind`.

        Raises:
            PluginError: If there is none.
        """

        self._boot(kind)
        with self._lock:
            keys = [k for k in self._entries if k[0] == kind and k[1] == name]
            if not keys:
                raise PluginError(f"no {KINDS[kind].noun} named {name!r} is registered")
            for key in keys:
                del self._entries[key]

    def entries(self, kind: Kind, *, project: Path | None = None) -> list[Entry]:
        """Every entry of `kind`, the project folder's included, without loading any."""

        self._boot(kind)
        with self._lock:
            found = [e for k, e in self._entries.items() if k[0] == kind]
        if project is not None:
            from awa.plugins import _folder

            found += [e for e in _folder.entries(project, kind) if e.kind == kind]
        return found

    def builtins(self, kind: Kind) -> tuple[Entry, ...]:
        """The entries the built-in providers of `kind` returned, in provider order.

        `register` and `unregister` never change them, so they name what awa ships even after a
        registration replaced one of them.
        """

        self._boot(kind)
        with self._lock:
            return self._builtins.get(kind, ())

    def names(self, kind: Kind, *, project: Path | None = None) -> list[str]:
        """The sorted names of `kind`."""

        return sorted({e.name for e in self.entries(kind, project=project)})

    def resolve(
        self, kind: Kind, name: str, *, project: Path | None = None, dist: str | None = None
    ) -> list[Entry]:
        """The entries named `name`, highest source first.

        Raises:
            PluginError: If there is none, or two installed packages declare it.
        """

        noun = KINDS[kind].noun
        wanted = normalize_dist(dist) if dist is not None else None
        found = [
            e
            for e in self.entries(kind, project=project)
            if e.name == name and (wanted is None or normalize_dist(e.dist or "") == wanted)
        ]
        if not found:
            close = difflib.get_close_matches(name, self.names(kind, project=project), n=1)
            hint = f"; did you mean {close[0]!r}?" if close else ""
            raise PluginError(
                f"no {noun} named {name!r}{hint} (see `awa plugins list --kind {kind}`)"
            )
        dists = sorted({e.dist or "" for e in found if e.source == "plugin"})
        if len(dists) > 1:
            raise PluginError(
                f"two installed packages declare the {noun} {name!r} ({', '.join(dists)}); "
                "uninstall one"
            )
        return sorted(found, key=lambda e: SOURCE_ORDER.index(e.source))

    def load(
        self, kind: Kind, name: str, *, project: Path | None = None, dist: str | None = None
    ) -> Any:
        """Load the entry that is in use under the resolution rules."""

        found = self.resolve(kind, name, project=project, dist=dist)
        replaced = self._replacing(found[:-1])
        return replaced if replaced is not None else found[-1].load()

    @staticmethod
    def _replacing(higher: list[Entry]) -> Any:
        """The first of the higher entries that says `replace` (or its object does), loaded."""

        for entry in higher:
            obj = entry.load()
            if entry.replace or getattr(obj, "__awa_replace__", False):
                return obj
        return None

    def replacement(self, kind: Kind, name: str, *, project: Path | None = None) -> Any:
        """The object of an entry that says `replace` for the built-in `name`, or `None`."""

        try:
            found = self.resolve(kind, name, project=project)
        except PluginError:
            return None
        return self._replacing(found[:-1])

    def loadable(self, kind: Kind, *, project: Path | None = None) -> dict[str, Any]:
        """Every name of `kind` loaded; a broken or clashing one is skipped with a warning."""

        loaded: dict[str, Any] = {}
        noun = KINDS[kind].noun
        for name in self.names(kind, project=project):
            try:
                loaded[name] = self.load(kind, name, project=project)
            except PluginError as exc:
                warnings.warn(f"{noun} {name!r} is not used: {exc}", RuntimeWarning, stacklevel=2)
        return loaded

    def infos(self, kind: Kind | None = None, *, project: Path | None = None) -> list[PluginInfo]:
        """The listing of one kind or of all, in kind order then name.

        Installed plugins are not imported; the Python files of `project` are.
        """

        rows: list[PluginInfo] = []
        for each in (kind,) if kind else tuple(KINDS):
            by_name: dict[str, list[Entry]] = {}
            for entry in self.entries(each, project=project):
                by_name.setdefault(entry.name, []).append(entry)
            for name in sorted(by_name):
                rows += _rows(KINDS[each].switch_prefix, by_name[name], project)
        return rows

    @contextmanager
    def preserved(self) -> Iterator[None]:
        """Undo every registration made inside the block."""

        with self._boot_lock, self._lock:
            saved, ready = dict(self._entries), set(self._ready)
        try:
            yield
        finally:
            with self._boot_lock, self._lock:
                self._entries.clear()
                self._entries.update(saved)
                self._ready.clear()
                self._ready.update(ready)


def _rows(prefix: str | None, group: list[Entry], project: Path | None) -> list[PluginInfo]:
    from awa.plugins.switches import is_off

    group = sorted(group, key=lambda e: SOURCE_ORDER.index(e.source))
    dists = {e.dist or "" for e in group if e.source == "plugin"}
    winner = group[-1]
    for entry in group[:-1]:
        if entry.replace and len(dists) < 2:
            winner = entry
            break
    rows: list[PluginInfo] = []
    for position, entry in enumerate(group):
        if entry.source == "plugin" and len(dists) > 1:
            status = "clash"
        elif entry is winner:
            status = "active"
        elif position > group.index(winner):
            status = "replaced"
        elif entry.source == "plugin":
            status = "shadowed (replace unknown until load)"
        else:
            status = "shadowed"
        key = f"{prefix}.{entry.name}" if prefix else None
        reference = None
        if entry.source == "plugin" and entry.kind in ("tools", "skills") and entry.dist:
            dist = normalize_dist(entry.dist)
            reference = f"{PLUGIN_PREFIX}{dist}/{entry.name}"
        rows.append(
            PluginInfo(
                entry.kind,
                entry.name,
                entry.source,
                entry.target,
                entry.summary,
                status,
                entry.dist,
                entry.version,
                key,
                not (key and is_off(key, project=project)),
                reference,
            )
        )
    return rows


class KindView(MutableMapping[str, Any]):
    """A `name -> object` mapping of one kind's loaded entries; broken or clashing entries are skipped.

    Assigning registers with `replace=True`; deleting unregisters.
    """

    def __init__(
        self, registry: Registry, kind: Kind, project: Callable[[], Path | None] | None = None
    ) -> None:
        self.registry = registry
        self.kind = kind
        self._project = project

    def _where(self) -> Path | None:
        return self._project() if self._project is not None else None

    def __getitem__(self, name: str) -> Any:
        try:
            return self.registry.load(self.kind, name, project=self._where())
        except PluginError as exc:
            if name in self.registry.names(self.kind, project=self._where()):
                noun = KINDS[self.kind].noun
                warnings.warn(f"{noun} {name!r} is not used: {exc}", RuntimeWarning, stacklevel=2)
            raise KeyError(name) from None

    def __setitem__(self, name: str, obj: Any) -> None:
        self.registry.register(self.kind, name, obj, replace=True)

    def __delitem__(self, name: str) -> None:
        try:
            self.registry.unregister(self.kind, name)
        except PluginError:
            raise KeyError(name) from None

    def __iter__(self) -> Iterator[str]:
        return iter(self.registry.loadable(self.kind, project=self._where()))

    def __len__(self) -> int:
        return len(self.registry.loadable(self.kind, project=self._where()))

    def __contains__(self, name: object) -> bool:
        return name in self.registry.names(self.kind, project=self._where())


REGISTRY = Registry()
"""The process-wide registry."""
