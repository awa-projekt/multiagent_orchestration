"""Tools of the purchase-requisition use case: the budget rules and the purchase-order ledger.

Reading requisitions, the supplier list and the policy is done by the `workspace` connector
(`connector:workspace.*`, see `blueprint.yaml`); this file holds only the real business logic:
the budget calculation and the issuing of purchase orders. Both work on the CSV files in
`data/`; in production they call your ERP (see the README).

`po_needs_approval` decides which purchase orders wait for the budget owner.
"""

from __future__ import annotations

import csv
import json
import math
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from awa.runtime import case_dir

HERE = Path(__file__).resolve().parent


def _data_dir() -> Path:
    """The sample data: the eval case's copy inside `awa eval`, else the folder here."""

    return case_dir(HERE) / "data"


def _ledger_file() -> Path:
    return _data_dir() / "outbox" / "purchase_orders.csv"


APPROVAL_LIMIT_EUR = 1000.0
"""Purchase orders above this amount wait for the budget owner's approval (policy P4)."""

_LEDGER_LOCK = threading.Lock()
_FIELDS = ["po_number", "requisition_id", "supplier_id", "cost_center", "amount_eur", "description"]


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _budget(cost_center: str) -> dict[str, Any] | None:
    row = next(
        (r for r in _rows(_data_dir() / "budgets.csv") if r["cost_center"] == cost_center), None
    )
    if row is None:
        return None
    issued = sum(
        float(r["amount_eur"]) for r in _rows(_ledger_file()) if r["cost_center"] == cost_center
    )
    available = (
        float(row["annual_budget_eur"])
        - float(row["spent_eur"])
        - float(row["committed_eur"])
        - issued
    )
    return {
        "cost_center": cost_center,
        "name": row["name"],
        "owner": row["owner"],
        "available_eur": round(available, 2),
    }


def _affordable(amount_eur: float, budget: dict[str, Any]) -> bool:
    return math.isfinite(amount_eur) and 0 < amount_eur <= budget["available_eur"]


def po_needs_approval(args: Mapping[str, Any]) -> bool:
    """Purchase orders above the limit, or without a valid positive amount, wait for the budget
    owner (policy P4)."""

    amount = float(args.get("amount_eur") or 0)
    return not (math.isfinite(amount) and 0 < amount <= APPROVAL_LIMIT_EUR)


def check_budget(cost_center: str, amount_eur: float) -> str:
    """Check whether a cost center can pay `amount_eur`: its owner, the budget still available
    (annual budget minus spent, committed and purchase orders already issued) and a verdict.

    In production: read the budget from the ERP.
    """

    budget = _budget(cost_center.strip().upper())
    if budget is None:
        return json.dumps({"status": "unknown_cost_center", "cost_center": cost_center})
    status = "ok" if _affordable(amount_eur, budget) else "insufficient"
    return json.dumps({"status": status, **budget, "requested_eur": amount_eur})


def create_purchase_order(
    requisition_id: str, supplier_id: str, cost_center: str, amount_eur: float, description: str
) -> str:
    """Issue a purchase order and book it against the cost center. Refused for a supplier that
    is not approved, an unknown cost center or too little budget. Calling it again for the same
    requisition returns the existing order. Above 1,000 EUR it waits for the budget owner.

    In production: create the order in the ERP.
    """

    cost_center = cost_center.strip().upper()
    with _LEDGER_LOCK:  # cases run in parallel threads: check, budget, number and row must not race
        existing = next(
            (r for r in _rows(_ledger_file()) if r["requisition_id"] == requisition_id), None
        )
        if existing:
            return f"Already created: {existing['po_number']} for {requisition_id}."
        suppliers = _rows(_data_dir() / "suppliers.csv")
        supplier = next((r for r in suppliers if r["supplier_id"] == supplier_id), None)
        if supplier is None or supplier["status"] != "approved":
            return f"Refused: supplier {supplier_id} is not an approved supplier (policy P1)."
        budget = _budget(cost_center)
        if budget is None or not _affordable(amount_eur, budget):
            return f"Refused: cost center {cost_center} cannot pay {amount_eur} EUR (policy P3)."
        _ledger_file().parent.mkdir(parents=True, exist_ok=True)
        number = f"PO-{7001 + len(_rows(_ledger_file()))}"
        new_file = not _ledger_file().is_file()
        row = [number, requisition_id, supplier_id, cost_center, amount_eur, description]
        with _ledger_file().open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(_FIELDS)
            writer.writerow(row)
        return f"Created {number}: {amount_eur} EUR to {supplier['name']} ({supplier['email']}), {cost_center}."


__all__ = ["check_budget", "create_purchase_order"]
