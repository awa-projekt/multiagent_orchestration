"""Use it: triggers in words, a run on a sample, the runs list, health, the Keep it running panel,
and the approvals inbox as cards that can be decided and continued here."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from awa.automation.store import hold_triggers
from awa.models import ModelRegistry
from awa.testing import FakeChatModel, tool_call
from awa.ui import create_ui_app
from awa.ui._operate import cron_words, sample_fields, trigger_words

TOKEN = "operate-token"
BASE = "http://127.0.0.1:8765"
SAFE = {"Origin": BASE}
SECRET = "sk-operate-secret-4711"

TOOLS = '''
from langchain_core.tools import tool

CALLS = []


@tool
def pay(order_id: str, amount_eur: float = 10) -> str:
    """Pay an order.

    Args:
        order_id: The order number.
        amount_eur: Amount in EUR.
    """
    CALLS.append(order_id)
    return f"paid {order_id}"


@tool
def lookup(key: str) -> str:
    """Look a key up."""
    return f"found {key}"
'''

ORDERS = """\
name: orders
agents:
  - name: cashier
    description: Pays orders.
    tools: [pay]
approvals:
  - tool: pay
    when: "amount_eur >= 100"
triggers:
  - name: shop
    webhook: {secret_env: SHOP_HOOK_SECRET}
    input: "Handle order {payload.order} for {payload.amount}"
  - name: morning
    schedule: {cron: "0 6 * * MON-FRI", timezone: Europe/Berlin}
    input: "Run the morning check for {payload.date}."
  - name: inbox
    file_drop: {directory: inbox, pattern: "*.pdf"}
    input: "Process {payload.name}"
"""

PLAIN = """\
name: plain
agents:
  - name: reader
    description: Looks keys up.
    tools: [lookup]
"""


def write(folder: Path, blueprint: str) -> None:
    (folder / "blueprint.yaml").write_text(blueprint, encoding="utf-8")
    (folder / "tools.py").write_text(TOOLS, encoding="utf-8")
    (folder / ".env").write_text(f"SHOP_HOOK_SECRET={SECRET}\n", encoding="utf-8")


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "tools", raising=False)
    project = tmp_path / "proj"
    project.mkdir()
    yield project
    sys.modules.pop("tools", None)


@pytest.fixture
def make_client(folder: Path) -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def make(blueprint: str = ORDERS, *replies: Any) -> TestClient:
        write(folder, blueprint)
        registry = ModelRegistry()
        if replies:
            registry.register("default", FakeChatModel(list(replies)))
        app = create_ui_app(folder, token=TOKEN, port=8765, environ={}, model_registry=registry)
        client = TestClient(app, base_url=BASE).__enter__()
        client.headers["Authorization"] = f"Bearer {TOKEN}"
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------- words


@pytest.mark.parametrize(
    ("cron", "zone", "words"),
    [
        ("0 6 * * MON-FRI", "Europe/Berlin", "Every weekday at 06:00 (Europe/Berlin)"),
        ("30 7 * * *", None, "Every day at 07:30"),
        ("0 9 * * MON,WED,FRI", None, "Every Monday, Wednesday and Friday at 09:00"),
        ("15 18 * * 1-5", None, "Every weekday at 18:15"),
        ("0 8 * * 0", None, "Every Sunday at 08:00"),
        ("*/15 * * * *", None, "Every 15 minutes"),
        ("0 * * * *", None, "Every hour"),
        ("@daily", "UTC", 'On the schedule "@daily" (UTC)'),
        ("0 6 1 * *", None, 'On the schedule "0 6 1 * *"'),
        ("not a cron", None, 'On the schedule "not a cron"'),
        ("99 6 * * *", None, 'On the schedule "99 6 * * *"'),
        ("0 9-17 * * *", None, 'On the schedule "0 9-17 * * *"'),
        ("0 8,18 * * 1-5", None, "Every weekday at 08:00 and 18:00"),
        ("0 */2 * * *", None, 'On the schedule "0 */2 * * *"'),
        ("0 8,12,18 * * *", None, "Every day at 08:00, 12:00 and 18:00"),
        ("*/1 * * * *", None, "Every minute"),
        ("* * * * *", None, "Every minute"),
        ("*/7 * * * *", None, 'On the schedule "*/7 * * * *"'),
        ("*/10 8-18 * * 1-5", None, 'On the schedule "*/10 8-18 * * 1-5"'),
        ("30 * * * MON", None, "Every Monday, every hour at 30 minutes past"),
        ("0 9 * * FRI-MON", None, 'On the schedule "0 9 * * FRI-MON"'),
        ("0 9 * * 5-1", None, 'On the schedule "0 9 * * 5-1"'),
        ("0 9 * * 7", None, "Every Sunday at 09:00"),
        ("0 9 * * mon-fri", None, "Every weekday at 09:00"),
        ("0 9 * JAN *", None, 'On the schedule "0 9 * JAN *"'),
        ("0 9 1,15 * *", None, 'On the schedule "0 9 1,15 * *"'),
        ("0 9-17/2 * * *", None, 'On the schedule "0 9-17/2 * * *"'),
        ("0 25 * * *", None, 'On the schedule "0 25 * * *"'),
        ("", None, 'On the schedule ""'),
    ],
)
def test_a_cron_expression_is_a_sentence(cron: str, zone: str | None, words: str) -> None:
    assert cron_words(cron, zone) == words


def test_every_trigger_kind_is_described_in_a_sentence(folder: Path) -> None:
    from awa.blueprints import Blueprint

    write(folder, ORDERS + "  - name: poll\n    schedule: {every: 15m}\n")
    section = Blueprint.from_yaml_file(folder / "blueprint.yaml").automation_section()
    assert section is not None

    said = {t.name: trigger_words(t, "orders") for t in section.triggers}

    assert said["shop"] == (
        "When another system sends a signed web request to /v1/automations/orders/webhooks/shop."
    )
    assert said["morning"] == "Every weekday at 06:00 (Europe/Berlin)"
    assert said["inbox"] == "When a file (*.pdf) is put into the folder inbox."
    assert said["poll"] == "Every 15 minutes"
    fields = {
        t.name: [f["name"] for f in sample_fields(t, section.input)] for t in section.triggers
    }
    assert fields == {"shop": ["order", "amount"], "morning": [], "inbox": ["name"], "poll": []}


def test_use_it_lists_the_automation_and_the_keep_it_running_panel(
    make_client: Callable[..., TestClient],
) -> None:
    data = make_client().get("/api/deploy").json()

    assert [(t["name"], t["kind"]) for t in data["automation"]] == [
        ("shop", "webhook"),
        ("morning", "schedule"),
        ("inbox", "file-drop"),
    ]
    assert data["automation"][0]["sample"] == [
        {"name": "order", "label": "Order"},
        {"name": "amount", "label": "Amount"},
    ]
    panel = data["keep_running"]
    assert panel["command"] == "awa serve blueprint.yaml"  # a webhook needs the server
    assert {w["path"] for w in panel["where"]} == {
        ".awa/awa.db",
        ".awa/checkpoints.db",
        ".env",
        "blueprint.yaml",
    }
    assert any("restart" in line.lower() for line in panel["restart"])
    assert [p["command"] for p in panel["problems"]][0] == "awa runs list --status failed"


def test_without_a_webhook_the_command_is_the_worker(
    make_client: Callable[..., TestClient],
) -> None:
    blueprint = ORDERS.split("  - name: shop")[0] + (
        "  - name: morning\n    schedule: {cron: '0 6 * * *'}\n"
    )

    panel = make_client(blueprint).get("/api/deploy").json()["keep_running"]

    assert panel["command"] == "awa worker blueprint.yaml"


# ---------------------------------------------------------------------- health


def test_health_says_whether_something_listens_and_whether_runs_failed(
    make_client: Callable[..., TestClient], folder: Path
) -> None:
    on_demand = make_client(PLAIN).get("/api/runs").json()["health"]
    client = make_client()
    stopped = client.get("/api/runs").json()["health"]
    with hold_triggers(folder / ".awa" / "awa.db"):
        running = client.get("/api/runs").json()["health"]

    assert on_demand["state"] == "ondemand" and "no triggers" in on_demand["text"]
    assert stopped["state"] == "stopped" and "Not running" in stopped["text"]
    assert stopped["worker_running"] is False
    assert running["state"] == "running" and running["worker_running"] is True


def test_there_are_no_runs_before_the_first_one(make_client: Callable[..., TestClient]) -> None:
    data = make_client().get("/api/runs").json()

    assert data["runs"] == [] and sum(data["counts"].values()) == 0
    assert data["health"]["problems"] == ""


# ---------------------------------------------------------------------- run on a sample


PAY_SMALL = tool_call("pay", {"order_id": "9", "amount_eur": 20})
PAY_BIG = tool_call("pay", {"order_id": "9", "amount_eur": 450})


def sample(client: TestClient, trigger: str = "shop", **values: str) -> Any:
    return client.post(
        "/api/trigger/run",
        json={"trigger": trigger, "values": values or {"order": "9", "amount": "20"}},
        headers=SAFE,
    )


def test_a_sample_run_finishes_and_appears_in_the_runs_list(
    make_client: Callable[..., TestClient], folder: Path
) -> None:
    client = make_client(ORDERS, PAY_SMALL, "Paid order 9.")

    response = sample(client)

    run = response.json()
    assert response.status_code == 200
    assert (run["status"], run["status_text"], run["trigger"]) == ("succeeded", "Done", "shop")
    assert run["summary"] == "Paid order 9." and run["open_approvals"] == 0
    listed = client.get("/api/runs").json()
    assert [r["id"] for r in listed["runs"]] == [run["id"]]
    assert listed["counts"]["succeeded"] == 1 and listed["health"]["problems"] == ""
    assert (folder / ".awa" / "awa.db").is_file()
    started = datetime.strptime(run["created"], "%Y-%m-%d %H:%M")
    assert abs(started - datetime.now()) < timedelta(minutes=2)  # the person's clock, not UTC


def test_a_sample_run_that_needs_an_ok_waits_in_the_inbox_as_a_card(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client(ORDERS, PAY_BIG)

    run = sample(client, order="9", amount="450").json()

    assert (run["status"], run["status_text"], run["open_approvals"]) == (
        "waiting",
        "Waiting for a decision",
        1,
    )
    [waiting] = client.get("/api/approvals").json()["approvals"]
    card = waiting["approvals"][0]["card"]
    assert waiting["id"] == run["id"] and "args" in waiting["approvals"][0]
    assert waiting["created"] == run["created"]
    assert card["summary"] == "Pay an order"
    assert card["why"] == "because the amount is 100 EUR or more"
    assert {f["name"]: f["value"] for f in card["fields"]} == {
        "order_id": "9",
        "amount_eur": "450.00 EUR",
    }


@pytest.mark.slow
def test_a_decision_is_saved_and_the_run_is_continued_here_when_no_worker_runs(
    make_client: Callable[..., TestClient], folder: Path
) -> None:
    client = make_client(ORDERS, PAY_BIG, "Paid order 9.")
    run = sample(client, order="9", amount="450").json()
    [waiting] = client.get("/api/approvals").json()["approvals"]

    decided = client.post(
        "/api/approvals",
        json={
            "run": run["id"],
            "interrupt_id": waiting["approvals"][0]["interrupt_id"],
            "approve": True,
            "note": "checked",
            "args": "",
        },
        headers=SAFE,
    )
    waiting_now = client.get("/api/runs").json()["runs"][0]
    continued = client.post("/api/runs/continue", json={"run": run["id"]}, headers=SAFE)

    assert decided.json() == {"run": run["id"], "status": "queued", "open": 0}
    assert waiting_now["status_text"] == "Waiting to start"
    assert continued.status_code == 200
    assert (continued.json()["status"], continued.json()["summary"]) == (
        "succeeded",
        "Paid order 9.",
    )
    assert sys.modules["tools"].CALLS == ["9"]
    assert client.get("/api/approvals").json() == {"approvals": []}


def test_a_worker_that_runs_continues_the_run_itself(
    make_client: Callable[..., TestClient], folder: Path
) -> None:
    client = make_client(ORDERS, PAY_SMALL, "Paid order 9.")
    run = sample(client).json()

    with hold_triggers(folder / ".awa" / "awa.db"):
        response = client.post("/api/runs/continue", json={"run": run["id"]}, headers=SAFE)

    assert (
        response.status_code == 400 and "worker is running" in response.json()["error"]["message"]
    )


def test_a_schedule_is_run_on_a_sample_without_asking_for_values(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client(ORDERS, "The morning check is done.")

    run = client.post(
        "/api/trigger/run", json={"trigger": "morning", "values": {}}, headers=SAFE
    ).json()

    assert run["status"] == "succeeded" and run["summary"] == "The morning check is done."


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"trigger": "nothere", "values": {}}, "There is no trigger"),
        ({"trigger": "x" * 100, "values": {}}, "incomplete"),
    ],
)
def test_an_unknown_trigger_is_explained(
    make_client: Callable[..., TestClient], body: dict[str, Any], message: str
) -> None:
    response = make_client(ORDERS, "x").post("/api/trigger/run", json=body, headers=SAFE)

    assert response.status_code in (400, 422) and message in response.text


def test_a_system_without_triggers_cannot_run_a_sample(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client(PLAIN, "x").post(
        "/api/trigger/run", json={"trigger": "any", "values": {}}, headers=SAFE
    )

    assert response.status_code == 400 and "no triggers" in response.json()["error"]["message"]


def test_a_failed_run_is_reported_with_what_to_do_and_without_secrets(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client(ORDERS, tool_call("pay", {"order_id": "9", "amount_eur": 20}))

    class Boom(FakeChatModel):
        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"401 invalid api key {SECRET}")

    client.app.state.project.model_registry.register("default", Boom(["x"]))  # type: ignore[attr-defined]
    run = sample(client).json()

    assert run["status"] in {"failed", "dead_lettered"}
    assert SECRET not in str(run) and "***" in run["summary"]
    assert "Set up" in run["fix"]
    listed = client.get("/api/runs").json()
    assert listed["health"]["problems"].startswith("1 of the last 1 runs failed")


def test_a_secret_typed_into_a_sample_never_comes_back(
    make_client: Callable[..., TestClient],
) -> None:
    client = make_client(ORDERS, f"I saw {SECRET} in the order.")

    run = sample(client, order=SECRET, amount="1").json()

    assert SECRET not in str(run) and SECRET not in client.get("/api/runs").text


# ---------------------------------------------------------------------- the access model

ENDPOINTS = [
    ("post", "/api/trigger/run", {"trigger": "shop", "values": {}}),
    ("post", "/api/runs/continue", {"run": "proc_x"}),
]


@pytest.mark.parametrize(("method", "path", "body"), ENDPOINTS)
@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 403),
        ({"Origin": "http://attacker.example"}, 403),
        ({"Origin": BASE, "Authorization": "Bearer forged"}, 401),
        ({"Origin": BASE, "Host": "attacker.example"}, 421),
    ],
)
def test_the_new_use_it_endpoints_keep_the_token_origin_and_host_checks(
    make_client: Callable[..., TestClient],
    folder: Path,
    method: str,
    path: str,
    body: dict[str, Any],
    headers: dict[str, str],
    status: int,
) -> None:
    client = make_client(ORDERS, "x")

    response = getattr(client, method)(path, json=body, headers=headers)

    assert response.status_code == status
    assert not (folder / ".awa" / "awa.db").exists()  # nothing ran


@pytest.mark.parametrize(("method", "path", "body"), [*ENDPOINTS, ("get", "/api/runs", None)])
def test_without_the_session_the_new_endpoints_are_closed(
    folder: Path, method: str, path: str, body: dict[str, Any] | None
) -> None:
    write(folder, ORDERS)
    app = create_ui_app(folder, token=TOKEN, port=8765, environ={})
    with TestClient(app, base_url=BASE) as client:
        kwargs = {} if body is None else {"json": body}
        response = getattr(client, method)(path, headers=SAFE, **kwargs)

    assert response.status_code == 401


def test_a_bad_sample_body_is_reported_without_echoing_it(
    make_client: Callable[..., TestClient],
) -> None:
    response = make_client(ORDERS, "x").post(
        "/api/trigger/run", json={"trigger": "shop", "values": {"order": 918273645}}, headers=SAFE
    )

    assert response.status_code == 422 and "918273645" not in response.text
