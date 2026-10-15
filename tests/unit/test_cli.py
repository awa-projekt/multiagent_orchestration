"""The `awa` command line, driven through `main([...])` with offline models: `--deps`, `awa cache`, and consoles that cannot print every character."""

from __future__ import annotations

import argparse
import io
import json
import runpy
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from awa.blueprints._resolve import load_tools
from awa.cli import build_parser, main
from awa.cli._common import _salt_project, deps_from_args, run_command
from awa.core.dotenv import read_env_file
from awa.core.settings import Settings, get_settings
from awa.errors import ConfigurationError
from awa.models import ModelRegistry
from awa.persistence.cache import SqliteCacheStore
from awa.runtime.ports import CacheScope
from awa.testing import FakeChatModel, FunctionChatModel, ModelCall, structured_output, tool_call

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "blueprints"
SUPPORT = str(EXAMPLES / "support_triage.yaml")
SUPPORT_TOOLS = str(EXAMPLES / "support_tools.py")
REVIEW = str(EXAMPLES / "document_review.yaml")

TOOLS_MODULE = '''
from langchain_core.tools import tool

CALLS = []


@tool
def lookup(order_id: str) -> str:
    """Look up an order."""
    CALLS.append(order_id)
    return f"order {order_id}: shipped"


def pay(order_id: str) -> str:
    """Pay an order."""
    CALLS.append(f"pay:{order_id}")
    return f"paid {order_id}"


def helper() -> None:
    pass


__all__ = ["lookup", "pay"]
'''

SOLO = """
name: solo
agents:
  - name: clerk
    description: Looks up orders.
    tools: [lookup]
"""

GATED = """
name: payments
agents:
  - name: cashier
    tools: [pay]
approvals:
  - tool: pay
"""

PIPELINE = """
name: pipeline
agents:
  - name: first
  - name: second
workflow:
  sequential: [first, second]
"""


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> ModelRegistry:
    registry = ModelRegistry()
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    return registry


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    for name, text in {
        "cli_test_tools.py": TOOLS_MODULE,
        "solo.yaml": SOLO,
        "gated.yaml": GATED,
        "pipeline.yaml": PIPELINE,
    }.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    monkeypatch.delitem(sys.modules, "cli_test_tools", raising=False)
    return tmp_path


def calls() -> list[str]:
    module: Any = sys.modules["cli_test_tools"]
    return list(module.CALLS)


# --------------------------------------------------------------------------- validate / show


def test_validate_example_blueprints(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", SUPPORT, "--tools", SUPPORT_TOOLS]) == 0
    assert capsys.readouterr().out.strip() == "support-triage: valid"

    assert main(["validate", REVIEW]) == 0
    assert "tool names not checked" in capsys.readouterr().out


def test_validate_and_show_accept_a_project_folder(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = workspace / "desk"
    project.mkdir()
    shutil.copy(SUPPORT, project / "blueprint.yaml")

    assert main(["validate", str(project)]) == 0
    assert capsys.readouterr().out.startswith("support-triage: valid")
    assert main(["show", "desk"]) == 0
    assert capsys.readouterr().out.startswith("support-triage ")
    assert main(["validate", str(workspace)]) == 1  # a folder without blueprint.yaml
    assert "pass blueprint.yaml or the project folder that contains it" in capsys.readouterr().err


def test_validate_reports_errors_with_exit_code_1(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / "bad.yaml").write_text(
        "name: bad\nagents:\n  - {name: a, tools: [ghost]}\n  - {name: b}\n", encoding="utf-8"
    )

    assert main(["validate", "bad.yaml", "--tools", "cli_test_tools.py"]) == 1

    out = capsys.readouterr().out
    assert "error: agents[0].tools[0]: unknown tool 'ghost'" in out
    assert "error: entrypoint: cannot infer the entrypoint" in out
    assert "bad: 2 error(s)" in out


def test_validate_counts_warnings(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (workspace / "warn.yaml").write_text(
        "name: w\nentrypoint: a\nagents:\n  - {name: a}\n  - {name: b}\n", encoding="utf-8"
    )

    assert main(["validate", "warn.yaml", "--tools", "cli_test_tools.py"]) == 0
    assert "w: valid with 1 warning(s)" in capsys.readouterr().out


def test_parse_errors_are_clean_messages(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / "broken.yaml").write_text("name: x\nagents: []\n", encoding="utf-8")

    assert main(["validate", "broken.yaml"]) == 1

    err = capsys.readouterr().err
    assert err.startswith("error: Invalid blueprint")
    assert "Traceback" not in err


def test_verbose_prints_tracebacks(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--verbose", "validate", "missing.yaml"]) == 1
    assert "Traceback" in capsys.readouterr().err


def test_show_describes_agents_topology_and_approvals(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show", SUPPORT]) == 0

    out = capsys.readouterr().out
    assert "entrypoint: team" in out
    assert "refunds             default  lookup_order, issue_refund" in out
    assert "peers: billing_specialist -> order_specialist" in out
    assert "shared: messaging, task board" in out
    assert "approvals: issue_refund" in out


def test_show_workflow_and_mermaid(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show", REVIEW]) == 0
    assert (
        "workflow: intake -> (legal_reviewer | compliance_reviewer | style_reviewer) -> editor"
        in capsys.readouterr().out
    )

    assert main(["show", SUPPORT, "--mermaid"]) == 0
    mermaid = capsys.readouterr().out
    assert mermaid.startswith("flowchart TD")
    assert "refunds<br/>approval: issue_refund" in mermaid
    assert "==>|handoff|" in mermaid


# --------------------------------------------------------------------------- run


def test_run_prints_the_output_and_usage(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    models.register("default", FakeChatModel([tool_call("lookup", {"order_id": "7"}), "Shipped."]))

    assert main(["run", "solo.yaml", "Where is 7?", "--tools", "cli_test_tools.py"]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == "Shipped."
    assert "[clerk] 2 model call(s)" in captured.err
    assert calls() == ["7"]


def test_run_json(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    models.register("default", FakeChatModel(["Shipped."]))

    assert main(["run", "solo.yaml", "Where?", "--tools", "cli_test_tools", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["agent"] == "clerk"
    assert data["output"] == "Shipped."
    assert data["usage"]["requests"] == 1
    assert data["usage"]["total_tokens"] > 0


def test_run_json_serializes_structured_output(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / "typed.yaml").write_text(
        "name: typed\nagents:\n  - name: rater\n    output_schema:\n      type: object\n"
        "      properties: {score: {type: integer}}\n      required: [score]\n",
        encoding="utf-8",
    )
    models.register("default", FakeChatModel([structured_output("RaterOutput", {"score": 4})]))

    assert main(["run", "typed.yaml", "Rate.", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["output"] == {"score": 4}

    models.register("default", FakeChatModel([structured_output("RaterOutput", {"score": 5})]))
    assert main(["run", "typed.yaml", "Rate."]) == 0
    assert json.loads(capsys.readouterr().out) == {"score": 5}


def test_run_stream_prints_events(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    models.register("default", FakeChatModel([tool_call("lookup", {"order_id": "7"}), "Shipped."]))

    assert (
        main(["run", "solo.yaml", "Where is 7?", "--tools", "cli_test_tools.py", "--stream"]) == 0
    )

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "> clerk started"
    assert '  clerk -> lookup({"order_id": "7"})' in lines
    assert "  clerk <- lookup [ok]: order 7: shipped" in lines
    assert any(line.startswith("< clerk finished") for line in lines)
    assert lines[-1] == "Shipped."


def test_run_stream_strips_terminal_control_characters(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    models.register(
        "default",
        FakeChatModel([tool_call("lookup", {"order_id": "7\x1b]0;owned\x07\r"}), "Shipped."]),
    )

    assert (
        main(["run", "solo.yaml", "Where is 7?", "--tools", "cli_test_tools.py", "--stream"]) == 0
    )

    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "\r" not in out
    assert "]0;owned" in out


def test_run_stream_for_workflows(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    models.register("default", FakeChatModel(["one", "two"]))

    assert main(["run", "pipeline.yaml", "Go.", "--stream"]) == 0

    out = capsys.readouterr().out
    assert out.startswith("> pipeline started")
    assert "  > first started" in out
    assert "    first: one" in out
    assert "  > second started" in out
    assert out.strip().endswith("two")


def test_run_thread_id_is_persisted(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    models.register("default", FakeChatModel(["Hello."]))

    assert (
        main(
            [
                "run",
                "solo.yaml",
                "Hi",
                "--tools",
                "cli_test_tools.py",
                "--thread-id",
                "t-1",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["thread_id"] == "t-1"


def test_workflows_run_on_a_thread(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    models.register("default", FakeChatModel(["one", "two"]))

    assert main(["run", "pipeline.yaml", "Go.", "--thread-id", "t", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["thread_id"] == "t"


def test_run_pauses_for_approval_when_not_interactive(
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "9"})]))

    assert main(["run", "gated.yaml", "Pay 9.", "--tools", "cli_test_tools.py"]) == 2

    assert "paused for approval" in capsys.readouterr().err
    assert calls() == []


def test_run_builds_a_blueprint_whose_policy_pauses_calls(
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (workspace / "asking.yaml").write_text(
        "name: asking\nagents:\n  - name: cashier\n    tools: [pay]\n"
        "tool_risks: {pay: [external_action]}\npolicies:\n  - ask: [external_action]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "9"})]))

    assert main(["run", "asking.yaml", "Pay 9.", "--tools", "cli_test_tools.py"]) == 2

    assert "paused for approval" in capsys.readouterr().err
    assert calls() == []


@pytest.mark.parametrize(
    ("answers", "expected_calls"), [(["y"], ["pay:9"]), (["n", "too much"], [])]
)
def test_run_asks_for_approval_interactively(
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: list[str],
    expected_calls: list[str],
) -> None:
    replies = iter(answers)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))
    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "9"}), "Done."]))

    assert main(["run", "gated.yaml", "Pay 9.", "--tools", "cli_test_tools.py"]) == 0

    captured = capsys.readouterr()
    assert "Approval needed" in captured.err
    assert '"tool": "pay"' in captured.err
    # the person at the terminal answers y or n; the resume calls for programs are not shown
    assert "agent.resume" not in captured.err and "instructions" not in captured.err
    assert captured.out.strip() == "Done."
    assert calls() == expected_calls


def test_run_left_unanswered_at_the_approval_prompt_stays_paused(
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def end_of_input(prompt: str = "") -> str:
        raise EOFError

    # A terminal whose input ends (Ctrl-D, or stdin from NUL on Windows, which reports a tty).
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", end_of_input)
    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "9"})]))

    assert main(["run", "gated.yaml", "Pay 9.", "--tools", "cli_test_tools.py"]) == 2

    assert "paused for approval" in capsys.readouterr().err
    assert calls() == []


def test_unexpected_run_failures_are_one_line_errors(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(call: ModelCall) -> str:
        raise RuntimeError("provider unavailable")

    models.register("default", FunctionChatModel(fail))

    assert main(["run", "pipeline.yaml", "Go."]) == 1
    err = capsys.readouterr().err
    assert "provider unavailable" in err
    assert "--verbose" in err
    assert "Traceback" not in err

    assert main(["--verbose", "run", "pipeline.yaml", "Go."]) == 1
    assert "Traceback" in capsys.readouterr().err


def test_run_reports_missing_tools_cleanly(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "solo.yaml", "Hi"]) == 1
    assert "unknown tool 'lookup'" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("tools", "message"),
    [
        ("missing.py", "does not exist"),
        ("no_such_module_anywhere", "Cannot import tools module"),
        ("empty_tools.py", "defines no tools"),
        ("failing_tools.py", "Importing tools file 'failing_tools.py' failed"),
    ],
)
def test_tools_option_errors(
    workspace: Path, capsys: pytest.CaptureFixture[str], tools: str, message: str
) -> None:
    (workspace / "empty_tools.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "failing_tools.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")

    assert main(["validate", "solo.yaml", "--tools", tools]) == 1
    assert message in capsys.readouterr().err


def test_tools_from_several_modules_must_not_clash(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / "other_tools.py").write_text(
        TOOLS_MODULE.replace("CALLS = []", "CALLS = ['other']"), encoding="utf-8"
    )

    assert main(["validate", "solo.yaml", "--tools", "cli_test_tools.py,other_tools.py"]) == 1
    assert "define a tool named 'lookup'" in capsys.readouterr().err


# --------------------------------------------------------------------------- skills


def test_skills_list_and_show(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["skills", "list"]) == 0
    assert "delegation-briefs" in capsys.readouterr().out

    assert main(["skills", "show", "delegation-briefs"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("delegation-briefs ")
    assert "tags:" in out

    skill = tmp_path / "house-style"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: house-style\ndescription: Our writing rules.\n---\nWrite short sentences.\n",
        encoding="utf-8",
    )
    assert main(["skills", "list", "--skills", str(tmp_path)]) == 0
    assert "house-style" in capsys.readouterr().out
    assert main(["skills", "show", "house-style", "--skills", str(tmp_path)]) == 0
    assert "Write short sentences." in capsys.readouterr().out


def test_unknown_skill_is_a_clean_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["skills", "show", "nope"]) == 1
    assert capsys.readouterr().err.startswith("error:")


def test_python_dash_m_awa(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["awa", "skills", "list"])

    with pytest.raises(SystemExit) as caught:
        runpy.run_module("awa", run_name="__main__")

    assert caught.value.code == 0
    assert "writing" in capsys.readouterr().out


def test_version_flag_prints_the_installed_version(capsys: pytest.CaptureFixture[str]) -> None:
    import awa

    with pytest.raises(SystemExit) as caught:
        main(["--version"])

    assert caught.value.code == 0
    assert capsys.readouterr().out.strip() == f"awa {awa.__version__}"


BLUEPRINT = """
name: arrows
description: Invoice \u2192 order \u2713 \u65e5\u672c\u8a9e
agents:
  - name: clerk
    description: Reads \u2192 writes.
"""


class Redirected(io.TextIOWrapper):
    """What `awa ... > out.txt` sees on Windows: a strict cp1252 text stream."""

    def __init__(self) -> None:
        self.raw_bytes = io.BytesIO()
        super().__init__(self.raw_bytes, encoding="cp1252", errors="strict", write_through=True)

    def text(self) -> str:
        self.flush()
        return self.raw_bytes.getvalue().decode("cp1252")


def redirect(monkeypatch: pytest.MonkeyPatch) -> tuple[Redirected, Redirected]:
    """Install the streams inside the test body (pytest swaps `sys.stdout` between phases)."""

    out, err = Redirected(), Redirected()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    return out, err


def test_show_with_arrows_and_cjk_survives_a_cp1252_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "blueprint.yaml").write_text(BLUEPRINT, encoding="utf-8")
    out, err = redirect(monkeypatch)

    code = main(["show", str(tmp_path / "blueprint.yaml")])

    assert code == 0, err.text()
    assert "Invoice ? order ? ???" in out.text()
    assert "UnicodeEncodeError" not in err.text()


def test_doctor_in_a_folder_with_a_cjk_name_survives_a_cp1252_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "\u65e5\u672c \u03a9 dir"
    folder.mkdir()
    monkeypatch.chdir(folder)
    out, err = redirect(monkeypatch)

    main(["doctor"])

    assert "UnicodeEncodeError" not in err.text()
    assert "Result:" in out.text()
    assert "?? ? dir" in out.text()


def test_streams_without_reconfigure_are_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    class Plain:
        def write(self, text: str) -> int:
            return len(text)

        def flush(self) -> None:
            pass

    monkeypatch.setattr(sys, "stdout", Plain())
    monkeypatch.setattr(sys, "stderr", Plain())

    with pytest.raises(SystemExit):  # `--version` exits: the point is that main did not crash first
        main(["--version"])


TOOLS = '''
from awa.runtime import current_run_context

SEEN = []

def whoami() -> str:
    """Tell who the run acts for."""
    ctx = current_run_context()
    SEEN.append(ctx.deps if ctx else None)
    return str(ctx.deps if ctx else None)

__all__ = ["whoami", "SEEN"]
'''

BLUEPRINT_DEPS = """
name: desk
agents:
  - name: clerk
    tools: [whoami]
"""


@pytest.fixture
def workspace_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    (tmp_path / "deps_cli_tools.py").write_text(TOOLS, encoding="utf-8")
    (tmp_path / "desk.yaml").write_text(BLUEPRINT_DEPS, encoding="utf-8")
    monkeypatch.delitem(sys.modules, "deps_cli_tools", raising=False)
    registry = ModelRegistry()
    registry.register("default", FakeChatModel([tool_call("whoami"), "Done."]))
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    return tmp_path


def namespace(**values: object) -> argparse.Namespace:
    return argparse.Namespace(**{"deps": None, "user_id": None, **values})


def test_deps_are_parsed_from_json_a_file_and_the_user_id(tmp_path: Path) -> None:
    (tmp_path / "deps.json").write_text('{"tenant": "acme"}', encoding="utf-8")

    assert deps_from_args(namespace()) is None
    assert deps_from_args(namespace(deps='{"a": 1}')) == {"a": 1}
    assert deps_from_args(namespace(deps=f"@{tmp_path / 'deps.json'}", user_id="u1")) == {
        "tenant": "acme",
        "user_id": "u1",
    }
    assert deps_from_args(namespace(user_id="u2")) == {"user_id": "u2"}
    assert deps_from_args(namespace(deps="[1]")) == [1]
    with pytest.raises(ConfigurationError, match="not valid JSON"):
        deps_from_args(namespace(deps="{nope"))
    with pytest.raises(ConfigurationError, match="JSON object"):
        deps_from_args(namespace(deps="[1]", user_id="u"))
    with pytest.raises(ConfigurationError, match="Cannot read"):
        deps_from_args(namespace(deps="@missing.json"))


def test_run_passes_the_deps_to_every_tool(
    workspace_deps: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "run",
            "desk.yaml",
            "Who am I?",
            "--tools",
            "deps_cli_tools.py",
            "--deps",
            '{"tenant": "acme"}',
            "--user-id",
            "ada",
            "--json",
        ]
    )

    assert code == 0, capsys.readouterr().err
    tools = sys.modules["deps_cli_tools"]
    assert tools.SEEN == [{"tenant": "acme", "user_id": "ada"}]
    assert json.loads(capsys.readouterr().out)["output"] == "Done."


def test_run_chat_and_eval_accept_the_options() -> None:
    parser = build_parser()
    for command in (["run", "b.yaml", "t"], ["chat", "b.yaml"], ["eval", "b.yaml", "d.yaml"]):
        args = parser.parse_args([*command, "--deps", "{}", "--user-id", "u"])
        assert (args.deps, args.user_id) == ("{}", "u")


def test_two_tools_files_with_the_same_name_do_not_collide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "path", list(sys.path))
    for folder, name in (("one", "alpha"), ("two", "beta")):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "tools.py").write_text(
            f'def {name}() -> str:\n    """Say {name}."""\n    return "{name}"\n\n'
            f'__all__ = ["{name}"]\n',
            encoding="utf-8",
        )

    loaded = load_tools([f"{tmp_path / 'one' / 'tools.py'},{tmp_path / 'two' / 'tools.py'}"])

    assert sorted(getattr(tool, "__name__", "") for tool in loaded) == ["alpha", "beta"]


BLUEPRINT_CACHE = """
name: shop
defaults:
  cache: {store: sqlite, tools_ttl: 600}
agents:
  - name: clerk
"""


def awa(*argv: str) -> int:
    args = build_parser(list(argv)).parse_args(list(argv))

    return run_command(args)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("AWA_CACHE_STORE", "AWA_CACHE_SALT", "AWA_CACHE_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    (tmp_path / "blueprint.yaml").write_text(BLUEPRINT_CACHE, encoding="utf-8")
    store = SqliteCacheStore(tmp_path / ".awa" / "cache.db")
    store.put(CacheScope("acme", "shop", "tools"), "k1", b"12345")
    store.put(CacheScope("acme", "shop", "steps"), "k2", b"12")
    store.put(CacheScope("globex", "shop", "tools"), "k3", b"1")
    return tmp_path


def test_stats_lists_entries_per_tenant_project_and_kind(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert awa("cache", "stats") == 0
    out = capsys.readouterr().out

    assert "| acme | shop | steps | 1 | 2 | 0 |" in out
    assert "| acme | shop | tools | 1 | 5 | 0 |" in out
    assert "| globex | shop | tools | 1 | 1 | 0 |" in out


def test_clear_deletes_what_it_matches(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert awa("cache", "clear", "--tenant", "acme", "--kind", "tools") == 0
    assert "deleted 1 cache entry" in capsys.readouterr().out
    assert awa("cache", "clear") == 0
    assert "deleted 2 cache entries" in capsys.readouterr().out
    assert SqliteCacheStore(project / ".awa" / "cache.db").stats() == []


def test_clear_with_a_recordings_file_leaves_the_cache_alone(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recordings = project / "recordings.db"
    SqliteCacheStore(recordings).put(CacheScope("acme", "shop", "models"), "r1", b"1")

    assert awa("cache", "clear", "--replay-store", str(recordings)) == 0

    assert "deleted 1 cache entry" in capsys.readouterr().out
    assert SqliteCacheStore(recordings).stats() == []
    assert len(SqliteCacheStore(project / ".awa" / "cache.db").stats()) == 3


def test_a_memory_store_has_nothing_to_show(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "blueprint.yaml").write_text(BLUEPRINT_CACHE.replace("store: sqlite, ", ""), "utf-8")

    assert awa("cache", "stats") == 0
    assert "lives inside one process" in capsys.readouterr().out


def test_salt_writes_a_random_salt_once_and_never_prints_it(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert awa("cache", "salt") == 0
    first = read_env_file(project / ".env")["AWA_CACHE_SALT"]
    assert awa("cache", "salt") == 0
    out = capsys.readouterr().out

    assert len(first) >= 32 and first not in out
    assert read_env_file(project / ".env")["AWA_CACHE_SALT"] == first
    assert "already set" in out
    assert Settings(_env_file=project / ".env").cache_salt is not None


def test_a_run_in_a_project_with_a_dot_env_gets_its_salt(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / ".env").write_text("OTHER=1\n", encoding="utf-8")

    args = build_parser(["validate"]).parse_args(["validate", "blueprint.yaml"])
    args.command = "eval"
    _salt_project(args)

    values = read_env_file(project / ".env")
    assert values["OTHER"] == "1" and values["AWA_CACHE_SALT"]
    assert values["AWA_CACHE_SALT"] not in capsys.readouterr().err


def test_stats_shows_every_switch_and_the_salt(
    project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert awa("cache", "stats") == 0
    out = capsys.readouterr().out.splitlines()

    assert out[0] == (
        "enabled: on; prompt: auto; tools: on (600 s across runs); steps: on; store: sqlite"
    )
    assert out[1] == "AWA_CACHE_SALT: not set (awa cache salt)"
    monkeypatch.setenv("AWA_CACHE_SALT", "x")
    monkeypatch.setenv("AWA_CACHE_ENABLED", "false")
    get_settings.cache_clear()
    assert awa("cache", "stats") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("enabled: off;") and out[1] == "AWA_CACHE_SALT: set"
