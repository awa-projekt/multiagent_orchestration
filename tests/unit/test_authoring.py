"""`awa new tool|toolbox|skill`, `awa add tool|graph|skills`, `awa tools list|call` and `awa plugins list`."""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from _helpers.polling import SLOW_SUBPROCESS_TIMEOUT
from awa.blueprints import Blueprint
from awa.blueprints._resolve import load_tools
from awa.blueprints.errors import BlueprintError
from awa.blueprints.scaffold import create_skill, create_tool, create_toolbox
from awa.cli import build_parser
from awa.errors import AdapterError, AwaError, ScaffoldError
from awa.skills import Skill, SkillRegistry

SPEC_KEYS = {"name", "description", "license", "compatibility", "allowed-tools", "metadata"}


def _cli(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except AwaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _pytest(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=SLOW_SUBPROCESS_TIMEOUT,
    )


def test_the_parser_has_new_and_tools() -> None:
    parser = build_parser(["new"])

    args = parser.parse_args(["new", "tool", "x", "--async", "--approval"])
    assert args.is_async and args.approval and args.dir == "."
    assert parser.parse_args(["new", "skill", "s"]).dir == "skills"


@pytest.mark.slow
def test_generated_tools_and_toolbox_pass_their_own_tests(tmp_path: Path) -> None:
    assert _cli(["new", "tool", "lookup_record", "--dir", str(tmp_path)]) == 0
    assert (
        _cli(["new", "tool", "close-record", "--async", "--approval", "--dir", str(tmp_path)]) == 0
    )
    assert _cli(["new", "toolbox", "crm", "--dir", str(tmp_path)]) == 0

    source = (tmp_path / "close_record.py").read_text(encoding="utf-8")
    assert "async def close_record(ctx: RunContext[CloseRecordDeps], record_id: str)" in source
    assert '@tool(approval=True, risk=["external_action"], timeout=10)' in source
    assert "{{" not in source
    test_source = (tmp_path / "tests" / "test_close_record.py").read_text(encoding="utf-8")
    assert "test_every_call_waits_for_a_human" in test_source
    completed = _pytest(tmp_path)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert re.search(r"\b1[0-9] passed\b|\b[2-9] passed\b", completed.stdout)


def test_scaffolds_refuse_to_overwrite_and_check_names(tmp_path: Path) -> None:
    create_tool("lookup", tmp_path)
    with pytest.raises(ScaffoldError, match="--force"):
        create_tool("lookup", tmp_path)
    assert create_tool("lookup", tmp_path, force=True)
    for bad in ("Lookup", "9lives", "test_x", "a b"):
        with pytest.raises(ScaffoldError):
            create_tool(bad, tmp_path)
    with pytest.raises(ScaffoldError):
        create_toolbox("CRM!", tmp_path)
    with pytest.raises(ScaffoldError):
        create_skill("Bad--name", tmp_path)
    with pytest.raises(ScaffoldError):
        create_skill("ok", tmp_path, category="Bad Category")
    assert _cli(["new", "tool", "lookup", "--dir", str(tmp_path)]) == 1


def test_generated_skill_follows_the_builtin_skill_rules(tmp_path: Path) -> None:
    assert (
        _cli(["new", "skill", "refund_policy", "--dir", str(tmp_path), "--category", "finance"])
        == 0
    )

    skill = Skill.from_path(tmp_path / "refund-policy")
    text = (tmp_path / "refund-policy" / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---")[1])
    assert set(frontmatter) <= SPEC_KEYS
    assert all(isinstance(value, str) for value in frontmatter["metadata"].values())
    assert re.search(r"\bUse (when|whenever|for|at|before)\b", skill.description)
    assert 100 <= len(skill.description) <= 400
    assert skill.version == "1.0.0" and skill.category == "finance" and len(skill.tags) >= 3
    assert skill.instructions.startswith("# Refund Policy")
    assert len(skill.instructions.splitlines()) <= 80
    mentioned = set(re.findall(r"`((?:references|scripts|assets)/[^`]+)`", skill.instructions))
    assert mentioned == set(skill.resources) == {"references/guide.md"}
    assert skill.read_resource("references/guide.md").strip()
    registry = SkillRegistry.from_directory(tmp_path, include_builtin=False)
    assert registry.names() == ["refund-policy"]


def test_tools_list_prints_risk_and_approval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    create_toolbox("crm", tmp_path)
    create_tool("close_record", tmp_path, approval=True)

    assert _cli(["tools", "list", str(tmp_path / "crm.py")]) == 0
    out = capsys.readouterr().out
    assert re.search(r"crm_search\s+private_data\s+never\s+Search records", out)
    assert re.search(r"crm_create\s+\S*external_action\s+always", out)

    assert _cli(["tools", "list", str(tmp_path / "close_record.py"), "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert row["name"] == "close_record" and row["approval"] == "always"
    assert "external_action" in row["risk"]


def test_tools_call_runs_refuses_gated_calls_and_reports_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    create_tool("lookup_record", tmp_path)
    create_tool("close_record", tmp_path, approval=True, is_async=True)
    lookup, close = str(tmp_path / "lookup_record.py"), str(tmp_path / "close_record.py")

    code = _cli(
        [
            "tools",
            "call",
            lookup,
            "lookup_record",
            "--args",
            '{"record_id": "R-2"}',
            "--deps",
            "LookupRecordDeps",
        ]
    )
    assert code == 0 and capsys.readouterr().out.strip() == "Record R-2 is closed."

    code = _cli(
        [
            "tools",
            "call",
            close,
            "close_record",
            "--args",
            '{"record_id": "R-1"}',
            "--deps",
            "CloseRecordDeps",
        ]
    )
    captured = capsys.readouterr()
    assert code == 2 and "--approve" in captured.err and captured.out == ""

    code = _cli(
        [
            "tools",
            "call",
            close,
            "close_record",
            "--args",
            '{"record_id": "R-1"}',
            "--deps",
            "CloseRecordDeps",
            "--approve",
            "--json",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["content"] == "Record R-1 is open."

    code = _cli(
        [
            "tools",
            "call",
            lookup,
            "lookup_record",
            "--args",
            '{"record_id": "R-9"}',
            "--deps",
            "LookupRecordDeps",
        ]
    )
    assert code == 1 and "unknown record" in capsys.readouterr().out

    assert _cli(["tools", "call", lookup, "nope"]) == 1
    assert "it has: lookup_record" in capsys.readouterr().err
    assert _cli(["tools", "call", lookup, "lookup_record", "--args", "[1]"]) == 1
    assert _cli(["tools", "call", lookup, "lookup_record", "--args", "{bad"]) == 1
    assert _cli(["tools", "call", lookup, "lookup_record", "--deps", "NoSuchThing"]) == 1


def test_tools_call_reads_json_arguments_from_a_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--args "@file.json"`: Windows PowerShell 5.1 drops the quotes of inline JSON."""

    create_tool("lookup_record", tmp_path)
    lookup = str(tmp_path / "lookup_record.py")
    arguments = tmp_path / "args.json"
    arguments.write_text('{"record_id": "R-2"}', encoding="utf-8-sig")  # Notepad writes a BOM

    code = _cli(
        ["tools", "call", lookup, "lookup_record", "--args", f"@{arguments}", "--deps",
         "LookupRecordDeps"]
    )  # fmt: skip
    assert code == 0 and capsys.readouterr().out.strip() == "Record R-2 is closed."

    assert _cli(["tools", "call", lookup, "lookup_record", "--args", "@missing.json"]) == 1
    assert "Cannot read --args file 'missing.json'" in capsys.readouterr().err
    # what PowerShell 5.1 makes of '{"record_id": "R-2"}' is refused with the way out
    assert _cli(["tools", "call", lookup, "lookup_record", "--args", "{record_id: R-2}"]) == 1
    assert '"@file.json"' in capsys.readouterr().err


def test_load_tools_collects_every_kind(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    module = tmp_path / "mixed_tools.py"
    module.write_text(
        '''
from langchain_core.tools import tool as lc_tool
from pydantic import SecretStr

from awa.tools import Toolbox, env_field, tool, tool_method


@lc_tool
def plain(text: str) -> str:
    """Plain LangChain tool."""
    return text


def documented(x: int) -> int:
    """Documented function."""
    return x


def undocumented(x: int) -> int:
    return x


class Ready(Toolbox, prefix="ready"):
    @tool_method()
    def ping(self) -> str:
        """Ping."""
        return "pong"


class NeedsConfig(Toolbox, prefix="cfg"):
    token: SecretStr = env_field("MIXED_TOOLS_UNSET_TOKEN")

    @tool_method()
    def ping(self) -> str:
        """Ping."""
        return "pong"


extra = tool(lambda: "e", name="extra", description="Extra.")
__all__ = ["documented", "undocumented"]
''',
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="awa.blueprints"):
        names = sorted(
            getattr(item, "name", None) or item.__name__ for item in load_tools(str(module))
        )

    assert names == ["documented", "extra", "plain", "ready_ping"]
    assert "Skipped toolbox NeedsConfig" in caplog.text
    empty = tmp_path / "empty_tools.py"
    empty.write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(BlueprintError, match="defines no tools"):
        load_tools(str(empty))
    with pytest.raises(BlueprintError):
        load_tools(str(tmp_path / "missing.py"))


BLUEPRINT = {
    "name": "support",
    "agents": [
        {"name": "lead", "description": "Coordinates.", "instructions": "Coordinate."},
        {"name": "researcher", "description": "Researches.", "instructions": "Research."},
    ],
    "team": {"orchestrator": "lead", "members": ["researcher"]},
}


def run(*argv: str) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv))
    code: int = args.handler(args)
    return code


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """A project folder (the working directory) with a blueprint and a module of tools."""

    library = f"awa_test_cli_lib_{uuid4().hex[:8]}"
    (tmp_path / f"{library}.py").write_text(
        textwrap.dedent(
            '''
            from langchain.agents import create_agent
            from langchain_core.tools import BaseTool, BaseToolkit, tool
            from awa.testing import FakeChatModel

            class FetchPage(BaseTool):
                name: str = "fetch_page"
                description: str = "Fetch a web page."

                def _run(self, url: str) -> str:
                    return url

            @tool
            def send_email(to: str) -> str:
                """Send an e-mail."""
                return to

            @tool
            def add(a: int, b: int) -> int:
                """Add two numbers."""
                return a + b

            class OfficeToolkit(BaseToolkit):
                def get_tools(self) -> list[BaseTool]:
                    return [send_email, add]

            graph = create_agent(FakeChatModel(["billed"]), tools=[add], name="billing")
            '''
        ),
        encoding="utf-8",
    )
    blueprint = tmp_path / "support.yaml"
    blueprint.write_text(
        "# the support desk\n" + yaml.safe_dump(BLUEPRINT, sort_keys=False), encoding="utf-8"
    )
    module = f"imported_{uuid4().hex[:8]}"
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    yield {"dir": tmp_path, "blueprint": blueprint, "library": library, "module": module}
    for name in (library, module):
        sys.modules.pop(name, None)


def test_add_tool_registers_the_import_in_the_blueprint(
    project: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    lib, module, bp = project["library"], project["module"], project["blueprint"]
    argv = ["add", "tool", f"{lib}.OfficeToolkit", "--blueprint", str(bp), "--agent", "researcher"]

    assert run(*argv, "--module", f"{module}.py") == 0

    out = capsys.readouterr().out
    assert "send_email  [" in out and "approval: required" in out
    assert f"agents[researcher].tools += {module}:send_email" in out
    assert "YAML comments are dropped" in out
    code = (project["dir"] / f"{module}.py").read_text(encoding="utf-8")
    assert f"send_email, add = from_langchain(\n    '{lib}.OfficeToolkit'," in code
    assert "requires_approval=False" in code
    data = yaml.safe_load(bp.read_text(encoding="utf-8"))
    assert data["agents"][1]["tools"] == [f"{module}:send_email", f"{module}:add"]
    assert data["approvals"] == [{"tool": f"{module}:send_email"}]
    assert data["tool_risks"][f"{module}:send_email"]["external_action"] is True

    blueprint = Blueprint.from_yaml_file(bp)
    assert not blueprint.check()
    team: Any = blueprint.build(allow_imports=True, checkpointer=InMemorySaver())
    researcher = team.agent("researcher")
    assert [t.name for t in researcher.tools] == ["send_email", "add"]
    assert (researcher.tools[0].metadata or {}).get("awa_requires_approval") is None


def test_add_tool_keeps_a_short_form_blueprint_short(project: dict[str, Any]) -> None:
    # The form `awa new process` writes: agents by name, `team: <orchestrator>`.
    lib, module = project["library"], project["module"]
    short = project["dir"] / "process.yaml"
    short.write_text(
        "name: desk\nagents:\n  lead: Coordinates.\n  clerk: Sends mail.\nteam: lead\n",
        encoding="utf-8",
    )

    target = ("--blueprint", str(short), "--module", f"{module}.py")
    assert run("add", "tool", f"{lib}:add", *target) == 0
    assert run("add", "tool", f"{lib}:send_email", *target, "--agent", "clerk") == 0

    data = yaml.safe_load(short.read_text(encoding="utf-8"))
    assert data["agents"] == {
        "lead": {"description": "Coordinates.", "tools": [f"{module}:add"]},
        "clerk": {"description": "Sends mail.", "tools": [f"{module}:send_email"]},
    }
    assert data["team"] == "lead"
    assert not [i for i in Blueprint.from_yaml_file(short).check() if i.severity == "error"]


def test_add_tool_refuses_names_already_imported(project: dict[str, Any]) -> None:
    lib, module = project["library"], project["module"]
    assert run("add", "tool", f"{lib}:add", "--module", f"{module}.py") == 0
    with pytest.raises(AdapterError, match="already defines add"):
        run("add", "tool", f"{lib}:add", "--module", f"{module}.py")
    assert run("add", "tool", f"{lib}:add", "--module", f"{module}.py", "--prefix", "m_") == 0
    namespace = importlib.import_module(module)
    assert namespace.add.name == "add" and namespace.m_add.name == "m_add"


def test_add_tool_without_blueprint_applies_approval_in_code(
    project: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    lib, module = project["library"], project["module"]

    assert run("add", "tool", f"{lib}.FetchPage", "--approval", "--module", f"{module}.py") == 0

    code = (project["dir"] / f"{module}.py").read_text(encoding="utf-8")
    assert f"fetch_page = from_langchain_tool('{lib}.FetchPage', requires_approval=True)" in code
    assert f"use them: from {module} import fetch_page" in capsys.readouterr().out
    tool = importlib.import_module(module).fetch_page
    assert tool.metadata["awa_requires_approval"] is True


def test_dry_runs_change_nothing(
    project: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    lib, module, bp = project["library"], project["module"], project["blueprint"]
    before = bp.read_text(encoding="utf-8")

    argv = ["add", "tool", f"{lib}:send_email", "--blueprint", str(bp), "--dry-run"]
    assert run(*argv, "--module", f"{module}.py") == 0

    out = capsys.readouterr().out
    assert f"would create {module}.py" in out
    assert f"would update {bp}" in out and "agents[lead].tools" in out
    assert "would be valid" in out
    assert bp.read_text(encoding="utf-8") == before
    assert not (project["dir"] / f"{module}.py").exists()


def test_add_graph_adds_a_delegate_tool(
    project: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    lib, module, bp = project["library"], project["module"], project["blueprint"]
    argv = ["add", "graph", f"{lib}:graph", "--name", "billing", "--description", "Bills."]

    assert run(*argv, "--blueprint", str(bp), "--module", f"{module}.py") == 0

    out = capsys.readouterr().out
    assert "billing: graph 'billing', tools: add" in out
    code = (project["dir"] / f"{module}.py").read_text(encoding="utf-8")
    assert f"billing = from_langgraph('{lib}:graph', name='billing', description='Bills.')" in code
    assert "billing_tool = billing.as_tool()" in code
    data = yaml.safe_load(bp.read_text(encoding="utf-8"))
    assert data["agents"][0]["tools"] == [f"{module}:billing_tool"]
    namespace = importlib.import_module(module)
    assert namespace.billing.run_sync("bill it").output == "billed"

    with pytest.raises(AdapterError, match="compiled LangGraph graph"):
        run("add", "graph", f"{lib}:add", "--name", "adder", "--module", f"{module}_2.py")
    with pytest.raises(AdapterError, match="no agent 'ghost'"):
        run(*argv, "--blueprint", str(bp), "--agent", "ghost", "--module", f"{module}_3.py")


def test_add_skills_imports_prompts_into_a_skills_folder(
    project: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    prompts = project["dir"] / "prompts"
    prompts.mkdir()
    (prompts / "triage.md").write_text("# Triage\n\nSort tickets by urgency.\n", "utf-8")

    assert run("add", "skills", str(prompts), "--into", "skills", "--dry-run") == 0
    assert not (project["dir"] / "skills").exists()
    assert run("add", "skills", str(prompts), "--into", "skills") == 0

    out = capsys.readouterr().out
    assert "review the generated names and descriptions of: triage" in out
    assert (project["dir"] / "skills" / "triage" / "SKILL.md").is_file()
    assert run("add", "skills", str(prompts), "--into", "skills") == 1  # already exists


def test_plugins_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("plugins", "list", "--kind", "parsers") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:5] == ["KIND", "NAME", "SOURCE", "STATUS", "SWITCH"]
    assert any(line.split()[:3] == ["parsers", "csv", "builtin"] for line in lines[1:])
    assert all(line.startswith("parsers") for line in lines[1:])


def test_module_option_is_validated(project: dict[str, Any]) -> None:
    with pytest.raises(AdapterError, match="valid module name"):
        run("add", "tool", f"{project['library']}:add", "--module", "not-a-module.py")
    other = project["dir"] / "flat.yaml"
    other.write_text(
        yaml.safe_dump(
            {"name": "flat", "entrypoint": "a", "agents": [{"name": "a"}, {"name": "b"}]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(AdapterError, match="Pass --agent"):
        run("add", "tool", f"{project['library']}:add", "--blueprint", str(other))
