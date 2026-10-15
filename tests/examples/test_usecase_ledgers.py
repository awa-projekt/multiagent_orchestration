"""The use cases on ledgers and stock: the ledgers, reconciliation and inventory replenishment,
offline."""

from __future__ import annotations

import contextlib
import csv
import importlib.util
import json
import re
import shutil
import sqlite3
import sys
import threading
import urllib.request
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from _helpers.conversation import request, results, system_text
from _helpers.usecase_support import (
    Script,
    decide,
    first_json,
    seed_schedule,
    serve,
    settle,
    tool_content,
    usecase,
)
from awa.connectors import HttpRequest, MockTransport, mock_response
from awa.testing import ModelCall, tool_call

USECASES = Path(__file__).resolve().parents[2] / "examples" / "usecases"
FINDINGS = [
    {"check": c, "result": "clear", "summary": "no issue", "source_url": "https://example.test/x"}
    for c in ("sanctions", "adverse_news", "ownership")
]


def _tools(name: str, tmp_path: Path) -> ModuleType:
    folder = tmp_path / name
    shutil.copytree(USECASES / name, folder, ignore=shutil.ignore_patterns("__pycache__", "outbox"))
    spec = importlib.util.spec_from_file_location(f"ledger_{name}", folder / "tools.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _expense(tools: ModuleType) -> tuple[Path, list[Callable[[], str]]]:
    sheet = [{"line": str(n), "total_eur": 1.0} for n in range(1, 12)]
    for report in ("EXP-2001", "EXP-2002"):
        tools.audit_report(report, sheet)
    return tools._outbox_dir() / "reimbursements.csv", [
        lambda: tools.submit_reimbursement("EXP-2001", "ok"),
        lambda: tools.submit_reimbursement("EXP-2002", "ok"),
    ]


def _purchase(tools: ModuleType) -> tuple[Path, list[Callable[[], str]]]:
    return tools._ledger_file(), [
        lambda: tools.create_purchase_order("REQ-1", "S-100", "CC-100", 100.0, "pens"),
        lambda: tools.create_purchase_order("REQ-2", "S-100", "CC-100", 100.0, "paper"),
    ]


def _supplier(tools: ModuleType) -> tuple[Path, list[Callable[[], str]]]:
    for supplier in ("S-901", "S-902"):
        tools.score_risk(supplier, FINDINGS)
    return tools._outbox_dir() / "supplier_decisions.csv", [
        lambda: tools.record_decision("S-901", "fine"),
        lambda: tools.record_decision("S-902", "fine"),
    ]


LEDGERS = {
    "expense-report-audit": _expense,
    "purchase-requisition": _purchase,
    "supplier-due-diligence": _supplier,
}


@pytest.mark.parametrize("name", LEDGERS)
def test_concurrent_bookings_write_one_header_and_every_row_once(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, bookings = LEDGERS[name](_tools(name, tmp_path))
    meeting = threading.Barrier(len(bookings))
    is_file = Path.is_file

    def looked_at_the_ledger(self: Path) -> bool:
        found = is_file(self)
        if self == ledger:  # both threads have seen the same state before either one writes
            with contextlib.suppress(threading.BrokenBarrierError):  # the other waits for a lock
                meeting.wait(timeout=0.3)
        return found

    monkeypatch.setattr(Path, "is_file", looked_at_the_ledger)
    replies: list[str] = []
    errors: list[BaseException] = []

    def book(call: Callable[[], str]) -> None:
        try:
            replies.append(call())
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=book, args=(call,)) for call in bookings]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    monkeypatch.undo()

    assert not errors, errors
    rows: list[list[str]] = list(csv.reader(ledger.open(encoding="utf-8", newline="")))
    header, body = rows[0], rows[1:]
    assert len(body) == len(bookings) and header not in body, rows
    assert len({row[0] for row in body}) == len(bookings), rows
    assert not any(reply.startswith(("Refused", "Already")) for reply in replies), replies


recon_project = usecase("data-quality-reconciliation")
COLUMNS = [
    {"file_column": "Customer", "database_column": "customer_id"},
    {"file_column": "Amount", "database_column": "amount"},
    {"file_column": "Currency", "database_column": "currency"},
    {"file_column": "Status", "database_column": "status"},
]


class ReconScript(Script):
    def reconciler(self, call: ModelCall) -> Any:
        stored = request(call).rsplit("stored at ", 1)[1].rstrip(".")
        if not results(call, "reconcile"):
            args = {
                "export_file": stored,
                "key_header": "Order ID",
                "table": "orders",
                "key_column": "order_id",
                "columns": COLUMNS,
            }
            return tool_call("reconcile", args)
        found = first_json(results(call, "reconcile")[0])
        if not results(call, "save_report"):
            lines = [f"# Reconciliation of {Path(stored).name}", ""]
            lines += [
                f"- {m['key']}: {m['column']} {m['export_value']} vs {m['database_value']}"
                for m in found["mismatches"]
            ]
            lines += [f"- missing in database: {', '.join(found['missing_in_database'])}"]
            lines += [f"- missing in export: {', '.join(found['missing_in_export'])}"]
            lines += [f"- duplicates in export: {', '.join(found['duplicate_keys'])}"]
            return tool_call(
                "save_report", {"name": Path(stored).name, "markdown": "\n".join(lines)}
            )
        amounts = [m for m in found["mismatches"] if m["column"] == "amount"]
        if not results(call, "warehouse_execute"):
            return [
                tool_call(
                    "warehouse_execute",
                    {
                        "statement": "UPDATE orders SET amount = ? WHERE order_id = ?",
                        "parameters": [m["export_value"], m["key"]],
                    },
                )
                for m in amounts
            ]
        return self.finish(
            "reconciler",
            {
                "export_rows": found["export_rows"],
                "mismatch_count": found["mismatch_count"],
                "missing_in_database": found["missing_in_database"],
                "missing_in_export": found["missing_in_export"],
                "duplicate_keys": found["duplicate_keys"],
                "fixes": [
                    {"key": m["key"], "new_amount": m["export_value"], "status": "applied"}
                    for m in amounts
                ],
                "report": results(call, "save_report")[0],
            },
        )


def warehouse(project: Path) -> dict[str, tuple[Any, ...]]:
    with closing(sqlite3.connect(project / "data" / "warehouse.db")) as db:
        return {row[0]: row for row in db.execute("SELECT * FROM orders")}


def test_a_dropped_export_is_reconciled_and_only_approved_amount_fixes_are_applied(
    recon_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = recon_project
    ReconScript().install(monkeypatch)
    before = warehouse(project)
    # the worker creates the watched folder itself (git keeps no empty folder, so it may not exist)
    shutil.rmtree(project / "data" / "inbox", ignore_errors=True)

    with serve(project) as client:
        shutil.copy(
            project / "data" / "samples" / "erp_export_2026-10-01.xlsx", project / "data" / "inbox"
        )
        [run] = settle(client, "waiting", trigger="export")
    # While its run waits, the file stays in in-progress/ (where the run reads it).
    assert (project / "data" / "inbox" / "in-progress" / "erp_export_2026-10-01.xlsx").is_file()
    assert warehouse(project) == before  # no change before the steward decides
    report = next((project / "out").glob("reconciliation-*.md")).read_text(encoding="utf-8")
    assert "O-1061" in report and "O-1050" in report and "O-1012" in report

    decide(run["id"])
    decide(run["id"])  # one answer per proposed change, oldest first

    with serve(project) as client:
        settle(client, "succeeded", trigger="export")
    assert (
        project / "data" / "inbox" / "processed" / "erp_export_2026-10-01.xlsx"
    ).is_file()  # moved on at the end
    after = warehouse(project)
    assert after["O-1007"][2] == 118.50 and after["O-1019"][2] == 240.00
    changed = {key for key in after if after[key] != before[key]}
    assert changed == {"O-1007", "O-1019"}  # the injected note changed nothing else


replenishment_project = usecase("inventory-replenishment")
HOOK = "/v1/automations/inventory-replenishment/webhooks/low-stock"


class ReplenishmentScript(Script):
    def replenishment_planner(self, call: ModelCall) -> Any:
        if not results(call, "erp_list_stock"):
            return tool_call("erp_list_stock", {})
        if not results(call, "inventory_query"):
            query = re.search(r"SELECT .*?ORDER BY i\.sku", system_text(call), re.S)
            assert query is not None, "the query is part of the instructions"
            return tool_call("inventory_query", {"query": " ".join(query.group().split())})
        if not results(call, "calculate_reorders"):
            stock = first_json(tool_content(results(call, "erp_list_stock")[0]))["items"]
            table = first_json(results(call, "inventory_query")[0])
            demand = [dict(zip(table["columns"], row, strict=True)) for row in table["rows"]]
            return tool_call("calculate_reorders", {"stock": stock, "demand": demand})
        plan = first_json(results(call, "calculate_reorders")[0])
        wanted = re.search(r"SKU (\S+) below", request(call))
        proposals = [p for p in plan["proposals"] if not wanted or p["sku"] == wanted.group(1)]
        if not results(call, "erp_create_purchase_order"):
            return [
                tool_call(
                    "erp_create_purchase_order", {**{k: p[k] for k in p if k != "order_value"}}
                )
                for p in proposals
            ]
        placed = results(call, "erp_create_purchase_order")
        ordered = [
            {
                "sku": p["sku"],
                "quantity": p["quantity"],
                "order_value": p["order_value"],
                "po_number": match.group() if (match := re.search(r"PO-\d+", text)) else None,
                "status": "placed",
            }
            for p, text in zip(proposals, placed, strict=False)
        ]
        issues = [f"{i['sku']}: {i['issue']}" for i in plan["issues"]]
        return self.finish(
            "replenishment_planner",
            {"ordered": ordered, "issues": issues, "summary": f"{len(ordered)} order(s) placed"},
        )


def install_erp(project: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The ERP behind the real REST connector: a mock transport; returns the orders it received."""

    orders: list[dict[str, Any]] = []
    stock = json.loads((project / "data" / "erp_stock.json").read_text(encoding="utf-8"))

    def create(received: HttpRequest) -> Any:
        assert received.header("X-Api-Key") == "erp-test-key-123"
        orders.append(json.loads(received.body or b"{}"))
        return mock_response(201, json={"po_number": f"PO-{7000 + len(orders)}"})

    transport = MockTransport(
        {"GET /stock": mock_response(json=stock), "POST /purchase-orders": create}
    )

    def fetch(
        policy: Any, url: str, *, method: str, headers: Any, data: Any = None, **_: Any
    ) -> Any:
        return transport(HttpRequest(method, url, dict(headers or {}), data))

    monkeypatch.setattr("awa.connectors._engine.fetch", fetch)
    monkeypatch.setenv("ERP_URL", "https://erp.example.test")
    monkeypatch.setenv("ERP_KEY", "erp-test-key-123")
    monkeypatch.setenv("ERP_HOOK_TOKEN", "hook-token")
    return orders


def test_the_weekly_check_places_small_orders_and_waits_for_the_large_one(
    replenishment_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = replenishment_project
    orders = install_erp(project, monkeypatch)
    ReplenishmentScript().install(monkeypatch)
    seed_schedule(project, "weekly", "inventory-replenishment")

    with serve(project) as client:
        [run] = settle(client, "waiting", trigger="weekly")
    assert sorted(o["sku"] for o in orders) == ["BRG-6204", "GSK-KIT-A"]  # 3,360 and 1,674 EUR
    decide(run["id"])

    with serve(project) as client:
        settle(client, "succeeded", trigger="weekly")
    assert sorted((o["sku"], o["quantity"]) for o in orders) == [
        ("BRG-6204", 800),
        ("GSK-KIT-A", 60),
        ("MOT-AC-750", 30),  # 9,300 EUR: only after the approval; nothing is sent twice
    ]
    assert all(o["supplier_id"] and o["reason"] for o in orders)


def test_a_low_stock_webhook_checks_one_sku_and_a_wrong_token_is_refused(
    replenishment_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = replenishment_project
    orders = install_erp(project, monkeypatch)
    ReplenishmentScript().install(monkeypatch)

    with serve(project) as client:
        assert (
            client.post(
                HOOK, json={"sku": "GSK-KIT-A"}, headers={"X-Awa-Token": "guess"}
            ).status_code
            == 401
        )
        sent = client.post(HOOK, json={"sku": "GSK-KIT-A"}, headers={"X-Awa-Token": "hook-token"})
        assert sent.status_code == 202
        settle(client, "succeeded", trigger="low-stock")
        repeat = client.post(HOOK, json={"sku": "GSK-KIT-A"}, headers={"X-Awa-Token": "hook-token"})
        assert repeat.json()["id"] == sent.json()["id"]  # the same business key: no second run
    assert [(o["sku"], o["quantity"]) for o in orders] == [("GSK-KIT-A", 60)]  # no approval needed


def test_the_sample_erp_serves_stock_and_takes_orders(
    replenishment_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(replenishment_project)
    monkeypatch.delitem(sys.modules, "mock_erp", raising=False)

    import mock_erp

    project = replenishment_project
    monkeypatch.setattr(mock_erp, "HERE", project)
    server = mock_erp.make_server(0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        keyed = {"X-Api-Key": "demo-key"}
        with urllib.request.urlopen(
            urllib.request.Request(f"{base}/stock", headers=keyed)
        ) as reply:
            assert len(json.load(reply)["items"]) == 6
        body = json.dumps(
            {"supplier_id": "S-10", "sku": "A", "quantity": 1, "unit_price": 2}
        ).encode()
        post = urllib.request.Request(
            f"{base}/purchase-orders", body, {**keyed, "Content-Length": str(len(body))}
        )
        with urllib.request.urlopen(post) as reply:
            assert json.load(reply)["po_number"] == "PO-7001"
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"{base}/stock")
        assert refused.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
    assert (project / "out" / "erp_purchase_orders.jsonl").read_text(encoding="utf-8").count(
        "\n"
    ) == 1


def test_the_readme_trigger_payload_names_a_file_reconcile_accepts(tmp_path: Path) -> None:
    name = "data-quality-reconciliation"
    readme = (USECASES / name / "README.md").read_text(encoding="utf-8")
    payload = re.search(r"awa trigger run blueprint\.yaml export --payload '(\{.*\})'", readme)
    assert payload is not None
    tools = _tools(name, tmp_path)

    assert tools._inside_data(json.loads(payload.group(1))["path"]).is_file()


def test_a_csv_export_keeps_its_first_line(tmp_path: Path) -> None:
    tools = _tools("data-quality-reconciliation", tmp_path)
    export = tmp_path / "export.csv"
    export.write_text("Key;Amount\nA-1;10.00\nA-2;20.00\n", encoding="utf-8")

    rows = tools._export_rows(export, "Key")

    assert rows == [{"Key": "A-1", "Amount": "10.00"}, {"Key": "A-2", "Amount": "20.00"}]
