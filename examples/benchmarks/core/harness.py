"""Harness of the core benchmark: scenarios, the four kinds of driver, statistics and the ledger.

A scenario is a folder (`blueprint.yaml`, `cases.yaml`, optional `tools.py` and `data/`). Its
cases run one at a time and each run gives a `RunRecord`: which scorers passed, model calls,
tokens and latency. Scenarios 1-5 are evaluated with `awa.evals` (the cases' scorer specs, code
scorers from `scorers.py`, a model judge last); scenario 6 serves its blueprint in process and
plays a day of triggers and approvals; scenario 7 builds a project with `awa.blueprints`;
scenario 8 serves scenario 2's blueprint with `awa mcp serve` and calls it with a real MCP client.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import math
import os
import re
import shutil
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import mcp_client
import offline
from scorers import CHECKS

from awa.blueprints import Blueprint, build_project, load, load_tools
from awa.contracts import ModelResponseEvent
from awa.evals import Custom, Dataset, EvalCase, evaluate, isolated_run
from awa.models import ModelRegistry
from awa.persistence import StateInMemorySaver
from awa.runtime import use_global_subscriber
from awa.testing import FunctionChatModel

HERE = Path(__file__).resolve().parent
Kind = Literal["eval", "automation", "build", "mcp"]


@dataclass(frozen=True)
class Scenario:
    number: int
    slug: str
    title: str
    kind: Kind
    timeout: float = 300.0
    run_calls: int = 60

    @property
    def directory(self) -> Path:
        return HERE / self.slug

    @property
    def dataset(self) -> Dataset:
        return Dataset.from_yaml(self.directory / "cases.yaml")

    @property
    def blueprint(self) -> Blueprint:
        return Blueprint.from_yaml(self.directory / "blueprint.yaml")

    def tools_module(self) -> ModuleType | None:
        path = self.directory / "tools.py"
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(f"awa_bench_{self.slug}_tools", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


SCENARIOS = [
    Scenario(1, "01_market_brief", "Market brief from the web (team, peer consult)", "eval", 420),
    Scenario(
        2, "02_supplier_orders", "Supplier orders against a REST API (CSV, approvals)", "eval"
    ),
    Scenario(3, "03_graphql_lookup", "GraphQL lookup joined with JSON", "eval"),
    Scenario(4, "04_document_intake", "Document intake (PDF, Word, Excel, approvals)", "eval"),
    Scenario(5, "05_agent_network", "Agent network (task board, blackboard)", "eval", 600),
    Scenario(
        6,
        "06_automation",
        "Automation end to end (webhook, file drop, approval)",
        "automation",
        600,
    ),
    Scenario(
        7, "07_build_from_description", "Template to application (awa build)", "build", 900, 150
    ),
    Scenario(
        8, "08_mcp_client", "MCP client (awa mcp serve over stdio and HTTP, approvals)", "mcp"
    ),
]


def select(only: list[str] | None) -> list[Scenario]:
    """The scenarios named by number or folder prefix (all when `only` is empty)."""

    if not only:
        return list(SCENARIOS)
    chosen = [s for s in SCENARIOS if any(o in (str(s.number), s.slug, s.slug[:2]) for o in only)]
    unknown = [
        o for o in only if not any(o in (str(s.number), s.slug, s.slug[:2]) for s in SCENARIOS)
    ]
    if unknown:
        raise SystemExit(f"unknown scenario(s) {', '.join(unknown)}; use 1-8 or a folder name")
    return chosen


# ------------------------------------------------------------------------ records and ledger


@dataclass
class RunRecord:
    scenario: int
    case: str
    run: int
    scores: dict[str, bool] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    calls: int = 0
    tokens: int = 0
    latency_ms: int = 0
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.error is None and bool(self.scores) and all(self.scores.values())


class Ledger:
    """Counts model calls across runs (and across invocations, with a file) and enforces the cap."""

    def __init__(self, max_calls: int, path: Path | None = None) -> None:
        self.max_calls, self.path = max_calls, path
        self.total = 0
        if path is not None and path.is_file():
            self.total = sum(
                json.loads(line)["calls"] for line in path.read_text("utf-8").splitlines()
            )

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self.total)

    def add(self, record: RunRecord) -> None:
        self.total += record.calls
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "scenario": record.scenario,
                "case": record.case,
                "run": record.run,
                "calls": record.calls,
                "tokens": record.tokens,
                "total": self.total,
            }
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")


# ------------------------------------------------------------------------ models


@dataclass
class Setup:
    """Where a scenario run gets its model from: the settings (live) or a script (offline)."""

    offline: bool

    def models(self, scenario: Scenario, blueprint: Blueprint | None = None) -> tuple[Any, Any]:
        """`(model registry, judge model)`; both `None` means the model of the settings."""

        if not self.offline:
            return None, None
        if scenario.kind == "build":
            return offline.registry_of(
                FunctionChatModel(offline.build_responses)
            ), FunctionChatModel(offline.build_responses)
        script = offline.script_for(scenario.slug, blueprint or scenario.blueprint)
        return script.registry(), script.model()

    def network(self) -> contextlib.AbstractContextManager[None]:
        return offline.network() if self.offline else contextlib.nullcontext()


def _record(scenario: Scenario, case: str, run: int) -> RunRecord:
    return RunRecord(scenario.number, case, run)


def _fail(record: RunRecord, exc: BaseException) -> RunRecord:
    record.error = f"{type(exc).__name__}: {exc}"[:300]
    record.scores = dict.fromkeys(record.scores, False) or {"completed": False}
    return record


def _within(record: RunRecord, limit: int) -> RunRecord:
    """Fail a run that went above its cap of model calls (for drivers that cannot stop it there)."""

    if record.calls > limit and record.error is None:
        _fail(record, RuntimeError(f"{record.calls} model calls, above the cap of {limit}"))
    return record


# ------------------------------------------------------------------------ driver: evaluation


async def run_eval(
    scenario: Scenario, case: EvalCase, run: int, setup: Setup, limit: int
) -> RunRecord:
    """One run of a case of scenarios 1-5: build the system fresh and evaluate it."""

    record = _record(scenario, case.id, run)
    blueprint = scenario.blueprint
    module = scenario.tools_module()
    tools = [getattr(module, name) for name in module.__all__] if module else None
    registry, judge = setup.models(scenario, blueprint)

    def factory() -> Any:
        if module is not None and hasattr(module, "reset_sample_data"):
            module.reset_sample_data()
        return blueprint.build(
            tools=tools,
            model_registry=registry,
            checkpointer=StateInMemorySaver() if blueprint.approval_agents() else None,
        )

    checks = [Custom(CHECKS[n], name=n, threshold=0.99) for n in case.metadata.get("checks", ())]
    with setup.network():
        report = await evaluate(
            factory,
            Dataset(name=scenario.slug, cases=[case]),
            checks,
            concurrency=1,
            usage_limits=_limits(limit),
            timeout=scenario.timeout,
            judge_model=judge,
        )
    [result] = report.cases
    record.scores = {score.name: score.passed for score in result.scores}
    record.notes = {score.name: score.reason for score in result.scores if not score.passed}
    record.calls = result.usage.requests + result.judge_usage.requests
    record.tokens = result.usage.total_tokens + result.judge_usage.total_tokens
    record.latency_ms = result.duration_ms
    record.error = result.error
    if result.error:
        record.scores = record.scores or {"completed": False}
    if not record.passed:  # keep what a failed run answered, to read the failure
        record.extra = {"output": json.dumps(result.output, default=str)[:1500]}
    return record


def _limits(limit: int) -> Any:
    from awa import UsageLimits

    return UsageLimits(max_requests=max(1, limit))


# ------------------------------------------------------------------------ driver: automation

TOKEN = "bench-token"  # the webhook token of the benchmark (not a secret)
AUTOMATION_CHECKS = (
    "webhook_run_succeeded",
    "large_run_waits_for_approval",
    "file_drop_run_succeeded",
    "decision_resumes_after_restart",
    "ledger_matches",
    "schedule_run_succeeded",
)
BUILD_CHECKS = ("build_passed", "project_validates", "approval_in_design", "project_runs")


@contextlib.contextmanager
def _workdir(scenario: Scenario) -> Iterator[Path]:
    """A temporary copy of the scenario folder as working directory, with a clean environment."""

    work = Path(tempfile.mkdtemp(prefix=f"awa-bench-{scenario.number}-"))
    for name in ("blueprint.yaml", "tools.py"):
        shutil.copy(scenario.directory / name, work / name)
    saved = (os.getcwd(), list(sys.path), dict(os.environ))
    os.chdir(work)
    sys.path.insert(0, str(work))
    os.environ["BENCH_WEBHOOK_TOKEN"] = TOKEN
    os.environ.pop("AWA_POSTGRES_DSN", None)
    try:
        yield work
    finally:
        os.chdir(saved[0])
        sys.path[:] = saved[1]
        os.environ.clear()
        os.environ.update(saved[2])
        shutil.rmtree(work, ignore_errors=True)


def _settle(
    client: Any, find: Callable[[dict[str, Any]], bool], seconds: float = 180.0
) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for run in client.get("/v1/process-runs").json()["runs"]:
            if find(run):
                return run  # type: ignore[no-any-return]
        time.sleep(0.1)
    raise TimeoutError("the runs did not settle")


def _decide(run_id: str, approve: bool) -> int:
    """`awa runs decide` in the working directory (the local runs store); its exit code.

    Goes through `awa.cli.main`, which reads the configuration of the working directory
    again, so a decision never lands in the store of an earlier case's folder.
    """

    from awa.cli import main

    args = ["runs", "decide", run_id, "--approve" if approve else "--reject"]
    args += ["--actor", "ap-lead", "--note", "benchmark decision"]
    with redirect_stdout(io.StringIO()):
        return main(args)


def summary_names(answer: str, ledger: list[str]) -> bool:
    """Does the weekly summary give the ledger's invoice count and total (in any wording)?"""

    total = sum(float(line.rsplit(",", 1)[1]) for line in ledger)
    text = re.sub(r"(?<=\d)[,.](?=\d{3}(?!\d))", "", answer)  # 1,530.00 -> 1530.00
    count = re.search(rf"(?<![\d.,:-]){len(ledger)}(?![\d.:-])", text)
    amount = re.search(rf"(?<![\d.,]){total:.0f}(?:[.,]\d{{1,2}})?(?!\d)", text)
    return bool(count and amount)


def _weekly_summary(ledger: list[str]) -> tuple[bool, str]:
    """Fire the weekly schedule once with `awa trigger run`; does the answer give count and total?"""

    from awa.cli import main

    out = io.StringIO()
    argv = ["trigger", "run", "blueprint.yaml", "weekly-summary", "--tools", "tools.py"]
    with redirect_stdout(out):
        code = main([*argv, "--payload", json.dumps({"date": "2024-10-07"})])
    answer = out.getvalue().rpartition("attempts=")[2].partition("\n")[2].strip()
    return code == 0 and summary_names(answer, ledger), answer[-160:]


def _day(case: EvalCase, work: Path) -> dict[str, tuple[bool, str]]:
    """Play one day of the ledger desk in process; returns the checks with a note each."""

    from fastapi.testclient import TestClient

    from awa.server import build_blueprint_app

    meta = case.metadata
    hook, header = "/v1/automations/ledger-desk/webhooks/portal", {"X-Awa-Token": TOKEN}
    store = work / ".awa" / "awa.db"
    checks: dict[str, tuple[bool, str]] = {}

    def serve() -> Any:
        app = build_blueprint_app("blueprint.yaml", tools=["tools.py"], store_path=store)
        return TestClient(app)

    small, large = meta["webhook"]
    with serve() as client:
        small_run = client.post(hook, json=small, headers=header).json()["id"]
        large_run = client.post(hook, json=large, headers=header).json()["id"]
        checks["webhook_run_succeeded"] = _status(client, small_run, "succeeded")
        checks["large_run_waits_for_approval"] = _status(client, large_run, "waiting")
    decided = _decide(large_run, meta["decision"] == "approve")
    with serve() as client:  # restarted: the decided run resumes from its saved state
        resumed = _status(client, large_run, "succeeded")
        checks["decision_resumes_after_restart"] = (resumed[0] and decided == 0, resumed[1])
        (work / "inbox").mkdir(exist_ok=True)
        (work / "inbox" / meta["file"]["name"]).write_bytes(meta["file"]["text"].encode())
        file_run = _settle(client, lambda r: r["trigger"] == "inbox")["id"]
        checks["file_drop_run_succeeded"] = _status(client, file_run, "succeeded")
    booked = work / "ledger.csv"
    ledger = booked.read_text("utf-8").splitlines()[1:] if booked.exists() else []
    checks["ledger_matches"] = (ledger == case.expected["ledger"], f"ledger {ledger}")
    checks["schedule_run_succeeded"] = _weekly_summary(case.expected["ledger"])
    return checks


def _status(client: Any, run_id: str, status: str) -> tuple[bool, str]:
    try:
        run = _settle(client, lambda r: r["id"] == run_id and r["status"] == status)
    except TimeoutError:
        now = next(
            (r for r in client.get("/v1/process-runs").json()["runs"] if r["id"] == run_id), {}
        )
        return False, f"run {run_id} is {now.get('status')} ({now.get('error')})"
    return True, f"run {run['id']} {status}"


async def run_automation(
    scenario: Scenario, case: EvalCase, run: int, setup: Setup, limit: int
) -> RunRecord:
    """One run of a case of scenario 6: serve the blueprint in process and play the day."""

    record = _record(scenario, case.id, run)
    events: list[Any] = []
    registry = ModelRegistry.default()
    scripted, _ = setup.models(scenario)
    if scripted is not None:
        registry.register("default", scripted.get("default"))
    started = time.perf_counter()

    def play() -> dict[str, tuple[bool, str]]:
        with _workdir(scenario) as work, use_global_subscriber(events.append):
            return _day(case, work)

    try:
        checks = await asyncio.wait_for(asyncio.to_thread(play), scenario.timeout)
        record.scores = {name: ok for name, (ok, _) in checks.items()}
        record.notes = {name: note for name, (ok, note) in checks.items() if not ok}
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the run
        _fail(record, exc)
        record.scores = dict.fromkeys(AUTOMATION_CHECKS, False)
    finally:
        if scripted is not None:
            registry.register("default", registry.settings.default_model)
    responses = [e for e in events if isinstance(e, ModelResponseEvent)]
    record.calls = len(responses)
    record.tokens = sum(e.input_tokens + e.output_tokens for e in responses)
    record.latency_ms = int((time.perf_counter() - started) * 1000)
    return _within(record, limit)


# ------------------------------------------------------------------------ driver: build


async def _written_project(
    record: RunRecord, report: Any, out: Path, registry: Any, setup: Setup, limit: int
) -> None:
    """Check the project `build_project` wrote: it validates and answers a case of its own."""

    from awa.cli import main

    tools_file = out / "tools.py"
    tools = [str(tools_file)] if tools_file.is_file() else []
    with redirect_stdout(io.StringIO()):
        argv = ["validate", str(out / "blueprint.yaml"), *(["--tools", *tools] if tools else [])]
        record.scores["project_validates"] = main(argv) == 0
    record.scores["approval_in_design"] = bool(report.design.blueprint.approvals)
    try:
        system = load(
            out / "blueprint.yaml",
            tools=load_tools(tools) if tools else None,
            model_registry=registry,
            checkpointer=StateInMemorySaver(),
        )
    except Exception as exc:  # noqa: BLE001 - a project that does not load fails the check
        record.notes["project_runs"] = f"{type(exc).__name__}: {exc}"[:200]
        return
    with setup.network():
        outcome = await isolated_run(
            system, report.design.evals.cases[0].input, usage_limits=_limits(limit - record.calls)
        )
    record.calls += outcome.usage.requests
    record.tokens += outcome.usage.total_tokens
    record.scores["project_runs"] = outcome.error is None and outcome.result is not None
    if outcome.error is not None:
        record.notes["project_runs"] = f"{type(outcome.error).__name__}: {outcome.error}"[:200]


async def run_build(
    scenario: Scenario, case: EvalCase, run: int, setup: Setup, limit: int
) -> RunRecord:
    """One run of the case of scenario 7: `awa build` a project, validate and run it."""

    record = _record(scenario, case.id, run)
    registry, model = setup.models(scenario)
    started = time.perf_counter()
    record.scores = dict.fromkeys(BUILD_CHECKS, False)
    try:
        with tempfile.TemporaryDirectory(prefix="awa-bench-7-") as temp:
            out = Path(temp) / "project"
            report = await asyncio.wait_for(
                build_project(
                    case.input,
                    out_dir=out,
                    model=model,
                    environment=None,
                    max_rounds=int(case.metadata.get("max_rounds", 3)),
                    usage_limits=_limits(limit),
                    force=True,
                ),
                scenario.timeout,
            )
            record.calls, record.tokens = report.usage.requests, report.usage.total_tokens
            record.extra = {
                "rounds": report.rounds,
                "pass_rate": round(report.pass_rate, 3),
                "stopped": report.stopped,
            }
            record.scores["build_passed"] = report.passed
            await _written_project(record, report, out, registry, setup, limit)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the run
        _fail(record, exc)
    record.latency_ms = int((time.perf_counter() - started) * 1000)
    return record


# ------------------------------------------------------------------------ driver: MCP client


async def run_mcp(
    scenario: Scenario, case: EvalCase, run: int, setup: Setup, limit: int
) -> RunRecord:
    """One run of a case of scenario 8: serve scenario 2 with `awa mcp serve`, call it as a client."""

    record = _record(scenario, case.id, run)
    started = time.perf_counter()
    work = mcp_client.new_workdir()
    try:
        checks, record.calls, record.tokens = await asyncio.wait_for(
            mcp_client.play(case.metadata, case.input, case.expected, setup.offline, work),
            scenario.timeout,
        )
        record.scores = {name: ok for name, (ok, _) in checks.items()}
        record.notes = {name: note for name, (ok, note) in checks.items() if not ok}
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the run
        _fail(record, _unwrap(exc))
        record.scores = dict.fromkeys(mcp_client.CHECKS, False)
        errors = work / "server.err"
        if errors.is_file():
            record.extra = {"server_stderr": errors.read_text("utf-8", errors="replace")[-600:]}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    record.latency_ms = int((time.perf_counter() - started) * 1000)
    return _within(record, limit)


def _unwrap(exc: BaseException) -> Exception:
    """The first real error inside the exception groups the client transports raise."""

    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc if isinstance(exc, Exception) else RuntimeError(type(exc).__name__)


DRIVERS: dict[Kind, Callable[..., Any]] = {
    "eval": run_eval,
    "automation": run_automation,
    "build": run_build,
    "mcp": run_mcp,
}


async def run_scenario(
    scenario: Scenario,
    runs: int,
    setup: Setup,
    ledger: Ledger,
    *,
    cases: list[str] | None = None,
    max_run_calls: int | None = None,
    on_record: Callable[[RunRecord], None] | None = None,
) -> tuple[list[RunRecord], bool]:
    """Run every case `runs` times; returns the records and whether the call cap stopped it."""

    records: list[RunRecord] = []
    dataset = scenario.dataset
    for case in dataset.cases:
        if cases and case.id not in cases:
            continue
        for run in range(1, runs + 1):
            if ledger.remaining <= 0:
                return records, True
            limit = min(max_run_calls or scenario.run_calls, ledger.remaining)
            record = await DRIVERS[scenario.kind](scenario, case, run, setup, limit)
            ledger.add(record)
            records.append(record)
            if on_record:
                on_record(record)
    return records, False


# ------------------------------------------------------------------------ statistics and tables


def percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank percentile of `values` (0 for none)."""

    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)] if ordered else 0


def summarize(records: list[RunRecord]) -> dict[str, Any]:
    """Per case: runs, passed runs, pass^k, scorer pass counts, calls, tokens, latency p50/p95."""

    by_case: dict[str, list[RunRecord]] = defaultdict(list)
    for record in records:
        by_case[record.case].append(record)
    cases = []
    for case, runs in by_case.items():
        scorers: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for record in runs:
            for name, ok in record.scores.items():
                scorers[name][0] += ok
                scorers[name][1] += 1
        latency = [r.latency_ms for r in runs]
        cases.append(
            {
                "case": case,
                "runs": len(runs),
                "passed_runs": sum(r.passed for r in runs),
                "pass_pow_k": all(r.passed for r in runs),
                "scorers": {name: {"passed": a, "total": b} for name, (a, b) in scorers.items()},
                "calls": sum(r.calls for r in runs),
                "tokens": sum(r.tokens for r in runs),
                "latency_ms": {
                    "p50": int(statistics.median(latency)),
                    "p95": percentile(latency, 0.95),
                },
                "failures": sorted(
                    {f"{n}: {t}" for r in runs for n, t in r.notes.items()}
                    | {r.error for r in runs if r.error}
                ),  # type: ignore[arg-type]
                "extra": [r.extra for r in runs if r.extra],
            }
        )
    return {"cases": cases}


def scenario_row(scenario: Scenario, records: list[RunRecord]) -> dict[str, Any]:
    cases = summarize(records)["cases"]
    runs = sum(c["runs"] for c in cases)
    latency = [r.latency_ms for r in records]
    return {
        "scenario": scenario.number,
        "title": scenario.title,
        "cases": len(cases),
        "runs": runs,
        "passed_runs": sum(c["passed_runs"] for c in cases),
        "pass_pow_k_cases": sum(c["pass_pow_k"] for c in cases),
        "calls": sum(c["calls"] for c in cases),
        "tokens": sum(c["tokens"] for c in cases),
        "latency_ms": {
            "p50": int(statistics.median(latency)) if latency else 0,
            "p95": percentile(latency, 0.95),
        },
        "detail": cases,
    }


def markdown_table(rows: list[dict[str, Any]], runs: int) -> str:
    """The results as the table of the README: one row per scenario, one per case."""

    head = (
        f"| Scenario / case | Runs passed (of {runs} per case) | pass^{runs} | Scorers passed | Calls | Tokens "
        "| Latency p50 / p95 (s) |\n| --- | --- | --- | --- | --- | --- | --- |"
    )
    lines = [head]
    for row in rows:
        lines.append(
            f"| **{row['scenario']}. {row['title']}** | {row['passed_runs']}/{row['runs']} | "
            f"{row['pass_pow_k_cases']}/{row['cases']} cases | | {row['calls']} | {row['tokens']} | "
            f"{row['latency_ms']['p50'] / 1000:.1f} / {row['latency_ms']['p95'] / 1000:.1f} |"
        )
        for case in row["detail"]:
            scored = "; ".join(
                f"{n} {s['passed']}/{s['total']}" for n, s in case["scorers"].items()
            )
            lines.append(
                f"| {case['case']} | {case['passed_runs']}/{case['runs']} | "
                f"{'yes' if case['pass_pow_k'] else 'no'} | {scored} | {case['calls']} | {case['tokens']} | "
                f"{case['latency_ms']['p50'] / 1000:.1f} / {case['latency_ms']['p95'] / 1000:.1f} |"
            )
    return "\n".join(lines)


def results_json(rows: list[dict[str, Any]], **header: Any) -> str:
    return json.dumps({**header, "scenarios": rows}, indent=2, ensure_ascii=False)
