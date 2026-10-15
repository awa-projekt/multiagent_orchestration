"""Tools for the customer-support-desk recipe, backed by small in-memory sample systems.

The tools stand in for the CRM (customers), the billing system (invoices, payments and
refunds), the order management system (orders, return labels), the knowledge base, the
status page and the ticketing system's human queue. Replace each body with a call to your
system; keep the names, signatures and docstrings, which the agents rely on.

`refund_needs_approval` decides which refunds wait for a human (see `approvals:` in
`blueprint.yaml`).
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any, Literal

REFUND_APPROVAL_THRESHOLD = 50.00
"""Refunds above this amount (in the order's currency) wait for a support lead."""

_CUSTOMERS: dict[str, dict[str, Any]] = {
    "C-1001": {"name": "Dana Example", "plan": "Pro", "since": "2023-04", "contact": "email"},
    "C-1002": {"name": "Sam Sample", "plan": "Basic", "since": "2025-11", "contact": "email"},
    "C-1003": {"name": "Robin Demo", "plan": "Business", "since": "2021-01", "contact": "phone"},
}

_INVOICES: dict[str, list[dict[str, Any]]] = {
    "C-1001": [{"invoice": "INV-7001", "date": "2026-09-01", "amount": 19.90, "status": "paid"}],
    "C-1002": [
        {"invoice": "INV-7002", "date": "2026-09-01", "amount": 29.90, "status": "paid"},
        {
            "invoice": "INV-7002",
            "date": "2026-09-01",
            "amount": 29.90,
            "status": "paid",
            "note": "second card payment for the same invoice",
        },
    ],
    "C-1003": [{"invoice": "INV-7003", "date": "2026-09-01", "amount": 99.00, "status": "paid"}],
}

_ORDERS: dict[str, dict[str, Any]] = {
    "O-5001": {
        "customer": "C-1001",
        "item": "Blender B-200",
        "total": 89.00,
        "status": "delivered",
        "delivered_on": "2026-09-20",
    },
    "O-5002": {
        "customer": "C-1002",
        "item": "Travel mug",
        "total": 14.50,
        "status": "delivered",
        "delivered_on": "2026-08-01",
    },
}

_ARTICLES: list[dict[str, str]] = [
    {
        "id": "KB-01",
        "title": "Reset your password",
        "body": "Open the sign-in page, choose 'Forgot password', enter your account email and "
        "follow the link we send (valid for 30 minutes). Check the spam folder if it does "
        "not arrive within 5 minutes.",
    },
    {
        "id": "KB-02",
        "title": "Files are not syncing",
        "body": "Check the status page first. If there is no incident: sign out and in again, "
        "then make sure the desktop app is version 5.2 or later.",
    },
    {
        "id": "KB-03",
        "title": "Returns and refunds",
        "body": "Items can be returned within 30 days of delivery. Damaged items are refunded "
        "in full without a return; other returns need a prepaid return label.",
    },
    {
        "id": "KB-04",
        "title": "Billing cycle",
        "body": "Plans are charged on the 1st of each month. Duplicate charges are refunded to "
        "the original payment method within 5 business days.",
    },
]

_STATUS: dict[str, dict[str, str]] = {
    "sync": {
        "state": "degraded",
        "incident": "INC-311",
        "detail": "Delayed file sync for some EU accounts since 07:40 UTC; fix rolling out.",
        "next_update": "2026-09-27 12:00 UTC",
    },
    "billing": {"state": "operational"},
    "sign-in": {"state": "operational"},
}

REFUNDS: list[dict[str, Any]] = []
"""Refunds issued (the billing system)."""
RETURN_LABELS: list[dict[str, Any]] = []
"""Return labels created (the order management system)."""
ESCALATIONS: list[dict[str, Any]] = []
"""Tickets in the human support queue."""


def reset_sample_data() -> None:
    """Restore the sample systems to their initial state (used by tests)."""

    REFUNDS.clear()
    RETURN_LABELS.clear()
    ESCALATIONS.clear()


def refund_needs_approval(args: Mapping[str, Any]) -> bool:
    """Refunds that take an order's refunded sum above the threshold, or without a valid positive
    amount, wait for a human (`tools:refund_needs_approval`)."""

    amount = float(args.get("amount") or 0)
    total = amount + _refunded(str(args.get("order_id") or ""))
    return not (math.isfinite(amount) and amount > 0 and total <= REFUND_APPROVAL_THRESHOLD)


def _refunded(order_id: str) -> float:
    key = order_id.strip().upper()
    return sum(r["amount"] for r in REFUNDS if key and r["order_id"].strip().upper() == key)


def lookup_customer(customer_id: str) -> str:
    """Return a customer's name, plan, customer-since date and preferred contact channel.

    In production: read the customer record from the CRM.
    """

    customer = _CUSTOMERS.get(customer_id.strip().upper())
    if customer is None:
        return f"No customer {customer_id!r}. Ask the customer for their customer number."
    return json.dumps({"customer_id": customer_id, **customer})


def search_knowledge_base(query: str) -> str:
    """Search the help-center articles; returns the best matches with id, title and text.

    In production: query your knowledge base or help-center search.
    """

    terms = {word for word in query.casefold().split() if len(word) > 3}
    scored = [
        (sum(term in f"{a['title']} {a['body']}".casefold() for term in terms), a)
        for a in _ARTICLES
    ]
    hits = [article for score, article in sorted(scored, key=lambda s: -s[0]) if score][:2]
    return json.dumps({"articles": hits}) if hits else "No matching article."


def lookup_invoices(customer_id: str) -> str:
    """Return a customer's invoices and card payments of the last three months.

    In production: query the billing system.
    """

    invoices = _INVOICES.get(customer_id.strip().upper())
    if invoices is None:
        return f"No invoices for {customer_id!r}."
    return json.dumps({"customer_id": customer_id, "payments": invoices})


def lookup_order(order_id: str) -> str:
    """Return an order's customer, item, total, status and delivery date.

    In production: read the order from the order management system.
    """

    order = _ORDERS.get(order_id.strip().upper())
    return json.dumps({"order_id": order_id, **order}) if order else f"No order {order_id!r}."


def get_service_status(component: Literal["sync", "billing", "sign-in"]) -> str:
    """Return the live status of a product component, with the incident id if there is one.

    In production: read your status page or monitoring system.
    """

    return json.dumps({"component": component, **_STATUS.get(component, {"state": "unknown"})})


def create_return_label(order_id: str) -> str:
    """Create a prepaid return label for an order and email it to the customer.

    In production: call the carrier integration of your order management system.
    """

    order = _ORDERS.get(order_id.strip().upper())
    if order is None:
        return f"No order {order_id!r}."
    label = f"RL-{9001 + len(RETURN_LABELS)}"
    RETURN_LABELS.append({"order_id": order_id, "label": label})
    return f"Return label {label} for order {order_id} was sent to the customer."


def issue_refund(customer_id: str, amount: float, reason: str, order_id: str = "") -> str:
    """Refund money to the customer's original payment method. Irreversible.

    Refunds above 50.00 (counted per order) wait for a support lead's approval. In production:
    call the payment provider through your billing system.
    """

    if not (math.isfinite(amount) and amount > 0):
        return "Refund refused: the amount must be a positive number."
    if customer_id.strip().upper() not in _CUSTOMERS:
        return f"Refund refused: unknown customer {customer_id!r}."
    if order_id:
        order = _ORDERS.get(order_id.strip().upper())
        if order is None or order["customer"] != customer_id.strip().upper():
            return f"Refund refused: order {order_id!r} does not belong to {customer_id}."
        left = order["total"] - _refunded(order_id)
        if amount > left:
            return f"Refund refused: {left:.2f} of the order total {order['total']:.2f} is left."
    reference = f"RF-{4001 + len(REFUNDS)}"
    REFUNDS.append(
        {"customer_id": customer_id, "order_id": order_id, "amount": amount, "reason": reason}
    )
    return f"Refunded {amount:.2f} as {reference}; it arrives within 5 business days."


def escalate_to_human(
    customer_id: str, summary: str, priority: Literal["normal", "high", "urgent"] = "normal"
) -> str:
    """Hand the case to the human support queue with a summary; returns the ticket number.

    Use it when the customer asks for a person, threatens legal action, or the problem
    cannot be solved with your tools. In production: create a ticket in your ticketing system.
    """

    ticket = f"T-{2001 + len(ESCALATIONS)}"
    ESCALATIONS.append(
        {"ticket": ticket, "customer_id": customer_id, "summary": summary, "priority": priority}
    )
    return f"Escalated as ticket {ticket} ({priority}); a support lead replies within 4 hours."


__all__ = [
    "create_return_label",
    "escalate_to_human",
    "get_service_status",
    "issue_refund",
    "lookup_customer",
    "lookup_invoices",
    "lookup_order",
    "search_knowledge_base",
]
