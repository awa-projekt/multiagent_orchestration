"""Regenerate or check the architecture ratchet `tests/architecture/baseline.json`.

    python scripts/arch_baseline.py                       # record improvements (counts that fell)
    python scripts/arch_baseline.py --check               # exit 1 if any count differs
    python scripts/arch_baseline.py --allow-increase      # record a deliberate increase
    python scripts/arch_baseline.py --enforce GT1.domain_to_adapter   # a report-only key becomes enforced

The counts come from the same measurements as the guard tests (`tests/architecture/measures.py`).
A count that rose is refused unless `--allow-increase` is given; a key that is new is recorded as
found. The `report_only` keys (the `GT1` structure counts, until the role table is enforced) are
kept as they are.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from architecture import ratchet  # noqa: E402
from architecture.measures import MEASURES  # noqa: E402

REPORT_ONLY_AT_START = [
    "GT1.adapter_inherits_adapter",
    "GT1.adapter_to_adapter",
    "GT1.adapters_built_in_domain",
    "GT1.domain_to_adapter",
    "GT1.domain_to_mixed",
    "GT1.mixed_modules",
    "GT1.ports_outside_ports_modules",
]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check", action="store_true", help="only compare; exit 1 on any difference"
    )
    parser.add_argument("--allow-increase", action="store_true", help="record counts that rose")
    parser.add_argument(
        "--enforce", nargs="*", default=[], metavar="KEY", help="stop reporting only"
    )
    args = parser.parse_args()

    recorded = (
        ratchet.load()
        if ratchet.BASELINE.exists()
        else {"counts": {}, "report_only": REPORT_ONLY_AT_START}
    )
    old: dict[str, int] = recorded["counts"]
    now = {key: len(measure.find()) for key, measure in MEASURES.items()}
    rose = sorted(key for key in now if key in old and now[key] > old[key])
    differs = sorted(key for key in now if now[key] != old.get(key)) + sorted(set(old) - set(now))

    for key in sorted(now | old.keys()):
        marker = (
            "" if now.get(key) == old.get(key) else "  <- rose" if key in rose else "  <- changed"
        )
        print(f"{key:44} baseline {old.get(key, '-')!s:>4}  now {now.get(key, '-')!s:>4}{marker}")

    if args.check:
        return 1 if differs else 0
    if rose and not args.allow_increase:
        print(
            f"\nRefused: {', '.join(rose)} rose. Fix the code, or record a decided increase with --allow-increase."
        )
        return 1
    report_only = [key for key in recorded["report_only"] if key in now and key not in args.enforce]
    ratchet.save(now, report_only)
    print(f"\nWrote {ratchet.BASELINE.relative_to(ratchet.BASELINE.parents[2])}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
