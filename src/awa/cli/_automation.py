"""`awa worker`, `awa trigger` and `awa runs`: run and operate automations (local SQLite store
and checkpoints in `.awa/`, or PostgreSQL with `--dsn`)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from awa.cli._common import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_PAUSED,
    Commands,
    add_command,
    add_limit_options,
    add_tools_options,
    ask_decision,
    blueprint_argument,
    blueprint_path,
    interactive,
    json_argument,
    limits_from_args,
    preview,
    print_json,
    print_table,
)
from awa.core.config import env_value

if TYPE_CHECKING:
    from awa.automation.automation import Automation
    from awa.automation.models import ProcessRun
    from awa.automation.store import RunStore
    from awa.automation.worker import Worker

DSN_ENV = "AWA_POSTGRES_DSN"
"""Environment variable naming the PostgreSQL database of runs, queues and checkpoints."""

DEFAULT_STORE = ".awa/awa.db"
"""Local data file used without a DSN (next to the blueprint, or in the working directory)."""

CHECKPOINTS_FILE = "checkpoints.db"
"""Local checkpointer file, kept next to the local run store."""


_RUN_ID = "the run's id (see awa runs list)"


def add_worker(commands: Commands) -> None:
    worker = add_command(
        commands,
        "worker",
        description="Run the automations of a project until stopped (Ctrl+C), without a web "
        "server: fire its triggers (schedule, file drop, email, queue), execute queued "
        "process runs, retry, recover runs of crashed workers and send SLA reminders. Needs a "
        "model key (awa setup). With --once: exit code 1 when a run failed, 2 when runs wait.",
    )
    worker.add_argument("target", help="blueprint file or project folder")
    add_tools_options(worker)
    add_limit_options(worker, "process run")
    _add_store(worker)
    worker.add_argument(
        "--concurrency",
        type=int,
        default=None,
        metavar="N",
        help="most runs at once in this worker (default: the sum of the automations' "
        "`concurrency`; each automation's own limit still applies across all workers)",
    )
    worker.add_argument(
        "--lease",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="how long a run stays claimed by this worker without a sign of life (default 300)",
    )
    _add_trace_level(worker)
    worker.add_argument(
        "--drain-timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="seconds runs in flight get to finish at shutdown (default 30)",
    )
    worker.add_argument("--no-triggers", action="store_true", help="only execute queued runs")
    worker.add_argument(
        "--once",
        action="store_true",
        help="handle what is ready now (trigger events, queued runs), then exit",
    )
    worker.set_defaults(handler=_worker)


def add_trigger(commands: Commands) -> None:
    trigger = add_command(commands, "trigger")
    actions = trigger.add_subparsers(dest="trigger_command", required=True, metavar="action")
    listing = actions.add_parser("list", help="list the triggers of a blueprint")
    listing.add_argument(
        "blueprint", type=blueprint_argument, help="blueprint file or project folder"
    )
    listing.set_defaults(handler=_trigger_list)
    fire = actions.add_parser(
        "run", help="fire a trigger once with a payload and run the process locally"
    )
    fire.add_argument("blueprint", type=blueprint_argument, help="blueprint file or project folder")
    fire.add_argument("trigger", help="trigger name")
    fire.add_argument("--payload", default="{}", help="payload as JSON, or @file.json (default {})")
    fire.add_argument(
        "--dry-run",
        action="store_true",
        help="only show the input, thread id and dedup key (builds no agents, needs no settings)",
    )
    fire.add_argument("--json", action="store_true", help="print the run as JSON")
    add_tools_options(fire)
    add_limit_options(fire, "process run")
    fire.set_defaults(handler=_trigger_run)


def add_runs(commands: Commands) -> None:
    runs = add_command(commands, "runs")
    run_actions = runs.add_subparsers(dest="runs_command", required=True, metavar="action")
    run_list = run_actions.add_parser("list", help="list process runs, newest first")
    _add_where(run_list)
    run_list.add_argument("--automation", help="only this automation")
    run_list.add_argument("--status", action="append", help="only this status (repeatable)")
    run_list.add_argument(
        "--limit", type=int, default=50, metavar="N", help="most runs to show (default 50)"
    )
    run_list.add_argument("--json", action="store_true", help="print JSON")
    _add_store(run_list)
    run_list.set_defaults(handler=_runs_list)
    show = run_actions.add_parser(
        "show",
        help="show one run with its history (--steps: its steps, attempts and results in order)",
    )
    show.add_argument("run_id", help=_RUN_ID)
    show.add_argument(
        "--steps",
        action="store_true",
        help="add the steps in order, each with its attempts (status, time, tokens, cost), "
        "results (output, tool calls, checks) and approvals (reviewer, reason)",
    )
    _add_store(show)
    show.set_defaults(handler=_runs_show)
    retry = run_actions.add_parser("retry", help="queue a failed or dead-lettered run again")
    retry.add_argument("run_id", help=_RUN_ID)
    retry.add_argument("--actor", default=None, help="who retries it (recorded)")
    retry.add_argument(
        "--restart",
        action="store_true",
        help="start again from the input instead of resuming where the run stopped "
        "(automatic when its paused state is lost)",
    )
    _add_store(retry)
    retry.set_defaults(handler=_runs_retry)
    cancel = run_actions.add_parser("cancel", help="cancel an unfinished run")
    cancel.add_argument("run_id", help=_RUN_ID)
    cancel.add_argument(
        "--reason",
        default="cancelled by operator",
        help="why; stored with the run (default: cancelled by operator)",
    )
    cancel.add_argument("--actor", default=None, help="who cancels it (recorded)")
    _add_store(cancel)
    cancel.set_defaults(handler=_runs_cancel)
    decide = run_actions.add_parser(
        "decide",
        help="approve or reject a run waiting for an approval",
        description="Answer the approval a process run waits for (the oldest open one, "
        "unless --interrupt-id names another). The decision is recorded in the run store; once "
        "every approval of the run is answered it is queued, and `awa worker` or `awa serve` "
        "using the same store resumes it, also after a restart. Use the same --dsn, or run "
        "the command in the blueprint's folder (or pass the same --store).",
    )
    decide.add_argument("run_id", help="the waiting run (see `awa runs list --status waiting`)")
    verdict = decide.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--approve", action="store_true", help="let the tool call run")
    verdict.add_argument("--reject", action="store_true", help="refuse the tool call")
    decide.add_argument("--note", default=None, help="reason or comment recorded with it")
    decide.add_argument(
        "--edit-args",
        metavar="JSON",
        default=None,
        help="with --approve: run the call with these arguments instead (JSON object or @file)",
    )
    decide.add_argument(
        "--interrupt-id", default=None, help="which approval, when the run waits for several"
    )
    decide.add_argument("--actor", default=None, help="the approver (recorded as reviewer)")
    _add_store(decide)
    decide.set_defaults(handler=_runs_decide)
    trace = run_actions.add_parser(
        "trace",
        help="show what a run did: agents, tools, delegations, approvals, tokens, cost, errors",
        description="Print the trace of a process run as a tree: the agents, what each of them "
        "called, delegated or asked for, with model calls, tokens, cost, time and errors per "
        "step. Traces are written by `awa worker`, `awa serve` and `awa run --trace` in "
        ".awa/traces next to the run store; use the same folder, or pass --traces or --store.",
    )
    trace.add_argument("run_id", help="the process run, or a trace file (.jsonl)")
    which = trace.add_mutually_exclusive_group()
    which.add_argument("--attempt", type=int, help="show attempt N (default: the last one)")
    which.add_argument("--all", action="store_true", help="show every attempt")
    trace.add_argument("--json", action="store_true", help="print the tree as JSON")
    trace.add_argument(
        "--prices", metavar="FILE", help="model prices to show the cost (default: the trace's own)"
    )
    _add_traces_location(trace)
    trace.set_defaults(handler=_runs_trace)
    export = run_actions.add_parser(
        "export-cases",
        help="turn finished runs into eval cases (a regression test from a real run)",
        description="Write one eval case per succeeded run: its input, the decisions "
        "taken at its approvals, `tool:` and `delegated:` expectations from its trace, and its "
        "output as the reference of a `judge:` scorer. Run the file with `awa eval`. Cases "
        "already in the file are kept as they are.",
    )
    export.add_argument("run_ids", nargs="+", metavar="run_id", help="succeeded process runs")
    export.add_argument("--out", required=True, metavar="FILE", help="dataset file (.yaml)")
    export.add_argument(
        "--no-judge", action="store_true", help="no judge scorer (only tools and delegations)"
    )
    _add_traces_location(export, with_store=True)
    export.set_defaults(handler=_runs_export_cases)
    dlq = run_actions.add_parser("dlq", help="list the dead-lettered runs")
    _add_where(dlq)
    dlq.add_argument("--automation", help="only this automation")
    dlq.add_argument(
        "--limit", type=int, default=50, metavar="N", help="most runs to show (default 50)"
    )
    dlq.add_argument("--json", action="store_true", help="print JSON")
    _add_store(dlq)
    dlq.set_defaults(handler=_runs_list)
    migrate = run_actions.add_parser(
        "migrate", help="move waiting runs of older versions to the blueprint's current version"
    )
    migrate.add_argument(
        "blueprint",
        type=blueprint_argument,
        help="blueprint file (defines the current version) or project folder",
    )
    migrate.add_argument("--dry-run", action="store_true", help="only list what would move")
    add_tools_options(migrate)
    _add_store(migrate)
    migrate.set_defaults(handler=_runs_migrate)


def _add_where(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "where",
        nargs="?",
        metavar="PROJECT|BLUEPRINT",
        help="the project folder or blueprint whose runs to show (default: the project of the "
        "current folder); the same store `awa worker` and `awa serve` use",
    )


def _add_store(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dsn", default=None, help=f"PostgreSQL address of the runs (default: ${DSN_ENV})"
    )
    parser.add_argument("--schema", default="awa", help="PostgreSQL schema (default awa)")
    parser.add_argument(
        "--store",
        default=None,
        metavar="FILE",
        help=f"local data file without a DSN (default {DEFAULT_STORE} next to the blueprint, "
        f"else in this folder; checkpoints are kept beside it in {CHECKPOINTS_FILE})",
    )


def _add_trace_level(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--trace-level",
        choices=("off", "summary", "full"),
        default=None,
        help="how much each run's trace records: summary (no message texts), full or off "
        "(default: $AWA_TRACE_LEVEL, else summary)",
    )


def _add_traces_location(parser: argparse.ArgumentParser, *, with_store: bool = False) -> None:
    if with_store:
        _add_store(parser)
    else:
        parser.add_argument(
            "--store",
            default=None,
            help=f"local run store whose folder holds the traces (default {DEFAULT_STORE})",
        )
    parser.add_argument(
        "--traces",
        default=None,
        metavar="DIR",
        help="folder of the trace files (default: traces/ beside the run store)",
    )


def trace_dir(args: argparse.Namespace) -> Path:
    """The trace folder the options select: `--traces`, else `traces/` beside the local run store."""

    if getattr(args, "traces", None):
        return Path(args.traces)
    return local_store(getattr(args, "store", None), project_blueprint(args)).parent / "traces"


def env_dsn() -> str | None:
    """The PostgreSQL DSN of the environment, unless `AWA_DATA_BACKEND=sqlite` keeps the data
    in the local file."""

    from awa.core.settings import get_settings

    value = (env_value(DSN_ENV) or "").strip()
    return value if value and get_settings().data_backend != "sqlite" else None


def _dsn(args: argparse.Namespace) -> str | None:
    return getattr(args, "dsn", None) or env_dsn()


def local_store(store: str | None, blueprint: str | os.PathLike[str] | None = None) -> Path:
    """The local data file: `store`, else `.awa/awa.db` next to `blueprint` (or here)."""

    if store:
        return Path(store)
    if blueprint is not None and _is_blueprint_file(str(blueprint)):
        return Path(blueprint).resolve().parent / DEFAULT_STORE
    return Path(DEFAULT_STORE)


def project_blueprint(args: argparse.Namespace) -> Path | None:
    """The blueprint whose folder holds the local run store: the `PROJECT|BLUEPRINT` argument,
    else the project folder (`--project`, or found above the working folder), as the worker
    keeps it; `None` when that is the working folder itself."""

    where = getattr(args, "where", None)
    if where:
        path = Path(where)
        return (
            path if path.suffix.lower() in {".yaml", ".yml", ".json"} else path / "blueprint.yaml"
        )
    from awa.core.config import current

    project = current().project_dir
    if os.path.normcase(str(project)) == os.path.normcase(str(Path.cwd().resolve())):
        return None
    return project / "blueprint.yaml"


def _with_store(
    args: argparse.Namespace,
    action: Callable[[RunStore], Awaitable[Any]],
    *,
    blueprint: str | None = None,
) -> Any:
    """Run `action` on the run store the options select, then close it."""

    async def main() -> Any:
        async with opened_data(args, blueprint=blueprint) as data:
            return await action(data.runs)

    return _run(main(), args)


@asynccontextmanager
async def opened_data(
    args: argparse.Namespace, *, blueprint: str | None = None
) -> AsyncIterator[Any]:
    """The project's data the options select: PostgreSQL (`PostgresData`, one pool) or the local
    files (`LocalData`). Yields an object with `runs`, `log` and `threads`."""

    from awa.server._data_store import PostgresData, ensure_pseudonym_key, open_local_data

    dsn = _dsn(args)
    if dsn:
        data: Any = PostgresData(dsn, args.schema)
        await data.open()
    else:
        path = local_store(args.store, blueprint or project_blueprint(args))
        folder = path.resolve().parent.parent if path.parent.name == ".awa" else path.parent
        data = open_local_data(path, pseudonym=ensure_pseudonym_key(folder))
    try:
        yield data
    finally:
        await data.close()


def _run(coro: Coroutine[Any, Any, Any], args: argparse.Namespace) -> Any:
    from awa._internal import aio as asyncio_compat

    return asyncio_compat.run(coro, selector_loop=_dsn(args) is not None)


@asynccontextmanager
async def _build_from_target(
    target: str, args: argparse.Namespace, *, store: RunStore, data: Any = None
) -> AsyncIterator[Automation]:
    """Build the automation of a blueprint file or project folder; its checkpointer stays open
    while the block runs, and with `data` its runs go to that run log."""

    from awa.errors import ConfigurationError

    resolved = _resolved(target)
    if not _is_blueprint_file(resolved):
        raise ConfigurationError(f"{target} is not a blueprint file or project folder")
    async with AsyncExitStack() as stack:
        yield await _from_blueprint(Path(resolved), args, store=store, stack=stack, data=data)


def _resolved(target: str) -> str:
    """`target`, or the `blueprint.yaml` of the project folder it names (like `awa validate`)."""

    path = blueprint_path(target)
    return str(path) if path.name == "blueprint.yaml" and Path(target).is_dir() else target


def _is_blueprint_file(target: str) -> bool:
    path = Path(target)
    return path.suffix.lower() in {".yaml", ".yml", ".json"} and ":" not in path.name


async def _from_blueprint(
    path: Path,
    args: argparse.Namespace,
    *,
    store: RunStore,
    stack: AsyncExitStack,
    data: Any = None,
) -> Automation:
    from awa._internal.upstream.checkpoint import StateInMemorySaver
    from awa.automation.blueprint import build_automation, load_automation_blueprint
    from awa.blueprints._resolve import load_tools, skill_registry, tools_files
    from awa.core.settings import Settings, get_settings
    from awa.persistence import open_checkpointer

    blueprint, spec = load_automation_blueprint(path)
    if spec is None:
        raise _no_automation(path)
    dsn = _dsn(args)
    checkpointer: Any
    if getattr(data, "checkpointer", None) is not None:
        checkpointer = data.checkpointer  # PostgreSQL, on the process's one pool
    elif dsn:
        settings = Settings(checkpointer_backend="postgres", postgres_dsn=dsn)
        checkpointer = await stack.enter_async_context(open_checkpointer(settings))
    elif isinstance(getattr(store, "path", None), Path):  # the JSON file or the SQLite data file
        # Next to the local run store, so a paused run resumes in any local process.
        local = Settings(
            checkpointer_backend="sqlite",
            checkpointer_path=str(store.path.with_name(CHECKPOINTS_FILE)),  # type: ignore[attr-defined]  # `path` was checked above
        )
        checkpointer = await stack.enter_async_context(open_checkpointer(local))
    else:
        checkpointer = StateInMemorySaver()
    # A workflow keeps no thread of its own, unless a step pauses for a human decision.
    threads = blueprint.resolved_entrypoint() != "workflow" or bool(blueprint.needs_checkpointer())
    system = await blueprint.abuild(
        tools=load_tools(tools_files(args.tools, path)),
        skill_registry=skill_registry(args.skills, path),
        checkpointer=checkpointer if threads else None,
        allow_imports=True,
    )
    return build_automation(
        blueprint,
        system,
        spec=spec,
        store=store,
        dsn=dsn,
        base_dir=path.resolve().parent,
        usage_limits=limits_from_args(args, blueprint),
        trace=getattr(args, "trace_level", None),
        run_log=getattr(data, "log", None),
        capture=get_settings().capture_content,
    )


def _worker(args: argparse.Namespace) -> int:
    from awa.automation.store import hold_triggers
    from awa.automation.worker import Worker
    from awa.cli._preflight import preflight

    target = _resolved(args.target)
    blueprint = target if _is_blueprint_file(target) else None
    preflight(
        blueprint, models=blueprint is not None, triggers=not args.no_triggers, dsn=_dsn(args)
    )
    from awa.cli._plugins import warn_security_off

    warn_security_off("worker")
    finished: list[ProcessRun] = []

    async def work() -> None:
        async with opened_data(args, blueprint=_resolved(args.target)) as data:
            with contextlib.ExitStack() as stack:
                await _work(data, stack)

    async def _work(data: Any, stack: contextlib.ExitStack) -> None:
        store: RunStore = data.runs
        async with _build_from_target(args.target, args, store=store, data=data) as automation:
            worker = Worker(
                [automation],
                concurrency=args.concurrency or automation.concurrency,
                lease=timedelta(seconds=args.lease),
                drain_timeout=timedelta(seconds=args.drain_timeout),
                triggers=not args.no_triggers,
            )
            path = getattr(store, "path", None)
            if _dsn(args) is None and path is not None:
                if worker.run_triggers:  # one process per local store runs the triggers
                    stack.enter_context(hold_triggers(Path(path)))
                print(
                    f"runs are kept in {path} and paused runs in "
                    f"{Path(path).with_name(CHECKPOINTS_FILE)} (this machine); use --dsn "
                    f"(or ${DSN_ENV}) to share them with workers elsewhere",
                    file=sys.stderr,
                )
            if args.once:
                for run in await worker.run_once():
                    print(_line(run))
                    finished.append(run)
                return
            _install_signals(worker)
            names = ", ".join(a.name for a in worker.automations)
            print(f"worker {worker.worker_id} running {names} (Ctrl+C to stop)", file=sys.stderr)
            await worker.run()

    _run(work(), args)
    return once_exit_code(finished) if args.once else EXIT_OK


def once_exit_code(runs: Sequence[ProcessRun]) -> int:
    """The exit code of `awa worker --once`: 1 when a run failed or was dead-lettered, 2 when
    runs only wait (for an approval or an event), else 0."""

    statuses = {run.status for run in runs}
    if statuses & {"failed", "dead_lettered"}:
        return EXIT_ERROR
    if "waiting" in statuses:
        return EXIT_PAUSED
    return EXIT_OK


def _install_signals(worker: Worker) -> None:
    loop = asyncio.get_running_loop()

    def request_stop(*_: Any) -> None:
        print("stopping: finishing running attempts...", file=sys.stderr)
        loop.call_soon_threadsafe(worker.stop)

    for number in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(number, request_stop)


def _trigger_list(args: argparse.Namespace) -> int:
    from awa.automation.blueprint import build_triggers, load_automation_blueprint

    _, spec = load_automation_blueprint(_resolved(args.blueprint))
    if spec is None or not spec.triggers:
        print("no triggers")
        return EXIT_OK
    from awa.automation.queues import InMemoryEventQueue

    stand_ins = {item.queue.name: InMemoryEventQueue() for item in spec.triggers if item.queue}
    triggers = {
        trigger.name: trigger
        for trigger in build_triggers(
            spec, base_dir=Path(_resolved(args.blueprint)).resolve().parent, queues=stand_ins
        )
    }
    rows = [("NAME", "KIND", "DETAILS")]
    for item in spec.triggers:
        built = triggers.get(item.name)
        details = built.describe() if built is not None else {"enabled": False}
        shown = [
            f"{key}={value}"
            for key, value in details.items()
            if key not in ("name", "type") and value not in (None, "", False)
        ]
        rows.append((item.name, item.kind, ", ".join(shown)))
    print_table(rows)
    return EXIT_OK


def _trigger_run(args: argparse.Namespace) -> int:
    payload = json_argument(args.payload, "--payload")
    if args.dry_run and _is_blueprint_file(_resolved(args.blueprint)):
        # Rendering needs only the templates: no agents, tools or connector settings.
        _preview(_unbuilt_automation(Path(_resolved(args.blueprint))), args.trigger, payload)
        return EXIT_OK

    async def main() -> int:
        from awa.automation.store import InMemoryRunStore

        store = InMemoryRunStore()
        async with _build_from_target(args.blueprint, args, store=store) as automation:
            trigger, event = _preview(automation, args.trigger, payload)
            if args.dry_run:
                return EXIT_OK
            submission = await automation.submit(event, trigger=trigger)
            runs = await automation.run_pending(worker="awa-trigger-run")
            run = runs[-1] if runs else submission.run
            while run.status == "waiting" and interactive():
                # Approvals are asked for in the terminal, as `awa run` does.
                open_waits = [w for w in run.waits if not w.answered and w.kind == "approval"]
                if not open_waits:
                    break
                try:
                    for wait in open_waits:
                        decision = ask_decision({"id": wait.interrupt_id, "value": wait.value})
                        run = await automation.decide(
                            run.id, decision, interrupt_id=wait.interrupt_id, actor="terminal"
                        )
                except EOFError:  # no answer: the run stays paused
                    break
                runs = await automation.run_pending(worker="awa-trigger-run")
                run = runs[-1] if runs else run
            if args.json:
                print_json(run.model_dump(mode="json"))
            else:
                print(_line(run))
                if run.output:
                    print(run.output)
                for wait in run.waits:
                    if not wait.answered:
                        print(f"waiting for {wait.kind} {wait.key}")
            if run.status == "waiting":
                return EXIT_PAUSED
            return EXIT_OK if run.status == "succeeded" else EXIT_ERROR

    result: int = _run(main(), args)
    return result


def _preview(automation: Automation, name: str, payload: Any) -> tuple[Any, Any]:
    """Print what firing trigger `name` with `payload` starts; return the trigger and event."""

    from awa.automation._time import utcnow
    from awa.automation.models import TriggerEvent
    from awa.automation.triggers import Schedule

    trigger = automation.trigger(name)
    now = utcnow()
    if isinstance(trigger, Schedule):
        base = trigger.event_for(trigger.next_after(now - timedelta(days=366)), now=now)
        event = base.model_copy(update={"payload": {**base.payload, **payload}})
    else:
        event = TriggerEvent(source=trigger.name, payload=payload, received_at=now)
    rendered = automation.render(event, trigger=trigger)
    print(f"input:     {preview(rendered['input'], 200)}")
    print(f"thread_id: {rendered['thread_id'] or '(one per run)'}")
    print(f"dedup_key: {rendered['dedup_key'] or '(none)'}")
    return trigger, event


class _Unbuilt:
    """Stands in for the system of a dry run, which only renders the trigger templates."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def run(self, *_: Any, **__: Any) -> Any:
        from awa.errors import ConfigurationError

        raise ConfigurationError("A dry run does not build the system")


def _unbuilt_automation(path: Path) -> Automation:
    """The automation of a blueprint file with its triggers and templates, without its system."""

    from awa.automation.blueprint import build_automation, load_automation_blueprint
    from awa.automation.queues import InMemoryEventQueue
    from awa.contracts.protocols import AgentLike

    blueprint, spec = load_automation_blueprint(path)
    if spec is None:
        raise _no_automation(path)
    stand_ins = {item.queue.name: InMemoryEventQueue() for item in spec.triggers if item.queue}
    system = cast(AgentLike, _Unbuilt(blueprint.name))
    return build_automation(
        blueprint, system, spec=spec, base_dir=path.resolve().parent, queues=stand_ins
    )


def _no_automation(path: Path) -> Exception:
    from awa.errors import ConfigurationError

    return ConfigurationError(
        f"{path} has no automation: add a `triggers:` list (and optionally an "
        "`automation:` section)"
    )


def _runs_list(args: argparse.Namespace) -> int:
    dead_letters = args.runs_command == "dlq"
    status = "dead_lettered" if dead_letters else args.status
    runs: list[ProcessRun] = _with_store(
        args, lambda store: store.list(automation=args.automation, status=status, limit=args.limit)
    )
    if not runs and not args.json and _dsn(args) is None:
        print(f"no runs in {local_store(args.store, project_blueprint(args))}")
        return EXIT_OK
    _print_runs(runs, as_json=args.json, error=dead_letters)
    return EXIT_OK


def _runs_show(args: argparse.Namespace) -> int:
    from awa.errors import RunNotFoundError

    async def main() -> tuple[ProcessRun | None, dict[str, Any] | None]:
        from awa.automation.run_view import run_scope, run_steps

        async with opened_data(args) as data:
            found = await data.runs.get(args.run_id)
            if found is None or not args.steps:
                return found, None
            return found, await run_steps(data.log, run_scope(found), found.id)

    run, steps = _run(main(), args)
    if run is None:
        raise RunNotFoundError(f"No process run {args.run_id!r}")
    data = run.model_dump(mode="json")
    data["usage"]["total_tokens"] = run.usage.total_tokens
    if steps is not None:
        data["steps"] = steps["steps"]
        data["steps_recorded"] = steps["recorded"]
    print_json(data)
    return EXIT_OK


def _runs_trace(args: argparse.Namespace) -> int:
    from awa.errors import RunNotFoundError
    from awa.observability.trace_store import TraceStore, read_trace
    from awa.observability.trace_view import build_tree

    prices = None
    if args.prices:
        from awa.contracts.usage import PriceTable

        prices = PriceTable.from_yaml(args.prices)
    given = Path(args.run_id)
    if given.suffix.lower() == ".jsonl" and given.is_file():
        files = [given]
    else:
        store = TraceStore(trace_dir(args))
        files = store.find(args.run_id)
        if not files:
            raise RunNotFoundError(
                f"No trace for {args.run_id!r} in {store.directory}. Traces are written by "
                "`awa worker`, `awa serve` and `awa run --trace`; run this command in the "
                "blueprint's folder, or pass --traces DIR (or --store FILE). A run made with "
                "--trace-level off has none."
            )
    chosen = files
    if args.attempt is not None:
        wanted = f".a{args.attempt}.jsonl"
        chosen = [f for f in files if f.name.endswith(wanted)]
        if not chosen:
            raise RunNotFoundError(
                f"{args.run_id} has no attempt {args.attempt} "
                f"(traces: {', '.join(f.name for f in files)})"
            )
    elif not args.all:
        chosen = files[-1:]
    trees = [build_tree(read_trace(path), prices=prices) for path in chosen]
    if args.json:
        documents = [tree.to_dict() for tree in trees]
        print_json(documents if args.all else documents[0])
        return EXIT_OK
    for number, tree in enumerate(trees):
        if number:
            print("\n" + "-" * 72 + "\n")
        print(tree.render(), end="")
    if len(files) > 1 and not args.all and args.attempt is None:
        print(
            f"\nThis run has {len(files)} attempts; this is the last one. "
            f"Use --attempt N or --all to see the others ({', '.join(f.name for f in files)})."
        )
    return EXIT_OK


def _runs_export_cases(args: argparse.Namespace) -> int:
    from awa.automation.trace_cases import add_cases, cases_from_runs
    from awa.errors import RunNotFoundError
    from awa.observability.trace_store import TraceStore

    async def load(store: RunStore) -> list[ProcessRun]:
        runs: list[ProcessRun] = []
        for run_id in args.run_ids:
            run = await store.get(run_id)
            if run is None:
                raise RunNotFoundError(f"No process run {run_id!r}")
            runs.append(run)
        return runs

    runs: list[ProcessRun] = _with_store(args, load)
    out = Path(args.out)
    exported = cases_from_runs(
        runs, TraceStore(trace_dir(args)), judge=not args.no_judge, name=out.stem
    )
    for line in exported.skipped:
        print(f"skipped {line}", file=sys.stderr)
    if not exported.dataset.cases:
        print("no case written", file=sys.stderr)
        return EXIT_ERROR
    for line in exported.warnings:
        print(f"warning {line}", file=sys.stderr)
    added, kept = add_cases(out, exported.dataset)
    for case_id in kept:
        print(f"kept the case {case_id} already in {out}", file=sys.stderr)
    print(f"wrote {len(added)} case(s) to {out}; run them with `awa eval <blueprint> {out}`")
    return EXIT_OK


def _runs_retry(args: argparse.Namespace) -> int:
    from awa.automation._time import utcnow
    from awa.automation.automation import retry_run

    run: ProcessRun = _with_store(
        args,
        lambda store: retry_run(
            store, args.run_id, now=utcnow(), actor=args.actor, restart=args.restart or None
        ),
    )
    print(_line(run))
    return EXIT_OK


def _runs_cancel(args: argparse.Namespace) -> int:
    from awa.automation._time import utcnow
    from awa.automation.automation import cancel_run

    run: ProcessRun = _with_store(
        args,
        lambda store: cancel_run(
            store, args.run_id, now=utcnow(), reason=args.reason, actor=args.actor
        ),
    )
    print(_line(run))
    return EXIT_OK


def _runs_decide(args: argparse.Namespace) -> int:
    from awa.automation._time import utcnow
    from awa.automation.automation import decide_run
    from awa.errors import RunNotFoundError

    decision = _decision(args)
    try:
        run: ProcessRun = _with_store(
            args,
            lambda store: decide_run(
                store,
                args.run_id,
                decision,
                now=utcnow(),
                interrupt_id=args.interrupt_id,
                actor=args.actor,
            ),
        )
    except RunNotFoundError as exc:
        if _dsn(args) is not None:
            raise
        raise RunNotFoundError(
            f"{exc} in {local_store(args.store)}. Run this command in the blueprint's folder "
            "(where `awa serve` and `awa worker` keep .awa/), or pass --store or the same "
            f"--dsn (or ${DSN_ENV})."
        ) from None
    print(_line(run))
    remaining = [w.interrupt_id for w in run.waits if not w.answered and w.kind == "approval"]
    if remaining:
        print(f"{len(remaining)} more approval(s) open; decide them to resume the run")
    return EXIT_OK


def _decision(args: argparse.Namespace) -> dict[str, Any]:
    """The resume value `Agent.resume` takes, from `--approve/--reject`, `--note`, `--edit-args`."""

    from awa.automation._decision import approval_decision
    from awa.errors import ConfigurationError

    edited = None if args.edit_args is None else json_argument(args.edit_args, "--edit-args")
    if edited is not None and not isinstance(edited, dict) and not args.reject:
        raise ConfigurationError(
            '--edit-args must be a JSON object of tool arguments, e.g. {"amount": 90}'
        )
    return approval_decision(not args.reject, args.note, edited)


def _runs_migrate(args: argparse.Namespace) -> int:
    async def migrate(store: RunStore) -> list[Any]:
        async with _build_from_target(args.blueprint, args, store=store) as automation:
            print(f"current version: {automation.version}")
            if args.dry_run:
                return await automation.migration_plan()
            return await automation.migrate()

    plan: list[Any] = _with_store(args, migrate, blueprint=_resolved(args.blueprint))
    if not plan:
        print("every unfinished run is on the current version")
        return EXIT_OK
    rows = [("RUN", "STATUS", "VERSION", "ACTION", "REASON")]
    rows.extend((i.run_id, i.status, i.version or "-", i.action, i.reason) for i in plan)
    print_table(rows)
    if args.dry_run:
        print("(dry run: nothing changed)")
    return EXIT_OK


def _line(run: ProcessRun) -> str:
    extra = f" error={run.error}" if run.error and run.status != "succeeded" else ""
    waits = f" waiting_for={','.join(run.wait_keys)}" if run.wait_keys else ""
    used = f" tokens={run.usage.total_tokens}" if run.usage.requests else ""
    cost = f" cost={_cost_text(run.cost)}" if run.cost is not None else ""
    return (
        f"{run.id} {run.automation} {run.status} attempts={run.attempts}{used}{cost}{waits}{extra}"
    )


def _cost_text(value: float | None) -> str:
    return "-" if value is None else f"{value:.4f}"


def _print_runs(runs: Sequence[ProcessRun], *, as_json: bool, error: bool = False) -> None:
    if as_json:
        print_json([run.summary() for run in runs])
        return
    if not runs:
        print("no runs")
        return
    header = (
        "RUN",
        "AUTOMATION",
        "STATUS",
        "ATTEMPTS",
        "TOKENS",
        "COST",
        "CREATED (UTC)",
        "TRIGGER",
    )
    rows: list[tuple[str, ...]] = [(*header, "ERROR") if error else header]
    for run in runs:
        row = (
            run.id,
            run.automation,
            run.status,
            str(run.attempts),
            str(run.usage.total_tokens),
            _cost_text(run.cost),
            run.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            run.trigger or "-",
        )
        rows.append((*row, preview(run.error or "", 60)) if error else row)
    print_table(rows)
