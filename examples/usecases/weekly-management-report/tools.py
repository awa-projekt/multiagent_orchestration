"""Tools for the weekly-management-report use case.

The figures come from SQL through the `finance` connector declared in `blueprint.yaml`; this
module holds what must be exact: the reporting weeks, the variance arithmetic with its
thresholds, and the mail to management (recipients from `data/report_config.json`, sent
copies in `out/`). Replace `send_report` by the `smtp` connector (see README); the approval stays.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from typing_extensions import TypedDict

from awa.runtime import case_dir

HERE = Path(__file__).resolve().parent


def _data_dir() -> Path:
    """The sample data: the eval case's copy inside `awa eval`, else the folder here."""

    return case_dir(HERE) / "data"


def _out_dir() -> Path:
    """Where the process writes: the eval case's copy inside `awa eval`, else out/ here."""

    return case_dir(HERE) / "out"


ALERT_PCT = 10.0
WATCH_PCT = 5.0


class Metric(TypedDict):
    """One KPI of the reporting week; `prior` and `budget` are `null` when there is none."""

    name: str
    actual: float
    prior: float | None
    budget: float | None


def reporting_weeks(run_date: str) -> str:
    """Return the weeks to report for a run date (YYYY-MM-DD): the last complete Monday-to-Sunday
    week before it and the week before that, as `week_start` / `week_end` (inclusive) dates."""

    day = date.fromisoformat(run_date)
    start = day - timedelta(days=day.weekday() + 7)
    weeks = {
        "week_start": start.isoformat(),
        "week_end": (start + timedelta(days=6)).isoformat(),
        "prior_week_start": (start - timedelta(days=7)).isoformat(),
        "prior_week_end": (start - timedelta(days=1)).isoformat(),
    }
    return json.dumps(weeks)


def _pct(value: float, reference: float | None) -> float | None:
    return None if not reference else (value - reference) / abs(reference) * 100


def _shown(pct: float | None) -> float | None:
    return None if pct is None else round(pct, 1)


def _flag(*changes: float | None) -> str:
    worst = max((abs(c) for c in changes if c is not None), default=0.0)
    return "alert" if worst >= ALERT_PCT else "watch" if worst >= WATCH_PCT else "ok"


def variance_table(metrics: list[Metric]) -> str:
    """Compute the variances of the KPIs against the prior week and the budget.

    Each metric has `name`, `actual`, `prior` and `budget` (`null` when there is none). Returns the same
    metrics with `vs_prior_pct`, `vs_budget_pct` and a `flag`: `alert` from 10 % off either
    reference, `watch` from 5 %, else `ok`. Explain every alert and watch metric in the report.
    """

    rows = []
    for metric in metrics:
        actual = float(metric["actual"])
        prior = float(metric["prior"]) if metric.get("prior") is not None else None
        budget = float(metric["budget"]) if metric.get("budget") is not None else None
        versus_prior, versus_budget = _pct(actual, prior), _pct(actual, budget)
        rows.append(
            {
                **metric,
                "vs_prior_pct": _shown(versus_prior),
                "vs_budget_pct": _shown(versus_budget),
                "flag": _flag(versus_prior, versus_budget),
            }
        )
    return json.dumps({"metrics": rows})


def send_report(subject: str, body: str) -> str:
    """Email the weekly report to management (recipients from the report configuration).

    Waits for a person's approval. In production: send through your mail system (`smtp`).
    """

    if not body.strip():
        return "Not sent: the report is empty."
    recipients = json.loads((_data_dir() / "report_config.json").read_text(encoding="utf-8"))[
        "recipients"
    ]
    _out_dir().mkdir(exist_ok=True)
    with (_out_dir() / "outbox.jsonl").open("a", encoding="utf-8") as outbox:
        outbox.write(json.dumps({"to": recipients, "subject": subject, "body": body}) + "\n")
    return f"Sent {subject!r} to {', '.join(recipients)}."


__all__ = ["reporting_weeks", "send_report", "variance_table"]
