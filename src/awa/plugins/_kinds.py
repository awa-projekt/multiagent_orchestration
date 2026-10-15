"""The kinds of plugin with their load checks, the built-in providers and the entry-point scan."""

from __future__ import annotations

import importlib
import importlib.metadata
import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from awa.errors import AwaError

if TYPE_CHECKING:
    from awa._internal.upstream.tools import BaseTool

Kind = Literal["tools", "connectors", "skills", "models", "parsers", "erasure"]


class PluginError(AwaError):
    """A plugin cannot be found, loaded or registered; the message says the fix."""


def _check_tools(obj: Any, where: str) -> list[BaseTool]:
    from awa._internal.upstream.tools import BaseTool, StructuredTool

    if isinstance(obj, BaseTool):
        return [obj]
    if isinstance(obj, list | tuple) and obj and all(isinstance(t, BaseTool) for t in obj):
        return list(obj)
    if type(obj).__name__ == "CatalogEntry":
        return [obj.create()]
    if inspect.isfunction(obj) and inspect.getdoc(obj):
        return [StructuredTool.from_function(obj)]
    raise PluginError(
        f"{where} is a {type(obj).__name__}, not a tool; "
        "return a BaseTool, a list of them or a documented function"
    )


def _check_callable(obj: Any, where: str) -> Any:
    if callable(obj):
        return obj
    raise PluginError(f"{where} is a {type(obj).__name__}, not callable")


def _check_connector(obj: Any, where: str) -> Any:
    named = (
        type(obj).__name__ == "ConnectorSpec"
        and hasattr(obj, "name")
        and hasattr(obj, "operations")
    )
    if named or callable(obj):
        return obj
    raise PluginError(f"{where} is a {type(obj).__name__}, not a ConnectorSpec or a tool factory")


def _import(target: str) -> Any:
    """The object a `module:attribute` string names (the way a lower layer reaches a higher one)."""

    module, _, attribute = target.partition(":")
    obj: Any = importlib.import_module(module)
    for part in filter(None, attribute.split(".")):
        obj = getattr(obj, part)
    return obj


def _check_skills(obj: Any, where: str) -> Any:
    skill, registry = _import("awa.skills:Skill"), _import("awa.skills:SkillRegistry")
    if isinstance(obj, skill | registry | Path | list | tuple) or inspect.ismodule(obj):
        return obj
    raise PluginError(
        f"{where} is a {type(obj).__name__}; expected a Skill, a list of skills, "
        "a SkillRegistry, a path or a package of skill folders"
    )


def _check_erasure(obj: Any, where: str) -> Any:
    if not hasattr(obj, "erase"):
        raise PluginError(f"{where} lacks erase")
    return obj


@dataclass(frozen=True)
class KindSpec:
    """One kind of plugin: `noun` names one object in messages, `check` validates a loaded
    object, `switch_prefix` keys its switch."""

    kind: Kind
    noun: str
    summary: str
    check: Callable[[Any, str], Any]
    switch_prefix: str | None = None

    @property
    def group(self) -> str:
        """The entry-point group, `awa.<kind>`."""

        return f"awa.{self.kind}"


KINDS: dict[Kind, KindSpec] = {
    spec.kind: spec
    for spec in (
        KindSpec("tools", "tool", "LangChain tools", _check_tools, "tool"),
        KindSpec("connectors", "connector", "systems to connect to", _check_connector, "connector"),
        KindSpec("skills", "skill", "skills agents can read", _check_skills, "skill"),
        KindSpec("models", "model", "chat model factories", _check_callable),
        KindSpec("parsers", "parser", "file readers", _check_callable, "parser"),
        KindSpec("erasure", "erasure handler", "erasure handlers", _check_erasure),
    )
}
"""Every kind, in listing order."""

Source = Literal["project", "plugin", "builtin"]
SOURCE_ORDER: tuple[Source, ...] = ("project", "plugin", "builtin")
"""Where an entry comes from, highest first."""


@dataclass(frozen=True)
class Entry:
    """One registered plugin (`target` is `module:attribute` or a file); only `load` runs its code."""

    kind: Kind
    name: str
    source: Source
    target: str
    loader: Callable[[], Any]
    replace: bool = False
    dist: str | None = None
    version: str | None = None
    summary: str = ""

    def load(self) -> Any:
        """Import and check the object; raises `PluginError` when either fails."""

        where = f"{KINDS[self.kind].noun} {self.name!r} ({self.target})"
        try:
            obj = self.loader()
        except Exception as exc:
            raise PluginError(f"cannot load {where}: {type(exc).__name__}: {exc}") from exc
        return KINDS[self.kind].check(obj, where)


# Built-ins come from this table, not from entry points, so a stale dist-info never switches a
# default off.
PROVIDERS: dict[Kind, tuple[str, ...]] = {
    "parsers": ("awa.data._parse:builtin_parsers",),
    "tools": ("awa.toolkits._catalog:builtin_tools",),
    "connectors": (
        "awa.connectors._catalog:builtin_connectors",
        "awa.connectors._saas_microsoft:builtin_connectors",
    ),
    "skills": ("awa.skills.registry:builtin_skill_rows",),
    "erasure": ("awa.memory.long_term:builtin_erasure", "awa.automation.store:builtin_erasure"),
}


PLUGIN_PREFIX = "plugin:"
"""Prefix of a blueprint tool reference to a plugin: `plugin:<dist>` or `plugin:<dist>/<name>`."""

_SEPARATORS = re.compile(r"[-_.]+")


def normalize_dist(name: str) -> str:
    """Normalize a distribution name like PyPI does (`Acme_AWA.tools` -> `acme-awa-tools`)."""

    return _SEPARATORS.sub("-", name).lower()


def parse_plugin_reference(reference: str) -> tuple[str, str | None]:
    """Split `plugin:<dist>/<name>` into `(dist, name)`; `name` is `None` for `plugin:<dist>`."""

    body = reference[len(PLUGIN_PREFIX) :]
    dist, _, name = body.partition("/")
    return dist, (name if "/" in body else None)


def scan(spec: KindSpec) -> list[Entry]:
    """The installed plugins of one kind, from entry-point metadata only."""

    found: list[Entry] = []
    for entry in importlib.metadata.entry_points(group=spec.group):
        dist = entry.dist
        found.append(
            Entry(
                spec.kind,
                entry.name,
                "plugin",
                entry.value,
                entry.load,
                dist=dist.name if dist is not None else None,
                version=dist.version if dist is not None else None,
                summary=f"from the installed package {dist.name if dist else entry.value}",
            )
        )
    return found
