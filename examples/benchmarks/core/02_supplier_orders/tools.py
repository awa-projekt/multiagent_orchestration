"""Rules and the reschedule request of the supplier order check.

`check_delivery_date` is the real business logic (weekends, public holidays, the next
business day); `request_reschedule` stands for the ERP call that asks a supplier to deliver on
another day: replace its body with your system and keep the signature and the docstring.
"""

from __future__ import annotations

from datetime import date, timedelta

REQUESTS: list[dict[str, str]] = []
"""The reschedule requests the buyer approved, in order."""


def reset_sample_data() -> None:
    """Forget the requests (the benchmark runs every case from the same state)."""

    REQUESTS.clear()


def check_delivery_date(delivery_date: str, holidays: list[str]) -> dict[str, object]:
    """Check a requested delivery date (YYYY-MM-DD) against weekends and public holidays.

    Args:
        delivery_date: The requested delivery date.
        holidays: The nationwide public holidays of the supplier's country, as YYYY-MM-DD dates.

    Returns:
        weekday, is_weekend, is_holiday, ok (a business day) and next_business_day.
    """

    day = date.fromisoformat(delivery_date)
    closed = {date.fromisoformat(item) for item in holidays}
    following = day
    while following.weekday() >= 5 or following in closed:
        following += timedelta(days=1)
    return {
        "delivery_date": day.isoformat(),
        "weekday": day.strftime("%A"),
        "is_weekend": day.weekday() >= 5,
        "is_holiday": day in closed,
        "ok": following == day,
        "next_business_day": following.isoformat(),
    }


def request_reschedule(order_no: str, new_date: str, reason: str) -> str:
    """Ask the supplier of an order to deliver on a business day instead (the buyer approves)."""

    REQUESTS.append({"order_no": order_no, "new_date": new_date, "reason": reason})
    return f"Reschedule request {len(REQUESTS)} sent: order {order_no} now for {new_date}."


__all__ = ["check_delivery_date", "request_reschedule"]
