"""The import-linter contract type `private_modules`, and the layers as `.importlinter` lists them.

A module whose name has a part starting with `_` below its top-level package
(`awa.blueprints._build`, `awa.core._util`) is private to that package: only modules of the same
top-level package import it. Top-level private packages listed in `shared` (`awa._compat`) are
the internal foundation every package may use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import grimp
from importlinter import Contract, ContractCheck, fields, output
from importlinter.api import read_configuration
from importlinter.application import contract_utils

CONFIG = Path(__file__).resolve().parents[2] / ".importlinter"


def _package(module: str) -> str:
    """`awa.x.y` -> `awa.x`; the root package for `awa`."""

    return ".".join(module.split(".")[:2])


def is_private(module: str) -> bool:
    """True for a module below a top-level package that has a part starting with `_`."""

    return any(part.startswith("_") for part in module.split(".")[2:])


class PrivateModulesContract(Contract):
    """Private modules of a package are imported only from inside that package.

    Configuration options:
        - shared: top-level private packages every package may import (`awa._compat`).
        - ignore_imports: the recorded violations (`importer -> imported`).
    """

    type_name = "private_modules"

    shared = fields.ListField(subfield=fields.ModuleField(), required=False)
    ignore_imports = fields.SetField(subfield=fields.ImportExpressionField(), required=False)

    def check(self, graph: grimp.ImportGraph, verbose: bool) -> ContractCheck:
        removal = contract_utils.remove_ignored_imports_and_report(
            graph=graph,
            ignore_imports=self.ignore_imports,  # type: ignore[arg-type]
            unmatched_alerting=contract_utils.AlertLevel.ERROR,
        )
        shared = {module.name for module in self.shared or []}  # type: ignore[attr-defined]
        found: list[dict[str, Any]] = []
        for root in self.session_options["root_packages"]:
            for link in graph.find_matching_direct_imports(
                import_expression=f"{root}.** -> {root}.**"
            ):
                importer, imported = link["importer"], link["imported"]
                if _package(importer) == _package(imported) or _package(imported) in shared:
                    continue
                if is_private(imported):
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
            "Import the name from the package's public module instead, or move the shared code "
            "to awa._compat."
        )


# ------------------------------------------------------------------ the layers contract

LAYER_NAMES = {
    "awa.cli": "Interfaces",
    "awa.exporter": "Compiler",
    "awa.connectors": "Integrations",
    "awa.team": "Orchestration",
    "awa.agents": "Runtime",
    "awa.config": "Foundation",
}
"""The first package of each layer, top to bottom."""


def layer_lines() -> list[list[str]]:
    """The `layers` of the layers contract, top first, as lists of package names."""

    [contract] = [
        c for c in read_configuration(str(CONFIG))["contracts_options"] if c["type"] == "layers"
    ]
    lines: list[list[str]] = []
    for line in contract["layers"]:
        names = line.replace("|", ":").split(":")
        lines.append([name.strip() for name in names])
    return lines


def layer_of_package() -> dict[str, str]:
    """`awa.x` -> Interfaces, Compiler, Integrations, Orchestration, Runtime or Foundation."""

    found: dict[str, str] = {}
    current = ""
    for line in layer_lines():
        current = LAYER_NAMES.get(line[0], current)
        found.update(dict.fromkeys(line, current))
    return found
