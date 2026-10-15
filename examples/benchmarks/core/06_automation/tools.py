"""Tools of the ledger desk: a ledger kept in `ledger.csv` next to this file.

Replace the ledger with a call to your accounting system and keep the signatures and docstrings.
"""

from __future__ import annotations

import csv
from pathlib import Path

LEDGER = Path(__file__).with_name("ledger.csv")


def _rows() -> list[dict[str, str]]:
    if not LEDGER.exists():
        return []
    with LEDGER.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def book_invoice(invoice_no: str, supplier: str, amount: float) -> str:
    """Book a supplier invoice (gross amount in EUR) in the ledger."""

    rows = _rows()
    if any(row["invoice_no"] == invoice_no for row in rows):
        return f"Invoice {invoice_no} is already booked; nothing changed."
    with LEDGER.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if not rows:
            writer.writerow(["invoice_no", "supplier", "amount"])
        writer.writerow([invoice_no, supplier, f"{amount:.2f}"])
    return f"Booked {invoice_no} from {supplier} over {amount:.2f} EUR as entry {len(rows) + 1}."


def ledger_summary() -> str:
    """Return how many invoices are booked and their total in EUR, as one line of text.

    Reads the whole ledger; it changes nothing.
    """

    rows = _rows()
    return f"{len(rows)} invoices booked, {sum(float(row['amount']) for row in rows):.2f} EUR in total."


__all__ = ["book_invoice", "ledger_summary"]
