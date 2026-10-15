"""Tools for the `support_triage.yaml` blueprint, backed by small in-memory sample data.

Replace the sample data with calls to your order, billing and payment systems.
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

ORDERS: dict[str, dict[str, object]] = {
    "1001": {"status": "delivered", "items": ["kettle"], "total": 49.90, "shipment": "SH-77"},
    "1002": {"status": "shipped", "items": ["toaster", "mug"], "total": 64.50, "shipment": "SH-78"},
}
SHIPMENTS: dict[str, dict[str, str]] = {
    "SH-77": {"carrier": "standard post", "delivered_on": "2026-09-20"},
    "SH-78": {"carrier": "standard post", "expected_on": "2026-09-29"},
}
INVOICES: dict[str, dict[str, object]] = {
    "INV-1001": {"order": "1001", "amount": 49.90, "paid": True},
    "INV-1002": {"order": "1002", "amount": 64.50, "paid": False},
}
REFUNDS: list[dict[str, object]] = []


@tool
def lookup_order(order_id: str) -> str:
    """Return the status, items, total and shipment id of an order."""

    order = ORDERS.get(order_id)
    return json.dumps(order) if order else f"No order {order_id!r} found."


@tool
def track_shipment(shipment_id: str) -> str:
    """Return the carrier and delivery date of a shipment."""

    shipment = SHIPMENTS.get(shipment_id)
    return json.dumps(shipment) if shipment else f"No shipment {shipment_id!r} found."


@tool
def lookup_invoice(invoice_id: str) -> str:
    """Return the order, amount and payment status of an invoice."""

    invoice = INVOICES.get(invoice_id)
    return json.dumps(invoice) if invoice else f"No invoice {invoice_id!r} found."


@tool
def issue_refund(order_id: str, amount: float, reason: str) -> str:
    """Refund `amount` for an order. Irreversible: requires human approval."""

    order = ORDERS.get(order_id)
    if order is None:
        return f"No order {order_id!r} found."
    total = order["total"]
    if not isinstance(total, float) or amount <= 0 or amount > total:
        return f"Refund rejected: amount must be between 0 and {total}."
    REFUNDS.append({"order": order_id, "amount": amount, "reason": reason})
    return f"Refunded {amount:.2f} for order {order_id}."


__all__ = ["issue_refund", "lookup_invoice", "lookup_order", "track_shipment"]
