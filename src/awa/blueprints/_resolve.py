"""Resolution helpers shared by checking, building and designing blueprints."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import os
import re
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from awa._internal.logging import get_logger
from awa._internal.upstream.tools import BaseTool
from awa._internal.upstream.tools import tool as as_langchain_tool
from awa.blueprints._mcp import is_mcp_reference
from awa.blueprints.errors import BlueprintError
from awa.blueprints.schema import Blueprint, SkillSource, ToolSource
from awa.errors import AwaError
from awa.runtime.case import in_case_dir
from awa.skills import SkillRegistry

_logger = get_logger("blueprints")

if TYPE_CHECKING:
    from awa.toolkits import CatalogEntry
    from awa.tools.risk import ToolRisk

_IDENTIFIER_PATH = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*"
IMPORT_REFERENCE = re.compile(rf"^{_IDENTIFIER_PATH}:{_IDENTIFIER_PATH}$")
"""Syntax of a `"package.module:attribute"` reference."""

FILE_REFERENCE = re.compile(rf"^(?P<path>.+\.py):(?P<attribute>{_IDENTIFIER_PATH})$")
"""Syntax of a `"path/to/file.py:attribute"` reference to a Python file."""

BUILTIN_PREFIX = "builtin:"
"""Prefix of a tool reference naming a builtin tool of `awa.toolkits` (`builtin:read_file`)."""

CONNECTOR_PREFIX = "connector:"
"""Prefix of a tool reference naming connector tools (`connector:jira.search_issues`)."""

_CONNECTOR_PATH_OPTIONS = ("root", "spec", "people_file")

_FILE_MODULES: dict[Path, ModuleType] = {}
_scope: ContextVar[dict[Path, ModuleType] | None] = ContextVar(
    "awa_file_modules_scope", default=None
)


def _loaded() -> dict[Path, ModuleType]:
    """The files loaded in the current `file_modules_scope` (or outside any scope)."""

    scope = _scope.get()
    return scope if scope is not None else _FILE_MODULES


def is_import_reference(text: str) -> bool:
    """Whether `text` is an import reference: it contains `:` and is no `mcp:`, `builtin:`, `connector:` or `plugin:` reference."""

    return (
        ":" in text
        and not is_mcp_reference(text)
        and not is_builtin_reference(text)
        and not is_connector_reference(text)
        and not text.startswith("plugin:")
    )


def is_builtin_reference(text: str) -> bool:
    """Return whether `text` names a builtin tool (`builtin:<name>`)."""

    return text.startswith(BUILTIN_PREFIX)


def builtin_entry(reference: str) -> CatalogEntry | None:
    """Return the catalog entry of `builtin:<name>`, or `None` if there is none."""

    from awa.toolkits import catalog

    name = reference.removeprefix(BUILTIN_PREFIX)
    tools = catalog()
    return tools.get(name) if name in tools else None


def builtin_issues(reference: str, toolkits: Mapping[str, Any]) -> list[str]:
    """Why `builtin:<name>` cannot be built from the blueprint's `toolkits:` (offline)."""

    entry = builtin_entry(reference)
    if entry is None:
        from awa.toolkits import catalog

        known = ", ".join(catalog().names())
        return [f"unknown builtin tool {reference!r} (known: {known})"]
    return entry.problems(toolkits)


def builtin_requires_approval(reference: str, toolkits: Mapping[str, Any]) -> bool:
    """Whether `builtin:<name>` waits for human approval under `toolkits:`."""

    entry = builtin_entry(reference)
    return entry is not None and entry.approval_for(toolkits)


def builtin_risk(reference: str, toolkits: Mapping[str, Any]) -> ToolRisk | None:
    """The risk of `builtin:<name>` under `toolkits:`, or `None` if it is unknown."""

    entry = builtin_entry(reference)
    return entry.risk_for(toolkits) if entry is not None else None


def build_builtin(reference: str, toolkits: Mapping[str, Any], *, gated: bool) -> BaseTool:
    """Create the builtin tool `reference` (`builtin:<name>`) from the blueprint's `toolkits:`.

    Args:
        gated: A blueprint approval wraps the tool, so its own default approval is turned off
            (a call must not pause twice).

    Raises:
        BlueprintError: If the tool is unknown, misconfigured or needs a missing extra.
    """

    entry = builtin_entry(reference)
    if entry is None:
        raise BlueprintError(f"Unknown builtin tool {reference!r}")
    try:
        return entry.create(toolkits, requires_approval=False if gated else None)
    except AwaError as exc:
        raise BlueprintError(f"Cannot build {reference!r}: {exc}") from exc


def is_connector_reference(text: str) -> bool:
    """Return whether `text` names connector tools (`connector:<name>[.<operation>]`)."""

    return text.startswith(CONNECTOR_PREFIX)


def connector_options(
    instance: str, connectors: Mapping[str, Any], base: Path | None = None
) -> dict[str, Any]:
    """The options of a connector instance in `connectors:`, relative paths resolved."""

    options = dict(connectors.get(instance) or {})
    for key in _CONNECTOR_PATH_OPTIONS:
        value = options.get(key)
        if (
            base is not None
            and isinstance(value, str)
            and not value.startswith(("https://", "http://"))
            and not Path(value).is_absolute()
        ):
            options[key] = str(in_case_dir(base, value))
    return options


def connector_operations(
    reference: str, connectors: Mapping[str, Any], base: Path | None = None
) -> tuple[str, list[Any]] | None:
    """The connector instance and operations a reference stands for (`connector:<name>`: its read operations).

    `None` when the operations are only known at run time (OpenAPI URL, MCP) or the reference
    cannot be resolved.
    """

    from awa.connectors._catalog import connector_context, split_reference

    instance, operation = split_reference(reference)
    try:
        ctx = connector_context(
            instance, env={}, config=connector_options(instance, connectors, base)
        )
        if ctx.spec.dynamic:
            return None
        if operation is None:
            return instance, [op for op in ctx.operations() if not op.write]
        return instance, [ctx.spec.operation(operation)]
    except AwaError:
        return None


def connector_issues(
    reference: str, connectors: Mapping[str, Any], base: Path | None = None
) -> list[str]:
    """Why `connector:<name>[.<operation>]` cannot be built (offline; env vars not checked)."""

    from awa.connectors._catalog import config_problems, connector_context, split_reference

    instance, operation = split_reference(reference)
    options = connector_options(instance, connectors, base)
    problems = config_problems(instance, options)
    if problems or operation is None:
        return problems
    try:
        ctx = connector_context(instance, env={}, config=options)
        if not ctx.spec.dynamic:
            ctx.spec.operation(operation)
    except AwaError as exc:
        return [str(exc)]
    return []


def connector_requires_approval(
    reference: str, connectors: Mapping[str, Any], base: Path | None = None
) -> bool:
    """Whether a tool of `reference` waits for approval by default (a write, not opted out)."""

    resolved = connector_operations(reference, connectors, base)
    if resolved is None:
        return False
    instance, operations = resolved
    enabled = (connectors.get(instance) or {}).get("requires_approval", True) is not False
    return enabled and any(op.write for op in operations)


def connector_missing(
    reference: str, connectors: Mapping[str, Any], base: Path | None = None
) -> tuple[str, ...]:
    """Names of the variables the instance of `reference` needs but that are not set
    (the process environment and `.env` in the working directory)."""

    from awa.connectors._catalog import connector_context, split_reference

    instance, _ = split_reference(reference)
    try:
        return connector_context(
            instance, config=connector_options(instance, connectors, base)
        ).missing()
    except AwaError:
        return ()


def build_connector(
    reference: str,
    connectors: Mapping[str, Any],
    *,
    base: Path | None = None,
    ungated: frozenset[str] = frozenset(),
) -> list[BaseTool]:
    """The tools `connector:<name>[.<operation>]` stands for (`<name>`: its read tools).

    Args:
        base: Folder relative `root` and `spec` options resolve against.
        ungated: Operations a blueprint approval gates instead.

    Raises:
        BlueprintError: If the connector is unknown or misconfigured.
    """

    from awa.connectors._catalog import build_tools, connector_context, split_reference

    instance, operation = split_reference(reference)
    options = connector_options(instance, connectors, base)
    if operation is not None:
        options["include"] = [operation]
    else:
        options["read_only"] = True
    try:
        return build_tools(connector_context(instance, config=options, ungated=ungated))
    except AwaError as exc:
        raise BlueprintError(f"Cannot build {reference!r}: {exc}") from exc


def is_valid_import_reference(text: str) -> bool:
    """Whether `text` is `"package.module:attribute"` or `"path/to/file.py:attribute"`."""

    return bool(IMPORT_REFERENCE.fullmatch(text) or FILE_REFERENCE.fullmatch(text))


@contextmanager
def file_modules_scope() -> Iterator[None]:
    """Load Python files afresh inside the block and resolve stems among them only (one CLI command each)."""

    token = _scope.set({})
    try:
        yield
    finally:
        _scope.reset(token)


def load_module_file(path: str | Path) -> ModuleType:
    """Import a Python file once under a module name unique to its path, and under its plain stem.

    The file's folder goes on `sys.path` so it can import its neighbors. The plain stem is
    never taken from a module imported otherwise or from the standard library (a file named
    `json.py` leaves `json` alone).
    Inside a `file_modules_scope` a second file with the same stem makes the stem ambiguous;
    outside one the newest file keeps the name.

    Raises:
        BlueprintError: If the file does not exist or importing it fails.
    """

    resolved = Path(path).resolve()
    loaded = _loaded()
    cached = loaded.get(resolved)
    if cached is not None:
        return cached
    if not resolved.is_file():
        raise BlueprintError(f"Python file {str(path)!r} does not exist")
    folder = str(resolved.parent)
    if folder not in sys.path:
        sys.path.insert(0, folder)
    digest = hashlib.sha1(str(resolved).encode(), usedforsecurity=False).hexdigest()[:10]
    stem = re.sub(r"\W", "_", resolved.stem)
    name = f"awa_file_{stem}_{digest}"
    spec = importlib.util.spec_from_file_location(name, resolved)
    if spec is None or spec.loader is None:
        raise BlueprintError(f"Cannot load Python file {str(path)!r}")
    module = importlib.util.module_from_spec(spec)
    namesakes = [m for file, m in loaded.items() if file.stem == resolved.stem]
    previous = sys.modules.get(resolved.stem)
    claims_stem = resolved.stem not in sys.stdlib_module_names and (
        previous is None or previous.__name__.startswith("awa_file_")
    )
    sys.modules[name] = module
    if claims_stem:
        sys.modules[resolved.stem] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        if claims_stem and previous is not None:
            sys.modules[resolved.stem] = previous
        elif claims_stem:
            sys.modules.pop(resolved.stem, None)
        raise BlueprintError(f"Importing {str(path)!r} failed: {exc}") from exc
    if claims_stem and namesakes and _scope.get() is not None:
        # A second file with this name in one scope: the plain name is ambiguous
        # now. Outside a scope the newest file keeps the name, as a plain import would.
        sys.modules.pop(resolved.stem, None)
    loaded[resolved] = module
    return module


def allow_local_imports() -> None:
    """Put the working directory on `sys.path`, so blueprints can import local modules."""

    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)


def import_module_entry(entry: str) -> ModuleType:
    """Import a dotted module name, or a `.py` file; raises `BlueprintError` if that fails."""

    path = Path(entry)
    if entry.endswith(".py") or path.is_file():
        if not path.is_file():
            raise BlueprintError(f"Tools file {entry!r} does not exist")
        try:
            return load_module_file(path)
        except BlueprintError as exc:
            if exc.__cause__ is None:
                raise
            raise BlueprintError(
                f"Importing tools file {entry!r} failed: {exc.__cause__}"
            ) from exc.__cause__
    try:
        return importlib.import_module(entry)
    except ImportError as exc:
        raise BlueprintError(f"Cannot import tools module {entry!r}: {exc}") from exc


def tools_files(specs: str | Sequence[str], blueprint: str | os.PathLike[str]) -> list[str]:
    """The `tools.py` beside `blueprint` (when there is one), then `specs`."""

    listed = [specs] if isinstance(specs, str) else list(specs)
    beside = Path(blueprint).resolve().parent / "tools.py"
    return [str(beside), *listed] if beside.is_file() else listed


def read_project(
    source: str | os.PathLike[str], options: Mapping[str, Any]
) -> tuple[Blueprint, dict[str, Any]]:
    """The blueprint of `source` and `options` plus the `tools.py` and `skills/` beside it."""

    found = Blueprint.from_yaml(Path(source, "blueprint.yaml") if Path(source).is_dir() else source)
    completed = dict(options)
    if found.source is not None:
        completed.setdefault("skill_registry", skill_registry([], found.source))
        if "tools" not in completed and (specs := tools_files([], found.source)):
            completed["tools"] = load_tools(specs)
    return found, completed


def load_tools(specs: str | Sequence[str]) -> list[BaseTool | Any]:
    """Collect the tools of Python modules or `.py` files (the `--tools` option; comma-separated).

    A module's tools are its `BaseTool` attributes, the tools of its `Toolbox` attributes and of
    the `Toolbox` subclasses it defines that take no arguments, and the documented functions
    in its `__all__`.

    Raises:
        BlueprintError: If a module cannot be imported or defines no tools, or two tools share
            a name.
    """

    allow_local_imports()
    tools: dict[str, BaseTool | Any] = {}
    for spec in [specs] if isinstance(specs, str) else specs:
        for entry in filter(None, (part.strip() for part in spec.split(","))):
            for name, value in _module_tools(import_module_entry(entry), entry).items():
                if name in tools and tools[name] is not value:
                    raise BlueprintError(f"Two tools modules define a tool named {name!r}")
                tools[name] = value
    return list(tools.values())


_TOOL_TYPES = re.compile(r"\b(?:BaseTool|StructuredTool|Tool|Toolbox)\b")


def _makes_tools(function: Callable[..., Any]) -> bool:
    """Whether `function` is a tool factory: its return annotation names a tool type
    (`-> BaseTool`, `-> list[BaseTool]`, `-> Toolbox`)."""

    try:
        annotation = inspect.signature(function).return_annotation
    except (TypeError, ValueError):
        return False
    if annotation is inspect.Signature.empty:
        return False
    text = annotation if isinstance(annotation, str) else repr(annotation)
    return bool(_TOOL_TYPES.search(text))


def _module_tools(module: ModuleType, entry: str) -> dict[str, BaseTool | Callable[..., Any]]:
    from awa.tools import Toolbox

    found: dict[str, BaseTool | Callable[..., Any]] = {}

    def add(name: str, item: BaseTool | Callable[..., Any]) -> None:
        if name in found and found[name] is not item:
            raise BlueprintError(f"Tools module {entry!r} defines two tools named {name!r}")
        found[name] = item

    for value in list(vars(module).values()):
        if isinstance(value, BaseTool):
            add(value.name, value)
        elif isinstance(value, Toolbox):
            for item in value.tools():
                add(item.name, item)
        elif _is_local_toolbox(value, module, Toolbox):
            try:
                instance = value()
            except Exception as exc:  # noqa: BLE001 - reported, the other tools still load
                _logger.warning("Skipped toolbox %s: %s", value.__name__, exc)
                continue
            for item in instance.tools():
                add(item.name, item)
    for attribute in getattr(module, "__all__", ()):
        value = getattr(module, attribute, None)
        if inspect.isfunction(value) and inspect.getdoc(value) and value.__name__ not in found:
            if _makes_tools(value):  # a factory: call it in your own code, it is no tool itself
                _logger.info("Skipped %s: it returns tools, it is not one", value.__name__)
                continue
            add(value.__name__, value)
    if not found:
        shown = getattr(module, "__file__", None) or module.__name__
        raise BlueprintError(
            f"Tools module {shown!r} defines no tools (BaseTool attributes, "
            "toolboxes or documented functions listed in __all__)"
        )
    return found


def _is_local_toolbox(value: Any, module: ModuleType, toolbox: type[Any]) -> bool:
    if not inspect.isclass(value) or not issubclass(value, toolbox) or value is toolbox:
        return False
    names: list[str] = getattr(value, "tool_method_names")()  # noqa: B009 - typed as object
    return value.__module__ == module.__name__ and bool(names)


def skill_registry(
    directories: Sequence[str], blueprint: str | Path | None = None
) -> SkillRegistry:
    """The builtin skills, then those of `skills/` beside `blueprint`, then those of `directories`."""

    beside = Path(blueprint).resolve().parent / "skills" if blueprint is not None else None
    listed = [str(beside), *directories] if beside and beside.is_dir() else list(directories)
    return SkillRegistry.from_directory(*listed, include_builtin=True)


def import_object(reference: str, *, base: str | Path | None = None) -> Any:
    """Import the attribute a `module:attribute` or `path/to/file.py:attribute` reference names.

    A single name that is the stem of a loaded Python file names that file. Relative paths are
    tried against `base`, then the working directory, then the loaded files by name.

    Raises:
        BlueprintError: If the reference is malformed, ambiguous or cannot be imported.
    """

    file_match = FILE_REFERENCE.fullmatch(reference)
    if file_match is not None:
        target: Any = _file_module(file_match["path"], base, reference)
        attribute_path = file_match["attribute"]
    elif IMPORT_REFERENCE.fullmatch(reference):
        module_name, _, attribute_path = reference.partition(":")
        target = _loaded_stem(module_name, reference)
        if target is None:
            try:
                target = importlib.import_module(module_name)
            except ImportError as exc:
                raise BlueprintError(f"Cannot import {reference!r}: {exc}") from None
    else:
        raise BlueprintError(
            f"Invalid import reference {reference!r}: expected 'package.module:attribute' "
            "or 'path/to/file.py:attribute'"
        )
    try:
        for attribute in attribute_path.split("."):
            target = getattr(target, attribute)
    except AttributeError as exc:
        raise BlueprintError(f"Cannot import {reference!r}: {exc}") from None
    return target


def _loaded_stem(name: str, reference: str) -> ModuleType | None:
    """The loaded file a single-name module reference means, if it means one."""

    if "." in name:
        return None
    if _scope.get() is None and name in sys.modules:
        return None  # an imported module of that name wins outside a scope
    files = {file: module for file, module in _loaded().items() if file.stem == name}
    if len(files) > 1:
        listing = ", ".join(sorted(str(path) for path in files))
        raise BlueprintError(
            f"{reference!r} is ambiguous: several loaded files are named {name}.py ({listing}); "
            f"use a path reference such as './{name}.py:{reference.partition(':')[2]}'"
        )
    return next(iter(files.values()), None)


def _file_module(path: str, base: str | Path | None, reference: str) -> ModuleType:
    candidate = Path(path)
    if candidate.is_absolute():
        return load_module_file(candidate)
    folders = [Path.cwd()] if base is None else [Path(base), Path.cwd()]
    for folder in folders:
        if (folder / candidate).is_file():
            return load_module_file(folder / candidate)
    named = [module for file, module in _loaded().items() if file.name == candidate.name]
    if len(named) == 1:
        return named[0]
    where = f"{base} or the working directory" if base is not None else "the working directory"
    raise BlueprintError(f"Cannot import {reference!r}: no file {path!r} in {where}")


def import_callable(
    reference: str, *, allow_imports: bool, what: str, base: str | Path | None = None
) -> Callable[..., Any]:
    """Import a callable; raises `BlueprintError` if imports are disallowed or the target is not callable."""

    if not allow_imports:
        raise BlueprintError(
            f"{what} {reference!r} is an import reference; pass allow_imports=True to "
            "import it (only for trusted blueprints)"
        )
    target = import_object(reference, base=base)
    if not callable(target):
        raise BlueprintError(f"{what} {reference!r} is not callable")
    return target  # type: ignore[no-any-return]  # `callable()` does not narrow an object


def to_tool(value: Any, *, source: str) -> BaseTool:
    """Return `value` as a `BaseTool`; raises `BlueprintError` if it is neither a tool nor a documented function."""

    if isinstance(value, BaseTool):
        return value
    if callable(value):
        try:
            converted: BaseTool = as_langchain_tool(value)
        except ValueError as exc:
            raise BlueprintError(f"Cannot use {source} as a tool: {exc}") from None
        return converted
    raise BlueprintError(f"{source} is not a tool or a function (got {type(value).__name__})")


@dataclass
class ToolCatalog:
    """The tools a blueprint may name, keyed by the name blueprints use."""

    tools: dict[str, BaseTool] = field(default_factory=dict)

    @classmethod
    def from_source(cls, source: ToolSource | None) -> ToolCatalog:
        """Collect the tools of a name mapping or a list.

        Raises:
            BlueprintError: If an entry is not a tool or a documented function, or two list
                entries share a name.
        """

        catalog = cls()
        if source is None:
            return catalog
        if isinstance(source, Mapping):
            catalog.tools = {
                str(name): to_tool(value, source=f"tool {name!r}") for name, value in source.items()
            }
        else:
            for value in source:
                tool = to_tool(value, source=repr(value))
                if tool.name in catalog.tools:
                    raise BlueprintError(f"Two tools are named {tool.name!r}")
                catalog.tools[tool.name] = tool
        return catalog

    def describe(self) -> list[tuple[str, str]]:
        """Return `(name, description)` for every available tool, sorted by name."""

        return sorted((name, tool.description) for name, tool in self.tools.items())


def skill_names(skills: SkillSource | None) -> set[str]:
    """Return the names of the available skills (the builtin skills by default)."""

    if skills is None:
        return set(SkillRegistry.builtin().names())
    if isinstance(skills, SkillRegistry):
        return set(skills.names())
    return {str(name) for name in skills}
