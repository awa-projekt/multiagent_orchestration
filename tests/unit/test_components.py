"""Component switches in every place they show, and the `awa new parser|connector|tool` scaffolds."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from _helpers.polling import SLOW_SUBPROCESS_TIMEOUT
from awa import Agent
from awa.blueprints.scaffold import PARSER_KINDS, ScaffoldError, create_connector, create_parser
from awa.cli import main
from awa.cli._doctor import _check_security
from awa.plugins import REGISTRY, list_plugins
from awa.plugins import switches as core
from awa.server import ApiKeyAuthenticator, Principal, create_app
from awa.testing import FakeChatModel
from awa.ui import create_ui_app

BLUEPRINT = """name: demo
agents:
  - name: helper
    instructions: Help.
    model: test
"""
TOKEN = "components-token"
BASE = "http://127.0.0.1:8766"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    folder = tmp_path / "project"
    (folder / ".awa").mkdir(parents=True)
    (folder / "blueprint.yaml").write_text(BLUEPRINT, encoding="utf-8")
    monkeypatch.setenv("AWA_PROJECT_DIR", str(folder))
    monkeypatch.chdir(folder)
    monkeypatch.syspath_prepend(str(folder))
    saved = core.python_entries()
    with REGISTRY.preserved():
        yield folder
    sys.modules.pop("own_csv_ok", None)
    for key in list(core.python_entries()):
        if key not in saved:
            core.set_python(key, None)


def _off(project: Path) -> None:
    assert main(["plugins", "switch", "guard.sql_guard", "off"]) == 0


# ---------------------------------------------------------------------- CLI


def test_cli_list_json_matches_the_python_list(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["plugins", "list", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [(r["kind"], r["name"]) for r in printed] == [
        (r.kind, r.name) for r in list_plugins(project=project)
    ]


def test_cli_switching_a_security_part_needs_no_flag_and_shows_as_off(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _off(project)
    assert "sql_guard: false" in (project / "blueprint.yaml").read_text(encoding="utf-8")
    assert main(["plugins", "list"]) == 0
    row = next(line for line in capsys.readouterr().out.splitlines() if " sql_guard " in line)
    assert "off" in row


def test_cli_unknown_name_and_bad_switch_exit_1(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["plugins", "switch", "tool.web_serch", "on"]) == 1
    assert "tool.web_search" in capsys.readouterr().err
    assert main(["plugins", "show", "tool.web_serch"]) == 1


def test_cli_check_and_reset(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (project / "own_bad_csv.py").write_text(
        "def profile_csv(path, ctx):\n    return ctx.profile\n", encoding="utf-8"
    )
    assert main(["plugins", "check", "parser.csv", "--target", "own_bad_csv:profile_csv"]) == 1
    assert "profiles_a_valid_sample" in capsys.readouterr().out
    assert main(["plugins", "check", "parser.csv"]) == 0  # the built-in passes
    assert main(["plugins", "switch", "parser.pdf", "off"]) == 0
    assert main(["plugins", "switch", "parser.pdf", "default"]) == 0
    assert "pdf" not in (project / "blueprint.yaml").read_text(encoding="utf-8")


def test_awa_data_inspect_skips_a_disabled_parser(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = project / "orders.csv"
    data.write_text("id,amount\n1,2\n", encoding="utf-8")
    assert main(["data", "inspect", str(data), "--json"]) == 0
    assert '"status": "ok"' in capsys.readouterr().out
    assert main(["plugins", "switch", "parser.csv", "off"]) == 0
    capsys.readouterr()
    main(["data", "inspect", str(data), "--json"])
    assert '"status": "unsupported"' in capsys.readouterr().out


# ---------------------------------------------------------------------- doctor, health, runs


def test_doctor_shows_a_security_part_that_is_off(project: Path) -> None:
    assert _check_security(project) == []
    _off(project)
    found = {c.name: c for c in _check_security(project)}
    assert found["security:guard.sql_guard"].status == "warn"
    assert "SECURITY OFF" in found["security:guard.sql_guard"].summary


def _echo() -> Agent:
    return Agent("echo", description="Echoes.", model=FakeChatModel(["hi"]))


async def test_health_shows_security_off_to_an_admin_and_run_records_carry_it(
    project: Path,
) -> None:
    keys = {"admin-key": Principal("ops", roles={"admin"}), "user-key": Principal("u")}
    app = create_app({"echo": _echo()}, authenticator=ApiKeyAuthenticator(keys))
    open_app = create_app({"echo": _echo()})
    admin = {"Authorization": "Bearer admin-key"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        assert (await client.get("/v1/health", headers=admin)).json() == {"status": "ok"}
        _off(project)
        assert (await client.get("/v1/health", headers=admin)).json() == {
            "status": "ok",
            "security_off": ["guard.sql_guard"],
        }
        # Anonymous, wrong or non-admin credentials: alive, but which parts are off stays hidden.
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Bearer user-key"}):
            answer = await client.get("/v1/health", headers=headers)
            assert answer.status_code == 200 and answer.json() == {"status": "ok"}
    transport = httpx.ASGITransport(app=open_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        assert (await client.get("/v1/health")).json() == {"status": "ok"}
    result = await _echo().run("hello")
    assert result.metadata["security_off"] == ["guard.sql_guard"]


async def test_a_run_without_security_changes_has_no_flag(project: Path) -> None:
    result = await _echo().run("hello")
    assert "security_off" not in result.metadata


# ---------------------------------------------------------------------- web app


def test_the_web_app_state_lists_the_security_parts_that_are_off(project: Path) -> None:
    app = create_ui_app(project, token=TOKEN, port=8766, environ={})
    with TestClient(app, base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        assert client.get("/api/state").json()["security_off"] == []
        _off(project)
        assert client.get("/api/state").json()["security_off"] == ["guard.sql_guard"]


# ---------------------------------------------------------------------- traces


async def test_spans_carry_the_switches_in_effect(project: Path) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from awa.observability.otel import OpenTelemetrySubscriber

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _off(project)
    assert main(["plugins", "switch", "parser.pdf", "off"]) == 0
    (project / "own_csv_ok.py").write_text(
        "from awa.data._parse import builtin_parsers\n"
        "profile_csv = {k: p for k, p, _ in builtin_parsers()}['csv']\n",
        encoding="utf-8",
    )
    from awa.data import register_parser

    register_parser("csv", importlib.import_module("own_csv_ok").profile_csv, replace=True)
    hook = OpenTelemetrySubscriber(provider.get_tracer("test"))
    agent = Agent("echo", description="Echoes.", model=FakeChatModel(["hi"]), hooks=[hook])
    await agent.run("hello")
    provider.shutdown()
    attributes = [dict(span.attributes or {}) for span in exporter.get_finished_spans()]
    assert attributes and all(
        a.get("awa.security_off") == "guard.sql_guard"
        and a.get("awa.components.disabled") == "guard.sql_guard, parser.pdf"
        and a.get("awa.components.replaced") == "parser.csv"
        for a in attributes
    )


def _pytest(test_file: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            str(test_file),
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=SLOW_SUBPROCESS_TIMEOUT,
        check=False,
    )


@pytest.mark.slow
@pytest.mark.parametrize("kind", ["parser", "connector", "tool"])
def test_each_scaffold_passes_its_contract(kind: str, tmp_path: Path) -> None:
    name = {"parser": "text", "connector": "inventory", "tool": "lookup_record"}[kind]
    assert main(["new", kind, name, "--dir", str(tmp_path)]) == 0
    [test_file] = (tmp_path / "tests").glob("test_*.py")
    outcome = _pytest(test_file, tmp_path)
    assert outcome.returncode == 0, outcome.stdout + outcome.stderr
    assert "contract" in test_file.read_text(encoding="utf-8")


def test_a_generated_parser_and_connector_can_be_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.connectors import get_connector
    from awa.plugins import register
    from awa.testing.plugin_contract import verify

    monkeypatch.setenv("AWA_PROJECT_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path))
    create_parser("text", tmp_path)
    create_connector("inventory", tmp_path)
    try:
        with REGISTRY.preserved():
            register("parsers", "text", "text_parser:profile_text", replace=True)
            register("connectors", "inventory", "inventory_connector:CONNECTOR")
            assert get_connector("inventory").name == "inventory"
            from awa.data._parse import PARSERS

            assert verify("parser.text", PARSERS["text"]).passed
    finally:
        for module in ("text_parser", "inventory_connector"):
            sys.modules.pop(module, None)


@pytest.mark.parametrize("kind", PARSER_KINDS)
def test_every_scaffolded_parser_passes_its_contract(tmp_path: Path, kind: str) -> None:
    import importlib.util

    from awa.testing.plugin_contract import verify

    [module_file, _] = create_parser(kind, tmp_path)
    spec = importlib.util.spec_from_file_location(f"scaffold_{kind}_parser", module_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    result = verify(f"parser.{kind}", getattr(module, f"profile_{kind}"))

    assert result.passed, result.summary()


def test_the_generated_tests_of_a_multi_word_connector_pass(tmp_path: Path) -> None:
    import subprocess

    create_connector("order_book", tmp_path)

    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=240,
    )

    assert done.returncode == 0, done.stdout + done.stderr


def test_scaffold_names_are_checked(tmp_path: Path) -> None:
    with pytest.raises(ScaffoldError, match="file kind"):
        create_parser("xls", tmp_path)
    with pytest.raises(ScaffoldError, match="Invalid tool name"):
        create_connector("Bad Name", tmp_path)
