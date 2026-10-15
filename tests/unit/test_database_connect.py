"""Connecting a database: `awa connect --database`, the Data page's systems part,
and no leaks."""

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from awa._internal.install import install_hint
from awa.blueprints import Blueprint, explain
from awa.blueprints._connect import (
    check_database,
    database_snippet,
    database_url,
    database_variable,
    run_connect_database,
    save_database,
)
from awa.cli import main
from awa.core.dotenv import read_env_file
from awa.ui import create_ui_app

PASSWORD = "Sup3r-Secret-Pw-8841"
BROKEN = f"postgresql://reporting:{PASSWORD}@127.0.0.1:1/sales"  # nothing listens on port 1
TOKEN = "db-token"
BASE = "http://127.0.0.1:8765"
SAFE = {"Origin": BASE}


@pytest.fixture
def shop(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "shop.db"
    path.parent.mkdir()
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, secret_note TEXT);"
        "CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id),"
        " total REAL);"
        "INSERT INTO customers VALUES (1, 'ACME GmbH', 'private-note-1');"
        "INSERT INTO orders VALUES (1, 1, 49.5);"
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for name in [n for n in os.environ if n.endswith("DATABASE_URL")]:
        monkeypatch.delenv(name)


def test_names_and_addresses(shop: Path) -> None:
    assert database_variable("Shop") == "SHOP_DATABASE_URL"
    assert database_variable(" ERP data-base ") == "ERP_DATA_BASE_DATABASE_URL"
    for bad in ("", "  ", "1st", "---"):
        with pytest.raises(ValueError, match="short name"):
            database_variable(bad)
    assert database_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"
    assert database_url(str(shop)) == "sqlite:///" + shop.resolve().as_posix()
    for bad in ("", str(shop.parent / "nothing.db"), str(shop.parent)):
        with pytest.raises(ValueError):
            database_url(bad)


def test_the_test_names_tables_but_reads_no_data(shop: Path) -> None:
    ok, message = check_database(str(shop))

    assert ok and "2 table(s) and view(s): customers, orders" in message
    assert "ACME" not in message and "private-note" not in message


def test_connect_database_tests_saves_and_prints_the_snippet(shop: Path, tmp_path: Path) -> None:
    out = io.StringIO()
    asked: list[str] = []

    def secret(prompt: str) -> str:
        asked.append(prompt)
        return str(shop)

    code = run_connect_database(tmp_path / "project", "Shop", secret=secret, out=out)

    assert code == 0 and "SHOP_DATABASE_URL" in asked[0]
    assert read_env_file(tmp_path / "project" / ".env") == {
        "SHOP_DATABASE_URL": "sqlite:///" + shop.resolve().as_posix()
    }
    text = out.getvalue()
    assert "customers, orders" in text and "Saved SHOP_DATABASE_URL" in text
    assert str(shop.resolve().as_posix()) not in text
    snippet = text.split("\n\n", 2)[2]
    blueprint = Blueprint.from_yaml_text("name: shop\n" + snippet)  # the shown lines are valid
    assert blueprint.connectors["shop"] == {
        "use": "sql",
        "env": {"DATABASE_URL": "SHOP_DATABASE_URL"},
        "read_only": True,
    }
    assert blueprint.agent("analyst").tools == ["connector:shop.schema", "connector:shop.query"]


@pytest.mark.slow
def test_a_failing_test_saves_nothing_and_leaks_nothing(tmp_path: Path) -> None:
    out = io.StringIO()

    code = run_connect_database(tmp_path, "sales", secret=lambda _: BROKEN, out=out)

    assert code == 1 and "connection test failed" in out.getvalue()
    assert PASSWORD not in out.getvalue()
    assert not (tmp_path / ".env").exists()
    saved, message, variable = save_database(tmp_path / ".env", "sales", BROKEN)
    assert not saved and variable == "SALES_DATABASE_URL" and PASSWORD not in message


def test_a_bad_input_is_explained(tmp_path: Path) -> None:
    out = io.StringIO()

    assert run_connect_database(tmp_path, "shop", secret=lambda _: "not a url", out=out) == 1
    assert "SQLite file" in out.getvalue()
    assert run_connect_database(tmp_path, "!!", secret=lambda _: "x", out=out) == 1
    assert "short name" in out.getvalue()


def test_the_command_line_asks_with_hidden_input(
    shop: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda prompt: str(shop))

    code = main(["connect", str(tmp_path), "--database", "shop"])

    assert code == 0 and "SHOP_DATABASE_URL" in read_env_file(tmp_path / ".env")
    assert "Test passed" in capsys.readouterr().out


def test_the_mssql_snippet_asks_for_the_text_check_option() -> None:
    assert "accept_text_check: true" in database_snippet("erp", "mssql://u:p@h/db")
    assert "accept_text_check" not in database_snippet("erp", "postgresql://u:p@h/db")


@pytest.fixture
def environ() -> dict[str, str]:
    return {}


@pytest.fixture
def client(tmp_path: Path, environ: dict[str, str]) -> Iterator[TestClient]:
    app = create_ui_app(tmp_path / "ui", token=TOKEN, port=8765, environ=environ)
    with TestClient(app, base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        yield client


def test_the_data_page_offers_databases_in_its_systems_part(client: TestClient) -> None:
    page = client.get("/").text
    kinds = client.get("/api/systems").json()

    assert 'id="system-type"' in page and "External systems" in page
    assert 'id="db-url"' not in page  # the database form is on the Your data page
    assert "sql" in {t["connector"] for t in kinds["types"]}
    assert {d["id"] for d in kinds["databases"]} == {"sqlite", "postgresql", "mysql", "mssql"}
    form = client.post("/api/systems/form", json={"connector": "sql", "name": "Shop"}, headers=SAFE)
    [field] = form.json()["fields"]
    assert field["name"] == "SHOP_DATABASE_URL" and field["kind"] == "secret"


def test_the_systems_part_tests_without_saving_then_saves(
    client: TestClient, shop: Path, tmp_path: Path, environ: dict[str, str]
) -> None:
    values = {"SHOP_DATABASE_URL": str(shop)}
    tested = client.post(
        "/api/systems/test",
        json={"connector": "sql", "name": "Shop", "values": values},
        headers=SAFE,
    )

    assert tested.status_code == 200 and tested.json()["ok"] is True
    assert "customers, orders" in tested.json()["message"]
    assert not (tmp_path / "ui" / ".env").exists()

    saved = client.post(
        "/api/systems", json={"connector": "sql", "name": "Shop", "values": values}, headers=SAFE
    )

    body = saved.json()
    assert saved.status_code == 200 and body["saved"] == ["SHOP_DATABASE_URL"]
    assert body["system"]["tools"] == ["shop_schema", "shop_query"]
    assert {t["name"] for t in body["system"]["tables"]} == {"customers", "orders"}
    assert shop.resolve().as_posix() not in saved.text and str(shop) not in saved.text
    assert environ["SHOP_DATABASE_URL"] == "sqlite:///" + shop.resolve().as_posix()
    setup = client.get("/api/setup").json()
    assert "SHOP_DATABASE_URL" in setup["env_names"]
    assert str(shop) not in json.dumps(setup) + client.get("/api/systems").text


@pytest.mark.slow
def test_the_web_app_never_shows_the_password(
    client: TestClient, tmp_path: Path, environ: dict[str, str]
) -> None:
    body = {"connector": "sql", "name": "Sales", "values": {"SALES_DATABASE_URL": BROKEN}}
    for path in ("/api/systems/test", "/api/systems"):
        response = client.post(path, json=body, headers=SAFE)
        assert PASSWORD not in response.text and response.json()["ok"] is False
    assert response.json()["saved"] == [] and response.json()["system"] is None
    assert "SALES_DATABASE_URL" not in environ and not (tmp_path / "ui" / ".env").exists()
    unsafe = client.post("/api/systems", json=body)  # no Origin header
    assert unsafe.status_code in (401, 403)


def test_a_database_address_names_its_kind_and_what_is_missing(
    shop: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    from awa.connectors._local import describe_database

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name, *a: None if name == "pymysql" else real(name, *a)
    )

    found = {
        "shop": describe_database(f"sqlite:///{shop.as_posix()}"),
        "gone": describe_database(f"sqlite:///{(tmp_path / 'gone.db').as_posix()}"),
        "weird": describe_database(f"oracle://u:{PASSWORD}@h/db"),
        "erp": describe_database(f"mysql://u:{PASSWORD}@h/db"),
    }

    assert found["shop"] == ("SQLite", None)
    assert "gone.db does not exist" in str(found["gone"][1])
    assert "must start with" in str(found["weird"][1])
    assert f"pymysql: {install_hint('sql')}" in str(found["erp"][1])
    assert PASSWORD not in json.dumps(found)


def test_auto_matching_offers_the_schema_tool_for_database_tasks() -> None:
    blueprint = Blueprint.from_yaml_text(
        "name: analysis\nauto: true\nagents:\n"
        "  analyst: Reads the database schema and answers questions about the tables and columns.\n"
    )

    report = explain(blueprint)

    names = {match.name for agent in report.agents for match in agent.matches}
    assert "builtin:sql_schema" in names
