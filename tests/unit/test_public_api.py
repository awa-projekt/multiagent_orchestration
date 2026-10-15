"""The top-level `awa` API, one version number everywhere, and no other agent product named in the library."""

from __future__ import annotations

import importlib
import importlib.metadata
import re
import tomllib
from pathlib import Path

import pytest

import awa

LAYERS = {
    "Agents": [
        "Agent",
        "AgentHooks",
        "tool",
        "Skill",
        "SkillRegistry",
        "AgentMemory",
        "Guardrail",
        "ModelSettings",
    ],
    "Multi-agent": [
        "Team",
        "TeamLimits",
        "AgentNetwork",
        "NetworkLimits",
        "Sequential",
        "Parallel",
        "Loop",
        "Blackboard",
        "TaskBoard",
        "AgentDirectory",
    ],
    "Running": [
        "RunContext",
        "current_run_context",
        "RunResult",
        "RunEvent",
        "UsageLimits",
        "AgentLike",
        "Settings",
        "setup",
    ],
    "Blueprints": ["Blueprint", "load", "aload", "quick"],
    "Errors": [
        "AwaError",
        "AgentRunError",
        "UsageLimitExceededError",
        "GuardrailTripwireError",
        "RunCancelledError",
        "RunTimeoutError",
        "BlueprintError",
    ],
}


def test_all_is_the_grouped_api_plus_version() -> None:
    expected = [name for names in LAYERS.values() for name in names] + ["__version__"]

    assert awa.__all__ == expected


@pytest.mark.parametrize("name", [name for names in LAYERS.values() for name in names])
def test_every_public_name_resolves_to_its_home(name: str) -> None:
    value = getattr(awa, name)

    home = importlib.import_module(awa._LAZY[name])
    assert value is (home if name == "quick" else getattr(home, name))
    assert name in dir(awa)


def test_version_is_a_string() -> None:
    assert isinstance(awa.__version__, str) and awa.__version__


def test_unknown_name_suggests_close_matches() -> None:
    with pytest.raises(AttributeError, match=r"did you mean 'Agent'"):
        _ = awa.Agnt


def test_demoted_names_are_not_top_level() -> None:
    for name in ("BaseTool", "AuditLog", "CircuitBreaker", "design_project", "MCPToolset"):
        assert not hasattr(awa, name)
        assert name not in dir(awa)


def test_top_level_has_37_names_plus_version() -> None:
    assert len(awa.__all__) == 38


TOP_LEVEL_CEILING = 37
"""Names in `awa.__all__` besides `__version__`; it only comes down (the aim is 30 or fewer)."""


def test_the_top_level_stays_small() -> None:
    names = [name for name in awa.__all__ if name != "__version__"]

    assert len(names) <= TOP_LEVEL_CEILING, (
        f"awa.__all__ has {len(names)} names, at most {TOP_LEVEL_CEILING}: a new name belongs in "
        "its package's door"
    )


def test_no_top_level_name_is_also_a_subpackage() -> None:
    """`awa.build` the function and `awa.build` a package cannot both exist: whichever is
    imported last would win."""

    import pkgutil

    packages = {info.name for info in pkgutil.iter_modules(awa.__path__)}

    assert set(awa.__all__) & packages <= {"quick"}  # awa.quick is the module itself


@pytest.mark.slow
def test_importing_every_subpackage_hides_no_top_level_name() -> None:
    """Import every module of awa, as the command line does, then use each top-level name."""

    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        """
        import importlib, os, types, warnings
        import awa

        before = {name: getattr(awa, name) for name in awa.__all__ if name != "__version__"}
        root = os.path.dirname(awa.__file__)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for folder, _dirs, files in os.walk(root):
                # runtime_src holds the files an export copies, not modules of awa.
                if "__pycache__" in folder or "runtime_src" in folder:
                    continue
                package = os.path.relpath(folder, os.path.dirname(root)).replace(os.sep, ".")
                for file in files:
                    if not file.endswith(".py") or file == "__main__.py":
                        continue
                    name = package if file == "__init__.py" else f"{package}.{file[:-3]}"
                    try:
                        importlib.import_module(name)
                    except ImportError as error:  # only an optional extra may be missing
                        if (error.name or "awa").split(".")[0] == "awa":
                            raise
        hidden = [
            name
            for name, value in before.items()
            if getattr(awa, name) is not value
            or (isinstance(getattr(awa, name), types.ModuleType) and name != "quick")
        ]
        assert not hidden, f"hidden by a subpackage: {hidden}"
        assert callable(awa.load) and callable(awa.aload) and callable(awa.tool)
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=600
    )

    assert result.returncode == 0 and result.stdout.strip().endswith("ok"), result.stderr[-3000:]


SRC = Path(__file__).resolve().parents[2] / "src" / "awa"
PRODUCTS = re.compile(
    r"crew[ -]?ai|pydantic[ -]?ai|deep[ -]?agents|llama[ -]?index|auto[ -]?gen", re.I
)


def test_no_source_file_names_another_agent_product() -> None:
    found = [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1)
        if PRODUCTS.search(line)
    ]

    assert not found, found


ROOT = Path(__file__).resolve().parents[2]


def test_the_version_is_the_one_in_pyproject() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    assert awa.__version__ == importlib.metadata.version("awa-agents") == declared
    assert re.fullmatch(r"\d+\.\d+\.\d+((a|b|rc)\d+)?", declared)
