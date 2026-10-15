"""The import contracts of `.importlinter` hold: layer order, private modules, outer boundary.

The contracts are the same that `lint-imports` checks from the repository root. Their
`ignore_imports` lists are the recorded baseline: a new violation fails here, and so does a listed
import that is gone (delete its line).
"""

from __future__ import annotations

import importlib

import grimp
import pytest
from importlinter.application.use_cases import lint_imports

from architecture.import_contracts import (
    CONFIG,
    LAYER_NAMES,
    PrivateModulesContract,
    is_private,
    layer_lines,
    layer_of_package,
)
from architecture.scan import REPO, SRC


def test_the_import_contracts_hold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.syspath_prepend(str(REPO))  # the contract type lives in tests/architecture
    monkeypatch.chdir(REPO)

    passed = lint_imports(str(CONFIG), cache_dir=None, no_logo=True)

    assert passed, capsys.readouterr().out


def test_every_top_level_package_has_a_layer() -> None:
    placed = {name for line in layer_lines() for name in line}
    actual = {
        f"awa.{path.stem}" if path.is_file() else f"awa.{path.name}"
        for path in (SRC / "awa").iterdir()
        if (path.suffix == ".py" and path.stem != "__init__")
        or (path.is_dir() and any(path.rglob("*.py")))
    }

    assert not actual - placed, f"give these a layer in .importlinter: {sorted(actual - placed)}"


def test_the_layers_run_from_the_interfaces_to_the_foundation() -> None:
    layers = layer_of_package()

    assert layers["awa.cli"] == layers["awa.server"] == layers["awa.automation"] == "Interfaces"
    assert layers["awa.blueprints"] == layers["awa.evals"] == "Compiler"
    assert layers["awa.connectors"] == layers["awa.toolkits"] == "Integrations"
    assert layers["awa.team"] == layers["awa.messaging"] == "Orchestration"
    assert layers["awa.agents"] == layers["awa.memory"] == layers["awa.tools"] == "Runtime"
    assert layers["awa.core"] == layers["awa.contracts"] == layers["awa._compat"] == "Foundation"
    assert list(dict.fromkeys(layers.values())) == list(LAYER_NAMES.values())


def _check(
    *links: tuple[str, str], ignore: tuple[str, ...] = ()
) -> tuple[bool, list[tuple[str, str]], int]:
    graph = grimp.ImportGraph()
    for importer, imported in links:
        graph.add_import(
            importer=importer, imported=imported, line_number=1, line_contents="import x"
        )
    options = {"shared": ["awa._compat"], **({"ignore_imports": list(ignore)} if ignore else {})}
    contract = PrivateModulesContract("private", {"root_packages": ["awa"]}, options)
    check = contract.check(graph, verbose=False)
    found = [(v["importer"], v["imported"]) for v in check.metadata["violations"]]
    return check.kept, found, check.ignored_import_count


def test_a_private_module_of_another_package_is_a_violation() -> None:
    kept, found, _ = _check(
        ("awa.cli._run", "awa.blueprints._build"),
        ("awa.cli._run", "awa.blueprints"),
        ("awa.blueprints.schema", "awa.blueprints._build"),
        ("awa.tools.base", "awa._compat.mcp"),
        ("awa.tools.base", "awa.core._util"),
    )

    assert not kept
    assert found == [
        ("awa.cli._run", "awa.blueprints._build"),
        ("awa.tools.base", "awa.core._util"),
    ]


def test_a_recorded_violation_is_ignored_and_a_stale_record_fails() -> None:
    kept, found, ignored = _check(
        ("awa.cli._run", "awa.blueprints._build"), ignore=("awa.cli._run -> awa.blueprints._build",)
    )
    assert (kept, found, ignored) == (True, [], 1)

    from importlinter.domain.helpers import MissingImport

    with pytest.raises(MissingImport):
        _check(("awa.cli._run", "awa.blueprints"), ignore=("awa.cli._run -> awa.blueprints._x",))


@pytest.mark.parametrize(
    ("module", "private"),
    [
        ("awa.blueprints._build", True),
        ("awa.blueprints.compile._plan", True),
        ("awa.connectors._microsoft.mail", True),
        ("awa.blueprints.schema", False),
        ("awa._compat.mcp", False),
        ("awa", False),
    ],
)
def test_which_modules_are_private(module: str, private: bool) -> None:
    assert is_private(module) is private


def test_lint_imports_finds_the_contract_type_from_the_repository_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`lint-imports` puts the working directory on `sys.path` and imports the contract type."""

    monkeypatch.syspath_prepend(str(REPO))
    module = importlib.import_module("tests.architecture.import_contracts")

    assert module.PrivateModulesContract.type_name == "private_modules"
