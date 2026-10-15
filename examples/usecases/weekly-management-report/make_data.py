"""Writes the sample data of the weekly-management-report use case into `data/` (deterministic):
the sales database `sales.db` with eight weeks of orders, new customers and weekly budgets,
and `report_config.json` with the recipients. The last week starts on `last_week` (default
2026-09-28); `python make_data.py --current` ends the data with the week before today, so
the schedule's next run finds a complete week."""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

REGIONS = ["North", "South", "East", "West"]
PRODUCTS = {"Pump X": (480.0, 0.34), "Valve V": (120.0, 0.41), "Sensor S": (35.0, 0.52)}
BASE_QUANTITY = {"Pump X": [3, 4, 2, 3], "Valve V": [9, 8, 7, 10], "Sensor S": [40, 35, 30, 45]}
CONFIG = {"recipients": ["management@example.com"], "currency": "EUR"}


def _orders(first_week: date) -> list[tuple[str, str, str | None, str, int, float, float, str]]:
    rows = []
    for week in range(8):
        start = first_week + timedelta(weeks=week)
        for r, region in enumerate(REGIONS):
            for p, (product, (price, margin)) in enumerate(PRODUCTS.items()):
                for day in (0, 2, 4):
                    quantity = BASE_QUANTITY[product][r] + (week * 7 + r * 3 + p * 5 + day) % 4
                    if week == 7 and (product, region) == ("Pump X", "South"):
                        quantity = 1  # last week: the supplier outage
                    if week == 7 and (product, region) == ("Sensor S", "East") and day == 2:
                        quantity *= 6  # last week: one large key-account order
                    revenue = round(quantity * price, 2)
                    order_id = f"O-{week + 1}{r}{p}{day}"
                    customer = f"C-{200 + (week * 3 + r * 2 + p + day) % 40}"
                    stored_region = None if (week == 7 and order_id.endswith("12")) else region
                    rows.append(
                        (
                            order_id,
                            (start + timedelta(days=day)).isoformat(),
                            stored_region,
                            product,
                            quantity,
                            revenue,
                            round(revenue * (1 - margin), 2),
                            customer,
                        )
                    )
    return rows


def generate(directory: Path, last_week: date = date(2026, 9, 28)) -> None:
    """Write `sales.db` and `report_config.json`; `last_week` is a Monday."""

    directory.mkdir(parents=True, exist_ok=True)
    first_week = last_week - timedelta(weeks=7)
    orders = _orders(first_week)
    seen: dict[str, str] = {}
    for _, order_date, _, _, _, _, _, customer in orders:
        seen.setdefault(customer, order_date)
    customers = sorted(seen.items())
    weeks = [first_week + timedelta(weeks=n) for n in range(8)]
    earlier = [o for o in orders if o[1] < weeks[7].isoformat()]
    baseline = [sum(o[5] for o in earlier) / 7, len(earlier) / 7]
    budget = [
        (week.isoformat(), metric, round(value * 1.02))
        for week in weeks
        for metric, value in zip(("revenue", "orders"), baseline, strict=True)
    ]
    database = directory / "sales.db"
    database.unlink(missing_ok=True)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TABLE orders (order_id TEXT PRIMARY KEY, order_date TEXT NOT NULL, region TEXT, "
            "product TEXT NOT NULL, quantity INTEGER NOT NULL, revenue REAL NOT NULL, "
            "cost REAL NOT NULL, customer_id TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE customers (customer_id TEXT PRIMARY KEY, first_order_date TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE budget (week_start TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT "
            "NULL, PRIMARY KEY (week_start, metric))"
        )
        connection.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)", orders)
        connection.executemany("INSERT INTO customers VALUES (?, ?)", customers)
        connection.executemany("INSERT INTO budget VALUES (?, ?, ?)", budget)
    text = json.dumps(CONFIG, indent=2) + "\n"
    (directory / "report_config.json").write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    today = date.today()
    current = today - timedelta(days=today.weekday() + 7)
    generate(
        Path(__file__).resolve().parent / "data",
        current if "--current" in sys.argv else date(2026, 9, 28),
    )
