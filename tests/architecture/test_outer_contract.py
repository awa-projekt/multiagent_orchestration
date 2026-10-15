"""The `outer_boundary` contract type: a core module importing an outer module is a violation."""

from __future__ import annotations

import configparser
import importlib

import grimp
import pytest

from architecture.outer_contract import OuterBoundaryContract, is_outer, lazy_imports
from architecture.scan import REPO, modules

OUTER = ["awa.server", "awa.team.network", "awa.team._network_*", "awa.checks"]


def _check(
    *links: tuple[str, str], ignore: tuple[str, ...] = ()
) -> tuple[bool, list[tuple[str, str]], int]:
    graph = grimp.ImportGraph()
    for importer, imported in links:
        graph.add_import(
            importer=importer, imported=imported, line_number=1, line_contents="import x"
        )
    options = {"outer": OUTER, **({"ignore_imports": list(ignore)} if ignore else {})}
    contract = OuterBoundaryContract("outer", {"root_packages": ["awa"]}, options)
    check = contract.check(graph, verbose=False)
    found = [(v["importer"], v["imported"]) for v in check.metadata["violations"]]
    return check.kept, found, check.ignored_import_count


@pytest.mark.parametrize(
    ("module", "outer"),
    [
        ("awa.server", True),
        ("awa.server._config", True),
        ("awa.team.network", True),
        ("awa.team._network_workers", True),
        ("awa.checks.microsoft", True),
        ("awa.team", False),
        ("awa.team.team", False),
        ("awa.serverless", False),
        ("awa.team.networks", False),
    ],
)
def test_which_modules_are_outer(module: str, outer: bool) -> None:
    assert is_outer(module, OUTER) is outer


def test_only_a_core_module_importing_an_outer_module_is_a_violation() -> None:
    kept, found, _ = _check(
        ("awa.cli._run", "awa.server._config"),
        ("awa.team", "awa.team.network"),
        ("awa.server._app", "awa.team.network"),
        ("awa.team.network", "awa.team._network_workers"),
        ("awa.server._app", "awa.team.team"),
        ("awa.checks.connect", "awa.server"),
    )

    assert not kept
    assert found == [("awa.cli._run", "awa.server._config"), ("awa.team", "awa.team.network")]


def test_an_import_by_the_root_package_is_checked_too() -> None:
    kept, found, _ = _check(("awa", "awa.server._config"), ("awa", "awa.team"))

    assert not kept
    assert found == [("awa", "awa.server._config")]


def test_an_import_of_a_lazily_loaded_name_counts_as_an_import_of_its_home() -> None:
    sources = {
        "awa.core_part": (
            "from typing import TYPE_CHECKING\n"
            "from awa.facade import Early, Late\n"
            "if TYPE_CHECKING:\n"
            "    from awa.facade import Hidden\n"
        ),
        "awa.facade": '_HOMES = homes({"awa.outer_part": "Late Hidden"})\nEarly = 1\n',
    }

    assert lazy_imports(["awa.core_part"], sources.get) == [
        ("awa.core_part", "awa.outer_part", 2, "from awa.facade import Late")
    ]


def test_every_outer_entry_names_a_module() -> None:
    config = configparser.ConfigParser()
    config.read(REPO / ".importlinter", encoding="utf-8")
    entries = config["importlinter:contract:outer"]["outer"].split()

    assert entries
    assert [e for e in entries if not any(is_outer(name, [e]) for name in modules())] == []


def test_a_recorded_violation_is_ignored_and_a_stale_record_fails() -> None:
    kept, found, ignored = _check(
        ("awa.cli._mcp", "awa.server.mcp"), ignore=("awa.cli._mcp -> awa.server.mcp",)
    )
    assert (kept, found, ignored) == (True, [], 1)

    from importlinter.domain.helpers import MissingImport

    with pytest.raises(MissingImport):
        _check(("awa.cli._mcp", "awa.server"), ignore=("awa.cli._mcp -> awa.server.mcp",))


def test_lint_imports_finds_the_contract_type_from_the_repository_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(REPO))
    module = importlib.import_module("tests.architecture.outer_contract")

    assert module.OuterBoundaryContract.type_name == "outer_boundary"
