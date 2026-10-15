"""The sample tools of the recipes refuse what their rules forbid, whatever the model passes."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

RECIPES = Path(__file__).resolve().parents[2] / "examples" / "recipes"


def _tools(slug: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"recipe_tools_{slug}", RECIPES / slug / "tools.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.reset_sample_data()
    return module


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -5.0])
def test_support_refunds_of_no_valid_amount_need_approval_and_are_refused(amount: float) -> None:
    tools = _tools("customer-support-desk")

    assert tools.refund_needs_approval({"amount": amount})
    assert tools.issue_refund("C-1001", amount, "test").startswith("Refund refused")
    assert tools.REFUNDS == []


def test_support_refunds_of_one_order_add_up_to_the_threshold_and_the_total() -> None:
    tools = _tools("customer-support-desk")
    first = {"customer_id": "C-1001", "amount": 40.0, "reason": "test", "order_id": "O-5001"}

    assert not tools.refund_needs_approval(first)
    assert tools.issue_refund(**first).startswith("Refunded")
    assert tools.refund_needs_approval({**first, "order_id": "o-5001"})
    assert tools.issue_refund(**{**first, "amount": 49.5}).startswith("Refund refused")


@pytest.mark.parametrize("gross", [float("nan"), float("inf"), -500.0, 0.0])
def test_invoice_bookings_of_no_valid_amount_need_approval_and_are_refused(gross: float) -> None:
    tools = _tools("invoice-processing")
    booked = len(tools.LEDGER)

    assert tools.booking_needs_approval({"gross_amount": gross, "currency": "EUR"})
    done = tools.book_invoice("V-100", "INV-NEW-1", "PO-1", gross, 0.0, gross, "EUR", "2026-11-01")
    assert done.startswith("Booking refused")
    assert len(tools.LEDGER) == booked


def test_invoice_duplicates_are_found_whatever_the_case_or_spacing() -> None:
    tools = _tools("invoice-processing")
    args = ("PO-1", 100.0, 19.0, 119.0, "EUR", "2026-11-01")

    assert tools.book_invoice("V-100", "INV-DUP-7", *args).startswith("Booked")
    assert tools.book_invoice(" v-100", "inv-dup-7 ", *args).startswith("Booking refused")
    assert json.loads(tools.find_booked_invoices("v-100", " INV-DUP-7"))["matches"]


def test_incident_tools_find_a_service_whatever_its_case() -> None:
    tools = _tools("it-incident-triage")

    assert json.loads(tools.query_logs(" Checkout"))["lines"]
    assert json.loads(tools.get_recent_deploys("CHECKOUT"))["deploys"]
