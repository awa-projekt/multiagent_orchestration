"""Shared helpers of the architecture guards: modules, imports and roles (stdlib `ast` only).

Nothing here imports `awa`: the guards read the source tree, so a broken import cannot hide a
violation. `roles.toml` is the one role table (see docs/develop/architecture.md).
"""

from __future__ import annotations

import ast
import fnmatch
import functools
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SRC = REPO / "src"
TESTS = REPO / "tests"

ROLES = (
    "domain",
    "protocol",
    "backend",
    "backend-kit",
    "mixed",
    "root",
    "testing",
    "seam",
    "facade",
)


@dataclass(frozen=True)
class Module:
    name: str  # awa.x.y
    rel: str  # awa/x/y.py (relative to src/)
    path: Path
    is_package: bool

    @property
    def package(self) -> str:
        """Top-level package or module under `awa` (`awa.x.y` -> `x`); `awa` for the facade."""

        parts = self.name.split(".")
        return parts[1] if len(parts) > 1 else "awa"

    @functools.cached_property
    def tree(self) -> ast.Module:
        return ast.parse(self.path.read_text(encoding="utf-8"), filename=str(self.path))

    def where(self, node: ast.AST) -> str:
        return f"src/{self.rel}:{getattr(node, 'lineno', 0)}"


@functools.cache
def modules() -> dict[str, Module]:
    found: dict[str, Module] = {}
    for path in sorted((SRC / "awa").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(SRC).with_suffix("").parts)
        is_package = parts[-1] == "__init__"
        name = ".".join(parts[:-1] if is_package else parts)
        found[name] = Module(name, path.relative_to(SRC).as_posix(), path, is_package)
    return found


# ------------------------------------------------------------------------------- roles


@functools.cache
def role_table() -> dict[str, Any]:
    with (HERE / "roles.toml").open("rb") as handle:
        return tomllib.load(handle)


def matching_overrides(module: Module) -> list[dict[str, str]]:
    return [o for o in role_table()["override"] if fnmatch.fnmatch(module.rel, o["path"])]


def role_of(module: Module) -> str | None:
    """The module's role; `None` when the table does not cover it (a guard failure)."""

    if module.rel.endswith("__init__.py"):
        return "facade"  # re-export modules: exempt from the direction rules
    found = matching_overrides(module)
    if found:
        return found[0]["role"]
    return role_table()["package"].get(module.package)


@functools.cache
def roles() -> dict[str, str]:
    return {name: role_of(m) or "?" for name, m in modules().items()}


# ----------------------------------------------------------------------------- imports


@dataclass(frozen=True)
class Edge:
    source: str  # importing module
    line: int
    kind: str  # "module" (runs at import), "lazy" (inside a function) or "typing"
    target: str  # an awa module name, or the dotted external name

    @property
    def internal(self) -> bool:
        return self.target == "awa" or self.target.startswith("awa.")

    def where(self) -> str:
        return f"src/{modules()[self.source].rel}:{self.line}"


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _walk(
    statements: list[ast.stmt], kind: str
) -> Iterator[tuple[ast.Import | ast.ImportFrom, str]]:
    for node in statements:
        if isinstance(node, ast.Import | ast.ImportFrom):
            yield node, kind
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield from _walk(node.body, "lazy")
        elif isinstance(node, ast.If) and _is_type_checking(node.test):
            yield from _walk(node.body, "typing")
            yield from _walk(node.orelse, kind)
        else:
            for field in ("body", "orelse", "finalbody"):
                yield from _walk(getattr(node, field, None) or [], kind)
            for handler in getattr(node, "handlers", None) or []:
                yield from _walk(handler.body, kind)
            for case in getattr(node, "cases", None) or []:
                yield from _walk(case.body, kind)


def _targets(node: ast.Import | ast.ImportFrom, module: Module) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level:
        package = module.name.split(".") if module.is_package else module.name.split(".")[:-1]
        base = package[: len(package) - (node.level - 1)]
        source = ".".join([*base, node.module] if node.module else base)
    else:
        source = node.module or ""
    known = modules()
    found = []
    for alias in node.names:  # `from awa.x import y`: y may be a submodule
        if alias.name.startswith("__"):
            continue
        sub = f"{source}.{alias.name}"
        found.append(sub if sub in known else source)
    return found or [source]


def _resolve(target: str) -> str:
    """The longest known awa module that `target` names (`awa.a.b.Thing` -> `awa.a.b`)."""

    if not (target == "awa" or target.startswith("awa.")):
        return target
    known = modules()
    while target not in known and "." in target:
        target = target.rsplit(".", 1)[0]
    return target


@functools.cache
def edges() -> tuple[Edge, ...]:
    found = {}  # one edge per (module, line, kind, target): `from x import (a, b)` is one import
    for module in modules().values():
        for node, kind in _walk(module.tree.body, "module"):
            for target in _targets(node, module):
                edge = Edge(module.name, node.lineno, kind, _resolve(target))
                found[edge] = None
    return tuple(found)


def strings(module: Module) -> Iterator[ast.Constant]:
    """String constants outside docstrings."""

    docstrings = {
        id(body[0].value)
        for node in ast.walk(module.tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and (body := node.body)
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    }
    for node in ast.walk(module.tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node


# ------------------------------------------------------------------------------- backend protocols


@dataclass(frozen=True)
class PortClass:
    qualname: str  # awa.automation.store.RunStore
    kind: str  # "protocol" or "abc"
    # public members that a class must have to satisfy the backend protocol
    members: tuple[str, ...]


def _is_abstract(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any("abstractmethod" in ast.unparse(d) for d in function.decorator_list)


def _signature(function: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return ("async " if isinstance(function, ast.AsyncFunctionDef) else "") + (
        f"{function.name}({ast.unparse(function.args)})"
    )


def base_names(node: ast.ClassDef) -> list[str]:
    return [ast.unparse(base).split("[")[0].split(".")[-1] for base in node.bases]


@functools.cache
def ports() -> tuple[PortClass, ...]:
    """Every `Protocol` class and every ABC with abstract methods, with its public members."""

    found = []
    for module in modules().values():
        for node in module.tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = base_names(node)
            functions = [
                f for f in node.body if isinstance(f, ast.FunctionDef | ast.AsyncFunctionDef)
            ]
            if "Protocol" in bases:
                kind = "protocol"
            elif "ABC" in bases and any(_is_abstract(f) for f in functions):
                kind = "abc"
            else:
                continue
            members = [  # `__call__` is the one member of a callable backend protocol such as `BuildEngine`
                _signature(f)
                for f in functions
                if f.name == "__call__" or not f.name.startswith("_")
            ]
            if kind == "protocol":  # attributes are members of a protocol too
                members += [
                    f"{s.target.id}: {ast.unparse(s.annotation)}"
                    for s in node.body
                    if isinstance(s, ast.AnnAssign)
                    and isinstance(s.target, ast.Name)
                    and not s.target.id.startswith("_")
                ]
            found.append(PortClass(f"{module.name}.{node.name}", kind, tuple(members)))
    return tuple(found)


def package_stability(package_init: Module | None) -> str:
    """`"preview"` when the package `__init__` declares `__stability__ = "preview"`, else stable."""

    if package_init is None:
        return "stable"
    for node in package_init.tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__stability__" for t in node.targets)
            and isinstance(node.value, ast.Constant)
        ):
            return str(node.value.value)
    return "stable"


def _exports(package_init: Module | None) -> set[str]:
    """The names in the `__all__` of a package `__init__`."""

    if package_init is None:
        return set()
    for node in package_init.tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
            and isinstance(node.value, ast.List | ast.Tuple)
        ):
            return {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    return set()


_PORT_DOORS = {"kernel": "contracts"}
"""A protocol defined in the kernel is keyed by the stable package that re-exports it."""


def port_snapshot() -> dict[str, dict[str, list[str]]]:
    """Members of every backend protocol, by package stability: `{"stable": {"awa.pkg.Protocol": [...]}, "preview": ...}`.

    Keyed by the package, not the defining module, so moving a backend protocol within its package (into a
    `ports` module) leaves the snapshot unchanged. This is the `ports` section of public_api.json.
    """

    inits = {m.package: m for m in modules().values() if m.name.count(".") == 1 and m.is_package}
    snapshot: dict[str, dict[str, list[str]]] = {"stable": {}, "preview": {}}
    for protocol in ports():
        package, name = protocol.qualname.split(".")[1], protocol.qualname.rsplit(".", 1)[1]
        if package.startswith("_") or name.startswith("_"):
            continue  # private packages (awa._compat) and private helper protocols are not public API
        private_module = any(part.startswith("_") for part in protocol.qualname.split(".")[2:-1])
        if protocol.kind == "abc" and private_module and name not in _exports(inits.get(package)):
            continue  # an unexported abstract base of a private module (the run API mixin) is no backend port
        package = _PORT_DOORS.get(package, package)
        section = snapshot[package_stability(inits.get(package))]
        assert f"awa.{package}.{name}" not in section, (
            f"two backend protocols named {name} in awa.{package}"
        )
        section[f"awa.{package}.{name}"] = list(protocol.members)
    return snapshot
