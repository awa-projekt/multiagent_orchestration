"""`awa eval`: evaluate a blueprint on a dataset, for local checks and CI gates (exit 1 below
the gates, every case in its own sandbox folder)."""

from __future__ import annotations

import argparse
import contextlib
import difflib
import os
import sys
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awa.cli._common import (
    EXIT_ERROR,
    EXIT_OK,
    Commands,
    add_blueprint_argument,
    add_command,
    add_deps_options,
    add_limit_options,
    add_tools_options,
    deps_from_args,
    limits_from_args,
)

if TYPE_CHECKING:
    from awa.blueprints import Blueprint
    from awa.contracts.protocols import AgentLike
    from awa.evals.dataset import Dataset
    from awa.evals.report import Comparison, EvalReport


def add_eval(commands: Commands) -> None:
    command = add_command(commands, "eval")
    add_blueprint_argument(command)
    command.add_argument("dataset", help="dataset file (.yaml, .yml or .json)")
    add_tools_options(command)
    command.add_argument(
        "--agent",
        metavar="NAME",
        help="evaluate this one agent of the blueprint instead of its entrypoint "
        "(for example a judge, with verdict:<decision> scorers)",
    )
    command.add_argument(
        "--scorer",
        action="append",
        default=[],
        metavar="SPEC",
        help="scorer applied to every case, e.g. contains, tool:lookup_order, max_tokens:20000 "
        "(repeatable)",
    )
    command.add_argument(
        "--tag", action="append", default=[], help="only cases with this tag (repeatable)"
    )
    command.add_argument(
        "--case",
        action="append",
        default=[],
        metavar="ID",
        help="only the case with this id (repeatable; combines with --tag)",
    )
    command.add_argument(
        "--concurrency", type=int, default=4, metavar="N", help="runs in flight (default 4)"
    )
    command.add_argument(
        "--repetitions", type=int, default=1, metavar="N", help="runs per case (default 1)"
    )
    command.add_argument(
        "--timeout", type=float, metavar="SECONDS", help="seconds per run before it is cancelled"
    )
    add_limit_options(command, "run", widen=True)
    command.add_argument(
        "--sandbox",
        metavar="DIR",
        help="folder whose subfolders every case gets a fresh copy of (default: data/, out/ "
        "and the workspace folders next to the blueprint)",
    )
    command.add_argument(
        "--no-sandbox",
        action="store_true",
        help="run every case on the files in place (cases may see each other's writes)",
    )
    command.add_argument(
        "--judge-model", metavar="MODEL", help="model for judge:<criteria> scorers"
    )
    command.add_argument("--fail-fast", action="store_true", help="stop at the first failure")
    command.add_argument("--json", metavar="FILE", help="write the report as JSON to FILE")
    command.add_argument(
        "--markdown", metavar="FILE", help="also write the report as Markdown to FILE"
    )
    command.add_argument(
        "--format",
        choices=("text", "markdown"),
        default="text",
        help="what is printed: text, tables aligned for the terminal (default), or Markdown",
    )
    command.add_argument("--baseline", metavar="FILE", help="compare with this JSON report")
    command.add_argument(
        "--min-pass-rate",
        type=float,
        default=1.0,
        metavar="RATE",
        help="fail (exit 1) below this pass rate (default 1.0)",
    )
    command.add_argument(
        "--min-pass-hat-k",
        type=float,
        metavar="R",
        help="fail (exit 1) when fewer than this share of cases passed every repetition "
        "(pass^k; needs --repetitions 2 or more)",
    )
    command.add_argument(
        "--cost-budget",
        type=float,
        metavar="AMOUNT",
        help="fail (exit 1) when the whole evaluation costs more than this (needs prices)",
    )
    command.add_argument(
        "--prices",
        metavar="FILE",
        help="YAML file with your model prices (per million tokens) to report the cost",
    )
    command.add_argument(
        "--tolerance",
        type=float,
        default=0.1,
        metavar="DROP",
        help="allowed drop of a mean score against the baseline (default 0.1)",
    )
    command.add_argument(
        "--replay",
        action="store_true",
        help="record model responses and answer unchanged calls from them on the next run "
        "(stand-in tools only; see --replay-store)",
    )
    command.add_argument(
        "--replay-store",
        metavar="FILE",
        help="SQLite file of recorded responses (default .awa/replay.db next to the blueprint)",
    )
    command.add_argument(
        "--search-fixture",
        metavar="FILE",
        help="answer web searches from this JSON file of recorded results (live without it)",
    )
    command.add_argument(
        "--record-search",
        action="store_true",
        help="with --search-fixture: search live for queries the file lacks and add them",
    )
    add_deps_options(command)
    command.set_defaults(handler=_eval)


def _eval(args: argparse.Namespace) -> int:
    from awa._internal import aio as asyncio_compat
    from awa.blueprints import Blueprint
    from awa.cli._preflight import preflight
    from awa.contracts.usage import PriceTable
    from awa.evals.dataset import Dataset
    from awa.evals.report import EvalReport
    from awa.evals.runner import evaluate
    from awa.runtime.case import use_case_dir

    preflight(args.blueprint)
    _check_options(args)
    given = PriceTable.from_yaml(args.prices) if args.prices else None
    dataset = _select_cases(Dataset.load(Path(args.dataset)), args)
    baseline = EvalReport.from_json(Path(args.baseline)) if args.baseline else None

    blueprint = Blueprint.from_yaml(Path(args.blueprint))
    prices = given or blueprint.price_table()
    if args.cost_budget is not None and prices is None:
        from awa.errors import EvalError

        raise EvalError(
            "--cost-budget needs prices: none were given (prices: in the blueprint or "
            "--prices FILE); awa ships no prices"
        )
    sandbox, folders = _sandbox(args, blueprint)
    build = _system_factory(args, blueprint, sandbox)
    deps = deps_from_args(args)
    limits = limits_from_args(args, blueprint, prices=given, widen=True)
    if limits is not None and given is not None:  # the prices you pass price the limit too
        limits = limits.model_copy(update={"prices": given})

    async def build_then_evaluate() -> EvalReport:
        with use_case_dir(sandbox):  # relative workspace roots as the cases see them
            await build()  # build once up front, so blueprint errors are reported before any run
        return await evaluate(
            build,
            dataset,
            list(args.scorer),
            concurrency=args.concurrency,
            repetitions=args.repetitions,
            usage_limits=limits,
            fail_fast=args.fail_fast,
            timeout=args.timeout,
            deps=deps,
            judge_model=args.judge_model,
            prices=prices,
            name=blueprint.name,
            sandbox=sandbox,
            sandbox_folders=folders,
        )

    with _replay_scope(args, blueprint), _search_scope(args):
        report = asyncio_compat.run(build_then_evaluate())
    _fail_on_missing_credentials(report)
    _send_scores(report)
    markdown = report.to_markdown()
    comparison = report.compare(baseline, tolerance=args.tolerance) if baseline else None
    if comparison is not None:
        markdown += "\n" + comparison.to_markdown()
    if args.format == "markdown":
        print(markdown, end="")
    else:
        from awa.cli._terminal_text import markdown_as_text

        print(markdown_as_text(markdown), end="")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        report.save(args.json)
        print(f"wrote {args.json}", file=sys.stderr)
    if args.markdown:
        Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
        Path(args.markdown).write_text(markdown, encoding="utf-8")
    problems = _gate_problems(report, comparison, args)
    if problems:
        print(f"evaluation failed: {'; '.join(problems)}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def _check_options(args: argparse.Namespace) -> None:
    from awa.errors import EvalError

    for option in ("concurrency", "repetitions"):
        if getattr(args, option) < 1:
            raise EvalError(f"--{option} must be at least 1")
    if not 0.0 <= args.min_pass_rate <= 1.0:
        raise EvalError("--min-pass-rate must be between 0 and 1")
    if args.min_pass_hat_k is not None:
        if not 0.0 <= args.min_pass_hat_k <= 1.0:
            raise EvalError("--min-pass-hat-k must be between 0 and 1")
        if args.repetitions < 2:
            raise EvalError("--min-pass-hat-k needs --repetitions 2 or more")
    if args.cost_budget is not None and not args.cost_budget > 0:
        raise EvalError("--cost-budget must be greater than 0")
    for option in ("json", "markdown"):
        if getattr(args, option):
            _check_output(Path(getattr(args, option)), f"--{option}")


def _check_output(target: Path, option: str) -> None:
    """Refuse an output path that cannot be written before the first model call."""

    from awa.errors import EvalError

    if target.is_dir():
        raise EvalError(f"{option} {target} is a folder; name a file")
    folder = target.parent
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    if not folder.is_dir() or not os.access(folder, os.W_OK):
        raise EvalError(f"{option} {target} cannot be written: {folder} is not a writable folder")


def _select_cases(dataset: Dataset, args: argparse.Namespace) -> Dataset:
    """The cases `--tag` and `--case` select; an id that is not in the dataset is an error."""

    from awa.errors import EvalError

    known = [case.id for case in dataset.cases]
    for case_id in args.case:
        if case_id not in known:
            close = difflib.get_close_matches(case_id, known, n=3)
            hint = f"; did you mean {', '.join(close)}?" if close else ""
            raise EvalError(
                f"No case {case_id!r} in dataset {dataset.name!r}{hint} (cases: {', '.join(known)})"
            )
    selected = dataset.filter(*args.tag, ids=args.case or None)
    if not selected.cases:
        raise EvalError(f"Dataset {dataset.name!r} has no cases to run")
    return selected


def _system_factory(
    args: argparse.Namespace, blueprint: Blueprint, sandbox: Path | None
) -> Callable[[], Awaitable[AgentLike]]:
    """Build a fresh system from the blueprint for every run (`--agent`: that one agent); with a
    sandbox each case imports its own copy of the tools files."""

    from awa._internal.upstream.checkpoint import StateInMemorySaver
    from awa.blueprints._resolve import file_modules_scope, load_tools, skill_registry, tools_files

    tool_files = tools_files(args.tools, args.blueprint)
    tools = load_tools(tool_files)
    skills = skill_registry(args.skills, args.blueprint)
    needs_checkpointer = bool(blueprint.needs_checkpointer())

    async def build_with(tools: list[Any]) -> AgentLike:
        options: dict[str, Any] = {
            "tools": tools,
            "skill_registry": skills,
            "checkpointer": StateInMemorySaver() if needs_checkpointer else None,
            "allow_imports": True,
        }
        if args.agent:
            return blueprint.build_agent(args.agent, **options)
        # `abuild` also loads the tools of the blueprint's MCP servers.
        return await blueprint.abuild(**options)

    async def build() -> AgentLike:
        if sandbox is None:
            return await build_with(tools)
        with file_modules_scope():
            return await build_with(load_tools(tool_files))

    return build


def _gate_problems(
    report: EvalReport, comparison: Comparison | None, args: argparse.Namespace
) -> list[str]:
    """Why the evaluation failed: the report's gate and, against a baseline, what got worse."""

    problems = list(
        report.gate(
            args.min_pass_rate, min_pass_hat_k=args.min_pass_hat_k, max_cost=args.cost_budget
        ).reasons
    )
    if comparison is not None and comparison.has_regressions:
        found = [f"{len(comparison.regressions)} regression(s)"] if comparison.regressions else []
        found += ["pass^k dropped"] if comparison.pass_hat_k_dropped else []
        problems.append(f"{', '.join(found)} against the baseline")
    return problems


def _sandbox(
    args: argparse.Namespace, blueprint: Blueprint
) -> tuple[Path | None, tuple[str, ...] | None]:
    """The folder every case gets a copy of and which of its subfolders (`None`: all):
    `--sandbox DIR` and all of DIR's, else the blueprint's folder with `data/`, `out/` and
    the first folder of every relative connector or toolkit `root`."""

    from awa.errors import EvalError

    if args.no_sandbox:
        if args.sandbox:
            raise EvalError("--sandbox and --no-sandbox exclude each other")
        return None, None
    if args.sandbox:
        folder = Path(args.sandbox)
        if not folder.is_dir():
            raise EvalError(f"--sandbox {args.sandbox!r} is not a folder")
        return folder.resolve(), None
    names = {"data", "out"}
    for options in (*blueprint.connectors.values(), *blueprint.toolkits.values()):
        root = options.get("root") if isinstance(options, dict) else None
        if isinstance(root, str) and root and not Path(root).is_absolute():
            names.add(Path(root).parts[0])
    return Path(args.blueprint).resolve().parent, tuple(sorted(names - {".", ".."}))


def _replay_problems(blueprint: Blueprint) -> list[str]:
    """Why replaying `blueprint` could reach an external system: connector, MCP and acting builtin
    tools (a replayed model decision would make the same call again, for real)."""

    from awa.blueprints._resolve import builtin_risk

    problems: list[str] = []
    for agent in blueprint.agents:
        for reference in agent.tools:
            if reference.startswith(("connector:", "mcp:")):
                problems.append(f"{agent.name}: {reference} reaches an external system")
            elif reference.startswith("builtin:"):
                risk = builtin_risk(reference, blueprint.toolkits)
                if risk is not None and risk.external_action:
                    problems.append(f"{agent.name}: {reference} acts outside awa")
    return problems


@contextlib.contextmanager
def _replay_scope(args: argparse.Namespace, blueprint: Blueprint) -> Iterator[None]:
    """Inside `--replay`: a replay scope on the SQLite store; otherwise nothing."""

    if not args.replay:
        yield
        return
    from awa.errors import EvalError
    from awa.models.replay import replaying
    from awa.persistence.cache import SqliteCacheStore
    from awa.runtime.cache import Cache, CacheSettings

    problems = _replay_problems(blueprint)
    if problems:
        raise EvalError(
            "--replay runs only on stand-in tools (your tools.py or read-only builtins); "
            + "; ".join(problems)
        )
    path = (
        Path(args.replay_store)
        if args.replay_store
        else Path(args.blueprint).resolve().parent / ".awa" / "replay.db"
    )
    cache = Cache(CacheSettings(), SqliteCacheStore(path), project=blueprint.name)
    with replaying(cache) as scope:
        yield
    counts = scope.counts
    print(
        f"replay: {counts['hits']} model call(s) replayed, {counts['misses']} recorded ({path})",
        file=sys.stderr,
    )


@contextlib.contextmanager
def _search_scope(args: argparse.Namespace) -> Iterator[None]:
    """Inside `--search-fixture`: web searches answered from the file; otherwise nothing."""

    if not args.search_fixture:
        if args.record_search:
            from awa.errors import EvalError

            raise EvalError("--record-search needs --search-fixture FILE")
        yield
        return
    from awa.toolkits import search_fixture

    with search_fixture(args.search_fixture, record=args.record_search) as fixture:
        yield
    print(
        f"search fixture: {fixture.hits} search(es) answered from {args.search_fixture}, "
        f"{fixture.misses} not recorded" + (" (searched live and added)" if fixture.record else ""),
        file=sys.stderr,
    )


def _fail_on_missing_credentials(report: EvalReport) -> None:
    """Every case failing for the same missing API key is one error, not a table of them."""

    from awa.errors import ModelProviderError

    ran = [case for case in report.cases if case.status != "skipped"]
    prefix = "ModelProviderError: no API key for"
    if ran and all(
        case.status == "error" and (case.error or "").startswith(prefix) for case in ran
    ):
        raise ModelProviderError((ran[0].error or "").partition(": ")[2], kind="authentication")


def _send_scores(report: EvalReport) -> None:
    """With Langfuse tracing on, attach every case result to its trace as scores."""

    from awa.cli._tracing import active_connection

    connection = active_connection()
    if connection is not None:
        connection.flush()
        sent = connection.score_eval(report)
        print(f"sent {sent} eval score(s) to Langfuse", file=sys.stderr)
