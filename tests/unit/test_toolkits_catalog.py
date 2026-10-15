"""The builtin tool catalog, `builtin:<name>` references, the shared toolkit helpers and the utility tools."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from _helpers.fake_secrets import fake_secret
from awa import Agent
from awa._internal.upstream.tools import ToolException
from awa.blueprints import Blueprint, BlueprintError
from awa.blueprints._resolve import is_builtin_reference, is_import_reference
from awa.errors import ConfigurationError, OptionalDependencyError, ToolNotFoundError
from awa.kernel.conditions import calculate
from awa.models import ModelRegistry
from awa.testing import FakeChatModel, tool_call
from awa.toolkits import catalog, utility_toolkit
from awa.toolkits._catalog import CATEGORIES
from awa.toolkits._common import build_tool, dig, tool_json, toolkits_install_hint
from awa.toolkits.utilities import resolve_timezone
from awa.tools.approval import APPROVAL_METADATA_KEY
from awa.tools.risk import ToolRisk, declared_tool_risk


@pytest.fixture
def workspace(tmp_path: Path) -> dict[str, Any]:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "notes.txt").write_text("The meeting is at 10.", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "faq.md").write_text("Returns within 30 days.", encoding="utf-8")
    database = tmp_path / "shop.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE t (x)")
    connection.commit()
    connection.close()
    return {
        "files": {"root": str(tmp_path / "data")},
        "sql": {"database": str(database)},
        "knowledge": {"path": str(tmp_path / "docs")},
        "http": {"base_url": "https://api.example.com"},
        "web": {"search": {"url_template": "https://search.example.com/?q={query}"}},
        "webhook": {"url": "https://hooks.example.com/x"},
        "email": {"sender": "bot@example.com"},
    }


def registry(model: Any) -> ModelRegistry:
    models = ModelRegistry()
    models.register("default", model)
    return models


# ---------------------------------------------------------------------------- the catalog


def test_the_catalog_covers_every_category() -> None:
    tools = catalog()
    assert len(tools) >= 30
    assert list(tools.by_category()) == list(CATEGORIES)
    assert len(set(tools.names())) == len(tools)
    for entry in tools:
        assert entry.category in CATEGORIES
        assert entry.summary and entry.summary.endswith(".")
        assert entry.reference == f"builtin:{entry.name}"
        if entry.requires_approval:
            assert entry.risk.external_action is True, entry.name


def test_every_builtin_tool_can_be_created(workspace: dict[str, Any]) -> None:
    for entry in catalog():
        if not entry.installed:
            continue
        tool = entry.create(workspace)
        assert tool.name == entry.name.removeprefix("awa.")
        assert tool.description
        assert tool.args_schema is not None
        risk = declared_tool_risk(tool)
        assert risk is not None, entry.name
        assert risk.untrusted_input == entry.risk.untrusted_input, entry.name
        assert risk.private_data == entry.risk.private_data, entry.name
        gated = (tool.metadata or {}).get(APPROVAL_METADATA_KEY) is True
        assert gated == entry.requires_approval, entry.name


def test_find_get_and_by_category() -> None:
    tools = catalog()
    assert [entry.name for entry in tools.find("csv")][:3] == ["read_csv", "query_csv", "write_csv"]
    assert tools.find("send email")[0].name == "send_email"
    assert tools.find("pdf")[0].name == "extract_pdf_text"
    assert tools.find("") == tools.entries()
    assert tools.find("quantum teleport") == []
    assert tools.get("builtin:read_file") is tools.get("read_file")
    assert "read_file" in tools and "nope" not in tools
    with pytest.raises(ToolNotFoundError, match="Unknown builtin tool 'nope'"):
        tools.get("nope")
    assert [e.name for e in tools.by_category()["files"]][0] == "list_files"
    assert tools.get("read_excel").extra == "files"


def test_create_validates_configuration(
    workspace: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = catalog()
    with pytest.raises(ConfigurationError, match="root"):
        tools.create("read_file")
    with pytest.raises(ConfigurationError, match="unknown option"):
        tools.create("read_file", workspace, colour="blue")
    tool = tools.create("write_file", workspace, requires_approval=False)
    assert APPROVAL_METADATA_KEY not in (tool.metadata or {})
    direct = tools.get("sql_query").create(database=workspace["sql"]["database"])
    assert direct.name == "sql_query"
    import awa.toolkits._catalog as catalog_module

    monkeypatch.setattr(catalog_module, "extra_installed", lambda module: False)
    with pytest.raises(OptionalDependencyError, match=re.escape(toolkits_install_hint())):
        tools.create("read_excel", workspace)
    assert "needs the files extra" in tools.get("extract_pdf_text").problems(workspace)[0]


def test_data_and_document_tools_inherit_the_files_root(workspace: dict[str, Any]) -> None:
    tool = catalog().create("read_csv", {"files": workspace["files"]})
    assert tool.name == "read_csv"
    assert catalog().get("read_csv").problems({"files": {"root": "x"}}) == []
    assert catalog().get("read_csv").problems({}) == [
        "read_csv needs toolkits.data.root (or toolkits.files.root) to be configured"
    ]


def test_risk_and_approval_follow_the_configuration() -> None:
    fetch = catalog().get("fetch_url")
    assert fetch.risk_for({}).external_action is True
    assert fetch.risk_for({"web": {"allowed_hosts": ["docs.example.com"]}}).external_action is False
    assert not catalog().get("send_email").approval_for({"email": {"requires_approval": False}})


def test_config_problems() -> None:
    problems = catalog().config_problems(
        {"files": {"root": "x", "rooot": "y"}, "ftp": {}, "sql": "shop.db"}  # type: ignore[dict-item]
    )
    assert ("toolkits.files.rooot", problems[0][1]) == problems[0]
    assert "unknown option 'rooot'" in problems[0][1]
    assert problems[1][0] == "toolkits.ftp" and "unknown toolkit" in problems[1][1]
    assert problems[2] == ("toolkits.sql", "must be a mapping of options")


# ---------------------------------------------------------------------------- blueprints


def blueprint(tools: list[str], **extra: Any) -> Blueprint:
    return Blueprint.from_dict(
        {
            "name": "desk",
            "agents": [{"name": "clerk", "instructions": "Help.", "tools": tools}],
            **extra,
        }
    )


def test_builtin_references_are_not_import_references() -> None:
    assert is_builtin_reference("builtin:read_file")
    assert not is_import_reference("builtin:read_file")
    assert is_import_reference("my_tools:lookup")


def test_check_reports_builtin_problems_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    import awa.toolkits._catalog as catalog_module

    monkeypatch.setattr(catalog_module, "extra_installed", lambda module: False)
    bp = blueprint(
        ["builtin:read_file", "builtin:teleport", "builtin:extract_pdf_text", "builtin:calculator"],
        toolkits={"documents": {"root": "./docs", "pages": 3}, "ftp": {}, "sql": {"database": "x"}},
    )
    issues = [(issue.severity, issue.path, issue.message) for issue in bp.check()]
    assert (
        "error",
        "agents[0].tools[0]",
        "read_file needs toolkits.files.root to be configured",
    ) in issues
    assert any(
        path == "agents[0].tools[1]" and "unknown builtin tool 'builtin:teleport'" in message
        for _, path, message in issues
    )
    assert any(
        path == "agents[0].tools[2]" and "needs the files extra" in message
        for _, path, message in issues
    )
    assert any(path == "toolkits.documents.pages" for _, path, _ in issues)
    assert any(path == "toolkits.ftp" and severity == "error" for severity, path, _ in issues)
    assert (
        "warning",
        "toolkits.sql",
        "toolkit 'sql' is configured but no agent uses its builtin tools",
    ) in issues


def test_a_valid_builtin_blueprint_checks_clean_and_round_trips(workspace: dict[str, Any]) -> None:
    bp = blueprint(
        ["builtin:list_files", "builtin:read_csv", "builtin:calculator"],
        toolkits={"files": workspace["files"]},
    )
    assert bp.check() == []
    again = Blueprint.from_yaml_text(bp.to_yaml())
    assert again.toolkits == {"files": workspace["files"]}


def test_default_approvals_of_builtin_tools_gate_the_risk_check(workspace: dict[str, Any]) -> None:
    risky = blueprint(
        ["builtin:fetch_url", "builtin:read_file", "builtin:send_email"],
        toolkits={"files": workspace["files"], "email": {"requires_approval": False}},
    )
    [warning] = [issue for issue in risky.check() if "untrusted content" in issue.message]
    assert "builtin:send_email" in warning.message and "lethal trifecta" in warning.message
    safe = blueprint(
        ["builtin:fetch_url", "builtin:read_file", "builtin:send_email"],
        toolkits={"files": workspace["files"], "web": {"allowed_hosts": ["docs.example.com"]}},
    )
    assert not [issue for issue in safe.check() if "untrusted content" in issue.message]
    assert safe.approval_agents() == ["clerk"]
    assert risky.approval_agents() == []


async def test_builtin_read_file_builds_and_runs_in_an_agent(workspace: dict[str, Any]) -> None:
    bp = blueprint(
        ["builtin:read_file", "builtin:calculator"], toolkits={"files": workspace["files"]}
    )
    model = FakeChatModel([tool_call("read_file", {"path": "notes.txt"}), "At 10."])
    agent = bp.build(model_registry=registry(model))
    assert isinstance(agent, Agent)
    assert [tool.name for tool in agent.tools] == ["read_file", "calculator"]
    result = await agent.run("When is the meeting?")
    assert result.output == "At 10."
    assert "The meeting is at 10." in str(model.calls[1].last_message.content)
    # sent sorted by name (prompt caching)
    assert model.calls[0].tool_names == ("calculator", "read_file")


async def test_gated_builtin_tools_need_a_checkpointer_and_pause_once(
    workspace: dict[str, Any], tmp_path: Path
) -> None:
    bp = blueprint(["builtin:write_file"], toolkits={"files": workspace["files"]})
    with pytest.raises(BlueprintError, match="checkpointer"):
        bp.build()
    model = FakeChatModel([tool_call("write_file", {"path": "a.txt", "content": "hi"}), "Done."])
    agent = bp.build(model_registry=registry(model), checkpointer=InMemorySaver())
    assert isinstance(agent, Agent)
    paused = await agent.run("Write it.")
    assert paused.is_interrupted and paused.thread_id is not None
    resumed = await agent.resume(paused.thread_id, {"action": "approve"})
    assert resumed.output == "Done."
    assert (tmp_path / "data" / "a.txt").read_text(encoding="utf-8") == "hi"

    # A blueprint approval replaces the tool's own gate: still one pause per call.
    gated = blueprint(
        ["builtin:write_file"],
        toolkits={"files": workspace["files"]},
        approvals=[{"tool": "builtin:write_file"}],
    )
    model = FakeChatModel([tool_call("write_file", {"path": "b.txt", "content": "x"}), "Ok."])
    agent = gated.build(model_registry=registry(model), checkpointer=InMemorySaver())
    assert isinstance(agent, Agent)
    paused = await agent.run("Write b.")
    assert paused.thread_id is not None
    resumed = await agent.resume(paused.thread_id, {"action": "approve"})
    assert not resumed.is_interrupted and resumed.output == "Ok."
    assert (tmp_path / "data" / "b.txt").exists()


def test_build_errors_name_the_builtin_tool(tmp_path: Path) -> None:
    bp = blueprint(["builtin:read_file"], toolkits={"files": {"root": str(tmp_path / "missing")}})
    with pytest.raises(BlueprintError, match="Cannot build 'builtin:read_file'"):
        bp.build()


class _Args(BaseModel):
    path: str


def _tool(impl: object) -> object:
    return build_tool(
        impl,  # type: ignore[arg-type]
        name="read_thing",
        description="Read a thing.",
        args_schema=_Args,
        toolkit="test",
        risk=ToolRisk(external_action=False),
    )


def test_an_unexpected_error_text_has_no_message_detail(caplog: pytest.LogCaptureFixture) -> None:
    def impl(args: _Args) -> str:
        raise OSError(
            f"cannot open /srv/jane/secrets.txt with token {fake_secret('sk-', 'abcdefghijklmnop1234')}"
        )

    tool = _tool(impl)
    with caplog.at_level(logging.ERROR, logger="awa.tools"):
        text = tool.invoke({"path": "x"})  # type: ignore[attr-defined]
    assert text == "Error: OSError: the tool failed; see the log"
    assert "jane" not in text and "sk-" not in text
    assert "OSError" in caplog.text and "read_thing" in caplog.text
    assert fake_secret("sk-", "abcdefghijklmnop1234") not in caplog.text


def test_a_tool_exception_keeps_its_own_message() -> None:
    def impl(args: _Args) -> str:
        raise ToolException("the file is too large")

    assert _tool(impl).invoke({"path": "x"}) == "Error: the file is too large"  # type: ignore[attr-defined]


def test_tool_json_has_no_nan() -> None:
    text = tool_json({"a": float("nan"), "b": [float("inf"), 1.5], "c": (float("-inf"),)})
    assert json.loads(text) == {"a": None, "b": [None, 1.5], "c": [None]}
    assert "NaN" not in text and "Infinity" not in text


def test_dig_ignores_double_minus_and_unicode_digits() -> None:
    data = {"items": [{"id": 1}, {"id": 2}]}
    assert dig(data, "items.-1.id") == 2
    assert dig(data, "items.0.id") == 1
    assert dig(data, "items.--1.id") is None
    assert dig(data, "items.\u00b2.id") is None


class _PageArgs(BaseModel):
    limit: int = 50


def _pages(args: _PageArgs) -> str:
    items = [{"id": f"m{i}", "preview": "x" * 200} for i in range(args.limit)]
    return json.dumps({"items": items, "has_more": True, "next_page": "token-1"})


def _page_tool() -> object:
    return build_tool(
        _pages,
        name="list_things",
        description="List things.",
        args_schema=_PageArgs,
        toolkit="test",
        risk=ToolRisk(),
        max_output_chars=2_000,
    )


def test_a_page_too_long_is_refused_with_a_limit_that_fits() -> None:
    tool = _page_tool()
    refused = tool.invoke({"limit": 50})
    assert refused.startswith("Error: the page of 50 items")
    fits = int(refused.split("limit=")[1].split()[0])
    page = json.loads(tool.invoke({"limit": fits}))
    assert page["next_page"] == "token-1"  # the end of the page survives
    assert len(page["items"]) == fits


def test_a_page_that_fits_is_returned_unchanged() -> None:
    assert json.loads(_page_tool().invoke({"limit": 3}))["has_more"] is True


TOOLS = {tool.name: tool for tool in utility_toolkit(holidays=["2026-12-25"])}


def call(name: str, **args: Any) -> str:
    result = TOOLS[name].invoke(args)
    assert isinstance(result, str)
    return result


def call_json(name: str, **args: Any) -> Any:
    text = call(name, **args)
    assert not text.startswith("Error"), text
    return json.loads(text)


def test_the_toolkit() -> None:
    assert list(TOOLS) == ["calculator", "datetime_now", "date_math"]
    assert [t.name for t in utility_toolkit(include=["calculator"])] == ["calculator"]
    with pytest.raises(ConfigurationError, match="unknown time zone"):
        utility_toolkit(default_timezone="Mars/Olympus")
    with pytest.raises(ConfigurationError, match="weekend"):
        utility_toolkit(weekend=[7])
    with pytest.raises(ConfigurationError, match="invalid date"):
        utility_toolkit(holidays=["christmas"])


# ---------------------------------------------------------------------------- calculator


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("(1200 * 0.19) + 15", "243"),
        ("2 ** 10", "1024"),
        ("7 // 2 + 7 % 2", "4"),
        ("-3 + +5", "2"),
        ("round(1234.5678, 2)", "1234.57"),
        ("sqrt(16) + abs(-2)", "6"),
        ("max(3, 9, 4) - min(3, 9, 4)", "6"),
        ("2 ** -1", "0.5"),
        ("10 / 4", "2.5"),
        ("pi", repr(3.141592653589793)),
    ],
)
def test_calculator(expression: str, expected: str) -> None:
    assert call("calculator", expression=expression) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo hacked')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('x')",
        "a + 1",
        "'a' * 10",
        "9 ** 9 ** 9",
        "10 ** 100000",
        "1 / 0",
        "sqrt(-1)",
        "exp(10000)",
        "[1, 2]",
        "1 if True else 2",
        "lambda: 1",
        "1 < 2",
        "True + 1",
        "x" * 3_000,
        "round(1, ndigits=2)",
        "round(5, -10 ** 7)",
        "1e400",
        "1e400 - 1e400",
    ],
)
def test_calculator_refuses_injection_and_blowups(expression: str) -> None:
    result = call("calculator", expression=expression)
    assert result.startswith("Error: "), result


def test_calculate_returns_numbers() -> None:
    assert calculate("3 * 4") == 12
    with pytest.raises(ToolException):
        calculate("")


# ---------------------------------------------------------------------------- dates


def test_datetime_now_in_a_time_zone() -> None:
    data = call_json("datetime_now", timezone="+02:00")
    assert data["utc_offset"] == "+0200"
    moment = datetime.fromisoformat(data["datetime"])
    assert moment.utcoffset() is not None
    assert call_json("datetime_now")["timezone"] == "UTC"
    assert "unknown time zone" in call("datetime_now", timezone="Nowhere/City")
    assert resolve_timezone("Europe/Berlin").utcoffset(datetime(2026, 7, 1)) is not None


def test_date_math_adds_months_to_month_ends() -> None:
    assert call_json("date_math", operation="add", date="2026-01-31", months=1)["result"] == (
        "2026-02-28"
    )
    assert (
        call_json("date_math", operation="subtract", date="2024-03-31", years=1, months=1)["result"]
        == "2023-02-28"
    )
    timed = call_json("date_math", operation="add", date="2026-03-28T22:00", hours=5)
    assert timed["result"] == "2026-03-29T03:00"
    today = call_json("date_math", operation="add", date="today", days=1)["result"]
    assert len(today) == 10 and "T" not in today  # a date, not a date and time


def test_business_days_skip_weekends_and_holidays() -> None:
    # Thu 2026-12-24 + 2 business days: Fri 25 is a holiday, then the weekend.
    moved = call_json("date_math", operation="add_business_days", date="2026-12-24", days=2)
    assert moved["result"] == "2026-12-29" and moved["weekday"] == "Tuesday"
    back = call_json("date_math", operation="add_business_days", date="2026-12-29", days=-2)
    assert back["result"] == "2026-12-24"
    count = call_json(
        "date_math",
        operation="business_days_between",
        date="2026-12-21",
        other_date="2027-01-04",
        holidays=["2027-01-01"],
    )
    assert count == {"business_days": 8}
    holiday = call_json("date_math", operation="info", date="2027-01-04", holidays=["2027-01-04"])
    assert holiday["is_business_day"] is False


def test_date_difference_and_info() -> None:
    diff = call_json(
        "date_math", operation="difference", date="2026-01-15", other_date="2026-03-14"
    )
    assert diff["days"] == 58 and diff["months"] == 1
    info = call_json("date_math", operation="info", date="2026-12-25")
    assert info["weekday"] == "Friday" and info["quarter"] == 4
    assert info["is_business_day"] is False
    assert "needs other_date" in call("date_math", operation="difference", date="2026-01-01")
    assert "invalid date" in call("date_math", operation="info", date="31.12.2026")
    assert call_json("date_math", operation="info", date="today")["result"]
