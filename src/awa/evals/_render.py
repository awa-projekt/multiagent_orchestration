"""Markdown rendering of evaluation reports and comparisons."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from statistics import fmean
from typing import TYPE_CHECKING

from awa._internal.util import preview
from awa.evals.scorers import Score

if TYPE_CHECKING:
    from awa.evals.report import CaseResult, Comparison, EvalReport


def comparison_markdown(comparison: Comparison) -> str:
    """The Markdown of a comparison (`Comparison.to_markdown`)."""

    lines = [
        f"## Comparison with {comparison.baseline}",
        "",
        f"Pass rate {pct(comparison.baseline_pass_rate)} -> {pct(comparison.current_pass_rate)} "
        f"({len(comparison.regressions)} regression(s), {len(comparison.improvements)} improvement(s))",
        "",
    ]
    if comparison.repetitions is not None:
        note = " (lower)" if comparison.pass_hat_k_dropped else ""
        lines[-1:] = [
            "",
            f"pass^{comparison.repetitions} {pct(comparison.baseline_pass_hat_k)} -> "
            f"{pct(comparison.current_pass_hat_k)}{note}",
            "",
        ]
    if comparison.scorers:
        lines += ["| scorer | baseline mean | mean | baseline pass rate | pass rate |"]
        lines += ["| --- | ---: | ---: | ---: | ---: |"]
        for item in comparison.scorers:
            lines.append(
                f"| {cell(item.name)} | {num(item.baseline_mean)} | {num(item.current_mean)}"
                f" | {pct(item.baseline_pass_rate)} | {pct(item.current_pass_rate)} |"
            )
        lines.append("")
    for title, changes in (
        ("Regressions", comparison.regressions),
        ("Improvements", comparison.improvements),
    ):
        if changes:
            lines.append(f"### {title}")
            lines += [
                f"- {cell(change.label)}: pass rate {pct(change.baseline_pass_rate)} -> "
                f"{pct(change.current_pass_rate)}, mean {change.baseline_mean:.2f} -> "
                f"{change.current_mean:.2f}"
                for change in changes
            ]
            lines.append("")
    if comparison.new_cases:
        lines += [f"New cases: {', '.join(comparison.new_cases)}", ""]
    if comparison.missing_cases:
        lines += [f"Missing cases: {', '.join(comparison.missing_cases)}", ""]
    return "\n".join(lines).rstrip() + "\n"


def caching_lines(report: EvalReport) -> list[str]:
    """The `## Caching` section: empty when no cache did any work."""

    done = report.caching()
    if not (done.model_calls or done.replayed_calls or done.tool_hits or done.tool_misses):
        return []
    rows = [
        ("model calls answered by the provider", str(done.model_calls)),
        ("model calls replayed (no cost)", str(done.replayed_calls)),
        (
            "model call p50 / p95",
            f"{ms(percentile(done.model_ms, 50))} / {ms(percentile(done.model_ms, 95))}",
        ),
        ("tool results reused / stored", f"{done.tool_hits} / {done.tool_misses}"),
    ]
    rows += [
        (
            f"prompt prefix of {agent}",
            f"`{hashes[0]}` (stable)"
            if len(hashes) == 1
            else f"{len(hashes)} different prefixes (changes miss the provider cache)",
        )
        for agent, hashes in sorted(done.prefixes.items())
    ]
    lines = ["## Caching", "", "| measure | value |", "| --- | ---: |"]
    lines += [f"| {cell(name)} | {value} |" for name, value in rows]
    return [*lines, ""]


def reliability_lines(report: EvalReport) -> list[str]:
    """The `## Reliability` section: empty when no run started."""

    runs = report.runs
    if not runs:
        return []
    k = report.repetitions
    rows = [("pass@1 (one run passes)", pct(report.pass_rate))]
    if report.pass_hat_k is None:
        why = "needs 2 or more repetitions" if k < 2 else "no case ran every repetition"
        rows.append(("pass^k (all runs of a case pass)", f"n/a ({why})"))
    else:
        rows += [
            (f"pass@{k} (at least one of {k} runs passes)", pct(report.pass_at_k)),
            (f"pass^{k} (all {k} runs of a case pass)", pct(report.pass_hat_k)),
        ]
    rows.append(("latency p50 / p95", f"{ms(report.latency_p50_ms)} / {ms(report.latency_p95_ms)}"))
    if report.cost is None:
        rows.append(("cost", "not computed (no prices given)"))
    else:
        rows.append(("cost", f"{report.cost:.4f} ({report.cost_per_run or 0.0:.4f} per run)"))
    rows.append(("cache hit rate", pct(report.cache_hit_rate)))
    lines = ["## Reliability", "", "| measure | value |", "| --- | ---: |"]
    lines += [f"| {name} | {value} |" for name, value in rows]
    wobbly = [case_id for case_id, rate in report.consistency().items() if 0.0 < rate < 1.0]
    if k > 1 and wobbly:
        lines += ["", "Passed only some of the time:"]
        for case_id in wobbly:
            ran = [r for r in runs if r.case_id == case_id]
            passed = sum(r.status == "passed" for r in ran)
            lines.append(f"- {cell(case_id)}: {passed} of {len(ran)} runs")
    return [*lines, ""]


def report_markdown(report: EvalReport, *, max_failures: int) -> str:
    """The Markdown of a report (`EvalReport.to_markdown`)."""

    runs = report.runs
    passed = sum(result.status == "passed" for result in runs)
    case_count = len({result.case_id for result in report.cases})
    lines = [
        f"# Evaluation: {report.name}",
        "",
        f"System `{report.system}` on dataset `{report.dataset}`: {case_count} case(s) x "
        f"{report.repetitions} repetition(s).",
        "",
        f"- Pass rate: **{pct(report.pass_rate)}** ({passed}/{len(runs)} runs)",
        f"- Errors: {report.errors}",
        f"- Usage: {report.usage.requests} model call(s), {report.usage.total_tokens} tokens "
        f"({report.usage.input_tokens} in, {report.usage.output_tokens} out), "
        f"{report.usage.tool_calls} tool call(s)",
        f"- Duration: {report.duration_ms / 1000:.1f} s",
    ]
    if report.judge_usage.requests:
        lines.append(
            f"- Judge usage (not included above): {report.judge_usage.requests} model call(s), "
            f"{report.judge_usage.total_tokens} tokens"
        )
    if report.stopped_early:
        skipped = sum(result.status == "skipped" for result in report.cases)
        lines.append(f"- Stopped early (fail_fast): {skipped} run(s) skipped")
    lines.append("")
    lines += reliability_lines(report)
    lines += caching_lines(report)
    if report.scorers:
        lines += [
            "## Scorers",
            "",
            "| scorer | mean | pass rate | passed | failed |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
        lines += [
            f"| {cell(s.name)} | {s.mean:.2f} | {pct(s.pass_rate)} | {s.passed} | {s.failed} |"
            for s in report.scorers
        ]
        lines.append("")
    lines += [
        "## Cases",
        "",
        "| case | passed | scores | ms | tokens |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for case_id, results in grouped(report.cases).items():
        started = [result for result in results if result.status != "skipped"]
        if not started:
            lines.append(f"| {cell(case_id)} | skipped | | | |")
            continue
        ok = sum(result.status == "passed" for result in started)
        status = f"{ok}/{len(started)}" + (
            " (error)" if any(r.status == "error" for r in started) else ""
        )
        lines.append(
            f"| {cell(case_id)} | {status} | {cell(score_cell(started))} | "
            f"{round(fmean(r.duration_ms for r in started))} | "
            f"{round(fmean(r.usage.total_tokens for r in started))} |"
        )
    failures = report.failures()
    if failures:
        lines += ["", "## Failures", ""]
        for result in failures[:max_failures]:
            where = f"{result.case_id} (repetition {result.repetition + 1})"
            if result.status == "error":
                lines.append(
                    f"- **{where}**: {preview(result.error or 'error', 200, flatten=True)}"
                )
                continue
            reasons = "; ".join(
                f"{score.name}: {preview(score.error or score.reason, 120, flatten=True)}"
                for score in result.scores
                if not score.passed
            )
            if result.decisions or result.unused_decisions:
                applied = ", ".join(result.decisions) or "none"
                reasons += f" (decisions applied: {applied}"
                if result.unused_decisions:
                    reasons += f"; {result.unused_decisions} never asked for"
                reasons += ")"
            lines.append(f"- **{where}**: {reasons}")
        if len(failures) > max_failures:
            lines.append(f"- ... and {len(failures) - max_failures} more")
    return "\n".join(lines).rstrip() + "\n"


def percentile(values: Sequence[int], percent: int) -> int | None:
    """The nearest-rank percentile: the value at rank ceil(percent / 100 * n) when sorted."""

    if not values:
        return None
    rank = -(-percent * len(values) // 100)
    return sorted(values)[max(rank, 1) - 1]


def scores_by_name(results: Iterable[CaseResult]) -> dict[str, list[Score]]:
    """Every score of `results`, grouped by scorer name in order of first appearance."""

    grouped: dict[str, list[Score]] = defaultdict(list)
    for result in results:
        for score in result.scores:
            grouped[score.name].append(score)
    return grouped


def grouped(results: Iterable[CaseResult]) -> dict[str, list[CaseResult]]:
    grouped: dict[str, list[CaseResult]] = {}
    for result in results:
        grouped.setdefault(result.case_id, []).append(result)
    return grouped


def score_cell(results: list[CaseResult]) -> str:
    parts = []
    for name, scores in scores_by_name(results).items():
        text = f"{name} {fmean(score.value for score in scores):.2f}"
        parts.append(text if all(score.passed for score in scores) else f"**{text}**")
    return ", ".join(parts) or "-"


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def ms(value: int | None) -> str:
    return "-" if value is None else f"{value} ms"


def num(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")
