"""Tools for the inventory-replenishment use case: the reorder calculation.

Stock comes from the ERP (REST connector `erp`) and item and sales data from the database
(connector `inventory`), both declared in `blueprint.yaml`; this module only holds the rule
that decides what to reorder, so it is plain, testable arithmetic.
"""

from __future__ import annotations

import json
import math
from typing import Any

from typing_extensions import TypedDict

SAFETY_WEEKS = 1.0
COVER_WEEKS = 4.0


class StockLevel(TypedDict):
    """Stock of one SKU as the ERP reports it."""

    sku: str
    on_hand: float
    on_order: float


class Demand(TypedDict):
    """One item with its supplier terms and recent sales, as the database query returns it."""

    sku: str
    supplier_id: str
    unit_price: float
    lead_days: int
    moq: int
    avg_weekly_units: float


def calculate_reorders(stock: list[StockLevel], demand: list[Demand]) -> str:
    """Decide which items to reorder and how much, from stock levels and demand data.

    `stock` rows: sku, on_hand, on_order (from the ERP). `demand` rows: sku, supplier_id,
    unit_price, moq, lead_days, avg_weekly_units (from the database). An item needs an order
    when on hand plus on order is at or below its reorder point (average weekly units times
    lead time plus one safety week). The quantity restores four weeks of cover after the lead
    time, rounded up to a multiple of the minimum order quantity. Negative stock counts, SKUs
    without demand data and items with neither stock nor sales are reported as issues and never
    ordered.
    """

    known = {str(row["sku"]): row for row in demand}
    proposals: list[dict[str, Any]] = []
    sufficient: list[str] = []
    issues: list[dict[str, str]] = []
    for level in stock:
        sku = str(level["sku"])
        row = known.get(sku)
        if row is None:
            issues.append({"sku": sku, "issue": "no item or sales data: not ordered"})
            continue
        on_hand, on_order = float(level["on_hand"]), float(level["on_order"])
        if on_hand < 0 or on_order < 0:
            issue = f"negative stock count ({on_hand:g}): check the booking, not ordered"
            issues.append({"sku": sku, "issue": issue})
            continue
        weekly = float(row["avg_weekly_units"])
        lead_weeks = float(row["lead_days"]) / 7
        reorder_point = weekly * (lead_weeks + SAFETY_WEEKS)
        position = on_hand + on_order
        if position > reorder_point:
            sufficient.append(sku)
            continue
        missing = weekly * (lead_weeks + COVER_WEEKS) - position
        moq = int(row["moq"])
        quantity = math.ceil(missing / moq) * moq
        if quantity <= 0:
            issues.append({"sku": sku, "issue": "no stock and no sales to plan from: not ordered"})
            continue
        price = float(row["unit_price"])
        proposals.append(
            {
                "sku": sku,
                "supplier_id": row["supplier_id"],
                "quantity": quantity,
                "unit_price": price,
                "order_value": round(quantity * price, 2),
                "reason": (
                    f"stock {position:g} (on hand {on_hand:g} + on order {on_order:g}) is at or "
                    f"below the reorder point {reorder_point:.0f} "
                    f"({weekly:g} units a week, {row['lead_days']} days lead time)"
                ),
            }
        )
    return json.dumps({"proposals": proposals, "sufficient": sufficient, "issues": issues})


__all__ = ["calculate_reorders"]
