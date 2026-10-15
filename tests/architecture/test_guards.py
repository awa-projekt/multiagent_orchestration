"""Architecture guards: measures at their baseline, the ratchet, no runtime locate, internal consumers."""

from __future__ import annotations

import ast
import fnmatch
import functools
import tomllib
from pathlib import Path

import pytest

from architecture import ratchet
from architecture.measures import MEASURES, Measure
from architecture.scan import SRC, modules, roles

# facade: the `__init__` of an ignored package (GT2 still flags a driver import in a facade)
DRIVER_ROLES = {"backend", "backend-kit", "mixed", "root", "testing", "seam", "facade"}


@pytest.mark.slow
@pytest.mark.parametrize("key", sorted(MEASURES))
def test_measure_stays_at_its_baseline(key: str) -> None:
    ratchet.check(key)


def test_baseline_lists_exactly_the_measures() -> None:
    baseline = ratchet.load()
    assert sorted(baseline["counts"]) == sorted(MEASURES)
    assert set(baseline["report_only"]) <= set(MEASURES)


def test_a_data_class_that_starts_like_an_adapter_name_is_not_counted_as_an_adapter() -> None:
    """`FileProfile` and `SqliteInfo` hold what awa.data learned; only a resource is a backend."""

    built = MEASURES["GT1.adapters_built_in_domain"].find()

    assert not [line for line in built if "FileProfile(" in line or "SqliteInfo(" in line]


def test_a_port_with_only_a_call_method_has_it_as_its_member() -> None:
    """`BuildEngine` and `DataStager` are called like functions; their signature is the backend protocol."""

    from architecture.scan import ports

    members = {p.qualname.rsplit(".", 1)[1]: p.members for p in ports()}

    assert members["BuildEngine"] and members["BuildEngine"][0].startswith("__call__(")


def test_ruff_driver_ignores_follow_the_role_table() -> None:
    """`src/ruff.toml` lists the files that may import a driver (rule R5); the role table says which
    files those are, and the guards GT2 and GT3 list the known violations (the work list)."""

    config = tomllib.loads((SRC / "ruff.toml").read_text(encoding="utf-8"))
    ignored = [
        pattern
        for pattern, codes in config["lint"]["per-file-ignores"].items()
        if "TID251" in codes
    ]
    known = {
        finding.split(":")[0].removeprefix("src/")
        for key in ("GT2.driver_in_domain", "GT3.behavioral")
        for finding in MEASURES[key].find()
    }
    problems = []
    for pattern in ignored:
        matched = [m for m in modules().values() if fnmatch.fnmatch(m.rel, pattern)]
        if not matched:
            problems.append(f"{pattern}: matches no module")
        problems += [
            f"{m.rel}: ignored by ruff, but its role is {roles()[m.name]!r} and it is no known violation"
            for m in matched
            if roles()[m.name] not in DRIVER_ROLES and m.rel not in known
        ]
    assert not problems, "\n".join(problems)


@pytest.fixture
def baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(ratchet, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(ratchet, "REPORT", [])
    ratchet.save({"X.count": 2, "X.report": 1}, ["X.report"])
    return tmp_path / "baseline.json"


def finds(monkeypatch: pytest.MonkeyPatch, key: str, found: list[str]) -> None:
    monkeypatch.setitem(MEASURES, key, Measure(lambda: found, "R0", "fix it"))


def test_the_baseline_count_passes(baseline: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    finds(monkeypatch, "X.count", ["a", "b"])

    ratchet.check("X.count")


def test_a_rise_fails_and_lists_the_findings(
    baseline: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finds(monkeypatch, "X.count", ["a", "b", "src/awa/new.py:3 the new one"])

    with pytest.raises(AssertionError, match=r"(?s)3 found, the baseline allows 2.*the new one"):
        ratchet.check("X.count")


def test_a_fall_fails_until_the_baseline_records_it(
    baseline: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finds(monkeypatch, "X.count", ["a"])

    with pytest.raises(AssertionError, match=r"1 found, below the baseline 2"):
        ratchet.check("X.count")


def test_a_report_only_key_never_fails_and_is_reported(
    baseline: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finds(monkeypatch, "X.report", ["a", "b", "c"])

    ratchet.check("X.report")

    assert ratchet.REPORT == [("X.report", 3, 1)]


AWA_SRC = Path(__file__).resolve().parents[2] / "src" / "awa"
FLAG_OWNERS = {AWA_SRC / "_compat" / "mcp.py", AWA_SRC / "_compat" / "mcp_server.py"}


def _names(path: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Name):
            found.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute):
            found.append((node.lineno, node.attr))
        elif isinstance(node, ast.alias):
            found.append((node.lineno if hasattr(node, "lineno") else 0, node.name))
    return found


@pytest.mark.parametrize(
    "path", sorted(AWA_SRC.rglob("*.py")), ids=lambda p: str(p.relative_to(AWA_SRC))
)
def test_no_locate_calls_and_no_capability_flags(path: Path) -> None:
    bad = []
    for line, name in _names(path):
        if (
            name in {"locate", "locate_optional"}
            or name.startswith("HAS_")
            and path not in FLAG_OWNERS
        ):
            bad.append((line, name))
    assert not bad, f"{path.relative_to(AWA_SRC)}: {bad}"


INTERNAL = AWA_SRC / "_internal"
MODULES = sorted(path.stem for path in INTERNAL.glob("*.py") if path.name != "__init__.py")


def _imported(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


@functools.cache
def _imports_of(path: Path) -> set[str]:
    return _imported(ast.parse(path.read_text(encoding="utf-8")))


def _consumers(module: str) -> set[str]:
    target = f"awa._internal.{module}"
    found: set[str] = set()
    for path in AWA_SRC.rglob("*.py"):
        relative = path.relative_to(AWA_SRC)
        package = relative.parts[0].removesuffix(".py")
        if package == "_internal" and relative.parts[1:2] != ("upstream",):
            continue  # the shared modules do not count for each other
        if target in _imports_of(path):
            found.add("_internal.upstream" if package == "_internal" else package)
    return found


@pytest.mark.parametrize("module", MODULES)
def test_an_internal_module_has_two_consumer_packages(module: str) -> None:
    consumers = _consumers(module)

    assert len(consumers) >= 2, f"awa._internal.{module} is used by {sorted(consumers)}"


ENGINE_NAMES = {
    "create_agent",
    "interrupt",
    "GraphInterrupt",
    "GraphBubbleUp",
    "GraphRecursionError",
    "var_child_runnable_config",
}
ENGINE_SIDE = (
    "_internal/upstream/*",
    "runtime/_langgraph.py",
    "runtime/_streaming.py",
    "exporter/*",
    "kernel/*",
    "agents/graph_agent.py",  # wraps a compiled LangGraph graph by definition
    "persistence/records.py",
    "testing/contracts/checkpointer.py",
    "testing/mas_scenarios.py",
)


def test_engine_primitives_are_reached_through_the_engine_port() -> None:
    """Building, pausing and stopping go through `awa.runtime.engine`, so the engine can change."""

    found = []
    for path in sorted(AWA_SRC.rglob("*.py")):
        relative = path.relative_to(AWA_SRC).as_posix()
        if any(fnmatch.fnmatch(relative, pattern) for pattern in ENGINE_SIDE):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                found += [f"{relative}: {a.name}" for a in node.names if a.name in ENGINE_NAMES]
            elif isinstance(node, ast.Attribute) and node.attr in ENGINE_NAMES:
                found.append(f"{relative}: .{node.attr}")
    assert not found, found
