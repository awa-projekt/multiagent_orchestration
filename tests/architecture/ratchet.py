"""The ratchet: a count may only go down, and a lower count must be recorded in the same commit."""

from __future__ import annotations

import json
from typing import Any

from architecture.measures import MEASURES
from architecture.scan import HERE

BASELINE = HERE / "baseline.json"
REPORT: list[tuple[str, int, int]] = []
"""(key, found, baseline) of the report-only keys of this run; conftest.py prints it."""

SHOW = 25


def load() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(BASELINE.read_text(encoding="utf-8"))
    return data


def save(counts: dict[str, int], report_only: list[str]) -> None:
    text = json.dumps(
        {"counts": dict(sorted(counts.items())), "report_only": sorted(report_only)}, indent=2
    )
    BASELINE.write_text(text + "\n", encoding="utf-8", newline="\n")


def check(key: str) -> None:
    """Fail when `key` found more than the baseline (a regression) or fewer (record the progress)."""

    measure = MEASURES[key]
    baseline = load()
    found = measure.find()
    allowed = baseline["counts"][key]
    if key in baseline["report_only"]:
        REPORT.append((key, len(found), allowed))
        return
    listing = "\n  ".join(found[:SHOW]) + (
        f"\n  ... {len(found) - SHOW} more" if len(found) > SHOW else ""
    )
    if len(found) > allowed:
        raise AssertionError(
            f"{key}: {len(found)} found, the baseline allows {allowed} (rule {measure.rule}). "
            f"How to fix: {measure.fix}.\n  {listing}\n"
            "A deliberate, decided increase needs `python scripts/arch_baseline.py --allow-increase` "
            "and a reason in the commit message."
        )
    if len(found) < allowed:
        raise AssertionError(
            f"{key}: {len(found)} found, below the baseline {allowed}. Progress: lock it in with "
            "`python scripts/arch_baseline.py` and commit baseline.json with the change."
        )
