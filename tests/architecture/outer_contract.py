"""The import-linter contract type `outer_boundary`: the core never imports an outer part.

The outer parts build on the core (serving, the web app, automation, the builder, Microsoft 365)
and are listed by module name: an entry names a module and everything below it (`awa.server`
covers `awa.server.jobs`), and an entry ending in `*` matches by name prefix
(`awa.team._network_*` covers `awa.team._network_workers`). Every other `awa` module is core.

A package facade may load a name from its home module on first use (`_HOMES = homes({...})`);
the import graph does not see that edge, so with `resolve_lazy_imports = True` an import of such
a name (`from awa.team import AgentNetwork`) counts as an import of its home module.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import grimp
from importlinter import Contract, ContractCheck, fields, output
from importlinter.application import contract_utils

SRC = Path(__file__).resolve().parents[2] / "src"


def is_outer(module: str, outer: Iterable[str]) -> bool:
    """True when an entry of `outer` names `module`, a package above it, or a prefix of it (`*`)."""

    for entry in outer:
        if entry.endswith("*"):
            if module.startswith(entry[:-1]):
                return True
        elif module == entry or module.startswith(f"{entry}."):
            return True
    return False


def read_source(module: str) -> str | None:
    """The source of `module` under src/, or `None` when it has no file there."""

    base = SRC.joinpath(*module.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return None


def lazy_homes(source: str) -> dict[str, str]:
    """The `{name: home module}` table of a facade's `_HOMES = homes({...})`, read from its source."""

    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "_HOMES" for target in node.targets
            )
            and isinstance(node.value, ast.Call)
            and node.value.args
        ):
            table: dict[str, str] = ast.literal_eval(node.value.args[0])
            return {name: home for home, names in table.items() for name in names.split()}
    return {}


def _runtime_imports(node: ast.AST) -> Iterator[ast.ImportFrom]:
    """The `from` imports in `node`, without those under `if TYPE_CHECKING:`."""

    if isinstance(node, ast.ImportFrom):
        yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.If) and "TYPE_CHECKING" in ast.unparse(child.test):
            children: list[ast.AST] = list(child.orelse)
        else:
            children = [child]
        for each in children:
            yield from _runtime_imports(each)


def lazy_imports(
    modules: Iterable[str], read: Callable[[str], str | None] = read_source
) -> list[tuple[str, str, int, str]]:
    """`(importer, home, line, text)` for each import of a name a facade loads from its home."""

    tables: dict[str, dict[str, str]] = {}
    found: list[tuple[str, str, int, str]] = []
    for importer in sorted(modules):
        text = read(importer)
        if text is None:
            continue
        for node in _runtime_imports(ast.parse(text)):
            if node.level or node.module is None:
                continue
            if node.module not in tables:
                facade = read(node.module)
                tables[node.module] = lazy_homes(facade) if facade else {}
            for alias in node.names:
                if home := tables[node.module].get(alias.name):
                    line = f"from {node.module} import {alias.name}"
                    found.append((importer, home, node.lineno, line))
    return found


class OuterBoundaryContract(Contract):
    """A core module does not import an outer module.

    Configuration options:
        - outer: the outer modules, as names or `*` prefixes.
        - ignore_imports: the recorded violations (`importer -> imported`).
        - resolve_lazy_imports: count an import of a lazily loaded facade name as an import of
          its home module (default false).
    """

    type_name = "outer_boundary"

    outer = fields.ListField(subfield=fields.StringField())
    ignore_imports = fields.SetField(subfield=fields.ImportExpressionField(), required=False)
    resolve_lazy_imports = fields.BooleanField(required=False, default=False)

    def check(self, graph: grimp.ImportGraph, verbose: bool) -> ContractCheck:
        if self.resolve_lazy_imports:
            for importer, home, number, text in lazy_imports(graph.modules):
                graph.add_import(
                    importer=importer, imported=home, line_number=number, line_contents=text
                )
        removal = contract_utils.remove_ignored_imports_and_report(
            graph=graph,
            ignore_imports=self.ignore_imports,  # type: ignore[arg-type]
            unmatched_alerting=contract_utils.AlertLevel.ERROR,
        )
        outer = list(self.outer)  # type: ignore[call-overload]
        found: list[dict[str, Any]] = []
        roots = self.session_options["root_packages"]
        links = [
            link
            for root in roots
            for expression in (f"{root}.** -> {root}.**", f"{root} -> {root}.**")
            for link in graph.find_matching_direct_imports(import_expression=expression)
        ]
        for link in links:
            importer, imported = link["importer"], link["imported"]
            if is_outer(imported, outer) and not is_outer(importer, outer):
                details = graph.get_import_details(importer=importer, imported=imported)
                found.append(
                    {
                        "importer": importer,
                        "imported": imported,
                        "line_numbers": [detail["line_number"] for detail in details],
                    }
                )
        return ContractCheck(
            kept=not found,
            warnings=list(removal.warnings),
            metadata={"violations": sorted(found, key=lambda v: (v["importer"], v["imported"]))},
            ignored_import_count=removal.ignored_import_count,
        )

    def render_broken_contract(self, check: ContractCheck) -> None:
        for violation in check.metadata["violations"]:
            lines = ", ".join(f"l.{number}" for number in violation["line_numbers"])
            output.print_error(
                f"{violation['importer']} -> {violation['imported']} ({lines})", bold=False
            )
        output.new_line()
        output.print(
            "Move the shared code into the core, look the outer part up through awa.plugins, "
            "or move the importing code into the outer part."
        )
