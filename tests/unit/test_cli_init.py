"""`awa init` templates and the `awa new process` wizard."""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from _helpers.polling import SLOW_SUBPROCESS_TIMEOUT
from awa import Team
from awa.blueprints import Blueprint
from awa.blueprints._resolve import load_tools
from awa.blueprints.scaffold import PROJECT_FILES, TEMPLATES, create_project, render
from awa.blueprints.wizard import create_process, process_blueprint, run_wizard
from awa.cli import build_parser, main
from awa.errors import ConfigurationError, ScaffoldError
from awa.evals import Dataset, parse_scorer

_SECRETS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "LANGSMITH_API_KEY")


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loading a project's `tools.py` must not leak into other tests."""

    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "tools", raising=False)


def load_project_tools(project: Path) -> list[Any]:
    tools = load_tools([str(project / "tools.py")])
    sys.modules.pop("tools", None)
    return tools


def run_project_tests(project: Path) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key not in _SECRETS}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=SLOW_SUBPROCESS_TIMEOUT,
    )


def assert_valid_project(project: Path) -> Blueprint:
    for relative in PROJECT_FILES:
        assert (project / relative).is_file(), relative
    blueprint = Blueprint.from_yaml(project / "blueprint.yaml")
    assert blueprint.name == project.name
    issues = blueprint.check(tools=load_project_tools(project))
    assert [issue for issue in issues if issue.severity == "error"] == []
    dataset = Dataset.load(project / "evals" / "cases.yaml")
    assert dataset.cases
    for case in dataset.cases:
        for spec in case.scorers:
            parse_scorer(spec)
    readme = (project / "README.md").read_text(encoding="utf-8")
    for command in ("awa validate", "awa chat", "awa run", "awa serve", "awa eval", "pytest"):
        assert command in readme
    assert "{{" not in readme
    pyproject = (project / "pyproject.toml").read_text(encoding="utf-8")
    assert "dependencies = []" in pyproject and "awa-agents" not in pyproject
    assert "awa-agents" not in readme and "pip install awa" not in readme
    return blueprint


# --------------------------------------------------------------------------- templates


@pytest.mark.slow
@pytest.mark.parametrize("template", sorted(TEMPLATES))
def test_every_template_scaffolds_a_project_whose_tests_pass(
    template: str, tmp_path: Path, isolated: None, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / f"desk-{template}"

    assert main(["init", str(project), "--template", template]) == 0

    out = capsys.readouterr().out
    assert f"(template {template})" in out
    steps = out.split("next:")[1]
    assert "awa chat blueprint.yaml" in steps and "--tools" not in steps
    assert "ANTHROPIC" not in steps  # no provider is assumed
    keyless = steps.index("\n  pytest ")
    assert steps.index("pip install pytest") < keyless < steps.index("awa setup")
    assert steps.index("awa setup") < steps.index("awa chat")
    blueprint = assert_valid_project(project)
    if template != "workflow":
        assert blueprint.approvals, "templates with a conversation gate one action"
    completed = run_project_tests(project)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert " passed" in completed.stdout


def test_the_default_template_is_minimal(tmp_path: Path, isolated: None) -> None:
    assert main(["init", str(tmp_path / "helper")]) == 0

    blueprint = Blueprint.from_yaml(tmp_path / "helper" / "blueprint.yaml")
    assert [agent.name for agent in blueprint.agents] == ["assistant"]
    assert blueprint.defaults.model == "default"


def test_validate_accepts_the_generated_project(
    tmp_path: Path, isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_project(tmp_path / "my-desk", template="support")
    monkeypatch.chdir(tmp_path / "my-desk")

    assert main(["validate", "blueprint.yaml", "--tools", "tools.py"]) == 0


def test_model_option_sets_the_blueprint_default(tmp_path: Path) -> None:
    assert main(["init", str(tmp_path / "fast-desk"), "--model", "anthropic:claude-x"]) == 0

    blueprint = Blueprint.from_yaml(tmp_path / "fast-desk" / "blueprint.yaml")
    assert blueprint.defaults.model == "anthropic:claude-x"


def test_init_into_the_current_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "here").mkdir()
    monkeypatch.chdir(tmp_path / "here")

    assert main(["init", ".", "--template", "team"]) == 0
    assert Blueprint.from_yaml(tmp_path / "here" / "blueprint.yaml").name == "here"


# --------------------------------------------------------------------------- safety


def test_existing_files_are_not_overwritten_without_force(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "desk"
    project.mkdir()
    (project / "tools.py").write_text("# mine\n", encoding="utf-8")
    (project / "notes.txt").write_text("keep", encoding="utf-8")

    assert main(["init", str(project)]) == 1
    err = capsys.readouterr().err
    assert "already contains tools.py" in err
    assert "--force" in err
    assert (project / "tools.py").read_text(encoding="utf-8") == "# mine\n"
    assert not (project / "blueprint.yaml").exists()

    assert main(["init", str(project), "--force"]) == 0
    assert "def lookup_account" in (project / "tools.py").read_text(encoding="utf-8")
    assert (project / "notes.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("name", ["1desk", "my desk", "team", "a" * 49, "desk.v2"])
def test_invalid_project_names_are_rejected(
    tmp_path: Path, name: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", str(tmp_path / name)]) == 1
    assert "Invalid project name" in capsys.readouterr().err
    assert not (tmp_path / name / "blueprint.yaml").exists()


def test_invalid_models_and_templates_are_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", str(tmp_path / "desk"), "--model", "not a model"]) == 1
    assert "Invalid model" in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main(["init", str(tmp_path / "desk"), "--template", "nope"])
    with pytest.raises(ScaffoldError, match="Unknown template"):
        create_project(tmp_path / "desk", template="nope")
    assert not (tmp_path / "desk").exists()


def test_a_file_in_place_of_the_directory_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "desk").write_text("", encoding="utf-8")

    with pytest.raises(ScaffoldError, match="not a directory"):
        create_project(tmp_path / "desk")


def test_render_substitutes_known_placeholders_only() -> None:
    assert render("name: {{name}} / {{ model }}", {"name": "x", "model": "m"}) == "name: x / m"
    with pytest.raises(ScaffoldError, match="has no value"):
        render("{{missing}}", {})


def _answers(*lines: str) -> tuple[Callable[[str], str], list[str]]:
    prompts: list[str] = []
    pending = iter(lines)

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return next(pending)

    return ask, prompts


def _args(**values: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "name": None,
        "agent": [],
        "structure": None,
        "lead": None,
        "model": None,
        "output": None,
        "auto": True,
        "force": False,
    }
    return argparse.Namespace(**{**defaults, **values})


def test_flags_write_the_shortest_blueprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    code = main(
        [
            "new",
            "process",
            "Invoice processing",
            "--structure",
            "team",
            "--lead",
            "coordinator",
            "--agent",
            "extractor=Reads incoming invoices and extracts the fields",
            "--agent",
            "checker: Validates the totals against the purchase order",
            "--model",
            "fast",
            "--no-input",
        ]
    )

    assert code == 0
    text = (tmp_path / "invoice_processing.yaml").read_text(encoding="utf-8")
    assert "auto: true" in text and "team: coordinator" in text and "model: fast" in text
    assert "  checker: Validates the totals against the purchase order\n" in text
    out = capsys.readouterr().out
    assert "wrote invoice_processing.yaml" in out
    assert "+ tool  builtin:calculator (matched: totals)" in out
    assert 'awa run invoice_processing.yaml "<a task for the team>"' in out
    blueprint = Blueprint.from_yaml_file(tmp_path / "invoice_processing.yaml")
    assert isinstance(blueprint.build(), Team)


def test_the_wizard_asks_for_what_is_missing(tmp_path: Path) -> None:
    ask, prompts = _answers(
        "Contract review",
        "reader: Extracts the clauses of the contract",
        "oops",
        "writer = Writes a short summary",
        "",
        "workflow",
        "",
        "",  # what should start it: nothing
    )
    out = io.StringIO()

    code = run_wizard(_args(output=str(tmp_path / "flow.yaml")), ask=ask, out=out)

    assert code == 0
    assert prompts[0] == "Process name: "
    assert "Give an agent as NAME=TASK" in out.getvalue()
    assert any(p.startswith("Structure [team/workflow/network]") for p in prompts)
    assert not any(p.startswith("Lead") for p in prompts)  # a workflow has no lead
    blueprint = Blueprint.from_yaml_file(tmp_path / "flow.yaml")
    assert [a.name for a in blueprint.agents] == ["reader", "writer"]
    assert blueprint.defaults.model is None


def test_the_wizard_asks_for_the_lead_of_a_network(tmp_path: Path) -> None:
    ask, _ = _answers("planner", "openai:gpt-5-mini", "")
    args = _args(
        name="Market scan",
        agent=["pricing=Compares competitor prices", "planner=Plans the scan"],
        structure="network",
        output=str(tmp_path / "scan.yaml"),
    )

    run_wizard(args, ask=ask, out=io.StringIO())

    blueprint = Blueprint.from_yaml_file(tmp_path / "scan.yaml")
    assert blueprint.network is not None and blueprint.network.lead == "planner"
    assert blueprint.defaults.model == "openai:gpt-5-mini"


def test_missing_answers_without_a_terminal_are_errors() -> None:
    with pytest.raises(ConfigurationError, match="process name"):
        run_wizard(_args(), ask=None, out=io.StringIO())
    with pytest.raises(ConfigurationError, match="--agent"):
        run_wizard(_args(name="x"), ask=None, out=io.StringIO())
    with pytest.raises(ConfigurationError, match="NAME=TASK"):
        run_wizard(_args(name="x", agent=["no task"]), ask=None, out=io.StringIO())


def test_existing_files_need_force(tmp_path: Path) -> None:
    path = tmp_path / "desk.yaml"
    create_process(path, "Desk", {"helper": "Answers questions."})

    with pytest.raises(ScaffoldError, match="--force"):
        create_process(path, "Desk", {"helper": "Answers questions."})
    written, report = create_process(
        path, "Desk", {"helper": "Answers questions."}, auto=False, force=True
    )
    assert written == path and not report.for_agent("helper").auto


def test_process_blueprint_reads_back_and_validates_input() -> None:
    text = process_blueprint(
        "Onboarding", {"checker": "Checks the tax id.", "writer": "Writes the welcome."}
    )

    blueprint = Blueprint.from_yaml_text(text)
    assert blueprint.team is not None and blueprint.team.orchestrator == "lead"
    assert text.startswith("format_version: awa/v1\nname: onboarding\n")
    with pytest.raises(ConfigurationError, match="needs a task"):
        process_blueprint("x", {"a": " "})
    with pytest.raises(ConfigurationError, match="Unknown structure"):
        process_blueprint("x", {"a": "t"}, structure="swarm")  # type: ignore[arg-type]


def test_process_is_added_to_awa_new() -> None:
    parser = build_parser(["new"])
    args = parser.parse_args(["new", "process", "--no-input"])
    assert args.handler.__name__ == "_new_process"
