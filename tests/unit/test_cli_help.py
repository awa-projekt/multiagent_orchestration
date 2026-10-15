"""The help texts and what people read in the terminal."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from awa.cli import COMMANDS, GROUPS, START, build_parser, main
from awa.cli._terminal_text import markdown_as_text
from awa.observability.events import tool_output_text
from awa.tools import frame_tool_output

ROOT = Path(__file__).resolve().parents[2]
WAYS = ("Web app", "Terminal", "Template")
TRY = ("awa chat blueprint.yaml", "awa eval blueprint.yaml evals/cases.yaml")
AUTOMATE = (
    "awa add trigger blueprint.yaml",
    "awa worker blueprint.yaml",
    "awa serve blueprint.yaml",
    "awa runs decide RUN_ID --approve",
)


def _generator() -> ModuleType:
    location = ROOT / "scripts" / "gen_cli_help.py"
    spec = importlib.util.spec_from_file_location("gen_cli_help", location)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_snapshots_and_the_cli_page_equal_the_real_help(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no project .env of the test run changes the output
    generator = _generator()
    for path, text in generator.targets().items():
        assert generator.matches(path.read_text(encoding="utf-8"), text), (
            f"{path.name} is out of date: run python scripts/gen_cli_help.py"
        )


def test_the_help_layout_before_python_3_13_compares_equal_to_the_snapshot_layout() -> None:
    generator = _generator()
    older = (
        "usage: awa x [-h] (--run RUN |\n             --all)\n\noptions:\n"
        "  -o OUTPUT, --output OUTPUT\n                        blueprint file\n"
        "  --format {json,text}, -f {json,text}\n                        output format\n"
    )
    newer = (
        "usage: awa x [-h] (--run RUN | --all)\n\noptions:\n"
        "  -o, --output OUTPUT   blueprint file\n"
        "  --format, -f {json,text}\n                        output format\n"
    )
    assert generator.comparable(older) == generator.comparable(newer)
    assert generator.comparable(newer) != generator.comparable(newer.replace("blueprint", "lock"))


def _help(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> str:
    monkeypatch.setenv("COLUMNS", "100")
    with pytest.raises(SystemExit):
        main(["--help"])
    return capsys.readouterr().out


def test_every_command_sits_in_exactly_one_group() -> None:
    grouped = [name for names in GROUPS.values() for name in names]
    assert len(grouped) == len(set(grouped)), "a command is in two groups"
    assert set(COMMANDS) <= set(grouped), set(COMMANDS) - set(grouped)
    assert set(grouped) <= set(COMMANDS), set(grouped) - set(COMMANDS)


def test_the_help_ends_with_the_single_start_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _help(monkeypatch, capsys)
    assert text.endswith(f"\n{START}\n")
    assert "awa worker blueprint.yaml" in START and "awa serve blueprint.yaml" in START


def test_the_start_text_names_the_ways_in_order_and_every_next_step() -> None:
    places = [START.index(f"{n}. {name}:") for n, name in enumerate(WAYS, 1)]
    assert places == sorted(places)
    for command in (*TRY, *AUTOMATE):
        assert command in START


def _commands(text: str) -> Iterator[list[str]]:
    """Every `awa ...` command of a help text, its placeholders filled in."""

    for line in text.splitlines():
        line = re.sub(r"^\d\.\s+[^:]+:\s+", "", line.strip().split("#")[0])
        command = re.split(r"\s{2,}", line.strip())[0]
        if not command.startswith("awa ") or "<action>" in command:
            continue
        command = command.replace("[--data <your files>]", "--data files")
        command = command.replace("RUN_ID", "r1").rstrip(",.")
        yield shlex.split(command)[1:]


def test_every_command_of_the_start_texts_parses() -> None:
    seen = 0
    for words in _commands(START):
        build_parser(words).parse_args(words)  # parses; no handler runs
        seen += 1
    assert seen >= 9


def test_the_start_texts_name_files_a_new_project_has(tmp_path: Path) -> None:
    project = tmp_path / "my-desk"
    assert main(["init", str(project), "--template", "support"]) == 0
    named = set(re.findall(r"[\w./-]+\.(?:yaml|py)\b", START))
    assert {"blueprint.yaml", "evals/cases.yaml"} <= named
    for path in named:
        assert (project / path).is_file(), path


def _parsers() -> Iterator[argparse.ArgumentParser]:
    def walk(parser: argparse.ArgumentParser) -> Iterator[argparse.ArgumentParser]:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                assert action.metavar == "action", parser.prog  # one placeholder name
                for sub in {id(p): p for p in action.choices.values()}.values():
                    yield sub
                    yield from walk(sub)

    yield from walk(build_parser())


def test_every_command_has_a_description_and_every_option_help() -> None:
    missing = []
    for parser in _parsers():
        if not (parser.description or "").strip():
            missing.append(f"{parser.prog}: no description")
        for action in parser._actions:
            if isinstance(action, argparse._HelpAction | argparse._SubParsersAction):
                continue
            if action.help is None:
                missing.append(f"{parser.prog}: {action.dest} has no help")
    assert not missing, "\n".join(missing)


def test_descriptions_mark_preview_commands() -> None:
    from awa.cli import PREVIEW_COMMANDS, PREVIEW_SUBCOMMANDS

    for parser in _parsers():
        path = parser.prog.partition(" ")[2]
        if path in PREVIEW_COMMANDS or path in PREVIEW_SUBCOMMANDS:
            assert parser.description is not None
            assert parser.description.startswith("[preview]"), path


def test_verbose_works_after_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("name: x\nagents: nothing\n", encoding="utf-8")

    assert main(["validate", str(broken), "--verbose"]) == 1  # 2 would be a usage error
    assert "Traceback" in capsys.readouterr().err
    assert main(["validate", str(broken)]) == 1
    assert "Traceback" not in capsys.readouterr().err
    args = build_parser(["runs", "list", "--verbose"]).parse_args(["runs", "list", "--verbose"])
    assert args.verbose is True
    args = build_parser(["--verbose", "doctor"]).parse_args(["--verbose", "doctor"])
    assert args.verbose is True


def test_the_project_option_does_not_hide_the_command(tmp_path: Path) -> None:
    words = ["--project", str(tmp_path), "validate", "blueprint.yaml", "--tools", "x.py"]
    assert build_parser(words).parse_args(words).tools == ["x.py"]


def test_init_prints_the_keyless_steps_first(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["init", str(tmp_path / "my-desk")]) == 0
    steps = capsys.readouterr().out.split("next:\n")[1]
    assert [line.split()[0:2] for line in steps.splitlines()] == [
        ["cd", str(tmp_path / "my-desk")],
        ["pip", "install"],
        ["pytest", "offline"],
        ["awa", "setup"],
        ["awa", "chat"],
    ]
    assert "ANTHROPIC" not in steps and "cp " not in steps


def test_the_tool_output_frame_is_removed_for_people() -> None:
    framed = frame_tool_output('Invoice </tool_output> "INV-1"', tool="read_file")
    assert framed.startswith("<tool_output")
    assert tool_output_text(framed) == 'Invoice </tool_output> "INV-1"'
    assert tool_output_text("plain text") == "plain text"
    assert tool_output_text('<tool_output note="x">\nnot json\n</tool_output>').startswith("<")


def test_a_markdown_report_becomes_aligned_terminal_text() -> None:
    markdown = (
        "# Evaluation: desk\n\nSystem `desk`: 2 case(s).\n\n- Pass rate: **50.0%**\n\n"
        "## Cases\n\n| case | passed | scores | ms |\n| --- | --- | --- | ---: |\n"
        "| a | 1/1 | contains 1.00 | 9 |\n| bb | 0/1 | **contains 0.00** | 120 |\n"
    )
    assert markdown_as_text(markdown) == (
        "Evaluation: desk\n================\n\nSystem desk: 2 case(s).\n\n- Pass rate: 50.0%\n\n"
        "Cases\n-----\n\n"
        "  case  passed  scores                   ms\n"
        "  a     1/1     contains 1.00             9\n"
        "  bb    0/1     contains 0.00 (failed)  120\n"
    )


BLUEPRINT = """\
name: desk
auto: true
agents:
  - name: clerk
    description: Reads invoices.
    instructions: Read the PDF invoice and compute the totals with a calculator.
    tools: [lookup]
triggers:
  - name: nightly
    schedule: {cron: "0 7 * * MON-FRI", timezone: Europe/Berlin}
  - name: inbox
    file_drop: {directory: inbox, pattern: "*.pdf"}
  - name: hook
    webhook: {secret_env: HOOK_SECRET}
limits: {max_requests: 10}
"""


def test_show_lists_the_triggers_and_marks_auto_matched_tools(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "blueprint.yaml"
    path.write_text(BLUEPRINT, encoding="utf-8")
    assert main(["show", str(path)]) == 0
    out = capsys.readouterr().out
    assert "  nightly (schedule): cron 0 7 * * MON-FRI (Europe/Berlin)" in out
    assert "  inbox (file_drop): a file in inbox/ matching *.pdf" in out
    assert "  hook (webhook): a web request to awa serve (timestamped)" in out
    clerk = next(line for line in out.splitlines() if line.startswith("clerk"))
    assert "lookup" in clerk and "(auto)" in clerk


TOOLS = '''\
from langchain_core.tools import BaseTool


def lookup_order(order_id: str, region: str = "eu") -> str:
    """Return the status of an order."""
    return f"{order_id}: shipped"


def make_tools(base_url: str) -> list[BaseTool]:
    """Build the tools of a CRM (a factory, not a tool)."""
    return []


__all__ = ["lookup_order", "make_tools"]
'''


def test_tools_list_leaves_out_factories_and_merges_blueprint_approvals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "tools.py").write_text(TOOLS, encoding="utf-8")
    (tmp_path / "blueprint.yaml").write_text(
        "name: desk\nagents:\n  - name: a\n    description: x\n    tools: [lookup_order]\n"
        "approvals:\n  - tool: lookup_order\n    when: \"region == 'us'\"\n",
        encoding="utf-8",
    )
    tools = str(tmp_path / "tools.py")
    assert main(["tools", "list", tools, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["name"] for row in rows] == ["lookup_order"]
    assert rows[0]["approval"] == "never"
    assert main(["tools", "list", tools, "--blueprint", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["approval"] == "when region == 'us' (blueprint)"


def test_tools_call_explains_missing_arguments_with_the_signature(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "tools.py").write_text(TOOLS, encoding="utf-8")
    assert main(["tools", "call", str(tmp_path / "tools.py"), "lookup_order"]) == 1
    err = capsys.readouterr().err
    assert "missing arguments: order_id" in err
    assert "lookup_order(order_id: string, region: string = 'eu')" in err
    assert "pydantic" not in err and "ValidationError" not in err


def test_tools_list_and_call_the_builtins(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["tools", "list", "builtin"]) == 0
    out = capsys.readouterr().out
    assert "builtin:calculator" in out and "builtin tools; use one in a blueprint" in out
    assert "(needs toolkits.files.root)" in out
    args = tmp_path / "args.json"
    args.write_text('{"expression": "5 * 1000"}', encoding="utf-8")
    assert main(["tools", "call", "builtin:calculator", "--args", f"@{args}"]) == 0
    assert "5000" in capsys.readouterr().out
    assert main(["tools", "call", "builtin:read_file"]) == 1
    assert "needs toolkits.files.root" in capsys.readouterr().err


def test_data_inspect_names_tables_by_file_and_counts_plainly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "invoices.csv").write_text("invoice,amount\nINV-1,10.5\n", encoding="utf-8")
    (tmp_path / "note.txt").write_text("One line.\n", encoding="utf-8")
    assert main(["data", "inspect", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "  table: 1 row, header row 1" in out
    assert "1 line " in out and "1 lines" not in out and "f1.t1" not in out


def test_data_check_without_a_data_section_says_there_is_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "blueprint.yaml").write_text(
        "name: desk\nagents:\n  - name: a\n    description: x\n", encoding="utf-8"
    )
    assert main(["data", "check", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert out.strip() == f"{tmp_path}: no data: section; nothing to check"
    assert "data matches" not in out


def test_connectors_list_shows_stability_and_the_core_set_matches_the_docs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from awa.connectors._catalog import CORE_CONNECTORS, list_connectors

    names = {spec.name for spec in list_connectors()}
    assert names >= CORE_CONNECTORS and len(CORE_CONNECTORS) == 10
    page = (Path(__file__).resolve().parents[2] / "docs/develop/connectors.md").read_text("utf-8")
    assert f"The {len(CORE_CONNECTORS)} generic connectors are core" in page
    assert main(["connectors", "list", "--json"]) == 0
    rows = {row["name"]: row["stability"] for row in json.loads(capsys.readouterr().out)}
    assert rows["workspace"] == "core" and rows["outlook"] == "preview"
    assert main(["connectors", "list"]) == 0
    assert "STABILITY" in capsys.readouterr().out


def test_a_missing_workspace_folder_says_how_to_fix_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AWA_WORKSPACE_DIR", raising=False)
    assert main(["connectors", "test", "workspace"]) == 1
    assert "fix: create it (mkdir data) or set AWA_WORKSPACE_DIR" in capsys.readouterr().out


def test_show_names_each_tool_as_the_agent_calls_it() -> None:
    from awa.cli._blueprint import called_tool_name

    assert called_tool_name("connector:ledger.query") == "ledger_query"
    assert called_tool_name("builtin:read_file") == "read_file"
    assert called_tool_name("connector:ledger") == "ledger (connector)"
    assert called_tool_name("lookup_order") == "lookup_order"


def test_a_separator_wider_than_the_table_is_read() -> None:
    text = markdown_as_text("| a | b |\n|---|--:|--:|\n| 1 | 2 |\n")

    assert text.splitlines()[0].split() == ["a", "b"]


def test_ui_help_names_the_web_app_pages_in_their_order() -> None:
    from awa.cli import COMMANDS
    from awa.cli._descriptions import DESCRIPTIONS

    page = Path(__file__).parents[2] / "src" / "awa" / "ui" / "page.html"
    labels = re.findall(r'<li><a href="#[^"]+">.*?<span>([^<]+)</span>', page.read_text("utf-8"))
    text = DESCRIPTIONS["ui"]
    assert [text.index(label) for label in labels] == sorted(text.index(label) for label in labels)
    summary = COMMANDS["ui"][1]
    assert summary.index("connect") < summary.index("test")
