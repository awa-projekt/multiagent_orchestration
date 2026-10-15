"""The single-agent export: what it writes, that it is clean, and that it never mentions awa."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from _helpers.export_projects import run_exported_tests, write_project
from awa.exporter import ExportError, export

if TYPE_CHECKING:
    from awa.exporter._plan import Plan

KERNEL = Path(__file__).resolve().parents[2] / "src" / "awa" / "kernel"


@pytest.fixture
def exported_desk(desk: Path, tmp_path: Path) -> Path:
    out = tmp_path / "desk-export"
    export(desk, out)
    return out


def _tree(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_the_desk_export_has_the_documented_layout(exported_desk: Path) -> None:
    names = {p.relative_to(exported_desk).as_posix() for p in _tree(exported_desk)}

    assert {
        "pyproject.toml",
        "requirements.txt",
        "langgraph.json",
        "README.md",
        "NOTICE.md",
        "SHA256SUMS",
        "run.py",
        ".env.example",
        "src/desk/graph.py",
        "src/desk/settings.py",
        "src/desk/tools.py",
        "src/desk/approvals.py",
        "src/desk/conditions.py",
        "src/desk/limits.py",
        "src/desk/skill_loader.py",
        "src/desk/prompts/assistant.md",
        "src/desk/skills/refund-policy/SKILL.md",
        "src/desk/skills/refund-policy/reference.md",
        "tests/fakes.py",
        "tests/test_graph.py",
    } <= names
    assert "src/desk/schemas.py" not in names


def test_hidden_folders_and_caches_of_a_skill_are_not_exported(desk: Path, tmp_path: Path) -> None:
    skill = desk / "skills" / "refund-policy"
    for folder in (".git", "__pycache__"):
        (skill / folder).mkdir()
        (skill / folder / "leftover.txt").write_text("x", encoding="utf-8")
    out = tmp_path / "out"

    export(desk, out)

    names = {
        p.relative_to(out / "src/desk/skills/refund-policy").as_posix()
        for p in _tree(out / "src/desk/skills/refund-policy")
    }
    assert names == {"SKILL.md", "reference.md"}


def test_the_prompt_is_what_awa_renders_with_the_skill_catalog(exported_desk: Path) -> None:
    prompt = (exported_desk / "src/desk/prompts/assistant.md").read_text("utf-8")

    assert "Look the order up before you answer." in prompt
    assert "`refund-policy`" in prompt  # on_demand mode: the catalog names the skill
    assert "load_skill" in prompt


def test_the_settings_and_graph_carry_the_blueprint(exported_desk: Path) -> None:
    settings = (exported_desk / "src/desk/settings.py").read_text("utf-8")
    graph = (exported_desk / "src/desk/graph.py").read_text("utf-8")

    assert "anthropic:claude-sonnet-5-5" in settings
    assert "'temperature': 0.2" in settings
    assert "MAX_STEPS = 5" in settings  # the smaller of max_steps 6 and max_requests 5
    assert "MAX_TOOL_CALLS = 4" in settings
    assert "MAX_TOTAL_TOKENS = 100000" in settings
    assert "MAX_DURATION = 120.0" in settings
    assert "'amount >= 100'" in graph
    assert "'ship': None" in graph
    assert "delete_order" not in graph
    assert json.loads((exported_desk / "langgraph.json").read_text("utf-8"))["graphs"] == {
        "assistant": "./src/desk/graph.py:graph"
    }


@pytest.mark.parametrize(
    "text", ['Desk \U0001f4e6 for "orders"', "line\nbreak\t\x7f", "M\u00fcller"]
)
def test_any_description_is_a_valid_toml_string(text: str) -> None:
    from awa.exporter._render import _toml_string

    assert tomllib.loads(f"description = {_toml_string(text)}")["description"] == text


def test_the_project_files_are_valid(exported_desk: Path) -> None:
    project = tomllib.loads((exported_desk / "pyproject.toml").read_text("utf-8"))["project"]
    pins = (exported_desk / "requirements.txt").read_text("utf-8").split()
    env = (exported_desk / ".env.example").read_text("utf-8")

    assert project["name"] == "desk"
    assert project["version"] == "0.2.0"
    assert any(d.startswith("langchain-anthropic") for d in project["dependencies"])
    assert any(p.startswith("langgraph==") for p in pins)
    assert "ANTHROPIC_API_KEY=\n" in env
    assert [line for line in env.splitlines() if "=" in line and not line.endswith("=")] == [
        "# MODEL=provider:model"
    ]


def test_the_readme_says_the_export_is_one_way(exported_desk: Path) -> None:
    readme = (exported_desk / "README.md").read_text("utf-8")

    assert "one-way" in readme
    assert "Calls of `refund`, `ship` pause the run" in readme


def test_sha256sums_prove_the_copied_files_are_unchanged(exported_desk: Path) -> None:
    lines = (exported_desk / "SHA256SUMS").read_text("utf-8").splitlines()

    assert len(lines) >= 6
    for line in lines:
        digest, _, path = line.partition("  ")
        assert hashlib.sha256((exported_desk / path).read_bytes()).hexdigest() == digest
    assert (exported_desk / "src/desk/conditions.py").read_bytes() == (
        KERNEL / "conditions.py"
    ).read_bytes()


def test_the_tools_file_is_copied_unchanged(desk: Path, exported_desk: Path) -> None:
    assert (exported_desk / "src/desk/tools.py").read_bytes() == (desk / "tools.py").read_bytes()


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text("utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


def test_nothing_in_the_export_imports_or_names_awa(exported_desk: Path, structured: Path) -> None:
    other = exported_desk.parent / "classifier-export"
    export(structured, other)

    for root in (exported_desk, other):
        for path in _tree(root):
            if path.suffix == ".py":
                assert not [n for n in _imports(path) if n.split(".")[0] == "awa"], path
            if path.name in {"requirements.txt", "langgraph.json"}:
                assert "awa" not in path.read_text("utf-8").lower(), path
            if path.name == "pyproject.toml":
                dependencies = tomllib.loads(path.read_text("utf-8"))["project"]["dependencies"]
                assert not [d for d in dependencies if d.lower().startswith("awa")], path


def test_the_generated_code_is_lint_clean(exported_desk: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "E,F,I", "--ignore", "E501", "."],
        cwd=exported_desk,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, done.stdout[-1500:]


def test_the_generated_part_is_small(exported_desk: Path) -> None:
    lines = sum(
        len((exported_desk / name).read_text("utf-8").splitlines())
        for name in ("src/desk/graph.py", "src/desk/settings.py")
    )

    assert lines <= 150


def test_the_exported_tests_pass(exported_desk: Path, structured: Path, tmp_path: Path) -> None:
    other = tmp_path / "classifier-export"
    export(structured, other)

    for folder in (exported_desk, other):
        done = run_exported_tests(folder)
        assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-1000:]


def test_the_structured_output_is_generated_as_a_model(structured: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    export(structured, out)

    schemas = (out / "src/classifier/schemas.py").read_text("utf-8")

    assert "class Output(BaseModel):" in schemas
    assert "Literal['spam', 'ham']" in schemas
    assert "Strict()" in schemas


def test_check_renders_and_imports_but_writes_nothing(desk: Path, tmp_path: Path) -> None:
    out = tmp_path / "nothing"

    report = export(desk, out, check=True)

    assert not report.written
    assert "src/desk/graph.py" in report.files
    assert not out.exists()


def test_an_export_is_reproducible(desk: Path, tmp_path: Path) -> None:
    export(desk, tmp_path / "one")
    export(desk, tmp_path / "two")

    for path in _tree(tmp_path / "one"):
        other = tmp_path / "two" / path.relative_to(tmp_path / "one")
        assert path.read_bytes() == other.read_bytes()


def test_an_existing_folder_is_not_overwritten_without_force(desk: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    export(desk, out)

    with pytest.raises(ExportError, match="not empty"):
        export(desk, out)
    export(desk, out, force=True)


def test_a_project_without_a_blueprint_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="no blueprint"):
        export(tmp_path, tmp_path / "out")


def test_a_secret_in_the_prompt_stops_the_export(tmp_path: Path) -> None:
    key = "sk-ant-api03-" + "abcdefghijklmnopqrstuvwxyz0123456789"
    project = write_project(
        tmp_path / "leaky", f"name: leaky\nagents:\n  - name: a\n    instructions: 'key {key}'\n"
    )

    with pytest.raises(ExportError, match="secrets"):
        export(project, tmp_path / "out")

    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "name", ["json", "langchain", "tests", "re", "openai", "langchain-anthropic", "dotenv"]
)
def test_a_blueprint_name_that_cannot_be_a_package_is_refused(name: str, tmp_path: Path) -> None:
    project = write_project(
        tmp_path / "p", f"name: '{name}'\nagents:\n  - name: a\n    instructions: hi\n"
    )

    with pytest.raises(ExportError, match="cannot name a package"):
        export(project, tmp_path / "out")


@pytest.mark.parametrize(
    ("model", "names"),
    [
        ("google_genai:gemini-2.5-flash", ["GOOGLE_API_KEY="]),
        ("mistralai:mistral-large-latest", ["MISTRAL_API_KEY="]),
        ("ollama:llama3.2", ["OLLAMA_HOST="]),
        ("cohere:command-r", ["COHERE_API_KEY="]),
    ],
)
def test_the_env_example_names_the_provider_table_variables(model: str, names: list[str]) -> None:
    from awa.exporter._render import _env_example

    env = _env_example(cast("Plan", SimpleNamespace(model=model)))

    assert [line for line in env.splitlines() if not line.startswith("#")] == names


def test_generated_tests_compile_for_tool_names_that_are_not_identifiers() -> None:
    from awa.exporter._render import _tests

    plan = SimpleNamespace(
        package="desk",
        name="desk",
        sample_output=None,
        sample_calls={"send-mail": {"to": "a"}, "send_mail": {"to": "b"}},
    )
    source = _tests(cast("Plan", plan))

    tree = ast.parse(source)
    names = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert len(names) == len(set(names))
    assert "'send-mail'" in source


def test_a_graph_that_hangs_at_import_is_refused_in_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.exporter import _probe

    package = tmp_path / "src" / "stuck"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "graph.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(_probe, "IMPORT_SECONDS", 1)

    with pytest.raises(ExportError, match="did not import within 1 seconds"):
        _probe.import_graph(tmp_path, "stuck")
