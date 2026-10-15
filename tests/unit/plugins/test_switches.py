"""The part switches as the runtime sees them: blueprint, Python layer and every choke point."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from _helpers.polling import SLOW_SUBPROCESS_TIMEOUT
from awa.errors import ConfigurationError
from awa.plugins import switches as components
from awa.plugins.switches import ComponentError, Entry, choice, override, security_off, set_python


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A project folder that `current()` finds, with no switches yet."""

    folder = tmp_path / "project"
    (folder / ".awa").mkdir(parents=True)
    monkeypatch.setenv("AWA_PROJECT_DIR", str(folder))
    saved = components.python_entries()
    yield folder
    for key in list(components.python_entries()):
        if key not in saved:
            set_python(key, None)


def _switch(project: Path, **entries: Entry) -> None:
    """Switch parts in the project's blueprint (`parser__csv=Entry(state="off")`)."""

    sections: dict[str, dict[str, str]] = {}
    for key, entry in entries.items():
        kind, name = key.split("__", 1)
        sections.setdefault(kind, {})[name] = entry.state or "on"
    lines = ["name: demo", "components:"]
    for kind, names in sections.items():
        lines.append(f"  {kind}:")
        lines += [f"    {name}: {state}" for name, state in names.items()]
    (project / "blueprint.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_without_a_file_everything_is_on(project: Path) -> None:
    assert choice("parser.csv").state == "on"
    assert choice("parser.csv").set_by == "default"
    assert choice("cache.steps").state == "on"
    assert security_off() == []


def test_names_are_validated() -> None:
    with pytest.raises(ComponentError, match="did you mean 'tool'"):
        components.split_key("tol.web_search")
    with pytest.raises(ComponentError, match="invalid part name"):
        components.split_key("tool.Web Search")
    with pytest.raises(ComponentError, match="did you mean 'guard'"):
        components.split_key("gard.sql_guard")
    assert components.split_key("guard.sql_guard") == ("guard", "sql_guard")


def test_precedence_python_over_blueprint(project: Path) -> None:
    (project / "blueprint.yaml").write_text(
        "name: demo\ncomponents:\n  parser:\n    csv: on\n", encoding="utf-8"
    )
    assert (choice("parser.csv").state, choice("parser.csv").set_by) == ("on", "blueprint")
    with override({"parser.csv": "off"}):
        assert (choice("parser.csv").state, choice("parser.csv").set_by) == ("off", "python")
    assert choice("parser.csv").set_by == "blueprint"


def test_a_blueprint_can_switch_a_security_part_and_cannot_replace_a_part(project: Path) -> None:
    (project / "blueprint.yaml").write_text(
        "name: demo\ncomponents:\n  guard:\n    sql_guard: off\n", encoding="utf-8"
    )
    entries, problems = components.blueprint_entries(project)
    assert problems == [] and set(entries) == {"guard.sql_guard"}
    assert choice("guard.sql_guard").state == "off"
    assert security_off() == ["guard.sql_guard"]
    _, problems = components.parse_section({"parser": {"csv": {"use": "x:y"}}}, where="bp")
    assert "must be on or off" in problems[0]
    entries, problems = components.parse_section({"parser": None}, where="bp")
    assert (entries, problems) == ({}, [])  # an empty kind is nothing, not an error


def test_python_switches_off_a_security_part_for_the_block(project: Path) -> None:
    with override({"guard.sql_guard": "off"}):
        assert security_off() == ["guard.sql_guard"]
    assert security_off() == []


def test_a_replacement_registered_in_python_is_in_effect_as_replaced(project: Path) -> None:
    set_python("parser.csv", Entry(use="python:register_parser"), lambda p, c: c.profile)
    try:
        assert components.in_effect()["replaced"] == ["parser.csv"]
        assert callable(components.replacement(choice("parser.csv")))
    finally:
        set_python("parser.csv", None)


# ---------------------------------------------------------------------- wired choke points


def test_a_disabled_parser_is_not_used(project: Path, tmp_path: Path) -> None:
    from awa.data import profile

    data = tmp_path / "orders.csv"
    data.write_text("id,amount\n1,2\n", encoding="utf-8")
    assert profile([data]).files[0].status == "ok"
    _switch(project, parser__csv=Entry(state="off"))
    found = profile([data]).files[0]
    assert found.status == "unsupported"
    assert found.has_flag("DF-W-UNSUPPORTED-KIND")


def test_register_parser_wins_over_the_blueprint(project: Path, tmp_path: Path) -> None:
    from awa.data import parsers_override, profile

    data = tmp_path / "orders.csv"
    data.write_text("id,amount\n1,2\n", encoding="utf-8")
    _switch(project, parser__csv=Entry(state="off"))

    def mine(path: Path, ctx: object) -> object:
        ctx.profile.role_hint = "own"  # type: ignore[attr-defined]
        return ctx.profile  # type: ignore[attr-defined]

    with parsers_override({"csv": mine}):
        assert choice("parser.csv").set_by == "python"
        assert profile([data]).files[0].role_hint == "own"
    assert profile([data]).files[0].status == "unsupported"


def test_a_disabled_connector_is_refused(project: Path) -> None:
    from awa.connectors import get_connector, list_connectors

    assert get_connector("jira").name == "jira"
    _switch(project, connector__jira=Entry(state="off"))
    with pytest.raises(ConfigurationError, match="jira.*switched off"):
        get_connector("jira")
    assert "jira" not in [spec.name for spec in list_connectors()]


def test_a_disabled_tool_and_toolkit_cannot_be_created(project: Path) -> None:
    from awa.errors import ConfigurationError
    from awa.toolkits import catalog

    assert catalog().create("calculator").name == "calculator"
    _switch(project, tool__calculator=Entry(state="off"))
    with pytest.raises(ConfigurationError, match="tool.calculator"):
        catalog().create("calculator")
    _switch(project, tool__calculator=Entry(state="on"), toolkit__utilities=Entry(state="off"))
    with pytest.raises(ConfigurationError, match="toolkit.utilities"):
        catalog().create("datetime_now")


def test_the_blueprint_build_drops_a_switched_off_tool(
    project: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from awa.blueprints import Blueprint

    blueprint = Blueprint.model_validate(
        {
            "name": "demo",
            "agents": [
                {
                    "name": "helper",
                    "instructions": "Help.",
                    "model": "test",
                    "tools": ["builtin:calculator", "builtin:datetime_now"],
                }
            ],
            "components": {"tool": {"calculator": "off"}},
        }
    )
    with caplog.at_level(logging.WARNING):
        agent = blueprint.build()
    names = [tool.name for tool in agent.tools]  # type: ignore[attr-defined]
    assert names == ["datetime_now"]
    assert "tool.calculator is switched off" in caplog.text


def test_the_blueprint_layer_decides_inside_its_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blueprint that is not the project file still decides inside its block."""

    from awa.blueprints import Blueprint

    here = tmp_path / "project"
    here.mkdir()
    monkeypatch.setenv("AWA_PROJECT_DIR", str(here))
    blueprint = Blueprint.model_validate(
        {
            "name": "elsewhere",
            "agents": [{"name": "a", "instructions": "x", "model": "test"}],
            "components": {"tool": {"calculator": "off"}},
        }
    )
    assert "tool.calculator" in blueprint.component_entries()
    assert not (here / "blueprint.yaml").exists()
    with components.using_blueprint(blueprint.component_entries()):
        assert choice("tool.calculator").is_off
    assert not choice("tool.calculator").is_off


def test_a_disabled_skill_is_left_out_of_the_builtin_registry(project: Path) -> None:
    from awa.skills import SkillRegistry

    assert "writing" in SkillRegistry.builtin().names()
    _switch(project, skill__writing=Entry(state="off"))
    assert "writing" not in SkillRegistry.builtin().names()


def test_the_sql_guard_off_lets_writes_through_and_a_replacement_decides(project: Path) -> None:
    from awa._internal.upstream.tools import ToolException
    from awa.toolkits._sql_guard import check_read_only_sql

    with pytest.raises(ToolException):
        check_read_only_sql("DELETE FROM t")
    with override({"guard.sql_guard": "off"}):
        check_read_only_sql("DELETE FROM t")

    def strict(statement: str, dialect: str | None = None) -> None:
        raise ValueError("nothing passes")

    set_python("guard.sql_guard", Entry(use="python:test"), strict)
    try:
        with pytest.raises(ToolException, match="nothing passes"):
            check_read_only_sql("SELECT 1")
    finally:
        set_python("guard.sql_guard", None)


def test_approvals_off_returns_the_tool_unwrapped(project: Path) -> None:
    from awa.tools import create_tool_approval_wrapper, tool

    @tool
    def pay(amount: int) -> str:
        """Pay an amount of money to the supplier."""
        return "paid"

    assert create_tool_approval_wrapper(pay) is not pay
    with override({"guard.approvals": "off"}):
        assert create_tool_approval_wrapper(pay) is pay


def test_upload_checks_off_accepts_a_name_that_lies(project: Path, tmp_path: Path) -> None:
    from awa.data import ingest
    from awa.data._errors import TypeMismatchError

    lying = tmp_path / "in" / "report.pdf"
    lying.parent.mkdir()
    lying.write_text("id,amount\n1,2\n", encoding="utf-8")
    with pytest.raises(TypeMismatchError):
        ingest([lying], tmp_path / "out1")
    with override({"guard.upload_checks": "off"}):
        assert len(ingest([lying], tmp_path / "out2")) == 1


def test_cache_switches_apply_and_the_environment_still_wins(project: Path) -> None:
    from awa.core.settings import Settings
    from awa.runtime.cache import CacheSettings

    settings = Settings(_env_file=None)
    assert CacheSettings().with_environment(settings).tools is True
    _switch(project, cache__tools=Entry(state="off"), cache__steps=Entry(state="on"))
    resolved = CacheSettings().with_environment(settings)
    assert (resolved.tools, resolved.steps) == (False, True)
    operator = Settings(_env_file=None, cache_tools=True)
    assert CacheSettings().with_environment(operator).tools is True


def test_tracers_switched_off_are_refused(project: Path) -> None:
    pytest.importorskip("opentelemetry")
    from awa.errors import ConfigurationError
    from awa.observability.otel import OpenTelemetrySubscriber

    OpenTelemetrySubscriber()
    with override({"tracer.otel": "off"}), pytest.raises(ConfigurationError, match="tracer.otel"):
        OpenTelemetrySubscriber()
    with override({"tracer.langfuse": "off"}):
        OpenTelemetrySubscriber()  # plain OpenTelemetry stays on
        with pytest.raises(ConfigurationError, match="tracer.langfuse"):
            OpenTelemetrySubscriber(langfuse=True)


def test_a_disabled_parser_is_not_used_by_a_new_process(project: Path, tmp_path: Path) -> None:
    import json
    import os
    import subprocess
    import sys

    data = tmp_path / "orders.csv"
    data.write_text("id,amount\n1,2\n", encoding="utf-8")
    _switch(project, parser__csv=Entry(state="off"))
    env = {**os.environ, "AWA_PROJECT_DIR": str(project)}
    done = subprocess.run(
        [sys.executable, "-m", "awa", "data", "inspect", str(data), "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=SLOW_SUBPROCESS_TIMEOUT,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["files"][0]["status"] == "unsupported"


def test_override_keeps_a_registration_made_inside_the_block(project: Path) -> None:
    with override({"parser.csv": "off"}):
        set_python("parser.pdf", Entry(state="off"))
    assert choice("parser.csv").state == "on"
    assert choice("parser.pdf").state == "off"


def test_a_blueprint_alias_bomb_does_not_break_the_switch_lookup(project: Path) -> None:
    names = "abcdefghijklmnopqrstuvwxyz"
    lines = ["a: &a [x, x]"]
    lines += [f"{c}: &{c} [*{p}, *{p}]" for p, c in zip(names, names[1:], strict=False)]
    (project / "blueprint.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert components.blueprint_entries(project) == ({}, [])


def test_the_plugin_list_reads_the_switches_of_the_project_it_lists(
    project: Path, tmp_path: Path
) -> None:
    from awa.plugins import list_plugins

    other = tmp_path / "other"
    other.mkdir()
    (other / "blueprint.yaml").write_text(
        "name: other\nagents:\n  - name: a\ncomponents:\n  parser:\n    csv: off\n",
        encoding="utf-8",
    )

    enabled = {(row.kind, row.name): row.enabled for row in list_plugins(project=other)}

    assert enabled[("parsers", "csv")] is False
    assert {(row.kind, row.name): row.enabled for row in list_plugins()}[("parsers", "csv")]


def test_a_replaced_built_in_tool_shows_in_the_plugin_list(project: Path) -> None:
    (project / "plugins.py").write_text(
        "from awa import tool\n"
        "from awa.plugins import register\n\n"
        "@tool\n"
        "def calculator(expression: str) -> str:\n"
        '    """Own calculator."""\n'
        "    return expression\n\n"
        'register("tools", "calculator", calculator, replace=True)\n',
        encoding="utf-8",
    )
    from awa.plugins import list_plugins

    statuses = {(row.kind, row.name): row.status for row in list_plugins(project=project)}
    assert statuses[("tools", "calculator")] == "replaced"
