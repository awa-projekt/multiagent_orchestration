"""Import LangChain tools, toolkits, functions and runnables as awa tools."""

from __future__ import annotations

import contextlib
import inspect
import re
import typing
import warnings
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from pydantic import Field, create_model

from awa._internal.upstream.runnables import Runnable
from awa._internal.upstream.tools import BaseTool, BaseToolkit, StructuredTool
from awa._internal.upstream.tools import tool as as_langchain_tool
from awa._internal.util import import_path
from awa.errors import AdapterError
from awa.tools.approval import APPROVAL_METADATA_KEY, create_tool_approval_wrapper
from awa.tools.risk import (
    RISK_METADATA_KEY,
    RISK_NAMES,
    RiskName,
    ToolRisk,
    declared_tool_risk,
    infer_description_risk,
    infer_tool_risk,
)

RiskLike: TypeAlias = ToolRisk | Mapping[str, bool] | Sequence[RiskName] | RiskName
"""An explicit risk: a `ToolRisk`, a mapping of flags, or the names of the true flags."""

RiskOrigin = Literal["explicit", "declared", "name", "description", "default"]
"""What decided a risk flag: the `risk=` argument, the tool's own annotation,
the name or description heuristics, or nothing (the flag is `False`)."""

IMPORT_METADATA_KEY = "awa_imported_from"
"""Tool metadata key naming where an imported tool came from."""

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class ImportedTool:
    """One imported tool and what the import decided about it.

    Attributes:
        tool: The tool, annotated with its risk and, when `requires_approval`, wrapped with
            approval.
        source: Where it came from (`"BaseTool"`, `"toolkit FileManagementToolkit"`, `"function"`,
            `"runnable"`, prefixed by the import path if any).
        original_name: The tool's name before prefixing or renaming.
        risk_origins: What decided each flag.
        approval_reason: Why approval was, or was not, added.
    """

    tool: BaseTool
    source: str
    original_name: str
    risk: ToolRisk
    risk_origins: Mapping[RiskName, RiskOrigin]
    requires_approval: bool
    approval_reason: str
    notes: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        """The imported tool's name."""

        return self.tool.name


@dataclass(frozen=True)
class ToolImportReport:
    """What `import_langchain_tools` imported and inferred, one `ImportedTool` per tool."""

    entries: tuple[ImportedTool, ...] = field(default=())

    @property
    def tools(self) -> list[BaseTool]:
        """The imported tools, ready for `Agent(tools=...)`."""

        return [entry.tool for entry in self.entries]

    def entry(self, name: str) -> ImportedTool:
        """The entry of the tool named `name`; `KeyError` if there is none."""

        for entry in self.entries:
            if entry.name == name:
                return entry
        raise KeyError(name)

    def render(self) -> str:
        """Render the report as readable text, one block per tool."""

        if not self.entries:
            return "no tools imported"
        lines: list[str] = []
        for entry in self.entries:
            flags = ", ".join(
                f"{flag} ({entry.risk_origins[flag]})"
                for flag in RISK_NAMES
                if getattr(entry.risk, flag)
            )
            lines.append(f"{entry.name}  [{entry.source}]")
            lines.append(f"  risk: {flags or 'none'}")
            approval = "required" if entry.requires_approval else "not required"
            lines.append(f"  approval: {approval} ({entry.approval_reason})")
            lines.extend(f"  note: {note}" for note in entry.notes)
        return "\n".join(lines)


def from_langchain(
    *sources: Any,
    risk: RiskLike | None = None,
    requires_approval: bool | None = None,
    prefix: str | None = None,
    select: Sequence[str] | None = None,
    init_kwargs: Mapping[str, Any] | None = None,
) -> list[BaseTool]:
    """Import LangChain tools, toolkits, functions or runnables as awa tools.

    Every tool keeps its name (plus `prefix`), description and schema, gets its risk in
    `metadata["awa_risk"]`, and is wrapped with approval when `requires_approval` is `True`, or when
    it is `None` and the tool is inferred to take external actions (the agent then needs a
    checkpointer).

    Args:
        *sources: `BaseTool`s, toolkits, documented functions, runnables, import paths (classes are
            instantiated with `init_kwargs`, factories called), or sequences of them.
        risk: Flags for every imported tool; they win over annotations and heuristics.
        select: Keep only the tools with these original names, in this order.
        init_kwargs: Keyword arguments for classes and factories named by an import path.

    Raises:
        AdapterError: If a source cannot be imported or converted, a selected name is missing, or
            two tools end up with the same name.

    Example:
        ```python
        create_issue, get_issue = from_langchain(
            "my_tools.github:make_tools",
            select=["create_issue", "get_issue"],
            init_kwargs={"repo": "acme/app"},
        )
        ```
    """

    return import_langchain_tools(
        *sources,
        risk=risk,
        requires_approval=requires_approval,
        prefix=prefix,
        select=select,
        init_kwargs=init_kwargs,
    ).tools


def from_langchain_tool(
    source: Any,
    *,
    name: str | None = None,
    risk: RiskLike | None = None,
    requires_approval: bool | None = None,
    prefix: str | None = None,
    init_kwargs: Mapping[str, Any] | None = None,
) -> BaseTool:
    """Import exactly one tool; see `from_langchain`.

    Args:
        source: A tool, function, runnable or import path yielding one tool, or a toolkit with
            `name`.
        name: Pick the tool with this original name, for example out of a toolkit.

    Raises:
        AdapterError: If the source yields no tool or several (without `name`).
    """

    tools = from_langchain(
        source,
        risk=risk,
        requires_approval=requires_approval,
        prefix=prefix,
        select=[name] if name is not None else None,
        init_kwargs=init_kwargs,
    )
    if len(tools) != 1:
        names = ", ".join(tool.name for tool in tools) or "none"
        raise AdapterError(
            f"{_describe(source)} yields {len(tools)} tools ({names}); pass name= to pick one"
        )
    return tools[0]


def import_langchain_tools(
    *sources: Any,
    risk: RiskLike | None = None,
    requires_approval: bool | None = None,
    prefix: str | None = None,
    select: Sequence[str] | None = None,
    init_kwargs: Mapping[str, Any] | None = None,
) -> ToolImportReport:
    """Import tools like `from_langchain` and report what was inferred for each.

    Returns:
        The report; `report.tools` are the tools and `report.render()` explains every decision.

    Raises:
        AdapterError: See `from_langchain`.
    """

    explicit = _as_risk(risk)
    collected: list[tuple[BaseTool, str]] = []
    for source in sources:
        collected.extend(_collect(source, init_kwargs or {}, origin=None))
    if select is not None:
        collected = _selected(collected, select)
    entries = [
        _import_one(tool, origin, explicit, requires_approval, prefix) for tool, origin in collected
    ]
    names = [entry.name for entry in entries]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise AdapterError(
            f"Imported tools share the names {', '.join(duplicates)}; pass prefix= or select="
        )
    return ToolImportReport(tuple(entries))


def _collect(
    source: Any, init_kwargs: Mapping[str, Any], *, origin: str | None
) -> list[tuple[BaseTool, str]]:
    def label(kind: str) -> str:
        return f"{origin} ({kind})" if origin else kind

    if isinstance(source, str):
        return _collect(
            _instantiate(import_path(source), init_kwargs, source), init_kwargs, origin=source
        )
    if isinstance(source, BaseTool):
        return [(source, label("BaseTool"))]
    if isinstance(source, BaseToolkit):
        tools = list(source.get_tools())
        kind = f"toolkit {type(source).__name__}"
        wrong = [type(value).__name__ for value in tools if not isinstance(value, BaseTool)]
        if wrong:
            raise AdapterError(f"{kind} returned non-tools: {', '.join(wrong)}")
        return [(tool, label(kind)) for tool in tools]
    from awa._internal.upstream.graph import Pregel

    if isinstance(source, Pregel):
        raise AdapterError(
            f"{_describe(source)} is a LangGraph graph; wrap it with "
            "awa.agents.graph_agent.from_langgraph(graph, name=...) and use it as an agent, or its "
            ".as_tool()"
        )
    if isinstance(source, Runnable):
        try:
            with warnings.catch_warnings():
                # `Runnable.as_tool` is marked beta; the conversion is what was asked for.
                warnings.filterwarnings("ignore", message="This API is in beta")
                converted = source.as_tool()
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"Cannot convert {_describe(source)} to a tool: {exc}") from None
        return [(converted, label("runnable"))]
    if isinstance(source, type):
        raise AdapterError(
            f"{source.__name__} is a class; pass an instance, or its import path to have "
            "it instantiated"
        )
    if callable(source):
        try:
            converted = as_langchain_tool(source)
        except ValueError as exc:
            raise AdapterError(
                f"Cannot convert {_describe(source)} to a tool: {exc} (a function needs a "
                "docstring, which becomes the tool description)"
            ) from None
        return [(converted, label("function"))]
    if isinstance(source, Iterable) and not isinstance(source, bytes | Mapping):
        collected: list[tuple[BaseTool, str]] = []
        for item in source:
            collected.extend(_collect(item, init_kwargs, origin=origin))
        return collected
    raise AdapterError(
        f"Cannot import {_describe(source)} as a tool: expected a BaseTool, a BaseToolkit, a "
        "documented function, a Runnable, an import path or a list of them"
    )


def _selected(
    collected: list[tuple[BaseTool, str]], select: Sequence[str]
) -> list[tuple[BaseTool, str]]:
    by_name: dict[str, tuple[BaseTool, str]] = {}
    for tool, origin in collected:
        by_name.setdefault(tool.name, (tool, origin))
    missing = [name for name in select if name not in by_name]
    if missing:
        available = ", ".join(sorted(by_name)) or "none"
        raise AdapterError(
            f"No tool named {', '.join(map(repr, missing))} to select (available: {available})"
        )
    return [by_name[name] for name in dict.fromkeys(select)]


def _instantiate(target: Any, init_kwargs: Mapping[str, Any], path: str) -> Any:
    """Instantiate a tool/toolkit class, or call a factory, named by an import path."""

    if isinstance(target, type):
        if issubclass(target, BaseTool | BaseToolkit):
            try:
                return target(**init_kwargs)
            except Exception as exc:
                raise AdapterError(
                    f"Cannot create {path!r}: {type(exc).__name__}: {exc} (pass its settings "
                    "with init_kwargs=)"
                ) from exc
        raise AdapterError(f"{path!r} is a class but neither a BaseTool nor a BaseToolkit")
    if isinstance(target, BaseTool | BaseToolkit | Runnable) or not callable(target):
        if init_kwargs:
            raise AdapterError(f"init_kwargs given, but {path!r} is not a class or factory")
        return target
    if _is_factory(target):
        try:
            return target(**init_kwargs)
        except Exception as exc:
            raise AdapterError(
                f"Calling the tool factory {path!r} failed: {type(exc).__name__}: {exc}"
            ) from exc
    if init_kwargs:
        raise AdapterError(f"init_kwargs given, but {path!r} is a tool function, not a factory")
    return target


def _is_factory(function: Callable[..., Any]) -> bool:
    """Whether a function named by an import path makes tools rather than being one."""

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return False
    annotation = signature.return_annotation
    with contextlib.suppress(Exception):  # unresolvable annotations: use the raw text
        annotation = typing.get_type_hints(function).get("return", annotation)
    text = annotation if isinstance(annotation, str) else repr(annotation)
    if any(word in text for word in ("BaseTool", "BaseToolkit", "Toolkit", "StructuredTool")):
        return True
    required = [
        p
        for p in signature.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    return not required and not inspect.getdoc(function)


def _import_one(
    tool: BaseTool,
    origin: str,
    explicit: ToolRisk | None,
    requires_approval: bool | None,
    prefix: str | None,
) -> ImportedTool:
    original = tool.name
    notes: list[str] = []
    name = f"{prefix or ''}{original}"
    if not _TOOL_NAME.fullmatch(name):
        fixed = _INVALID_NAME_CHARS.sub("_", name).strip("_")[:64] or "tool"
        notes.append(f"renamed from {name!r}: tool names allow only letters, digits, _ and -")
        name = fixed
    if not (tool.description or "").strip():
        notes.append("the tool has no description; the model will not know when to use it")

    risk, origins = _resolve_risk(tool, explicit)
    gated, reason = _approval(requires_approval, risk, origins)
    metadata = {
        **(tool.metadata or {}),
        RISK_METADATA_KEY: risk.model_dump(),
        IMPORT_METADATA_KEY: origin,
    }
    imported = tool if name == original else tool.model_copy(update={"name": name})
    if gated:
        if imported.args_schema is None:
            # A single-input tool: give the approval wrapper a schema to validate against.
            imported = _single_input_tool(imported)
        imported = create_tool_approval_wrapper(imported)
        metadata[APPROVAL_METADATA_KEY] = True
        notes.append("calls pause for a human decision; the agent needs a checkpointer")
    imported = imported.model_copy(update={"metadata": metadata, "tags": list(tool.tags or [])})
    return ImportedTool(
        tool=imported,
        source=origin,
        original_name=original,
        risk=risk,
        risk_origins=origins,
        requires_approval=gated,
        approval_reason=reason,
        notes=tuple(notes),
    )


def _approval(
    requested: bool | None, risk: ToolRisk, origins: Mapping[RiskName, RiskOrigin]
) -> tuple[bool, str]:
    if requested is True:
        return True, "requested"
    if requested is False:
        return False, "opted out" if risk.external_action else "not requested"
    if risk.external_action:
        return True, f"inferred: external action ({origins['external_action']})"
    return False, "no external action inferred"


def _resolve_risk(
    tool: BaseTool, explicit: ToolRisk | None
) -> tuple[ToolRisk, dict[RiskName, RiskOrigin]]:
    """The tool's risk flags and what decided each one (explicit > declared > heuristics)."""

    try:
        declared = declared_tool_risk(tool)
    except ValueError as exc:
        raise AdapterError(
            f"Tool {tool.name!r} has a malformed awa_risk annotation: {exc}"
        ) from exc
    by_name = infer_tool_risk(tool.name)
    by_description = infer_description_risk(tool.description or "")
    flags: dict[str, bool] = {}
    origins: dict[RiskName, RiskOrigin] = {}
    for flag in RISK_NAMES:
        candidates: list[tuple[RiskOrigin, bool | None]] = [
            ("explicit", getattr(explicit, flag) if explicit is not None else None),
            ("declared", getattr(declared, flag) if declared is not None else None),
        ]
        for origin, value in candidates:
            if value is not None:
                flags[flag], origins[flag] = bool(value), origin
                break
        else:
            if getattr(by_name, flag):
                flags[flag], origins[flag] = True, "name"
            elif getattr(by_description, flag):
                flags[flag], origins[flag] = True, "description"
            else:
                flags[flag], origins[flag] = False, "default"
    return ToolRisk(**flags), origins


def _single_input_tool(tool: BaseTool) -> BaseTool:
    """`tool` (which takes one string) as a structured tool with a `tool_input` argument."""

    schema = create_model(
        f"{tool.name}_input", tool_input=(str, Field(description="The input of the tool."))
    )

    def run(tool_input: str) -> Any:
        return tool.invoke(tool_input)

    async def arun(tool_input: str) -> Any:
        return await tool.ainvoke(tool_input)

    return StructuredTool.from_function(
        func=run,
        coroutine=arun,
        name=tool.name,
        description=tool.description,
        args_schema=schema,
        return_direct=tool.return_direct,
    )


def _as_risk(risk: RiskLike | None) -> ToolRisk | None:
    if risk is None or isinstance(risk, ToolRisk):
        return risk
    try:
        return ToolRisk.model_validate(risk if isinstance(risk, str | Mapping) else list(risk))
    except ValueError as exc:
        raise AdapterError(f"Invalid risk {risk!r}: {exc}") from None


def _describe(value: Any) -> str:
    if isinstance(value, str):
        return repr(value)
    name = getattr(value, "name", None) or getattr(value, "__name__", None)
    kind = type(value).__name__
    return f"{kind} {name!r}" if isinstance(name, str) else kind


__all__ = [
    "APPROVAL_METADATA_KEY",
    "IMPORT_METADATA_KEY",
    "ImportedTool",
    "RiskLike",
    "RiskOrigin",
    "ToolImportReport",
    "from_langchain",
    "from_langchain_tool",
    "import_langchain_tools",
]
