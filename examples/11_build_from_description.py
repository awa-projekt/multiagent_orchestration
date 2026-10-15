"""Build a tested system from a process description.

`awa.blueprints.build_project` works acceptance-first: a test designer writes the acceptance
criteria, eval cases, sample data for every system the process uses and the exact rules it
states; the architect designs the agents against those cases; the system is evaluated on a local
workspace of the sample data and repaired from the failing cases until it passes (or the rounds
or usage limits run out). The project folder holds everything the build wrote, evaluated on
sample data only (the summary says whether the target pass rate was reached): blueprint.yaml,
tools.py (workspace tools and rules), data/ (the workspace), evals/cases.yaml, tests/,
CONNECT.md (how to connect each real system) and README.md.

The same from the command line:
    awa build "Incoming supplier invoices are checked ..." --out invoice_desk
    awa connect invoice_desk

Run:
    pip install -e ".[anthropic]"
    export ANTHROPIC_API_KEY=...
    python examples/11_build_from_description.py
    # No model key? Run its offline tests instead:
    #   python -m pytest tests/examples/test_11_build_from_description.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Literal

from langchain_core.language_models import BaseChatModel

from _support import require_model_credentials
from awa import UsageLimits
from awa.blueprints import BuildProgress, BuildReport, build_project
from awa.connectors import EnvironmentReport

PROCESS = """\
Incoming supplier invoices are checked against their purchase order in the ERP: the vendor
must be the same and the amount may differ by at most 1 %. A matching invoice up to
10,000 EUR is approved for payment; above 10,000 EUR a manager must approve it first.
An invoice that does not match is put on hold with the reason."""


def show_progress(step: BuildProgress) -> None:
    """Print one build step."""

    print(f"[{step.stage}] {step.message}", file=sys.stderr)


async def build_invoice_desk(
    out_dir: str | Path,
    *,
    model: str | BaseChatModel | None = None,
    environment: EnvironmentReport | Literal["auto"] | None = "auto",
) -> BuildReport:
    """Build the invoice desk into `out_dir`: at most 3 rounds and 150 model calls."""

    return await build_project(
        PROCESS,
        out_dir=out_dir,
        model=model,
        environment=environment,
        max_rounds=3,
        usage_limits=UsageLimits(max_requests=150),
        on_progress=show_progress,
    )


async def main() -> None:
    require_model_credentials()
    report = await build_invoice_desk("invoice_desk")
    print(report.summary())


if __name__ == "__main__":
    asyncio.run(main())
