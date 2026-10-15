"""`awa serve`: zero-config local automation, every trigger run in-process, and runs shared with `--dsn`."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import awa.server._app as server_app
from _helpers.polling import poll
from awa.cli import build_parser
from awa.cli._serve import _allowed_hosts
from awa.models import ModelRegistry
from awa.server import build_blueprint_app
from awa.testing import FakeChatModel, tool_call

TOOLS = '''
import time

from langchain_core.tools import tool

CALLS = []


@tool
def pay(order_id: str) -> str:
    """Pay an order."""
    CALLS.append(order_id)
    return f"paid {order_id}"


@tool
def wait_long(seconds: float) -> str:
    """Wait a while."""
    time.sleep(seconds)
    return "waited"
'''

BLUEPRINT = """
name: orders
agents:
  - name: cashier
    description: Pays orders.
    tools: [pay]
approvals:
  - tool: pay
triggers:
  - name: shop
    webhook: {allow_unsigned: true}
    input: "Handle order {payload.order}"
  - name: scans
    file_drop: {directory: inbox, stable_for: 0, poll_interval: 0.05}
    input: "Book the file {payload.name}: {payload.text}"
"""


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AWA_POSTGRES_DSN", raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "local_serve_tools", raising=False)
    (tmp_path / "local_serve_tools.py").write_text(TOOLS, encoding="utf-8")
    (tmp_path / "orders.yaml").write_text(BLUEPRINT, encoding="utf-8")
    return tmp_path


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> ModelRegistry:
    registry = ModelRegistry()
    monkeypatch.setattr(ModelRegistry, "_default", registry)
    return registry


def cli(*argv: str) -> int:
    args = build_parser(list(argv)).parse_args(argv)
    code: int = args.handler(args)
    return code


def app(workspace: Path) -> Any:
    return build_blueprint_app(
        "orders.yaml", tools=["local_serve_tools.py"], store_path=workspace / ".awa" / "awa.db"
    )


def settle(
    client: TestClient, run_id: str, done: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    return poll(
        lambda: client.get(f"/v1/process-runs/{run_id}").json(),
        done,
        timeout=15,
        what=f"run {run_id} settling",
    )


def calls() -> list[str]:
    return list(sys.modules["local_serve_tools"].CALLS)


def test_an_approval_survives_a_restart_and_is_decided_on_the_command_line(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "9"}), "Paid 9."]))

    with TestClient(app(workspace)) as client:
        queued = client.post("/v1/automations/orders/webhooks/shop", json={"order": 9})
        run_id = queued.json()["id"]
        waiting = settle(client, run_id, lambda r: r["status"] == "waiting")
    assert waiting["status"] == "waiting" and calls() == []
    assert (workspace / ".awa" / "awa.db").is_file()
    assert (workspace / ".awa" / "checkpoints.db").is_file()

    # The server is down; the decision goes into the local store from the terminal.
    assert cli("runs", "list", "--status", "waiting") == 0
    assert run_id in capsys.readouterr().out
    assert cli("runs", "decide", run_id, "--approve", "--actor", "lead") == 0
    assert f"{run_id} orders queued" in capsys.readouterr().out

    with TestClient(app(workspace)) as client:  # a new process: new system, saver and store
        done = settle(client, run_id, lambda r: r["status"] == "succeeded")
    assert done["status"] == "succeeded" and done["output"] == "Paid 9."
    assert calls() == ["9"] and done["attempts"] == 1


def test_the_server_data_file_drops_itself_without_a_database(
    workspace: Path, models: ModelRegistry
) -> None:
    models.register("default", FakeChatModel(["Booked RE-17."]))
    (workspace / "inbox").mkdir()
    (workspace / "inbox" / "re-17.csv").write_bytes(b"invoice;amount\nRE-17;10\n")

    with TestClient(app(workspace)) as client:
        [run] = poll(
            lambda: client.get("/v1/process-runs").json()["runs"],
            lambda runs: bool(runs) and runs[0]["status"] == "succeeded",
            what="the dropped file being processed",
        )
        detail = client.get(f"/v1/process-runs/{run['id']}").json()
    assert run["trigger"] == "scans" and run["status"] == "succeeded"
    assert detail["input"] == "Book the file re-17.csv: invoice;amount\nRE-17;10\n"
    assert (workspace / "inbox" / "processed" / "re-17.csv").is_file()


def test_trigger_run_asks_for_approvals_in_the_terminal(
    workspace: Path,
    models: ModelRegistry,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:

    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "4"}), "Paid 4."]))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    code = cli(
        "trigger",
        "run",
        "orders.yaml",
        "shop",
        "--payload",
        '{"order": 4}',
        "--tools",
        "local_serve_tools.py",
    )
    out = capsys.readouterr()
    assert code == 0 and "succeeded" in out.out and "Paid 4." in out.out
    assert "Approval needed" in out.err and calls() == ["4"]


def test_trigger_run_without_a_terminal_stays_paused(
    workspace: Path, models: ModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:

    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "5"})]))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert (
        cli(
            "trigger",
            "run",
            "orders.yaml",
            "shop",
            "--payload",
            '{"order": 5}',
            "--tools",
            "local_serve_tools.py",
        )
        == 2
    )
    assert calls() == []


def test_worker_concurrency_defaults_to_the_automations_limits(
    workspace: Path, models: ModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.automation import Worker

    seen: list[int] = []
    real_once = Worker.run_once

    async def once(self: Worker) -> list[Any]:
        seen.append(self.concurrency)
        return await real_once(self)

    monkeypatch.setattr(Worker, "run_once", once)
    (workspace / "orders.yaml").write_text(
        BLUEPRINT + "automation:\n  concurrency: 3\n", encoding="utf-8"
    )
    assert cli("worker", "orders.yaml", "--tools", "local_serve_tools.py", "--once") == 0
    assert (
        cli(
            "worker",
            "orders.yaml",
            "--tools",
            "local_serve_tools.py",
            "--once",
            "--concurrency",
            "1",
        )
        == 0
    )
    assert seen == [3, 1]


def test_blueprint_tools_get_a_default_timeout(workspace: Path, models: ModelRegistry) -> None:
    from awa.blueprints import Blueprint
    from awa.blueprints._resolve import load_tools
    from awa.errors import ToolTimeoutError

    source = """
name: slow
defaults: {tool_timeout: 0.2}
agents:
  - name: waiter
    tools: [wait_long]
"""
    models.register("default", FakeChatModel([tool_call("wait_long", {"seconds": 2})]))
    agent = Blueprint.from_yaml_text(source).build(tools=load_tools(["local_serve_tools.py"]))
    from awa._internal import aio as asyncio_compat

    with pytest.raises(ToolTimeoutError, match="0.2"):
        asyncio_compat.run(agent.run("wait"))
    unlimited = Blueprint.from_yaml_text(source.replace("0.2", "null"))
    assert unlimited.defaults.tool_timeout is None
    assert Blueprint.from_yaml_text("name: x\nagents:\n  - name: a\n").defaults.tool_timeout == 120


def test_worker_once_handles_dropped_files_into_the_local_store(
    workspace: Path, models: ModelRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    from awa._internal import aio as asyncio_compat
    from awa.automation import SqliteRunStore

    models.register("default", FakeChatModel(["Booked."]))
    (workspace / "inbox").mkdir()
    (workspace / "inbox" / "a.txt").write_bytes(b"Invoice A")
    assert cli("worker", "orders.yaml", "--tools", "local_serve_tools.py", "--once") == 0
    assert "succeeded" in capsys.readouterr().out
    store = SqliteRunStore(workspace / ".awa" / "awa.db")
    [run] = asyncio_compat.run(store.list())
    asyncio_compat.run(store.close())
    assert run.trigger == "scans" and run.status == "succeeded"
    assert run.input == "Book the file a.txt: Invoice A"
    assert (workspace / "inbox" / "processed" / "a.txt").is_file()


def test_serve_store_keeps_the_given_file_name(
    workspace: Path, models: ModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    import awa.server._app as server_app

    built: list[Any] = []
    monkeypatch.setattr(server_app, "import_uvicorn", lambda: object())
    monkeypatch.setattr(server_app, "run_uvicorn", lambda _, app, **__: built.append(app))
    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "3"})]))

    assert (
        cli("serve", "orders.yaml", "--tools", "local_serve_tools.py", "--store", "x/foo.db") == 0
    )
    # `awa serve` without a key answers only its own host names.
    with TestClient(built[0], base_url="http://localhost") as client:
        run_id = client.post("/v1/automations/orders/webhooks/shop", json={"order": 3}).json()["id"]
        settle(client, run_id, lambda r: r["status"] == "waiting")
    assert (workspace / "x" / "foo.db").is_file()
    assert (workspace / "x" / "checkpoints.db").is_file()
    assert not (workspace / "x" / "awa.db").exists()
    assert not (workspace / ".awa").exists()


def test_one_process_per_local_store_runs_the_triggers(
    workspace: Path, models: ModelRegistry
) -> None:
    from awa.automation.store import hold_triggers
    from awa.errors import ProcessRunStateError

    store = workspace / ".awa" / "awa.db"
    with TestClient(app(workspace)):
        # `awa worker` with triggers must not start next to the server that runs them ...
        with pytest.raises(ProcessRunStateError, match="already runs the triggers"):
            cli("worker", "orders.yaml", "--tools", "local_serve_tools.py", "--once")
        # ... a second server must not either ...
        with (
            pytest.raises(ProcessRunStateError, match="already runs the triggers"),
            TestClient(app(workspace)),
        ):
            pass
        # ... but a worker that only executes runs may.
        assert (
            cli(
                "worker",
                "orders.yaml",
                "--tools",
                "local_serve_tools.py",
                "--once",
                "--no-triggers",
            )
            == 0
        )
    with hold_triggers(store):  # released when the server stopped
        pass
    assert cli("worker", "orders.yaml", "--tools", "local_serve_tools.py", "--once") == 0


def test_serve_checks_the_claim_before_it_starts(
    workspace: Path, models: ModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    import awa.server._app as server_app
    from awa.automation.store import hold_triggers
    from awa.errors import ProcessRunStateError

    monkeypatch.setattr(server_app, "import_uvicorn", lambda: object())
    monkeypatch.setattr(server_app, "run_uvicorn", lambda *_, **__: None)
    with (
        hold_triggers(workspace / ".awa" / "awa.db"),
        pytest.raises(ProcessRunStateError, match="`awa worker --no-triggers`"),
    ):
        cli("serve", "orders.yaml", "--tools", "local_serve_tools.py")


def test_the_server_closes_its_local_files_at_shutdown(
    workspace: Path, models: ModelRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awa.persistence import SqliteCheckpointer

    closed: list[Path] = []
    real_close = SqliteCheckpointer.close

    def close(self: SqliteCheckpointer) -> None:
        closed.append(self.path)
        real_close(self)

    monkeypatch.setattr(SqliteCheckpointer, "close", close)
    with TestClient(app(workspace)):
        assert closed == []
    assert closed == [workspace / ".awa" / "checkpoints.db"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_local_state_files_are_private(workspace: Path, models: ModelRegistry) -> None:
    import os
    import stat

    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "6"})]))
    previous = os.umask(0o022)
    try:
        with TestClient(app(workspace)) as client:
            run_id = client.post("/v1/automations/orders/webhooks/shop", json={"order": 6}).json()[
                "id"
            ]
            settle(client, run_id, lambda r: r["status"] == "waiting")
    finally:
        os.umask(previous)
    for name in ("awa.db", "checkpoints.db"):
        assert stat.S_IMODE((workspace / ".awa" / name).stat().st_mode) == 0o600, name


def _hosts(*flags: str) -> list[str]:
    args = build_parser().parse_args(["serve", "blueprint.yaml", *flags])
    return _allowed_hosts(args)


def test_the_variable_sets_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWA_ALLOWED_HOSTS", "a.example, b.example,,")

    assert _hosts() == ["a.example", "b.example"]


def test_an_explicit_flag_replaces_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWA_ALLOWED_HOSTS", "a.example")

    assert _hosts("--allowed-host", "c.example", "--allowed-host", "d.example") == [
        "c.example",
        "d.example",
    ]


def test_nothing_set_means_no_extra_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWA_ALLOWED_HOSTS", raising=False)

    assert _hosts() == []


TOOLS_AUTOMATION = '''
import time

from langchain_core.tools import tool

CALLS = []


@tool
def pay(order_id: str) -> str:
    """Pay an order."""
    CALLS.append(order_id)
    return f"paid {order_id}"


@tool
def archive(order_id: str) -> str:
    """Archive an order (slowly)."""
    time.sleep(0.5)
    CALLS.append(f"archived {order_id}")
    return "archived"
'''

BLUEPRINT_AUTOMATION = """
name: orders
agents:
  - name: cashier
    description: Pays and archives orders.
    tools: [pay, archive]
approvals:
  - tool: pay
triggers:
  - name: shop
    webhook: {allow_unsigned: true}
    input: "Handle order {payload.order}"
  - name: nightly
    schedule: {cron: "0 2 * * *"}
"""


@pytest.fixture
def workspace_automation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "serve_automation_tools", raising=False)
    (tmp_path / "serve_automation_tools.py").write_text(TOOLS_AUTOMATION, encoding="utf-8")
    (tmp_path / "orders.yaml").write_text(BLUEPRINT_AUTOMATION, encoding="utf-8")
    return tmp_path


def app_for(**options: Any) -> FastAPI:
    return build_blueprint_app("orders.yaml", tools=["serve_automation_tools.py"], **options)


def settle_automation(
    client: TestClient, run_id: str, done: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    """Poll the run until `done` holds (the server's worker runs in the background)."""

    return poll(
        lambda: client.get(f"/v1/process-runs/{run_id}").json(),
        done,
        timeout=10,
        what=f"run {run_id} settling",
    )


def calls_automation() -> list[str]:
    return list(sys.modules["serve_automation_tools"].CALLS)


def test_a_webhook_run_is_executed_by_the_server(
    workspace_automation: Path, models: ModelRegistry
) -> None:
    models.register("default", FakeChatModel(["Order 7 handled."]))

    with TestClient(app_for()) as client:
        queued = client.post("/v1/automations/orders/webhooks/shop", json={"order": 7})
        run = settle_automation(client, queued.json()["id"], lambda r: r["status"] == "succeeded")

    assert queued.status_code == 202 and queued.json()["status"] == "queued"
    assert run["status"] == "succeeded" and run["output"] == "Order 7 handled."
    assert run["input"] == "Handle order 7" and run["attempts"] == 1


def test_an_approved_event_run_resumes_in_the_server(
    workspace_automation: Path, models: ModelRegistry
) -> None:
    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "9"}), "Paid 9."]))

    with TestClient(app_for()) as client:
        queued = client.post("/v1/automations/orders/events", json={"payload": {"order": 9}})
        run_id = queued.json()["id"]
        waiting = settle_automation(client, run_id, lambda r: r["status"] == "waiting")
        assert calls_automation() == []
        decided = client.post(
            f"/v1/process-runs/{run_id}/decision", json={"decision": {"action": "approve"}}
        )
        done = settle_automation(client, run_id, lambda r: r["status"] == "succeeded")

    assert waiting["waits"][0]["kind"] == "approval"
    assert decided.status_code == 200, decided.text
    assert done["status"] == "succeeded" and done["output"] == "Paid 9."
    assert calls_automation() == ["9"]


def test_shutdown_lets_a_running_attempt_finish(
    workspace_automation: Path, models: ModelRegistry
) -> None:
    models.register(
        "default", FakeChatModel([tool_call("archive", {"order_id": "5"}), "Archived."])
    )
    app = app_for(drain_timeout=10)

    with TestClient(app) as client:
        run_id = client.post("/v1/automations/orders/webhooks/shop", json={"order": 5}).json()["id"]
        settle_automation(client, run_id, lambda r: r["status"] == "running")
    # The app stopped only after the attempt in flight finished.
    assert calls_automation() == ["archived 5"]
    with TestClient(app) as client:
        run = client.get(f"/v1/process-runs/{run_id}").json()
    assert run["status"] == "succeeded" and run["attempts"] == 1


def test_an_attempt_past_the_drain_timeout_goes_back_to_the_queue(
    workspace_automation: Path, models: ModelRegistry
) -> None:
    models.register(
        "default",
        FakeChatModel(
            [tool_call("archive", {"order_id": "6"}), tool_call("archive", {"order_id": "6"})]
            + ["Archived."]
        ),
    )
    app = app_for(drain_timeout=0)

    with TestClient(app) as client:
        run_id = client.post("/v1/automations/orders/webhooks/shop", json={"order": 6}).json()["id"]
        settle_automation(client, run_id, lambda r: r["status"] == "running")
    with TestClient(app) as client:
        stopped = client.get(f"/v1/process-runs/{run_id}").json()
        done = settle_automation(client, run_id, lambda r: r["status"] == "succeeded")

    assert stopped["status"] in ("queued", "running")
    assert done["status"] == "succeeded" and done["attempts"] == 1


class FakeUvicorn:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, Any]]] = []
        self.loops: list[type[Any] | None] = []
        fake = self

        class Config:
            def __init__(self, app: Any, **options: Any) -> None:
                self.app, self.options = app, options

        class Server:
            def __init__(self, config: Config) -> None:
                self.config = config

            def run(self) -> None:
                fake.calls.append((self.config.app, self.config.options))
                fake.loops.append(None)

            async def serve(self) -> None:
                fake.calls.append((self.config.app, self.config.options))
                fake.loops.append(type(asyncio.get_running_loop()))

        self.Config, self.Server = Config, Server


@pytest.fixture
def uvicorn(monkeypatch: pytest.MonkeyPatch) -> FakeUvicorn:
    fake = FakeUvicorn()
    monkeypatch.setattr(server_app, "import_uvicorn", lambda: fake)
    return fake


def serve(*argv: str) -> int:
    args = build_parser(["serve"]).parse_args(["serve", *argv])
    code: int = args.handler(args)
    return code


def test_serve_explains_which_triggers_run_where(
    workspace_automation: Path, uvicorn: FakeUvicorn, capsys: pytest.CaptureFixture[str]
) -> None:
    assert serve("orders.yaml", "--tools", "serve_automation_tools.py") == 0

    err = capsys.readouterr().err
    assert "the server runs every trigger" in err
    assert str(workspace_automation / ".awa") in err and "--dsn" in err
    [(_, options)] = uvicorn.calls
    assert "loop" not in options


def test_serve_takes_the_worker_dsn(
    workspace_automation: Path,
    uvicorn: FakeUvicorn,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    pytest.importorskip("psycopg_pool")
    built: list[dict[str, Any]] = []
    real = build_blueprint_app

    def recording(blueprint: str, **options: Any) -> FastAPI:
        built.append(options)
        return real(blueprint, **options)

    monkeypatch.setattr("awa.server._blueprint_app.build_blueprint_app", recording)
    monkeypatch.setenv("AWA_POSTGRES_DSN", "postgresql://u:p@db.invalid:5432/awa")
    reached: list[str] = []
    # The pre-flight connects once; here the database is a stand-in that answers.
    monkeypatch.setattr(
        "awa.persistence.sql.connect_once", lambda dsn, timeout: reached.append(dsn)
    )

    assert serve("orders.yaml", "--tools", "serve_automation_tools.py", "--schema", "ops") == 0

    assert built[0]["dsn"] == "postgresql://u:p@db.invalid:5432/awa"
    assert reached == ["postgresql://u:p@db.invalid:5432/awa"]
    assert built[0]["schema"] == "ops"
    err = capsys.readouterr().err
    assert "`awa worker orders.yaml --dsn ...`" in err and "p@db" not in err
    [(_, options)] = uvicorn.calls
    # psycopg needs a selector loop on Windows; awa creates it, uvicorn's loop option stays unset.
    assert "loop" not in options
    if sys.platform == "win32":
        assert uvicorn.loops == [asyncio.SelectorEventLoop]
    else:
        assert uvicorn.loops == [None]


def test_the_serve_help_names_the_dsn() -> None:
    parser = build_parser(["serve"])
    args = parser.parse_args(["serve", "bp.yaml", "--dsn", "postgresql://x/y"])
    assert args.dsn == "postgresql://x/y" and args.schema == "awa"


def test_approver_roles_also_guard_the_decisions_of_automated_runs(
    workspace_automation: Path, models: ModelRegistry
) -> None:
    from awa.server import ApiKeyAuthenticator, Principal

    models.register("default", FakeChatModel([tool_call("pay", {"order_id": "3"}), "Paid 3."]))
    auth = ApiKeyAuthenticator(
        {
            "k-carol": Principal("carol", tenant="acme"),
            "k-dave": Principal("dave", tenant="acme", roles=frozenset({"approver"})),
        }
    )
    carol, dave = ({"Authorization": f"Bearer k-{who}"} for who in ("carol", "dave"))
    approve = {"decision": {"action": "approve"}}

    with TestClient(app_for(approver_roles=["approver"], authenticator=auth)) as client:
        queued = client.post("/v1/automations/orders/events", json={"payload": {}}, headers=carol)
        run_id = queued.json()["id"]
        settle_run = lambda done: poll(  # noqa: E731
            lambda: client.get(f"/v1/process-runs/{run_id}", headers=carol).json(),
            done,
            timeout=10,
            what="the run settling",
        )
        settle_run(lambda r: r["status"] == "waiting")
        refused = client.post(f"/v1/process-runs/{run_id}/decision", json=approve, headers=carol)
        assert refused.status_code == 403 and calls_automation() == []
        granted = client.post(f"/v1/process-runs/{run_id}/decision", json=approve, headers=dave)
        done = settle_run(lambda r: r["status"] == "succeeded")

    assert granted.status_code == 200, granted.text
    assert done["output"] == "Paid 3." and calls_automation() == ["3"]
