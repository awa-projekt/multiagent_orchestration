"""Plugins: one registry for everything that extends awa.

Six kinds (`tools`, `connectors`, `skills`, `models`, `parsers`, `erasure`) come, in resolution
order, from the project folder, installed packages (entry-point group `awa.<kind>`) and the
built-in parts. A higher entry takes a lower entry's name only with `replace=True`; the
blueprint's `components:` section switches parts on and off (`awa.plugins.switches`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from awa.plugins._kinds import (
    KINDS,
    PLUGIN_PREFIX,
    Kind,
    PluginError,
    normalize_dist,
    parse_plugin_reference,
)
from awa.plugins._registry import REGISTRY, KindView, PluginInfo, Registry, current_project

_MISSING: Any = object()
_PLURAL = {kind.removesuffix("s"): kind for kind in KINDS}
_PART_FAMILIES = ("guard", "cache", "tracer")


def _kind(name: str) -> Kind:
    plural = _PLURAL.get(name, name)
    if plural not in KINDS:
        raise PluginError(f"unknown plugin kind {name!r}; the kinds are {', '.join(KINDS)}")
    return plural


def register(kind: str, name: str, obj: Any, *, replace: bool = False, summary: str = "") -> Any:
    """Register `obj` (or a `module:attribute` string) as the plugin `name` of `kind`.

    Called from a project's `plugins.py` the entry belongs to the project; elsewhere it is a
    built-in-level registration for this process.

    Raises:
        PluginError: If the kind is unknown, or `name` is taken and `replace` is false.

    Example:
        ```python
        register("tools", "lookup_order", lookup_order)
        ```
    """

    return REGISTRY.register(_kind(kind), name, obj, replace=replace, summary=summary)


def find(
    kind: str,
    name: str,
    *,
    project: Path | None = None,
    dist: str | None = None,
    default: Any = _MISSING,
) -> Any:
    """The loaded plugin `name` of `kind` that is in use (this imports its code).

    Args:
        kind: A kind, singular or plural (`skill` or `skills`).
        project: A project folder whose files are the first source.
        dist: Only the plugin of this distribution.
        default: Returned instead of raising when there is no plugin of that name.

    Raises:
        PluginError: If there is none (and no `default`), two packages declare it, or it cannot be
            loaded (a broken `plugins.py` is an error even with a `default`).
    """

    plural = _kind(kind)
    if default is not _MISSING and name not in REGISTRY.names(plural, project=project):
        return default
    return REGISTRY.load(plural, name, project=project, dist=dist)


def replacement_for(kind: str, name: str, *, project: Path | None = None) -> Any:
    """The loaded object of a project or installed plugin that replaces the built-in `name`, or `None`."""

    return REGISTRY.replacement(_kind(kind), name, project=project)


def list_plugins(kind: str | None = None, *, project: Path | None = None) -> list[PluginInfo]:
    """Every plugin and switchable part; installed plugins are not imported.

    With `project`, its `tools.py`, `tools/`, `connectors/` and `plugins.py` are imported to read
    their names, as when the project runs: list only projects whose code you trust.

    Args:
        kind: Only this kind (singular or plural) or a part family (`guard`, `cache`, `tracer`,
            `toolkit`).
        project: A project folder whose files are the first source.
    """

    from awa.plugins.switches import PARTS, SECURITY, is_off

    other = kind in (*_PART_FAMILIES, "toolkit")
    wanted = kind if kind is None or other else _kind(kind)
    rows = [] if other else REGISTRY.infos(wanted, project=project)  # type: ignore[arg-type]
    if wanted in (None, "toolkit"):
        rows += _toolkit_rows(project)
    for key, summary in PARTS.items():
        family, _, name = key.partition(".")
        if wanted in (None, family):
            rows.append(
                PluginInfo(
                    family,
                    name,
                    "builtin",
                    summary=summary,
                    key=key,
                    enabled=not is_off(key, project=project),
                    security=key in SECURITY,
                )
            )
    return rows


def _toolkit_rows(project: Path | None) -> list[PluginInfo]:
    """The built-in toolkits (groups of built-in tools that share settings), as switchable rows."""

    from awa.plugins.switches import is_off

    tools: dict[str, list[str]] = {}
    for entry in REGISTRY.entries("tools", project=project):
        if entry.source == "builtin":
            toolkit = getattr(entry.loader(), "toolkit", None)
            if toolkit is not None:
                tools.setdefault(toolkit.name, []).append(entry.name)
    return [
        PluginInfo(
            "toolkit",
            name,
            "builtin",
            summary=f"Builds the tools {', '.join(names)}.",
            key=f"toolkit.{name}",
            enabled=not is_off(f"toolkit.{name}", project=project),
        )
        for name, names in sorted(tools.items())
    ]


def load_reference(reference: str) -> list[Any]:
    """The tools a `plugin:<dist>[/<name>]` blueprint reference names (this imports their code).

    Raises:
        PluginError: If the reference is invalid, names no installed plugin, or cannot be loaded.
    """

    if (problem := reference_problem(reference)) is not None:
        raise PluginError(problem[0].upper() + problem[1:])
    dist, name = parse_plugin_reference(reference)
    wanted = normalize_dist(dist)
    tools: list[Any] = []
    for entry in REGISTRY.entries("tools"):
        if entry.source != "plugin" or normalize_dist(entry.dist or "") != wanted:
            continue
        if name is not None and entry.name != name:
            continue
        origin = f"{PLUGIN_PREFIX}{wanted}/{entry.name}"
        for tool in entry.load():
            tools.append(
                tool.model_copy(
                    update={"metadata": {**(tool.metadata or {}), "awa_plugin": origin}}
                )
            )
    if not tools:
        raise PluginError(f"plugin {reference} provides no tools")
    return tools


def reference_problem(reference: str) -> str | None:
    """Why a `plugin:` tool reference cannot resolve, without importing anything; else `None`."""

    dist, name = parse_plugin_reference(reference)
    if not dist or name == "" or (name is not None and "/" in name):
        return (
            f"invalid plugin reference {reference!r}: expected 'plugin:<dist>' or "
            "'plugin:<dist>/<name>'"
        )
    wanted = normalize_dist(dist)
    entries = [
        e
        for e in REGISTRY.entries("tools")
        if e.source == "plugin" and normalize_dist(e.dist or "") == wanted
    ]
    if any(e.name == name for e in entries) or (name is None and entries):
        return None
    installed = sorted(
        {normalize_dist(e.dist or "") for e in REGISTRY.entries("tools") if e.source == "plugin"}
    )
    what = f"plugin {name!r} of distribution {dist!r}" if name else f"distribution {dist!r}"
    return (
        f"no installed awa.tools {what} (installed plugin distributions: "
        f"{', '.join(installed) or 'none'})"
    )


__all__ = [
    "KINDS",
    "REGISTRY",
    "KindView",
    "PluginError",
    "PluginInfo",
    "Registry",
    "current_project",
    "find",
    "list_plugins",
    "load_reference",
    "normalize_dist",
    "reference_problem",
    "replacement_for",
    "register",
]
