"""The Connect step, the generated Set up forms and the Setup page."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sqlite3
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
import httpx
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from _helpers.fake_secrets import fake_secret
from awa import connectors
from awa.blueprints import Blueprint
from awa.connectors import (
    ConnectorStatus,
    EnvironmentReport,
    MockTransport,
    ProviderStatus,
    mock_response,
)
from awa.connectors import _discover as discovery
from awa.core import providers as provider_table
from awa.core.providers import ProviderSpec
from awa.ui import Project, create_ui_app
from awa.ui import _connections as connections
from awa.ui._connections import advice, check_link, kind_of, label_of
from awa.ui._project import env_names

TOKEN = "connect-token"
BASE = "http://127.0.0.1:8765"
SAFE = {"Origin": BASE}
SECRET = "hunter2-very-secret-4711"


def sql_step(key: str, system: str) -> dict[str, Any]:
    variable = f"{key.upper()}_DATABASE_URL"
    return {
        "system": system,
        "key": key,
        "status": "map",
        "connector": "sql",
        "env_vars": [variable],
        "login": [],
        "config": {"use": "sql", "env": {"DATABASE_URL": variable}},
        "switch": {},
    }


def jira_step() -> dict[str, Any]:
    return {
        "system": "Tickets",
        "key": "tickets",
        "status": "configure",
        "connector": "jira",
        "env_vars": ["JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"],
        "login": [],
        "config": {"use": "jira"},
        "switch": {"connector:tickets.query": ["connector:tickets.search_issues"]},
    }


def make_project(folder: Path, steps: list[dict[str, Any]]) -> None:
    connectors_yaml = "".join(
        f"  {step['key']}:\n    use: workspace\n    root: data\n    database: {step['key']}.db\n"
        for step in steps
    )
    tools = []
    for step in steps:
        tools.append(f"      - connector:{step['key']}.query")
    blueprint = (
        "name: desk\nagents:\n  - name: clerk\n    description: Looks things up.\n"
        "    instructions: Look it up.\n    tools:\n" + "\n".join(tools) + "\n"
        "connectors:\n" + connectors_yaml
    )
    (folder / "blueprint.yaml").write_text(blueprint, encoding="utf-8")
    guide = "# Connecting desk\n\n" + "".join(
        f"## {i}. {step['system']}\n\n<!-- awa:step {json.dumps(step)} -->\nSteps.\n\n"
        for i, step in enumerate(steps, 1)
    )
    (folder / "CONNECT.md").write_text(guide, encoding="utf-8")
    (folder / "data").mkdir(exist_ok=True)
    for step in steps:
        with sqlite3.connect(folder / "data" / f"{step['key']}.db") as db:
            db.execute("CREATE TABLE IF NOT EXISTS invoices (invoice_id TEXT, status TEXT)")
            db.execute("INSERT INTO invoices VALUES ('INV-1', 'open')")


@pytest.fixture
def environ() -> dict[str, str]:
    return {}


@pytest.fixture
def make_client(tmp_path: Path, environ: dict[str, str]) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def make(
        steps: list[dict[str, Any]] | None = None, transport: MockTransport | None = None
    ) -> TestClient:
        make_project(tmp_path, steps or [sql_step("ledger", "Invoice ledger")])
        app = create_ui_app(tmp_path, token=TOKEN, port=8765, environ=environ, transport=transport)
        client = TestClient(app, base_url=BASE).__enter__()
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def database_url(folder: Path, key: str = "ledger") -> str:
    return "sqlite:///" + (folder / "data" / f"{key}.db").resolve().as_posix()


# ---------------------------------------------------------------------- labels, kinds, links


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("JIRA_API_TOKEN", "Jira API token"),
        ("AZURE_OPENAI_ENDPOINT", "Azure OpenAI endpoint"),
        ("LEDGER_DATABASE_URL", "Ledger database URL"),
        ("GITHUB_TOKEN", "GitHub token"),
    ],
)
def test_a_variable_name_becomes_a_readable_label(name: str, label: str) -> None:
    assert label_of(name) == label


@pytest.mark.parametrize(
    ("name", "kwargs", "kind"),
    [
        ("JIRA_API_TOKEN", {"secret": True}, "secret"),
        ("AZURE_OPENAI_API_KEY", {}, "secret"),
        ("AZURE_OPENAI_ENDPOINT", {}, "link"),
        ("JIRA_URL", {"secret": False}, "link"),
        ("OLLAMA_HOST", {"example": "http://"}, "link"),
        ("SMTP_HOST", {"secret": False, "example": "smtp.example.com"}, "text"),
        ("ORDERS_DATABASE_URL", {}, "text"),
        ("OPENAI_API_VERSION", {}, "text"),
    ],
)
def test_the_kind_of_a_setting_decides_how_it_is_entered(
    name: str, kwargs: dict[str, Any], kind: str
) -> None:
    assert kind_of(name, **kwargs) == kind


@pytest.mark.parametrize(
    "value", ["https://acme.atlassian.net", " https://api.example.com/v3 ", "https://[::1]:8443/x"]
)
def test_an_https_address_is_accepted(value: str) -> None:
    assert check_link("JIRA_URL", value) == value.strip()


@pytest.mark.parametrize(
    "value",
    [
        "http://acme.example.com",
        "ftp://acme.example.com",
        "https://user:zq9kx@acme.example.com",
        "https://acme example.com",
        "https://",
        "acme.example.com",
        "",
        "https://acme.example.com/" + "a" * 3000,
        "javascript:alert(1)",
    ],
)
def test_other_addresses_are_refused_without_echoing_them(value: str) -> None:
    with pytest.raises(ValueError, match="Jira URL") as error:
        check_link("JIRA_URL", value)

    assert "zq9kx" not in str(error.value) and "acme" not in str(error.value)


def test_plain_http_is_for_this_computer_or_an_explicit_option() -> None:
    assert check_link("OLLAMA_HOST", "http://localhost:11434") == "http://localhost:11434"
    assert check_link("X_URL", "http://intranet.example", allow_http=True)
    with pytest.raises(ValueError, match="https"):
        check_link("X_URL", "http://intranet.example")
    assert check_link("CRM_SPEC", "specs/crm.json", allow_path=True) == "specs/crm.json"


@pytest.mark.parametrize(
    ("message", "ok", "expected"),
    [
        ("HTTP 401 Unauthorized", False, "refused the key"),
        ("HTTP 404 not found", False, "address was not found"),
        ("the request timed out", False, "did not answer in time"),
        ("cannot resolve host 'x': Name or service not known", False, "could not reach"),
        ("not configured: set JIRA_URL", False, "missing settings"),
        ("HTTP 503", False, "problem on its side"),
        ("the SQLite file 'x.db' does not exist", False, "file or folder was not found"),
        ("something odd", False, "ask the person"),
        ("anything", True, "works"),
    ],
)
def test_a_failed_test_gets_advice_in_plain_words(message: str, ok: bool, expected: str) -> None:
    assert expected in advice(message, ok)


# ---------------------------------------------------------------------- the Set up forms


def test_the_model_forms_come_from_the_provider_tables(
    make_client: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    newco = ProviderSpec("newco", "Newco", required=("NEWCO_API_KEY", "NEWCO_BASE_URL"))
    for module in (provider_table, discovery, connections):
        monkeypatch.setattr(module, "PROVIDERS", (*provider_table.PROVIDERS, newco))
    client = make_client()

    providers = {p["name"]: p for p in client.get("/api/setup").json()["providers"]}

    azure = {f["name"]: f["kind"] for f in providers["azure_openai"]["fields"]}
    assert azure == {
        "AZURE_OPENAI_API_KEY": "secret",
        "AZURE_OPENAI_ENDPOINT": "link",
        "AZURE_OPENAI_DEPLOYMENT_NAME": "text",
        "OPENAI_API_VERSION": "text",
        "AZURE_OPENAI_AUTH": "text",
        "AZURE_TENANT_ID": "text",
        "AZURE_CLIENT_ID": "text",
        "AZURE_CLIENT_SECRET": "secret",
        "AZURE_AUTHORITY_HOST": "text",
    }
    assert providers["newco"]["title"] == "Newco"  # a new provider needs no UI code
    assert [f["kind"] for f in providers["newco"]["fields"]] == ["secret", "link"]
    assert "MISTRAL_API_KEY" in {f["name"] for f in providers["mistralai"]["fields"]}
    assert providers["ollama"]["fields"][0]["allow_http"] is True


def test_the_set_up_page_refuses_a_bad_address_and_saves_a_good_one(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()
    bad = "http://leak.example/?key=" + SECRET

    refused = client.post(
        "/api/setup", json={"values": {"AZURE_OPENAI_ENDPOINT": bad}}, headers=SAFE
    )
    saved = client.post(
        "/api/setup",
        json={
            "values": {
                "AZURE_OPENAI_ENDPOINT": "https://my-resource.openai.azure.com",
                "OLLAMA_HOST": "http://localhost:11434",
            }
        },
        headers=SAFE,
    )

    assert refused.status_code == 400
    assert SECRET not in refused.text and "leak.example" not in refused.text
    assert "https" in refused.json()["error"]["message"]
    assert saved.status_code == 200
    env = dotenv_values(tmp_path / ".env")
    assert env["AZURE_OPENAI_ENDPOINT"] == "https://my-resource.openai.azure.com"


# ---------------------------------------------------------------------- the Connect page


def test_each_system_gets_a_form_from_its_connector(make_client: Callable[..., TestClient]) -> None:
    client = make_client([sql_step("ledger", "Invoice ledger"), jira_step()])

    data = client.get("/api/connections").json()

    assert data["exists"] is True
    ledger, tickets = data["systems"]
    assert (ledger["system"], ledger["connector"], ledger["status"], ledger["mode"]) == (
        "Invoice ledger",
        "sql",
        "not_set_up",
        "sample",
    )
    assert [(f["name"], f["kind"], f["required"], f["set"]) for f in ledger["fields"]] == [
        ("LEDGER_DATABASE_URL", "secret", True, False)
    ]
    assert ledger["fields"][0]["description"]
    fields = {f["name"]: f for f in tickets["fields"]}
    assert fields["JIRA_URL"]["kind"] == "link" and fields["JIRA_EMAIL"]["kind"] == "text"
    assert fields["JIRA_API_TOKEN"]["kind"] == "secret"
    assert [f["name"] for f in tickets["optional"]] == ["JIRA_TOKEN"]
    assert tickets["docs"].startswith("https://") and tickets["setup"]
    assert data["counts"] == {"connected": 0, "attention": 0, "not_set_up": 2}


def test_a_value_is_saved_tested_and_the_system_switched_without_echoing_it(
    make_client: Callable[..., TestClient], tmp_path: Path, environ: dict[str, str]
) -> None:
    client = make_client()
    address = database_url(tmp_path)

    response = client.post(
        "/api/connections/ledger", json={"values": {"LEDGER_DATABASE_URL": address}}, headers=SAFE
    )

    body = response.json()
    assert response.status_code == 200
    assert body["saved"] == ["LEDGER_DATABASE_URL"]
    assert body["test"]["ok"] is True and "invoices" in body["test"]["message"]
    assert body["connected"] is True and body["removed"] == []
    assert address not in response.text
    assert dotenv_values(tmp_path / ".env")["LEDGER_DATABASE_URL"] == address
    assert environ["LEDGER_DATABASE_URL"] == address  # exported, as the Set up page does
    if os.name == "posix":
        assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    switched = Blueprint.from_yaml_file(tmp_path / "blueprint.yaml").connectors["ledger"]
    assert switched["use"] == "sql"
    card = client.get("/api/connections").json()["systems"][0]
    assert card["status"] == "connected" and card["mode"] == "real"
    assert card["fields"][0]["set"] is True
    assert address not in client.get("/api/connections").text


def test_review_and_use_it_follow_the_connection_once_it_is_saved(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client([sql_step("ledger", "Invoice ledger"), sql_step("orders", "Orders")])

    def seen() -> tuple[list[tuple[str, str]], list[str]]:
        review = client.get("/api/project").json()
        return (
            [(step["system"], step["status"]) for step in review["steps"]],
            client.get("/api/deploy").json()["not_connected"],
        )

    before = seen()
    client.post(
        "/api/connections/ledger/test",
        json={"values": {"LEDGER_DATABASE_URL": database_url(tmp_path)}},
        headers=SAFE,
    )
    tested_only = seen()
    client.post(
        "/api/connections/ledger",
        json={"values": {"LEDGER_DATABASE_URL": database_url(tmp_path)}},
        headers=SAFE,
    )
    after = seen()

    assert before == ([("Invoice ledger", "map"), ("Orders", "map")], ["Invoice ledger", "Orders"])
    assert tested_only == before  # a test changes nothing
    assert after == ([("Invoice ledger", "ready"), ("Orders", "map")], ["Orders"])


def test_the_test_button_tests_what_was_typed_and_saves_nothing(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()
    good = database_url(tmp_path)
    missing = "sqlite:///" + (tmp_path / "data" / "nowhere.db").as_posix()

    passed = client.post(
        "/api/connections/ledger/test", json={"values": {"LEDGER_DATABASE_URL": good}}, headers=SAFE
    ).json()
    failed = client.post(
        "/api/connections/ledger/test",
        json={"values": {"LEDGER_DATABASE_URL": missing}},
        headers=SAFE,
    ).json()
    empty = client.post("/api/connections/ledger/test", json={"values": {}}, headers=SAFE).json()

    assert passed["ok"] is True and passed["advice"] == "The connection works."
    assert failed["ok"] is False and failed["advice"]
    assert empty["ok"] is False and "missing settings" in empty["advice"]
    assert not (tmp_path / ".env").exists()
    assert "sqlite:///" not in json.dumps(failed)  # the address is not echoed


def test_a_failed_connection_is_saved_but_not_switched(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()
    missing = "sqlite:///" + (tmp_path / "data" / "nowhere.db").as_posix()

    body = client.post(
        "/api/connections/ledger", json={"values": {"LEDGER_DATABASE_URL": missing}}, headers=SAFE
    ).json()

    assert body["saved"] == ["LEDGER_DATABASE_URL"] and body["connected"] is False
    assert Blueprint.from_yaml_file(tmp_path / "blueprint.yaml").connectors["ledger"]["use"] == (
        "workspace"
    )
    card = client.get("/api/connections").json()["systems"][0]
    assert card["status"] == "attention" and card["mode"] == "sample"


@pytest.mark.parametrize(
    ("key", "values", "message"),
    [
        ("ledger", {"PATH": "/evil"}, "not a setting of Invoice ledger"),
        ("ledger", {"AWA_DEFAULT_MODEL": "x:y"}, "not a setting"),
        ("nothere", {}, "no system"),
    ],
)
def test_only_the_names_of_the_systems_form_are_accepted(
    make_client: Callable[..., TestClient],
    tmp_path: Path,
    environ: dict[str, str],
    key: str,
    values: dict[str, str],
    message: str,
) -> None:
    client = make_client()

    response = client.post(f"/api/connections/{key}", json={"values": values}, headers=SAFE)

    assert response.status_code == 400 and message in response.json()["error"]["message"]
    assert not (tmp_path / ".env").exists() and not environ


def test_a_link_field_checks_the_address_before_anything_is_written(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client([jira_step()])

    response = client.post(
        "/api/connections/tickets",
        json={"values": {"JIRA_URL": "http://insecure.example", "JIRA_API_TOKEN": SECRET}},
        headers=SAFE,
    )

    assert response.status_code == 400
    assert "Jira URL" in response.json()["error"]["message"]
    assert SECRET not in response.text
    assert not (tmp_path / ".env").exists()


def test_a_catalog_connector_uses_its_own_health_probe(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    transport = MockTransport(
        {"GET /rest/api/2/myself": mock_response(401, json={"message": "no"})}
    )
    client = make_client([jira_step()], transport=transport)
    values = {
        "JIRA_URL": "https://acme.atlassian.net",
        "JIRA_EMAIL": "me@acme.example",
        "JIRA_API_TOKEN": SECRET,
    }

    failed = client.post("/api/connections/tickets/test", json={"values": values}, headers=SAFE)
    transport.add("GET /rest/api/2/myself", mock_response(200, json={"displayName": "Me"}))
    passed = client.post("/api/connections/tickets/test", json={"values": values}, headers=SAFE)

    urls = [request.url for request in transport.requests]
    assert urls == ["https://acme.atlassian.net/rest/api/2/myself"] * 2
    assert SECRET not in failed.text and "refused the key" in failed.json()["advice"]
    assert passed.json()["ok"] is True
    assert not (tmp_path / ".env").exists()  # a test saves nothing


def test_complete_settings_wait_for_a_test_and_partial_ones_need_attention(
    make_client: Callable[..., TestClient], environ: dict[str, str]
) -> None:
    client = make_client([jira_step()])
    environ["JIRA_URL"] = "https://acme.atlassian.net"

    partial = client.get("/api/connections").json()["systems"][0]
    environ.update(JIRA_EMAIL="me@acme.example", JIRA_API_TOKEN=SECRET)
    complete = client.get("/api/connections").json()["systems"][0]

    assert partial["status"] == "attention" and "Still missing" in partial["status_text"]
    assert complete["status"] == "attention" and "Save and connect" in complete["status_text"]
    assert SECRET not in client.get("/api/connections").text


def test_without_a_system_there_is_nothing_to_connect(tmp_path: Path) -> None:
    app = create_ui_app(tmp_path, token=TOKEN, port=8765, environ={})
    with TestClient(app, base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        data = client.get("/api/connections").json()

    assert data == {"exists": False, "systems": []}


def test_the_systems_a_blueprint_uses_without_connect_steps_get_forms_too(
    tmp_path: Path, environ: dict[str, str]
) -> None:
    (tmp_path / "blueprint.yaml").write_text(
        "name: x\nagents:\n  - name: a\n    description: d\n    instructions: i\n"
        "    tools: ['connector:slack.read_channel']\n",
        encoding="utf-8",
    )
    app = create_ui_app(tmp_path, token=TOKEN, port=8765, environ=environ)
    with TestClient(app, base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        card = client.get("/api/connections").json()["systems"][0]

    assert (card["key"], card["connector"], card["status"]) == ("slack", "slack", "not_set_up")
    assert {f["name"] for f in card["fields"]} == {"SLACK_BOT_TOKEN"}


HAND_WRITTEN = """\
name: crm_desk
agents:
  - name: clerk
    description: Looks customers up.
    instructions: Look it up.
    tools: ['connector:crm.get']
connectors:
  crm: {use: rest}
"""


def test_a_hand_written_system_is_named_and_labelled_without_repeating_itself(
    tmp_path: Path, environ: dict[str, str]
) -> None:
    (tmp_path / "blueprint.yaml").write_text(HAND_WRITTEN, encoding="utf-8")
    app = create_ui_app(tmp_path, token=TOKEN, port=8765, environ=environ)
    with TestClient(app, base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        card = client.get("/api/connections").json()["systems"][0]
        refused = client.post(
            "/api/connections/crm",
            json={"values": {"CRM_URL": "http://insecure.example"}},
            headers=SAFE,
        )

    assert (card["system"], card["connector_title"]) == ("CRM", "REST API")
    assert {f["name"]: f["label"] for f in (*card["fields"], *card["optional"])} == {
        "CRM_URL": "API link",
        "CRM_TOKEN": "API key",
        "CRM_USERNAME": "User name",
        "CRM_PASSWORD": "Password",
    }
    assert card["status_text"] == "Not set up: enter the settings below to connect CRM."
    assert "sample data" not in card["status_text"] and card["sample"] is False
    assert "CRM_URL to the API's base address" in card["setup"] and "<NAME>" not in card["setup"]
    assert refused.status_code == 400 and "API link" in refused.json()["error"]["message"]


def test_a_generated_system_names_its_database_without_the_system_prefix(
    make_client: Callable[..., TestClient],
) -> None:
    card = make_client().get("/api/connections").json()["systems"][0]

    assert [f["label"] for f in card["fields"]] == ["Database connection address"]
    assert "stand-in" in card["status_text"] and card["sample"] is True


@pytest.mark.parametrize(
    ("key", "title"), [("crm", "CRM"), ("order_desk", "Order desk"), ("hr_portal", "HR portal")]
)
def test_a_system_key_becomes_a_readable_title(key: str, title: str) -> None:
    from awa.ui._connections import system_title

    assert system_title(key) == title


# ---------------------------------------------------------------------- security of the endpoints

ENDPOINTS = [("post", "/api/connections/ledger/test"), ("post", "/api/connections/ledger")]


@pytest.mark.parametrize(("method", "path"), ENDPOINTS)
@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 403),
        ({"Origin": "http://attacker.example"}, 403),
        ({"Origin": BASE, "Authorization": "Bearer forged"}, 401),
        ({"Origin": BASE, "Host": "attacker.example"}, 421),
    ],
)
def test_the_connect_endpoints_keep_the_token_origin_and_host_checks(
    make_client: Callable[..., TestClient],
    tmp_path: Path,
    method: str,
    path: str,
    headers: dict[str, str],
    status: int,
) -> None:
    client = make_client()

    response = getattr(client, method)(
        path, json={"values": {"LEDGER_DATABASE_URL": SECRET}}, headers=headers
    )

    assert response.status_code == status
    assert not (tmp_path / ".env").exists()


def test_without_the_session_the_connect_endpoints_are_closed(tmp_path: Path) -> None:
    make_project(tmp_path, [sql_step("ledger", "Invoice ledger")])
    app = create_ui_app(tmp_path, token=TOKEN, port=8765, environ={})
    with TestClient(app, base_url=BASE) as client:
        got = [
            client.get("/api/connections"),
            client.post("/api/connections/ledger", json={"values": {}}, headers=SAFE),
            client.post("/api/connections/ledger/test", json={"values": {}}, headers=SAFE),
        ]

    assert [r.status_code for r in got] == [401, 401, 401]


@pytest.mark.parametrize("key", ["Ledger", "../etc", "a b", "1x", "x" * 80])
def test_a_system_key_must_be_a_plain_name(
    make_client: Callable[..., TestClient], key: str
) -> None:
    response = make_client().post(f"/api/connections/{key}", json={"values": {}}, headers=SAFE)

    assert response.status_code in (404, 422)


def test_a_body_that_is_not_a_form_is_reported_without_echoing_it(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client().post(
        "/api/connections/ledger", json={"values": {"LEDGER_DATABASE_URL": 918273645}}, headers=SAFE
    )

    assert response.status_code == 422 and "918273645" not in response.text


async def test_two_saves_at_once_both_end_up_in_the_env_file(
    tmp_path: Path, environ: dict[str, str]
) -> None:
    make_project(tmp_path, [sql_step("ledger", "Ledger"), sql_step("orders", "Orders")])
    app = create_ui_app(tmp_path, token=TOKEN, port=8765, environ=environ)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        answers = await asyncio.gather(
            *(
                client.post(
                    f"/api/connections/{key}",
                    json={"values": {f"{key.upper()}_DATABASE_URL": database_url(tmp_path, key)}},
                    headers=SAFE,
                )
                for key in ("ledger", "orders")
            )
        )

    assert [a.status_code for a in answers] == [200, 200]
    env = dotenv_values(tmp_path / ".env")
    assert set(env) == {"LEDGER_DATABASE_URL", "ORDERS_DATABASE_URL"}
    connectors_ = Blueprint.from_yaml_file(tmp_path / "blueprint.yaml").connectors
    assert connectors_["ledger"]["use"] == "sql" and connectors_["orders"]["use"] == "sql"


def test_a_secret_is_never_logged(
    make_client: Callable[..., TestClient], caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client([jira_step()])
    caplog.set_level(logging.DEBUG)

    client.post(
        "/api/connections/tickets",
        json={"values": {"JIRA_URL": "https://acme.invalid", "JIRA_API_TOKEN": SECRET}},
        headers=SAFE,
    )
    client.post("/api/connections/tickets", json={"values": {"PATH": SECRET}}, headers=SAFE)

    assert SECRET not in caplog.text


def test_the_page_script_and_the_models_never_contain_a_saved_value(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    client = make_client()
    client.post(
        "/api/connections/ledger",
        json={"values": {"LEDGER_DATABASE_URL": database_url(tmp_path)}},
        headers=SAFE,
    )

    pages = [
        client.get(path).text for path in ("/", "/api/setup", "/api/connections", "/api/state")
    ]
    archive = client.get("/api/export").content

    assert all(database_url(tmp_path) not in page for page in pages)
    assert b"LEDGER_DATABASE_URL=" not in archive  # `.env` stays out of the download


SECRET_SETUP = fake_secret("sk-", "very-secret-value-4711")


@pytest.fixture
def client(tmp_path: Path, environ: dict[str, str]) -> Iterator[TestClient]:
    app = create_ui_app(tmp_path, token=TOKEN, port=8765, environ=environ)
    with TestClient(app, base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        yield client


def test_saving_returns_names_only_and_exports_the_values(
    client: TestClient, tmp_path: Path, environ: dict[str, str]
) -> None:
    response = client.post(
        "/api/setup",
        json={"values": {"ANTHROPIC_API_KEY": SECRET_SETUP, "OPENAI_API_KEY": ""}},
        headers=SAFE,
    )

    assert response.status_code == 200
    assert SECRET_SETUP not in response.text
    assert response.json()["written"] == ["ANTHROPIC_API_KEY"]
    assert response.json()["setup"]["env_names"] == ["ANTHROPIC_API_KEY"]
    assert dotenv_values(tmp_path / ".env")["ANTHROPIC_API_KEY"] == SECRET_SETUP
    assert environ["ANTHROPIC_API_KEY"] == SECRET_SETUP


def test_existing_values_are_never_rendered(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"ANTHROPIC_API_KEY={SECRET_SETUP}\nAWA_DEFAULT_MODEL=x:y\n")
    environ: dict[str, str] = {}
    app = create_ui_app(tmp_path, token=TOKEN, port=8765, environ=environ)
    with TestClient(app, base_url=BASE) as client:
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        responses = [client.get(path) for path in ("/", "/api/setup", "/api/state")]
        archive = client.get("/api/export").content

    assert all(SECRET_SETUP not in response.text for response in responses)
    assert responses[1].json()["env_names"] == ["ANTHROPIC_API_KEY", "AWA_DEFAULT_MODEL"]
    assert responses[1].json()["default_model_set"] is True
    assert environ["ANTHROPIC_API_KEY"] == SECRET_SETUP  # exported for the providers
    assert ".env" not in zipfile.ZipFile(io.BytesIO(archive)).namelist()


def test_a_saved_value_does_not_replace_an_exported_one_until_saved_again(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OPENAI_API_KEY=from-file\n")
    environ = {"OPENAI_API_KEY": "from-shell"}

    project = Project(tmp_path, environ=environ)
    assert environ["OPENAI_API_KEY"] == "from-shell"

    project.save_settings({"OPENAI_API_KEY": "typed-in-ui"})
    assert environ["OPENAI_API_KEY"] == "typed-in-ui"
    assert env_names(tmp_path / ".env") == ["OPENAI_API_KEY"]


def test_process_settings_are_never_offered_saved_or_exported(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text(
        "HTTPS_PROXY=\nPATH=\nPYTHONPATH=\nSSL_CERT_FILE=\nCRM_TOKEN=\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text("HTTPS_PROXY=http://evil.example:8080\n", encoding="utf-8")
    environ: dict[str, str] = {}

    project = Project(tmp_path, environ=environ)

    assert "HTTPS_PROXY" not in environ
    allowed = project.allowed_settings()
    assert "CRM_TOKEN" in allowed
    assert not allowed & {"HTTPS_PROXY", "PATH", "PYTHONPATH", "SSL_CERT_FILE"}
    with pytest.raises(ValueError, match="not a setting"):
        project.save_settings({"HTTPS_PROXY": "http://evil.example:8080"})
    assert "HTTPS_PROXY" not in environ


def test_saving_many_settings_looks_up_the_allowed_names_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project(tmp_path, environ={})
    calls: list[int] = []
    original = project.allowed_settings

    def counted() -> set[str]:
        calls.append(1)
        return original()

    monkeypatch.setattr(project, "allowed_settings", counted)
    project.save_settings(
        {"OPENAI_API_KEY": "a", "ANTHROPIC_API_KEY": "b", "AWA_DEFAULT_MODEL": "m"}
    )
    assert len(calls) == 1
    with pytest.raises(ValueError, match="not a setting"):
        project.save_settings({"OPENAI_API_KEY": "a", "PATH": "x"})


def test_an_invalid_name_is_explained_with_the_fix(client: TestClient) -> None:
    response = client.post("/api/setup", json={"values": {"MY KEY": SECRET_SETUP}}, headers=SAFE)

    assert response.status_code == 400
    assert SECRET_SETUP not in response.text
    assert response.json()["error"]["fix"]


def test_setup_lists_what_discovery_reports(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def discover(env: object = None, *, dotenv: object = ".env") -> EnvironmentReport:
        seen["dotenv"] = dotenv
        return EnvironmentReport(
            providers=(ProviderStatus("azure_openai", False, ("AZURE_OPENAI_API_KEY",)),),
            connectors=(
                ConnectorStatus("jira", "Jira", "work", False, ("JIRA_URL",), ("jira.search",)),
            ),
        )

    monkeypatch.setattr(connectors, "discover", discover, raising=False)

    data = client.get("/api/setup").json()

    assert seen["dotenv"] == tmp_path.resolve() / ".env"
    provider = data["providers"][0]
    # what discovery reports comes first; the providers only the tables name follow
    assert (provider["name"], provider["configured"], provider["missing"]) == (
        "azure_openai",
        False,
        ["AZURE_OPENAI_API_KEY"],
    )
    assert provider["title"] == "Azure OpenAI"
    # the key or the Entra sign-in: the Setup page may save every variable the models read
    assert [f["name"] for f in provider["fields"]] == [
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_DEPLOYMENT_NAME",
        "OPENAI_API_VERSION",
        "AZURE_OPENAI_AUTH",
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_AUTHORITY_HOST",
    ]
    assert data["connectors"][0]["missing"] == ["JIRA_URL"]
    assert data["connectors"][0]["tools"] == ["jira.search"]


def test_errors_never_show_a_saved_value(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"OPENAI_API_KEY={SECRET_SETUP}\n")
    project = Project(tmp_path, environ={})

    explained = project.explain(RuntimeError(f"401: invalid api key {SECRET_SETUP}"))

    assert SECRET_SETUP not in explained.message
    assert "Set up" in explained.fix and explained.link == "#setup"


@pytest.mark.parametrize("name", ["PATH", "PYTHONPATH", "HTTPS_PROXY", "SSL_CERT_FILE", "OTHER"])
def test_the_setup_page_only_saves_names_it_lists(
    client: TestClient, tmp_path: Path, environ: dict[str, str], name: str
) -> None:
    response = client.post("/api/setup", json={"values": {name: "x"}}, headers=SAFE)

    assert response.status_code == 400
    assert not (tmp_path / ".env").exists() and name not in environ


def test_the_setup_page_saves_what_the_project_lists(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text(
        "ORDERS_DATABASE_URL=" + chr(10) + "AWA_DEFAULT_MODEL=" + chr(10)
    )
    names = ["AWA_DEFAULT_MODEL", "ORDERS_DATABASE_URL", "OPENAI_API_KEY"]

    response = client.post("/api/setup", json={"values": dict.fromkeys(names, "v")}, headers=SAFE)

    assert response.status_code == 200 and response.json()["written"] == sorted(names)
