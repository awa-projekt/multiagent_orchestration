"""Run the core benchmark: eight scenarios, several runs per case, one results table.

    python examples/benchmarks/core/run.py --offline                 # scripted models, no network
    python examples/benchmarks/core/run.py --live --runs 3           # the model of the settings
    python examples/benchmarks/core/run.py --live --only 2 4 --max-calls 120

Every run is recorded with the scorers it passed, its model calls, tokens and latency. A running
ledger counts the model calls (live runs keep it in `results/ledger.jsonl`, so the cap counts
across invocations) and the benchmark stops at `--max-calls`. Results go to
`results/core-<mode>-<time>.json` and `.md`; the markdown table is what the README shows.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent.parent)]  # this folder and examples/ (for _support)

import harness  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true", help="scripted models and a fake network")
    mode.add_argument(
        "--live", action="store_true", help="the model of the settings (AWA_DEFAULT_MODEL)"
    )
    parser.add_argument("--runs", type=int, default=3, help="runs per case (default 3)")
    parser.add_argument("--only", nargs="+", metavar="N", help="scenario numbers or folder names")
    parser.add_argument("--cases", nargs="+", metavar="ID", help="only these case ids")
    parser.add_argument(
        "--max-calls", type=int, default=400, help="stop after this many model calls (default 400)"
    )
    parser.add_argument(
        "--max-run-calls",
        type=int,
        help="model calls one run may use (default 60, 150 for the build)",
    )
    parser.add_argument(
        "--out", type=Path, default=HERE / "results", help="folder for results and ledger"
    )
    parser.add_argument(
        "--fresh-ledger", action="store_true", help="live: ignore the calls of earlier invocations"
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    scenarios = harness.select(args.only)
    setup = harness.Setup(offline=args.offline)
    ledger_file = None if args.offline else args.out / "ledger.jsonl"
    if ledger_file is not None and args.fresh_ledger:
        ledger_file.unlink(missing_ok=True)
    ledger = harness.Ledger(args.max_calls, ledger_file)
    provider = "scripted"
    if args.live:
        from _support import require_model_credentials
        from awa.core.settings import get_settings

        require_model_credentials()
        provider = get_settings().default_model.partition(":")[0]
    stopped = False
    rows = []
    for scenario in scenarios:
        print(f"[{scenario.number}] {scenario.title}", file=sys.stderr)

        def progress(record: harness.RunRecord) -> None:
            state = "pass" if record.passed else "FAIL"
            print(
                f"    {record.case} run {record.run}: {state} ({record.calls} calls, "
                f"{record.latency_ms / 1000:.1f} s; ledger {ledger.total}/{ledger.max_calls})",
                file=sys.stderr,
            )

        records, capped = await harness.run_scenario(
            scenario,
            args.runs,
            setup,
            ledger,
            cases=args.cases,
            max_run_calls=args.max_run_calls,
            on_record=progress,
        )
        rows.append(harness.scenario_row(scenario, records))
        if capped:
            stopped = True
            print(f"stopped: the ledger reached --max-calls ({ledger.max_calls})", file=sys.stderr)
            break
    stamp = time.strftime("%Y%m%d-%H%M%S")
    mode = "offline" if args.offline else "live"
    args.out.mkdir(parents=True, exist_ok=True)
    table = harness.markdown_table(rows, args.runs)
    header = {
        "date": time.strftime("%Y-%m-%d"),
        "mode": mode,
        "provider": provider,
        "runs": args.runs,
        "max_calls": args.max_calls,
        "calls": ledger.total,
        "stopped_at_max_calls": stopped,
    }
    (args.out / f"core-{mode}-{stamp}.json").write_text(
        harness.results_json(rows, **header), "utf-8"
    )
    (args.out / f"core-{mode}-{stamp}.md").write_text(table + "\n", "utf-8")
    print(table)
    print(f"\n{ledger.total} model calls; results in {args.out}")
    failed = any(r["passed_runs"] < r["runs"] for r in rows)
    return 2 if stopped else (1 if args.offline and failed else 0)


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
