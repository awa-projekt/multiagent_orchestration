"""Tools for the sales-operations use case: recording a customer discount.

The sales data is read through the blueprint's `sales` connector (the generic `workspace`
connector on `data/sales.db`); this module only holds the one write: a discount agreement,
appended to `data/outbox/discounts.csv`. The blueprint gates it: a discount above 10 percent
waits for the sales director.
"""

from __future__ import annotations

import csv
import sqlite3
from contextlib import closing
from pathlib import Path

from awa.runtime import case_dir

HERE = Path(__file__).resolve().parent


def _data_dir() -> Path:
    """The sample data: the eval case's copy inside `awa eval`, else the folder here."""

    return case_dir(HERE) / "data"


def _outbox_dir() -> Path:
    return _data_dir() / "outbox"


MAX_PERCENT = 25.0
"""The largest discount `record_discount` accepts, in percent."""


def record_discount(customer_id: str, discount_percent: float, reason: str) -> str:
    """Record a discount agreement for one customer, effective for their next orders.

    `customer_id` is the customer id of the database (for example FENM0),
    `discount_percent` the discount in percent (5 means 5 percent, at most 25), `reason` why the
    customer gets it. Returns the agreement number. In production: write the agreement to the
    pricing conditions of your ERP or CRM.
    """

    customer_id = customer_id.strip().upper()
    if not 0 < discount_percent <= MAX_PERCENT:
        return f"Refused: a discount must be above 0 and at most {MAX_PERCENT:g} percent."
    with closing(sqlite3.connect(_data_dir() / "sales.db")) as database:
        found = database.execute(
            "SELECT TRIM(company_name) FROM customers WHERE customer_id = ?", (customer_id,)
        ).fetchone()
    if found is None:
        return f"Refused: there is no customer {customer_id!r}."
    _outbox_dir().mkdir(parents=True, exist_ok=True)
    ledger = _outbox_dir() / "discounts.csv"
    new = not ledger.is_file()
    count = 0 if new else len(ledger.read_text(encoding="utf-8").splitlines()) - 1
    number = f"DA-{1001 + count}"
    with ledger.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(["agreement", "customer_id", "customer", "discount_percent", "reason"])
        writer.writerow([number, customer_id, found[0], f"{discount_percent:g}", reason.strip()])
    return (
        f"Discount agreement {number} recorded: {discount_percent:g}% for {found[0]} "
        f"({customer_id})."
    )


__all__ = ["record_discount"]
