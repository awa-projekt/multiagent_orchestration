"""Tools of the supplier-due-diligence use case: the risk rules and the decision record.

The web search and page reading are builtin tools (`builtin:web_search`, `builtin:fetch_url`),
the supplier list and the checklist are read through the `workspace` connector; this file holds
the real logic: `score_risk` turns cited findings into a risk level by fixed rules (a model
never decides the risk), and `record_decision` stores what `score_risk` saved. In production the
record goes to your supplier master data (see the README).
"""

from __future__ import annotations

import csv
import json
import re
import threading
from pathlib import Path
from typing import Any

from typing_extensions import TypedDict

from awa.runtime import case_dir

HERE = Path(__file__).resolve().parent


def _data_dir() -> Path:
    """The sample data: the eval case's copy inside `awa eval`, else the folder here."""

    return case_dir(HERE) / "data"


def _outbox_dir() -> Path:
    return _data_dir() / "outbox"


_LEDGER_LOCK = threading.Lock()
_ID = re.compile(r"[A-Za-z0-9_-]{1,40}")
CHECKS = ("sanctions", "adverse_news", "ownership")
RESULTS = ("clear", "concern", "hit", "no_data")
_SEVERITY = {"clear": 0, "no_data": 1, "concern": 2, "hit": 3}


class Finding(TypedDict):
    """One check of the due diligence, as the researcher reports it."""

    check: str
    result: str
    summary: str
    source_url: str | None


def _score_file(supplier_id: str) -> Path:
    return _outbox_dir() / "scores" / f"{supplier_id.strip().upper()}.json"


def score_risk(supplier_id: str, findings: list[Finding]) -> str:
    """Score a supplier's due diligence by the checklist rules and save the result.

    `findings` has one entry per check (sanctions, adverse_news, ownership) with `check`,
    `result` (clear, concern, hit or no_data), a one-sentence `summary` and the `source_url`
    the finding rests on. A finding without a source counts as no_data; of several findings for
    one check the worst counts. Returns the risk level (low, medium, high, critical), the
    recommendation, the reasons, the checks that are missing or uncited and the sources.

    In production: keep the rules, they are your policy.
    """

    if not _ID.fullmatch(supplier_id):
        return f"Refused: {supplier_id!r} is not a valid supplier id."
    by_check: dict[str, dict[str, Any]] = {}
    uncited = []
    for given in findings:
        finding: dict[str, Any] = dict(given)
        check, result = str(finding.get("check")), str(finding.get("result"))
        if check not in CHECKS or result not in RESULTS:
            return f"Refused: unknown check or result in {finding!r}; checks are {CHECKS}."
        url = str(finding.get("source_url") or "").strip()
        if result != "no_data" and not url.startswith(("http://", "https://")):
            uncited.append(check)
            finding = {**finding, "result": "no_data", "source_url": ""}
        kept = by_check.get(check)
        if kept is None or _SEVERITY[finding["result"]] > _SEVERITY[kept["result"]]:
            by_check[check] = finding
    open_checks = [c for c in CHECKS if by_check.get(c, {}).get("result", "no_data") == "no_data"]
    reasons = [f"{c}: {f['result']}, {f.get('summary', '')}" for c, f in by_check.items()]
    if by_check.get("sanctions", {}).get("result") == "hit":
        level, advice = "critical", "do_not_onboard"
    elif any(by_check.get(c, {}).get("result") == "concern" for c in ("adverse_news", "ownership")):
        level, advice = "high", "manual_review"
    elif open_checks:
        level = "medium"
        advice = "manual_review" if "sanctions" in open_checks else "onboard_with_conditions"
    else:
        level, advice = "low", "onboard"
    sources = sorted({str(f["source_url"]) for f in by_check.values() if f.get("source_url")})
    score = {
        "supplier_id": supplier_id.strip().upper(),
        "risk_level": level,
        "recommendation": advice,
        "reasons": reasons,
        "open_checks": open_checks,
        "uncited_checks": uncited,
        "sources": sources,
    }
    _score_file(supplier_id).parent.mkdir(parents=True, exist_ok=True)
    _score_file(supplier_id).write_text(json.dumps(score, indent=1), encoding="utf-8")
    return json.dumps(score)


def record_decision(supplier_id: str, summary: str) -> str:
    """Record the due-diligence decision for a supplier in the supplier master data: the risk
    level, recommendation and sources come from the saved `score_risk` result, `summary` is the
    one-paragraph rationale. A compliance officer approves every record.

    In production: write the decision to your supplier master data.
    """

    if not _ID.fullmatch(supplier_id):
        return f"Refused: {supplier_id!r} is not a valid supplier id."
    path = _score_file(supplier_id)
    if not path.is_file():
        return f"Refused: run score_risk for {supplier_id!r} first."
    score = json.loads(path.read_text(encoding="utf-8"))
    ledger = _outbox_dir() / "supplier_decisions.csv"
    with _LEDGER_LOCK:  # cases run in parallel threads: check, header and row must not race
        new_file = not ledger.is_file()
        if not new_file:
            with ledger.open(encoding="utf-8", newline="") as handle:
                if any(r["supplier_id"] == score["supplier_id"] for r in csv.DictReader(handle)):
                    return f"Already recorded: {score['supplier_id']}."
        with ledger.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            if new_file:
                writer.writerow(
                    ["supplier_id", "risk_level", "recommendation", "summary", "sources"]
                )
            row = [score["supplier_id"], score["risk_level"], score["recommendation"], summary]
            writer.writerow([*row, " ".join(score["sources"])])
    return (
        f"Recorded {score['supplier_id']}: {score['risk_level']} risk, {score['recommendation']}."
    )


__all__ = ["record_decision", "score_risk"]
