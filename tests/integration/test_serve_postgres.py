"""Serving and declared systems against a real PostgreSQL."""

from __future__ import annotations

import asyncio
import re
import sys
import urllib.parse
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from _helpers.polling import poll
from awa.blueprints._declare import declare_system, read_declared_systems, try_system
from awa.cli import build_parser
from awa.connectors import _local
from awa.models import ModelRegistry
from awa.server import build_blueprint_app
from awa.testing import FakeChatModel, tool_call

pytestmark = pytest.mark.postgres

SCHEMA = "awa_test"

TOOLS = '''
from langchain_core.tools import tool

CALLS = []


@tool
def refund(order_id: str, amount: int) -> str:
    """Refund an order."""
    CALLS.append((order_id, amount))
    return f"refunded {amount}"
'''


def blueprint(name: str) -> str:
    return (
        f"name: {name}\n"
        "agents:\n"
        "  - name: clerk\n"
        "    description: Refunds orders.\n"
        "    tools: [refund]\n"
        "approvals:\n"
        "  - tool: refund\n"
        "triggers:\n"
        "  - name: shop\n"
        "    webhook: {allow_unsigned: true}\n"
        '    input: "Refund order {payload.order}"\n'
    )


def cli(*argv: str) -> int:
    args = build_parser(list(argv)).parse_args(argv)
    code: int = args.handler(args)
    return code


def settle(
    client: TestClient, run_id: str, done: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    return poll(
        lambda: client.get(f"/v1/process-runs/{run_id}").json(),
        done,
        timeout=20,
        what=f"run {run_id} settling",
    )


def test_a_run_paused_in_the_server_is_approved_from_the_cli(
    postgres_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "serve_pg_tools", raising=False)
    (tmp_path / "serve_pg_tools.py").write_text(TOOLS, encoding="utf-8")
    name = f"refunds{uuid4().hex[:10]}"
    (tmp_path / "refunds.yaml").write_text(blueprint(name), encoding="utf-8")
    registry = ModelRegistry()
    registry.register(
        "default",
        FakeChatModel([tool_call("refund", {"order_id": "7", "amount": 120}), "Refunded."]),
    )
    monkeypatch.setattr(ModelRegistry, "_default", registry)

    app = build_blueprint_app(
        "refunds.yaml", tools=["serve_pg_tools.py"], dsn=postgres_dsn, schema=SCHEMA
    )
    # psycopg's async driver needs a selector event loop on Windows.
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    with TestClient(app, backend_options=options) as client:
        run_id = client.post(f"/v1/automations/{name}/webhooks/shop", json={"order": 7}).json()[
            "id"
        ]
        waiting = settle(client, run_id, lambda r: r["status"] == "waiting")
        assert waiting["waits"][0]["kind"] == "approval"

        store = ("--dsn", postgres_dsn, "--schema", SCHEMA)
        assert cli("runs", "list", "--status", "waiting", *store) == 0
        assert run_id in capsys.readouterr().out
        assert (
            cli(
                "runs",
                "decide",
                run_id,
                "--approve",
                "--edit-args",
                '{"amount": 90}',
                "--note",
                "partial refund",
                "--actor",
                "ops",
                *store,
            )
            == 0
        )
        decided = capsys.readouterr()
        assert f"{run_id} {name} queued" in decided.out and "note:" not in decided.err
        done = settle(client, run_id, lambda r: r["status"] == "succeeded")

    assert done["status"] == "succeeded" and done["output"] == "Refunded."
    assert sys.modules["serve_pg_tools"].CALLS == [("7", 90)]
    assert any("answered" in entry.get("note", "") for entry in done["history"])

    # A restarted server (a new lifespan) reads the same durable run.
    with TestClient(app, backend_options=options) as client:
        again = client.get(f"/v1/process-runs/{run_id}").json()
    assert again["status"] == "succeeded" and again["id"] == run_id


def _with_login(address: str, user: str, password: str) -> str:
    parts = urllib.parse.urlsplit(address)
    host = parts.hostname or "localhost"
    netloc = f"{user}:{urllib.parse.quote(password)}@{host}" + (
        f":{parts.port}" if parts.port else ""
    )
    return urllib.parse.urlunsplit(parts._replace(netloc=netloc))


@pytest.fixture
def reader(postgres_dsn: str) -> Iterator[tuple[str, str]]:
    """A schema with an invoices table and a login of the same name that may only read it;
    yields `(admin address, reader address)` and removes both afterwards."""

    admin = re.sub(r"^postgres(ql)?://", "postgresql://", postgres_dsn)
    name = f"awa_ds_{uuid4().hex[:8]}"
    password = uuid4().hex
    connection = _local._connector("postgresql", admin, read_only=False)()
    cursor = connection.cursor()
    try:
        cursor.execute(f"CREATE ROLE {name} LOGIN PASSWORD '{password}'")
    except Exception as exc:  # noqa: BLE001 - the test login may not create logins
        connection.close()
        pytest.skip(f"the test database does not allow creating a login: {type(exc).__name__}")
    for statement in (
        f"CREATE SCHEMA {name}",
        f"GRANT USAGE ON SCHEMA {name} TO {name}",
        f"CREATE TABLE {name}.invoices (invoice_id text PRIMARY KEY, supplier text, "
        "amount_eur numeric(10, 2), status text)",
        f"INSERT INTO {name}.invoices VALUES ('INV-2041', 'Nordwind', 1180.00, 'open'), "
        "('INV-2042', 'Alpen', 7450.00, 'open')",
        f"ANALYZE {name}.invoices",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {name} TO {name}",
    ):
        cursor.execute(statement)
    connection.commit()
    connection.close()
    try:
        yield admin, _with_login(admin, name, password)
    finally:
        connection = _local._connector("postgresql", admin, read_only=False)()
        cursor = connection.cursor()
        cursor.execute(f"DROP SCHEMA {name} CASCADE")
        cursor.execute(f"DROP ROLE {name}")
        connection.commit()
        connection.close()


def test_a_read_only_login_is_declared_with_its_tables_and_no_warning(
    reader: tuple[str, str], tmp_path: Path
) -> None:
    _, address = reader

    outcome = declare_system(
        tmp_path,
        connector="sql",
        name="Invoice ledger",
        values={"INVOICE_LEDGER_DATABASE_URL": address},
        environ={},
        samples=True,
    )

    assert outcome.ok, outcome.message
    assert outcome.warnings == ()
    [system] = read_declared_systems(tmp_path)
    assert system.kind == "PostgreSQL database"
    [table] = system.tables
    assert table.name == "invoices" and table.rows == 2
    assert "amount_eur numeric(10,2)" in table.columns or "amount_eur numeric" in table.columns[2]
    assert tuple(table.sample_rows[0][:2]) == ("INV-2041", "Nordwind")
    text = (tmp_path / "data" / "SYSTEMS.json").read_text(encoding="utf-8")
    assert address not in text and str(urllib.parse.urlsplit(address).password) not in text


def test_a_login_that_may_change_data_is_named(reader: tuple[str, str], tmp_path: Path) -> None:
    admin, _ = reader

    outcome = try_system(
        tmp_path, connector="sql", name="Ledger", values={"LEDGER_DATABASE_URL": admin}, environ={}
    )

    assert outcome.ok and outcome.warnings and "read-only login" in outcome.warnings[0]


def test_a_wrong_password_gets_plain_advice_and_is_never_shown(
    reader: tuple[str, str], tmp_path: Path
) -> None:
    _, address = reader
    user = urllib.parse.urlsplit(address).username or ""
    wrong = _with_login(address, user, "Wrong-Password-4242")

    outcome = declare_system(
        tmp_path, connector="sql", name="Ledger", values={"LEDGER_DATABASE_URL": wrong}, environ={}
    )

    assert not outcome.ok and "refused the login" in outcome.advice
    assert "Wrong-Password-4242" not in outcome.message + outcome.advice
    assert not (tmp_path / ".env").exists()
