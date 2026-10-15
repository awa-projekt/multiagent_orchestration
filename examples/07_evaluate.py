"""Evaluation: measure a multi-agent process before and after every change.

The support desk from `blueprints/support_triage.yaml` is evaluated on the
cases in `evals/support_triage_cases.yaml`. Each case states what the desk
must *do*, checked from the recorded run: which member the work is
delegated to, which tools are called, that refunds are handed off and never
paid out without a human approval, and (with a model judge, the one
non-deterministic scorer) whether the reply answers the customer. Every case
runs on a freshly built desk, several at a time.

The report is printed as Markdown and saved as JSON. Pass an earlier report
to compare with it: the script exits with code 1 when the pass rate is below
80% or any case got worse, which is exactly the gate a CI job needs before a
prompt, model or topology change ships.

The same from the command line:
    awa eval examples/blueprints/support_triage.yaml examples/evals/support_triage_cases.yaml \\
        --tools examples/blueprints/support_tools.py --scorer max_tokens:60000 \\
        --json report.json --baseline baseline.json --min-pass-rate 0.8

Run:
    pip install -e ".[anthropic]"
    export ANTHROPIC_API_KEY=...
    python examples/07_evaluate.py                 # writes support_triage_report.json
    python examples/07_evaluate.py baseline.json   # also compares with a baseline
    # No model key? Run its offline tests instead:
    #   python -m pytest tests/examples/test_07_evaluate.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from _support import require_model_credentials
from awa import AgentLike, UsageLimits, load
from awa.evals import Dataset, EvalReport, MaxTokens, evaluate
from awa.persistence import StateInMemorySaver
from blueprints.support_tools import issue_refund, lookup_invoice, lookup_order, track_shipment

EXAMPLES = Path(__file__).resolve().parent
BLUEPRINT_PATH = EXAMPLES / "blueprints" / "support_triage.yaml"
DATASET_PATH = EXAMPLES / "evals" / "support_triage_cases.yaml"
REPORT_PATH = Path("support_triage_report.json")
SUPPORT_TOOLS = [lookup_order, track_shipment, lookup_invoice, issue_refund]
MIN_PASS_RATE = 0.8


def build_support_desk() -> AgentLike:
    """Build a fresh support desk: its own task board and approval checkpointer per run."""

    return load(BLUEPRINT_PATH, tools=SUPPORT_TOOLS, checkpointer=StateInMemorySaver())


def load_cases() -> Dataset:
    """Load the eval cases."""

    return Dataset.from_yaml(DATASET_PATH)


async def run_evaluation(dataset: Dataset | None = None, *, concurrency: int = 4) -> EvalReport:
    """Evaluate the support desk on `dataset` (default: every case)."""

    return await evaluate(
        build_support_desk,
        dataset if dataset is not None else load_cases(),
        [MaxTokens(60_000)],  # plus each case's own scorers from the dataset file
        concurrency=concurrency,
        usage_limits=UsageLimits(max_requests=40),
        timeout=300,
    )


def gate(report: EvalReport, baseline: EvalReport | None = None) -> list[str]:
    """Return why `report` must not ship (empty when it may)."""

    problems: list[str] = []
    if not report.passed(MIN_PASS_RATE):
        problems.append(f"pass rate {report.pass_rate:.0%} is below {MIN_PASS_RATE:.0%}")
    if baseline is not None:
        comparison = report.compare(baseline)
        problems += [f"regression: {change.label}" for change in comparison.regressions]
    return problems


async def main() -> int:
    require_model_credentials()
    baseline = EvalReport.from_json(Path(sys.argv[1])) if len(sys.argv) > 1 else None
    report = await run_evaluation()
    print(report.to_markdown())
    if baseline is not None:
        print(report.compare(baseline).to_markdown())
    report.save(REPORT_PATH)
    print(f"Saved {REPORT_PATH}; pass it as the baseline of the next run.")
    problems = gate(report, baseline)
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
