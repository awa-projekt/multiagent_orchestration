"""Validation rules, booking and hold for the document intake.

`validate_invoice` is the real logic: it checks the extracted fields against the vendor master
(`data/vendors.csv`, German CSV in Windows-1252) and the purchase orders. `book_invoice` and
`hold_invoice` stand for the ERP and the exception queue: replace their bodies with your systems
and keep the signatures and docstrings, which is what the agent sees.
"""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path
from typing import Any

DATA = Path(__file__).with_name("data")
LEDGER: list[dict[str, Any]] = []
"""Booked invoices, in order."""
HOLDS: list[dict[str, Any]] = []
"""Invoices put on hold, in order."""


def reset_sample_data() -> None:
    """Empty the ledger and the hold queue (every benchmark case starts clean)."""

    LEDGER.clear()
    HOLDS.clear()


def _number(text: str) -> float:
    return float(text.replace(".", "").replace(",", "."))


def _vendors() -> list[dict[str, str]]:
    with (DATA / "vendors.csv").open(encoding="cp1252", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


def validate_invoice(
    vendor_vat_id: str,
    iban: str,
    po_number: str,
    net_amount: float,
    tax_amount: float,
    gross_amount: float,
    invoice_date: str,
    due_date: str,
) -> dict[str, Any]:
    """Validate an extracted invoice against the vendor master and the purchase orders.

    Rules: V1 the vendor (found by VAT id) exists and is active; V2 the IBAN is the one in the
    vendor master; V3 the purchase order exists, belongs to the vendor and the net amount is at
    most 2 % above it; V4 net plus tax is the gross amount and the tax is 19 % or 7 % of the
    net amount; V5 the due date is after the invoice date; V6 the gross amount is within the
    vendor's order limit.

    Args:
        vendor_vat_id: VAT id of the vendor as printed (e.g. DE811111111).
        iban: IBAN as printed.
        po_number: Purchase order number as printed.
        net_amount: Net amount in EUR as a number.
        tax_amount: Tax amount in EUR as a number.
        gross_amount: Gross amount in EUR as a number.
        invoice_date: Invoice date as YYYY-MM-DD.
        due_date: Due date as YYYY-MM-DD.

    Returns:
        valid, the vendor id, and the violations (rule id and detail).
    """

    violations: list[dict[str, str]] = []

    def violate(rule: str, detail: str) -> None:
        violations.append({"rule": rule, "detail": detail})

    vendor = next((v for v in _vendors() if v["USt-IdNr"] == vendor_vat_id.strip()), None)
    if vendor is None:
        violate("V1", f"no vendor with VAT id {vendor_vat_id}")
    elif vendor["Status"] != "aktiv":
        violate("V1", f"vendor {vendor['Lieferantennr']} is {vendor['Status']}")
    if vendor is not None and iban.replace(" ", "") != vendor["IBAN"]:
        violate("V2", f"IBAN {iban} differs from the vendor master")
    orders = json.loads((DATA / "purchase_orders.json").read_text(encoding="utf-8"))
    order = orders.get(po_number.strip())
    if order is None:
        violate("V3", f"purchase order {po_number} does not exist")
    elif vendor is not None and order["vendor_id"] != vendor["Lieferantennr"]:
        violate("V3", f"purchase order {po_number} belongs to {order['vendor_id']}")
    elif net_amount > order["net_amount"] * 1.02:
        violate("V3", f"net {net_amount:.2f} exceeds the order value {order['net_amount']:.2f}")
    rate_ok = any(abs(tax_amount - net_amount * rate) <= 0.02 for rate in (0.19, 0.07))
    if abs(net_amount + tax_amount - gross_amount) > 0.01 or not rate_ok:
        violate("V4", f"net {net_amount:.2f} + tax {tax_amount:.2f} vs gross {gross_amount:.2f}")
    if date.fromisoformat(due_date) <= date.fromisoformat(invoice_date):
        violate("V5", f"due date {due_date} is not after the invoice date {invoice_date}")
    if vendor is not None and gross_amount > _number(vendor["Bestelllimit_EUR"]):
        violate("V6", f"gross {gross_amount:.2f} exceeds the limit of {vendor['Bestelllimit_EUR']}")
    return {
        "valid": not violations,
        "vendor_id": vendor["Lieferantennr"] if vendor else None,
        "violations": violations,
    }


def book_invoice(
    vendor_id: str,
    invoice_number: str,
    po_number: str,
    net_amount: float,
    tax_amount: float,
    gross_amount: float,
    due_date: str,
) -> str:
    """Book a validated invoice in the ERP (amounts in EUR; above 10,000 EUR a manager approves)."""

    reference = f"FI-2024-{len(LEDGER) + 1:04d}"
    LEDGER.append(
        {
            "reference": reference,
            "vendor_id": vendor_id,
            "invoice_number": invoice_number,
            "po_number": po_number,
            "net_amount": net_amount,
            "tax_amount": tax_amount,
            "gross_amount": gross_amount,
            "due_date": due_date,
        }
    )
    return f"Booked invoice {invoice_number} as {reference}."


def hold_invoice(document: str, invoice_number: str | None, reasons: list[str]) -> str:
    """Put a document on hold in the exception queue with every reason (rule id and sentence)."""

    case = f"HOLD-{len(HOLDS) + 1:03d}"
    HOLDS.append(
        {"case": case, "document": document, "invoice_number": invoice_number, "reasons": reasons}
    )
    return f"Put {document} on hold as {case}."


__all__ = ["book_invoice", "hold_invoice", "validate_invoice"]
