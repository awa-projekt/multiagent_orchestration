"""Plugins from a folder and from installed entry points."""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from awa.plugins import (
    PluginError,
    Registry,
    find,
    list_plugins,
    load_reference,
    normalize_dist,
    reference_problem,
)
from awa.plugins._kinds import PLUGIN_PREFIX, Kind, parse_plugin_reference
from awa.tools import declared_tool_risk
from awa.tools.importing import from_langchain
from conftest import PACKAGE

TOOL = textwrap.dedent(
    '''
    from awa import tool

    @tool
    def lookup_order(order_id: str) -> str:
        """Look up an order."""
        return f"order {order_id}"

    def note(text: str) -> str:
        """Note something."""
        return text

    __all__ = ["note"]
    '''
)


def _names(project: Path, kind: Kind) -> list[str]:
    return Registry(providers={}).names(kind, project=project)


def test_the_project_folder_convention_is_read(tmp_path: Path) -> None:
    (tmp_path / "tools.py").write_text(TOOL, encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "more.py").write_text(
        TOOL.replace("lookup_order", "find_invoice").replace("note", "remark"), encoding="utf-8"
    )
    (tmp_path / "connectors").mkdir()
    (tmp_path / "connectors" / "crm.py").write_text(
        "from awa.connectors import ConnectorSpec\n"
        "CRM = ConnectorSpec(name='crm_folder', title='CRM', category='data', summary='A CRM',"
        "base_url='https://crm.example')\n",
        encoding="utf-8",
    )
    skill = tmp_path / "skills" / "tone"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: tone\ndescription: Keep the tone friendly.\n---\nBe kind.\n", encoding="utf-8"
    )
    (tmp_path / "plugins.py").write_text(
        "from awa.plugins import register\nregister('models', 'quick', lambda: None)\n",
        encoding="utf-8",
    )
    assert {"lookup_order", "note", "find_invoice", "remark"} <= set(_names(tmp_path, "tools"))
    assert "crm_folder" in _names(tmp_path, "connectors")
    assert _names(tmp_path, "skills") == ["tone"]
    assert _names(tmp_path, "models") == ["quick"]


def test_plugins_py_registers_into_the_project_source(tmp_path: Path) -> None:
    (tmp_path / "plugins.py").write_text(
        "from awa.plugins import register\nregister('models', 'quick', lambda: None)\n",
        encoding="utf-8",
    )
    registry = Registry(providers={})
    [row] = registry.infos("models", project=tmp_path)
    assert row.source == "project"
    assert registry.names("models") == []  # outside the project nothing is registered


def test_awa_tool_objects_and_documented_all_functions_are_found(tmp_path: Path) -> None:
    (tmp_path / "tools.py").write_text(TOOL, encoding="utf-8")
    registry = Registry(providers={})
    tools = {
        name: registry.load("tools", name, project=tmp_path) for name in ("lookup_order", "note")
    }
    assert tools["lookup_order"][0].invoke({"order_id": "7"}) == "order 7"
    assert tools["note"][0].invoke({"text": "x"}) == "x"


def test_a_changed_file_is_read_again(tmp_path: Path) -> None:
    path = tmp_path / "plugins.py"
    path.write_text(
        "from awa.plugins import register\nregister('models', 'one', lambda: None)\n",
        encoding="utf-8",
    )
    registry = Registry(providers={})
    assert registry.names("models", project=tmp_path) == ["one"]
    path.write_text(
        "from awa.plugins import register\nregister('models', 'two', lambda: None)\n",
        encoding="utf-8",
    )
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert registry.names("models", project=tmp_path) == ["two"]


def test_listing_plugins_imports_nothing(installed: Path) -> None:
    rows = [r for r in list_plugins() if r.source == "plugin"]

    assert [(r.kind, r.reference) for r in rows] == [
        ("tools", "plugin:acme-awa/broken"),
        ("tools", "plugin:acme-awa/crm"),
        ("tools", "plugin:acme-awa/lookup_order"),
        ("skills", "plugin:acme-awa/acme"),
    ]
    assert {r.version for r in rows} == {"1.4.0"}
    assert f"{PACKAGE}.tools" not in sys.modules
    assert [r.name for r in list_plugins("skills") if r.source == "plugin"] == ["acme"]


def test_plugin_tools_load_on_demand(installed: Path) -> None:
    (lookup,) = find("tools", "lookup_order")
    assert lookup.invoke({"order_id": "7"}) == "order 7"

    (delete,) = from_langchain(find("tools", "crm"), requires_approval=False)
    risk = declared_tool_risk(delete)
    assert risk is not None and risk.external_action

    with pytest.raises(PluginError, match="cannot load tool 'broken'"):
        find("tools", "broken")
    with pytest.raises(PluginError, match="no tool named 'nope'"):
        find("tools", "nope")
    assert find("tools", "nope", default=None) is None


def test_a_plugin_skills_package_loads(installed: Path) -> None:
    from awa.skills import SkillRegistry

    package = find("skills", "acme")
    registry = SkillRegistry.from_directory(*package.__path__)
    assert registry.names() == ["order-lookup"]


def test_blueprint_references_resolve_to_plugin_tools(installed: Path) -> None:
    assert parse_plugin_reference("plugin:acme-awa/lookup_order") == ("acme-awa", "lookup_order")
    assert parse_plugin_reference("plugin:acme-awa") == ("acme-awa", None)
    assert reference_problem("plugin:acme-awa/lookup_order") is None
    assert "invalid plugin reference" in (reference_problem("plugin:/x") or "")
    assert "invalid plugin reference" in (reference_problem("plugin:a/b/c") or "")
    assert "installed plugin distributions: acme-awa" in (
        reference_problem("plugin:acme-awa/nope") or ""
    )
    tools = load_reference("plugin:acme-awa/crm")
    assert [t.name for t in tools] == ["delete_customer"]
    assert tools[0].metadata["awa_plugin"] == "plugin:acme-awa/crm"
    with pytest.raises(PluginError, match="No installed"):
        load_reference("plugin:unknown/tool")


def test_blueprints_build_plugin_references(installed: Path) -> None:
    from awa.blueprints import Blueprint

    blueprint = Blueprint.from_dict(
        {
            "name": "desk",
            "agents": [
                {
                    "name": "clerk",
                    "instructions": "Help.",
                    "tools": ["plugin:acme-awa/lookup_order"],
                }
            ],
        }
    )
    assert not [issue for issue in blueprint.check(tools=[]) if issue.severity == "error"]
    agent: Any = blueprint.build(allow_imports=False)
    assert [tool.name for tool in agent.tools] == ["lookup_order"]
    assert (agent.tools[0].metadata or {})["awa_plugin"] == "plugin:acme-awa/lookup_order"


def test_normalize_dist_follows_pypi_rules() -> None:
    assert normalize_dist("Acme_AWA.tools") == "acme-awa-tools"
    assert PLUGIN_PREFIX == "plugin:"
