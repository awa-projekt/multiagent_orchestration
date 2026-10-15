"""Writes the sample data of the inventory-replenishment use case into `data/` (deterministic):
the item and sales database (`inventory.db`) and the stock levels the sample ERP serves."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

SUPPLIERS = [
    ("S-10", "Nordlager GmbH"),
    ("S-20", "Hydraulik Meier AG"),
    ("S-30", "Elektro Weber KG"),
]
# sku, name, supplier, unit price EUR, minimum order quantity, lead time in days
ITEMS = [
    ("BRG-6204", "Ball bearing 6204", "S-10", 4.20, 200, 7),
    ("HYD-HOSE-12", "Hydraulic hose 12 mm", "S-20", 18.50, 50, 14),
    ("MOT-AC-750", "AC motor 0.75 kW", "S-30", 310.00, 5, 21),
    ("GSK-KIT-A", "Gasket kit A", "S-10", 27.90, 20, 7),
    ("FLT-OIL-3", "Oil filter 3 inch", "S-20", 9.80, 100, 10),
]
WEEKS = ["2026-08-24", "2026-08-31", "2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28"]
WEEKLY_UNITS = {  # units sold per week, oldest first
    "BRG-6204": [140, 160, 150, 145, 155, 150],
    "HYD-HOSE-12": [18, 22, 20, 19, 21, 20],
    "MOT-AC-750": [5, 7, 6, 6, 5, 7],
    "GSK-KIT-A": [10, 14, 12, 11, 13, 12],
    "FLT-OIL-3": [38, 42, 40, 41, 39, 40],
}
# What the ERP reports: on hand and on order. HYD-HOSE-12 has a negative count (a booking
# error) and XTR-77 is not in the item database.
STOCK = [
    {"sku": "BRG-6204", "on_hand": 120, "on_order": 0},
    {"sku": "HYD-HOSE-12", "on_hand": -4, "on_order": 0},
    {"sku": "MOT-AC-750", "on_hand": 12, "on_order": 0},
    {"sku": "GSK-KIT-A", "on_hand": 15, "on_order": 0},
    {"sku": "FLT-OIL-3", "on_hand": 150, "on_order": 100},
    {"sku": "XTR-77", "on_hand": 3, "on_order": 0},
]


def generate(directory: Path) -> None:
    """Write `inventory.db` and `erp_stock.json` into `directory`."""

    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "inventory.db"
    database.unlink(missing_ok=True)
    sales = [
        (sku, week, units)
        for sku, row in WEEKLY_UNITS.items()
        for week, units in zip(WEEKS, row, strict=True)
    ]
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE suppliers (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE items (sku TEXT PRIMARY KEY, name TEXT NOT NULL, supplier_id TEXT "
            "NOT NULL REFERENCES suppliers(id), unit_price REAL NOT NULL, moq INTEGER NOT NULL, "
            "lead_days INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE weekly_sales (sku TEXT NOT NULL, week_start TEXT NOT NULL, "
            "units INTEGER NOT NULL, PRIMARY KEY (sku, week_start))"
        )
        connection.executemany("INSERT INTO suppliers VALUES (?, ?)", SUPPLIERS)
        connection.executemany("INSERT INTO items VALUES (?, ?, ?, ?, ?, ?)", ITEMS)
        connection.executemany("INSERT INTO weekly_sales VALUES (?, ?, ?)", sales)
    text = json.dumps({"items": STOCK}, indent=2) + "\n"
    (directory / "erp_stock.json").write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    generate(Path(__file__).resolve().parent / "data")
