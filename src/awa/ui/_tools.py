"""What the tools of a built system are called and take, in plain words, for the Try it page."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from awa._internal.upstream.tools import BaseTool
from awa.ui._plain import humanize, tool_label

_SECTIONS = re.compile(
    r"^\s*(Args|Arguments|Parameters|Returns?|Raises|Yields|Examples?|Notes?):?\s*$"
)
_ARGUMENT = re.compile(r"^\s{2,}(\w+)\s*(?:\([^)]*\))?:\s*(.+)$")
_MAX_NODES = 500


@dataclass(frozen=True)
class ToolInfo:
    """One tool in the words of a non-technical reader.

    Attributes:
        fields: For every argument its `title` and `description`.
    """

    name: str
    label: str
    fields: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


def _agents_of(system: Any) -> Iterator[Any]:
    """`system` and every agent reachable from its attributes (members, steps, branches...)."""

    seen: set[int] = set()
    stack = [system]
    while stack and len(seen) < _MAX_NODES:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        for value in list(getattr(node, "__dict__", {}).values()):
            items = list(value.values()) if isinstance(value, dict) else value
            for item in items if isinstance(items, list | tuple) else (items,):
                if hasattr(item, "card") and hasattr(item, "run"):
                    stack.append(item)


def _argument_docs(description: str) -> tuple[str, dict[str, str]]:
    """A description split into its prose and the `Args:` entries of a docstring."""

    prose: list[str] = []
    arguments: dict[str, str] = {}
    in_arguments = False
    for line in description.splitlines():
        heading = _SECTIONS.match(line)
        if heading:
            in_arguments = heading.group(1) in {"Args", "Arguments", "Parameters"}
            continue
        entry = _ARGUMENT.match(line) if in_arguments else None
        if entry:
            arguments[entry.group(1)] = entry.group(2).strip()
        elif not in_arguments:
            prose.append(line)
    return " ".join(" ".join(prose).split()), arguments


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _schema(tool: BaseTool) -> Mapping[str, Any]:
    schema = tool.args_schema
    try:
        found = schema if isinstance(schema, dict) else schema.model_json_schema()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - a tool without a usable schema just has no field titles
        return {}
    return found if isinstance(found, dict) else {}


def tool_info(tool: BaseTool, instances: Mapping[str, str]) -> ToolInfo:
    """The plain-words view of one tool; `instances` are the titles of connector instances."""

    prose, documented = _argument_docs(tool.description or "")
    metadata = tool.metadata or {}
    properties = _schema(tool).get("properties", {})
    fields: dict[str, dict[str, str]] = {}
    for name, spec in properties.items():
        spec = spec if isinstance(spec, dict) else {}
        title = str(spec.get("title") or "")
        auto = _letters(humanize(name)) == _letters(title)  # the schema's own title of the name
        fields[name] = {
            "title": humanize(name) if not title or auto else title,
            "description": str(spec.get("description") or documented.get(name, "")),
        }
    return ToolInfo(
        name=tool.name, label=tool_label(tool.name, prose, metadata, instances), fields=fields
    )


def tool_infos(system: Any, instances: Mapping[str, str] | None = None) -> dict[str, ToolInfo]:
    """Every tool of `system` by name (the first of a name wins).

    Example:
        ```python
        infos = tool_infos(project.system(), {"ledger": "Invoice ledger"})
        infos["ledger_query"].label  # 'Look something up in the Invoice ledger'
        ```
    """

    found: dict[str, ToolInfo] = {}
    for agent in _agents_of(system):
        tools = getattr(agent, "tools", None)
        if not isinstance(tools, tuple | list):
            continue
        for tool in tools:
            if isinstance(tool, BaseTool) and tool.name not in found:
                found[tool.name] = tool_info(tool, instances or {})
    return found
