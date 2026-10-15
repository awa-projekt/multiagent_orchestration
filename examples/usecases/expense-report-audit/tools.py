"""Tools of the expense-report-audit use case: the policy rules and the reimbursement.

Reading reports, receipts and the policy is done by the `workspace` connector
(`connector:workspace.*`, see `blueprint.yaml`); this file holds the real logic: the policy
rules E1 to E6 applied to every report line, and the reimbursement, whose amount comes from the
saved audit and not from the model. In production they read your expense system and pay through
your accounting system (see the README).

`reimbursement_needs_approval` decides which reimbursements wait for the manager (rule E7).
"""

from __future__ import annotations

import csv
import json
import math
import re
import threading
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from typing_extensions import TypedDict

from awa.runtime import case_dir

HERE = Path(__file__).resolve().parent


def _data_dir() -> Path:
    """The sample data: the eval case's copy inside `awa eval`, else the folder here."""

    return case_dir(HERE) / "data"


def _outbox_dir() -> Path:
    return _data_dir() / "outbox"


_LEDGER_LOCK = threading.Lock()
AS_OF: date | None = date(2026, 10, 4)
"""The audit date; the sample reports are dated around it. Set `None` to use today."""
MANAGER_LIMIT_EUR = 500.0
"""Reports above this total need the manager's approval (rule E7)."""
ALCOHOL = ("wine", "beer", "alcohol", "whisky", "whiskey", "cocktail", "champagne", "schnaps")


class ReceiptTotal(TypedDict):
    """The total read on the receipt of one report line."""

    line: str
    total_eur: float


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _flag(flags: list[dict[str, str]], rule: str, severity: str, detail: str) -> None:
    flags.append({"rule": rule, "severity": severity, "detail": detail})


def _audit_file(report_id: str) -> Path:
    return _outbox_dir() / "audits" / f"{report_id.strip().upper()}.json"


def _saved_audit(report_id: str) -> dict[str, Any] | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", report_id.strip()):
        return None
    path = _audit_file(report_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def reimbursement_needs_approval(args: Mapping[str, Any]) -> bool:
    """A reimbursement waits for the manager when the audit says so (rule E7); without a saved
    audit it always waits."""

    audit = _saved_audit(str(args.get("report_id") or ""))
    return audit is None or bool(audit["needs_manager_approval"])


def audit_report(report_id: str, receipt_totals: list[ReceiptTotal]) -> str:
    """Audit an expense report against the policy and save the result.

    `receipt_totals` has one entry (`line`, `total_eur`) for every line that has a receipt: the
    total printed on that receipt (read the receipt PDFs first), compared with the amount
    claimed. Returns, per line, the flags (rule, severity, detail) and the amount that can be
    reimbursed, plus the report totals and whether the manager must approve the payment.

    In production: read the report from your expense system.
    """

    report_id = report_id.strip().upper()
    lines = _rows(_data_dir() / "reports" / f"{report_id}.csv")
    head = next(
        (r for r in _rows(_data_dir() / "reports" / "index.csv") if r["report_id"] == report_id),
        None,
    )
    if not lines or head is None:
        return f"Unknown expense report {report_id!r}."
    policy = {r["category"]: r for r in _rows(_data_dir() / "policy.csv")}
    today = AS_OF or date.today()
    received = {str(entry["line"]): float(entry["total_eur"]) for entry in receipt_totals}
    invalid = [line for line, total in received.items() if not math.isfinite(total) or total < 0]
    if invalid:
        return f"Refused: the receipt total of line(s) {', '.join(invalid)} is not an amount."
    unread = [r["line"] for r in lines if r["receipt"] and r["line"] not in received]
    if unread:
        return (
            f"Refused: pass receipt_totals with the total printed on the receipt of line(s) "
            f"{', '.join(unread)} (read the receipt PDFs first)."
        )
    seen: set[tuple[str, str, str]] = set()
    audited: list[dict[str, Any]] = []
    for row in lines:
        claimed, number = float(row["amount_eur"]), row["line"]
        flags: list[dict[str, str]] = []

        allowed = claimed
        rule = policy.get(row["category"])
        if rule is None:
            allowed = 0.0
            _flag(flags, "E1", "medium", f"unknown category {row['category']!r}")
        elif float(rule["limit_eur"]) == 0:
            allowed = 0.0
            _flag(flags, "E1", "medium", f"{row['category']} is not reimbursable as an expense")
        elif claimed > float(rule["limit_eur"]) * int(row["quantity"]):
            allowed = float(rule["limit_eur"]) * int(row["quantity"])
            _flag(
                flags,
                "E1",
                "medium",
                f"above the limit of {rule['limit_eur']} EUR per {rule['unit']}",
            )
        receipt = _data_dir() / "receipts" / row["receipt"]
        needs_receipt = rule and claimed > float(rule["receipt_required_above_eur"])
        if needs_receipt and not (row["receipt"] and receipt.is_file()):
            allowed = 0.0
            _flag(flags, "E2", "medium", "receipt required but missing")
        key = (row["date"], row["merchant"], row["amount_eur"])
        if key in seen:
            allowed = 0.0
            _flag(flags, "E3", "high", "duplicate of an earlier line")
        seen.add(key)
        spent = date.fromisoformat(row["date"])
        if spent > today or spent < today - timedelta(days=60):
            _flag(flags, "E4", "low", f"dated {row['date']}")
        if any(word in row["description"].lower() for word in ALCOHOL):
            allowed = 0.0
            _flag(flags, "E5", "high", "alcohol is not reimbursable")
        if number in received and abs(received[number] - claimed) > 0.01:
            allowed = min(allowed, received[number])
            _flag(
                flags,
                "E6",
                "high",
                f"claimed {claimed:.2f} EUR, receipt shows {received[number]:.2f} EUR",
            )
        audited.append(
            {
                "line": number,
                "category": row["category"],
                "merchant": row["merchant"],
                "claimed_eur": claimed,
                "reimbursable_eur": round(allowed, 2),
                "flags": flags,
            }
        )
    total = round(sum(line["claimed_eur"] for line in audited), 2)
    high = any(f["severity"] == "high" for line in audited for f in line["flags"])
    audit = {
        "report_id": report_id,
        "employee": head["employee"],
        "claimed_eur": total,
        "reimbursable_eur": round(sum(line["reimbursable_eur"] for line in audited), 2),
        "needs_manager_approval": total > MANAGER_LIMIT_EUR or high,
        "lines": audited,
    }
    _audit_file(report_id).parent.mkdir(parents=True, exist_ok=True)
    _audit_file(report_id).write_text(json.dumps(audit, indent=1), encoding="utf-8")
    return json.dumps(audit)


def submit_reimbursement(report_id: str, note: str) -> str:
    """Pay the reimbursable amount of an audited report to the employee. The amount is taken
    from the saved audit, never from the caller. Needs the manager's approval when the audit says
    so (report above 500 EUR or a high flag).

    In production: create the payment in your accounting system.
    """

    audit = _saved_audit(report_id)
    if audit is None:
        return f"Refused: audit {report_id!r} first (audit_report)."
    if audit["reimbursable_eur"] <= 0:
        return f"Nothing to reimburse for {audit['report_id']}."
    ledger = _outbox_dir() / "reimbursements.csv"
    with _LEDGER_LOCK:  # cases run in parallel threads: check, header and row must not race
        if any(r["report_id"] == audit["report_id"] for r in _rows(ledger)):
            return f"Already submitted: {audit['report_id']}."
        ledger.parent.mkdir(parents=True, exist_ok=True)
        new_file = not ledger.is_file()
        with ledger.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(["report_id", "employee", "amount_eur", "note"])
            writer.writerow(
                [audit["report_id"], audit["employee"], audit["reimbursable_eur"], note]
            )
    return (
        f"Submitted {audit['report_id']} for payment: "
        f"{audit['reimbursable_eur']:.2f} EUR to {audit['employee']}."
    )


__all__ = ["audit_report", "submit_reimbursement"]
