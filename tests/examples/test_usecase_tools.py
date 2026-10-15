"""The sample tools of the use cases keep their rules whatever the model passes them."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import threading
from pathlib import Path
from types import ModuleType

import pytest

USECASES = Path(__file__).resolve().parents[2] / "examples" / "usecases"


def _tools(name: str, tmp_path: Path) -> ModuleType:
    folder = tmp_path / name
    shutil.copytree(USECASES / name, folder, ignore=shutil.ignore_patterns("__pycache__", "out*"))
    spec = importlib.util.spec_from_file_location(f"usecase_{name}", folder / "tools.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_triage_tasks_created_at_once_get_distinct_numbers(tmp_path: Path) -> None:
    tools = _tools("email-inbox-triage", tmp_path)
    start = threading.Barrier(8)

    def create(n: int) -> None:
        start.wait()
        tools.create_task(f"m-{n}", "title", "support", "normal", "summary")

    threads = [threading.Thread(target=create, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    board = (tools._out_dir() / "board.jsonl").read_text(encoding="utf-8").splitlines()
    assert sorted(json.loads(line)["id"] for line in board) == sorted(f"T-{n}" for n in range(1, 9))


def test_an_audit_refuses_receipt_totals_that_are_no_amount(tmp_path: Path) -> None:
    tools = _tools("expense-report-audit", tmp_path)
    totals = [{"line": str(n), "total_eur": float("nan")} for n in range(1, 12)]

    assert tools.audit_report("EXP-2001", totals).startswith("Refused: the receipt total")


def test_a_follow_up_subject_cannot_add_header_lines(tmp_path: Path) -> None:
    tools = _tools("meeting-notes-to-tasks", tmp_path)
    assert tools.add_task("MTG-301", "Draft the plan", "Anna", "2026-10-09").startswith("Created")

    tools.send_follow_up("MTG-301", "Tasks\nBcc: someone@else.example", "See the tasks.")

    mail = (tools._outbox_dir() / "follow-ups" / "MTG-301.txt").read_text(encoding="utf-8")
    assert mail.split("\n\n", 1)[0].splitlines()[1:] == ["Subject: Tasks Bcc: someone@else.example"]


def test_a_task_without_an_owner_name_is_refused(tmp_path: Path) -> None:
    tools = _tools("meeting-notes-to-tasks", tmp_path)

    assert "no owner given" in tools.add_task("MTG-301", "Draft the plan", " ., ", "2026-10-09")


def test_a_dunning_subject_cannot_add_header_lines(tmp_path: Path) -> None:
    tools = _tools("order-to-cash-exceptions", tmp_path)

    sent = tools.send_dunning_letter("INV-3011", 2, "Reminder\r\nBcc: x@else.example", "6400.00")

    assert sent.startswith("Queued")
    letter = (tools._outbox_dir() / "dunning" / "INV-3011-L2.txt").read_text(encoding="utf-8")
    assert letter.split("\n\n", 1)[0].splitlines()[1:] == ["Subject: Reminder Bcc: x@else.example"]


def test_a_small_invoice_paid_not_at_all_is_still_collected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = _tools("order-to-cash-exceptions", tmp_path)
    invoice = {"order_id": "SO-9", "invoice_no": "INV-9999", "amount_eur": 10.0}
    monkeypatch.setattr(tools, "_query", lambda *_: [("SO-9", "C-9", "Name", "a@b.example", 10.0)])
    monkeypatch.setattr(tools, "_invoices", lambda: [{**invoice, "due_date": "2026-01-01"}])
    monkeypatch.setattr(tools, "_payments", lambda: [])

    state = tools._classify("SO-9")

    assert state is not None and "unpaid_overdue" in state["issues"]


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -10.0])
def test_a_purchase_order_of_no_valid_amount_is_gated_and_refused(
    tmp_path: Path, amount: float
) -> None:
    tools = _tools("purchase-requisition", tmp_path)

    assert tools.po_needs_approval({"amount_eur": amount})
    assert json.loads(tools.check_budget("CC-100", amount))["status"] == "insufficient"
    assert tools.create_purchase_order("PR-1", "S-100", "CC-100", amount, "x").startswith("Refused")
    assert json.loads(tools.check_budget("CC-100", 100.0))["available_eur"] == 17000.0


def test_a_later_clear_finding_does_not_hide_a_sanctions_hit(tmp_path: Path) -> None:
    tools = _tools("supplier-due-diligence", tmp_path)
    source = "https://example.test/x"
    findings = [
        {"check": "sanctions", "result": "hit", "summary": "listed", "source_url": source},
        {"check": "sanctions", "result": "clear", "summary": "no issue", "source_url": source},
    ]

    assert json.loads(tools.score_risk("SUP-1", findings))["risk_level"] == "critical"


def test_news_links_differ_by_their_query_but_not_by_tracking_parameters(tmp_path: Path) -> None:
    tools = _tools("compliance-news-watch", tmp_path)
    key = tools._key

    assert key("https://www.Site.example/a/?utm_source=x&id=7#top") == key(
        "https://site.example/a?id=7"
    )
    assert key("https://site.example/story?id=1") != key("https://site.example/story?id=2")


def test_a_postmortem_id_with_a_trailing_newline_is_refused(tmp_path: Path) -> None:
    tools = _tools("incident-postmortem", tmp_path)

    assert tools.save_postmortem_draft("INC-1\n", "# Draft").startswith("Not saved")


def test_the_log_level_the_timeline_builder_reads_shows_the_acknowledgement(tmp_path: Path) -> None:
    tools = _tools("incident-postmortem", tmp_path)
    text = (USECASES / "incident-postmortem" / "blueprint.yaml").read_text(encoding="utf-8")
    level = re.search(r"resolved, at level (\w+)", text)
    assert level is not None

    events = tools.log_events(
        "app-2026-10-02.log", "2026-10-02T08:42:00", "2026-10-02T10:33:00", level[1]
    )

    assert "acknowledged the alert" in events


def test_an_item_without_stock_or_sales_is_reported_not_ordered(tmp_path: Path) -> None:
    tools = _tools("inventory-replenishment", tmp_path)
    stock = [{"sku": "X-1", "on_hand": 0, "on_order": 0}]
    demand = [
        {
            "sku": "X-1",
            "supplier_id": "S-1",
            "unit_price": 5.0,
            "lead_days": 14,
            "moq": 10,
            "avg_weekly_units": 0,
        }
    ]

    result = json.loads(tools.calculate_reorders(stock, demand))

    assert result["proposals"] == [] and [i["sku"] for i in result["issues"]] == ["X-1"]


def test_a_cv_email_or_an_invite_message_cannot_forge_headers_or_take_slots(tmp_path: Path) -> None:
    tools = _tools("recruiting-cv-screening", tmp_path)
    profile = (["Python", "SQL", "Airflow", "dbt"], 8.0, ["English", "German"])
    forged = "a@b.example\nBcc: c@d.example"

    assert tools.score_candidate("R-100", "CV-90", forged, *profile).startswith("Refused")
    assert json.loads(tools.score_candidate("R-100", "CV-90", "a@b.example", *profile))[
        "shortlisted"
    ]
    tools.send_interview_invite("CV-90", "S-1", "See you.\nSlot: S-2")

    assert tools._taken_slots(tools._outbox_dir() / "invites") == {"S-1"}


def test_a_variance_just_below_the_watch_limit_is_not_flagged(tmp_path: Path) -> None:
    tools = _tools("weekly-management-report", tmp_path)
    metric = {"name": "revenue", "actual": 104.96, "prior": 100.0, "budget": None}

    [row] = json.loads(tools.variance_table([metric]))["metrics"]

    assert (row["vs_prior_pct"], row["flag"]) == (5.0, "ok")
