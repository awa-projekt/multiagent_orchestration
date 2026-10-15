"""`awa validate|show|run|build|connect`: check, describe, run and create blueprints."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from awa.cli._common import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_PAUSED,
    Commands,
    add_blueprint_argument,
    add_command,
    add_deps_options,
    add_limit_options,
    add_tools_options,
    ask_decision,
    blueprint_path,
    deps_from_args,
    interactive,
    limit_option,
    limits_from_args,
    output_text,
    preview,
    print_json,
    print_table,
)
from awa.cli._terminal_text import terminal_safe

if TYPE_CHECKING:
    from awa.blueprints import (
        Blueprint,
        BuildQuestions,
        NetworkBlueprint,
        TeamBlueprint,
        WorkflowNode,
    )
    from awa.blueprints._automation import TriggerBlueprint
    from awa.blueprints._lint import Profile
    from awa.contracts.events import RunEvent
    from awa.contracts.protocols import AgentLike
    from awa.contracts.results import RunResult
    from awa.contracts.usage import Usage, UsageLimits
    from awa.observability.trace_store import TraceRecorder


def add_validate(commands: Commands) -> None:
    validate = add_command(commands, "validate")
    add_blueprint_argument(validate)
    add_tools_options(validate)
    validate.add_argument(
        "--profile",
        choices=["authored", "generated"],
        default="authored",
        help="lint profile: authored (default) or generated (every rule strict)",
    )
    validate.add_argument(
        "--notes", dest="verbose_lint", action="store_true", help="also print every lint note"
    )
    validate.set_defaults(handler=_validate)


def add_show(commands: Commands) -> None:
    show = add_command(commands, "show")
    show.add_argument(
        "view",
        nargs="?",
        choices=["cases", "dry-run"],
        help=(
            "cases: the special cases in words and their boundary tests; dry-run: the path a "
            "run would take with --input values; both without a model"
        ),
    )
    add_blueprint_argument(show)
    add_tools_options(show)
    show.add_argument("--mermaid", action="store_true", help="print a Mermaid diagram only")
    show.add_argument(
        "--input",
        metavar="VALUES",
        help=(
            "dry-run: the field values the steps answer, as a JSON object or a .json/.yaml "
            'file; {"<step>": {...}} gives one step its own values'
        ),
    )
    show.add_argument(
        "--fail",
        action="append",
        default=[],
        metavar="STEP[=TRIES]",
        help="dry-run: let the first TRIES tries of STEP fail with an error (default 1)",
    )
    show.add_argument(
        "--decide",
        choices=["approve", "reject"],
        default="approve",
        help="dry-run: how every question to a person is answered (default approve)",
    )
    show.set_defaults(handler=_show)


def add_run(commands: Commands) -> None:
    run = add_command(commands, "run")
    add_blueprint_argument(run)
    run.add_argument("task", help="the task or message for the system")
    run.add_argument("--stream", action="store_true", help="print events as they happen")
    run.add_argument(
        "--thread-id",
        metavar="ID",
        help="thread id for the run (state lives in memory for this invocation only)",
    )
    run.add_argument("--json", action="store_true", help="print the result as JSON")
    run.add_argument(
        "--trace",
        nargs="?",
        const="",
        default=None,
        metavar="FILE",
        help="write the run's trace (agents, tools, delegations, approvals, tokens; secrets "
        "redacted) to FILE, default .awa/traces/RUN_ID.jsonl; read it with `awa runs trace`",
    )
    run.add_argument(
        "--trace-level",
        choices=("summary", "full"),
        default="summary",
        help="what --trace records: summary (no message texts) or full (default summary)",
    )
    add_tools_options(run)
    add_deps_options(run)
    add_limit_options(run, "run")
    run.set_defaults(handler=_run)


def add_build(commands: Commands) -> None:
    build = add_command(
        commands,
        "build",
        description="Write acceptance tests for the process, design a system against them, "
        "evaluate it on sample data, repair it from the failing cases, and write the project "
        "folder. Exit code 1 when the target pass rate is not reached.",
    )
    build.add_argument("process", help="plain-language description of the process")
    build.add_argument("--out", metavar="DIR", help="project folder (default: ./<system name>)")
    build.add_argument("--model", help="model profile or provider:model for every step")
    build.add_argument(
        "--rounds", type=int, default=3, metavar="N", help="evaluation rounds (default 3)"
    )
    build.add_argument(
        "--target",
        type=float,
        default=1.0,
        metavar="RATE",
        help="pass rate to stop at (default 1.0)",
    )
    build.add_argument("--guidance", default="", help="extra requirements for the design")
    build.add_argument(
        "--max-requests", type=int, metavar="N", help="model-call limit for the whole build"
    )
    build.add_argument(
        "--max-total-tokens", type=int, metavar="N", help="token limit for the whole build"
    )
    build.add_argument("--force", action="store_true", help="overwrite an existing project")
    build.add_argument(
        "--no-input",
        action="store_true",
        help="never ask (for example what should start it, or the questions about your files: "
        "they are printed as JSON and the exit code is 2)",
    )
    build.add_argument(
        "--data",
        action="append",
        default=[],
        metavar="PATH",
        help="build on your real files: a file, folder or glob, repeatable. They are copied into "
        "the project's data/ folder (the originals are never touched) and replace invented "
        "sample data",
    )
    build.add_argument(
        "--note",
        action="append",
        default=[],
        metavar="FILE=TEXT",
        help="what must happen with a file of --data (a name or glob), repeatable",
    )
    build.add_argument(
        "--mask",
        choices=("none", "placeholders", "drop"),
        default="none",
        help="what the design model sees of personal data in the summary of your files: "
        "everything (none, the default), placeholders such as <IBAN_1> for names, tax and bank "
        "numbers, email addresses and phone numbers, or no sample values (drop). Agents that "
        "read your files through the file tools see the file content unmasked at run time: "
        "give an agent only the files it needs",
    )
    build.add_argument(
        "--answer",
        action="append",
        default=[],
        metavar="TEXT",
        help="your answer to the questions of an earlier build of the same description and "
        "files (q1=TEXT answers one question), repeatable",
    )
    build.set_defaults(handler=build_command)


def add_connect(commands: Commands) -> None:
    connect = add_command(
        commands,
        "connect",
        description="Walk through CONNECT.md: ask for each missing setting with hidden "
        "input, save it in .env, test the connector and switch the agents to it. "
        "`awa connect microsoft` sets up Microsoft 365 instead and checks it (a project folder "
        "called microsoft is given as ./microsoft).",
    )
    connect.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="project folder (default .), or the word microsoft",
    )
    connect.add_argument(
        "--teams",
        action="store_true",
        help="with microsoft: also sign the Teams user in (device code)",
    )
    connect.add_argument(
        "--people",
        action="store_true",
        help="with microsoft: sign a user in who may find colleagues (User.ReadBasic.All)",
    )
    connect.add_argument(
        "--project",
        default=".",
        metavar="DIR",
        help="with microsoft: the project folder whose .env is used (default .)",
    )
    connect.add_argument(
        "--database",
        metavar="NAME",
        help="connect one database called NAME (postgresql://, mysql://, mssql:// or a SQLite "
        "file): test it, save <NAME>_DATABASE_URL in .env and print the blueprint lines",
    )
    connect.set_defaults(handler=_connect)


def _validate(args: argparse.Namespace) -> int:
    from awa.blueprints import Blueprint
    from awa.blueprints._resolve import load_tools, skill_registry, tools_files

    path = blueprint_path(args.blueprint)
    blueprint = Blueprint.from_yaml(path)
    specs = tools_files(args.tools, path)
    tools = load_tools(specs) if specs else None
    registry = skill_registry(args.skills, path)
    issues = blueprint.check(tools=tools, skills=registry)
    if blueprint.data:  # the files the `data:` section names are read here, at the edge
        from awa.blueprints._data_project import check_project

        issues += check_project(blueprint)
    for issue in issues:
        print(issue)
    findings = [
        *_lint(blueprint, tools, registry, args.profile),
        *_lint_skills(blueprint, registry),
    ]
    notes = [finding for finding in findings if finding.severity == "info"]
    for finding in findings:
        if finding.severity != "info" or args.verbose_lint:
            print(finding)
    errors = sum(issue.severity == "error" for issue in issues) + sum(
        finding.severity == "error" for finding in findings
    )
    warnings = len(issues) + len(findings) - len(notes) - errors
    if errors:
        print(f"{blueprint.name}: {errors} error(s), {warnings} warning(s)")
        return EXIT_ERROR
    suffix = f" with {warnings} warning(s)" if warnings else ""
    note = "" if tools is not None else " (tool names not checked; no tools.py beside it)"
    print(f"{blueprint.name}: valid{suffix}{note}")
    return EXIT_OK


def called_tool_name(reference: str) -> str:
    """The name an agent calls a tool by, for every place a person reads it (`awa show`, the
    review and test pages): `connector:ledger.query` -> `ledger_query`, `builtin:read_file` ->
    `read_file`. `connector:ledger` (all its read operations) stays `ledger (connector)`."""

    if reference.startswith("builtin:"):
        return reference.removeprefix("builtin:")
    if reference.startswith("connector:"):
        instance, _, operation = reference.removeprefix("connector:").partition(".")
        if not operation:
            return f"{instance} (connector)"
        from awa.connectors._engine import tool_name

        return tool_name(instance, operation)
    return reference


def _trigger_source(item: TriggerBlueprint) -> str:
    """What starts a trigger's runs, in words: `cron 0 7 * * MON-FRI`, `a file in inbox/ ...`."""

    if item.schedule is not None:
        rule = item.schedule
        when = f"cron {rule.cron}" if rule.cron else f"every {rule.every}"
        text = f"{when} ({rule.timezone})" if rule.timezone else when
    elif item.webhook is not None:
        signed = item.webhook.scheme if item.webhook.secret_env else "unsigned"
        text = f"a web request to awa serve ({signed})"
    elif item.file_drop is not None:
        text = f"a file in {item.file_drop.directory}/ matching {item.file_drop.pattern}"
    elif item.mailbox is not None:
        text = f"new mail in {item.mailbox.folder} on {item.mailbox.host}"
    elif item.queue is not None:
        text = f"a message on the queue {item.queue.name}"
    else:
        text = "-"
    return text if item.enabled else f"{text}; disabled"


def _lint(blueprint: Blueprint, tools: Any, registry: Any, profile: Profile) -> list[Any]:
    """The lint findings of `awa validate` under `profile`."""

    from awa.blueprints._lint import lint_blueprint
    from awa.blueprints._resolve import ToolCatalog

    catalog = ToolCatalog.from_source(tools) if tools is not None else None
    return lint_blueprint(
        blueprint, profile=profile, tools=catalog, skills=[skill.name for skill in registry.all()]
    )


def awa_tool_names() -> frozenset[str]:
    """The names of awa's own tools: builtin toolkit tools and the tools teams add."""

    from awa.directory import AgentDirectory, create_directory_tools
    from awa.tasks import TaskBoard, create_task_board_tools
    from awa.toolkits import catalog

    names = set(catalog().names())
    names |= {t.name for t in create_task_board_tools(TaskBoard(), "a", can_assign=True)}
    names |= {t.name for t in create_directory_tools(AgentDirectory(), viewer="a")}
    names |= {
        "delegate_task", "consult_peer", "send_message", "broadcast", "read_inbox",
        "reply_to_message", "check_delegations", "wait_for_delegations", "cancel_delegation",
        "remember", "recall", "forget", "read_shared", "write_shared", "list_shared",
    }  # fmt: skip
    return frozenset(names)


def _lint_skills(blueprint: Blueprint, registry: Any) -> list[Any]:
    """The skill lint findings (`awa.skills._lint`) of the skills the blueprint's agents use."""

    from awa.skills._lint import lint_skill

    used = dict.fromkeys(name for spec in blueprint.agents for name in spec.skills)
    names = awa_tool_names()
    return [
        finding
        for name in used
        if name in registry
        for finding in lint_skill(registry.get(name), tool_names=names)
    ]


def _show(args: argparse.Namespace) -> int:
    from awa.blueprints import Blueprint
    from awa.blueprints._diagram_roles import role_tree_lines
    from awa.blueprints._roles import role_root

    blueprint = Blueprint.from_yaml(blueprint_path(args.blueprint))
    if args.view == "cases":
        from awa.cli._show import show_cases

        return show_cases(blueprint)
    if args.view == "dry-run":
        from awa.cli._show import show_dry_run

        return show_dry_run(blueprint, args.input, args.fail, args.decide)
    if args.mermaid:
        print(blueprint.to_mermaid())
        return EXIT_OK
    print(f"{blueprint.name} {blueprint.version}")
    if blueprint.description:
        print(blueprint.description)
    print(f"entrypoint: {blueprint.resolved_entrypoint() or 'undetermined'}")
    print()
    _print_agents(blueprint)
    section = blueprint.automation_section()
    if section is not None and section.triggers:
        print("\ntriggers (start runs by themselves; awa worker or awa serve runs them):")
        for item in section.triggers:
            print(f"  {item.name} ({item.kind}): {_trigger_source(item)}")
    root = role_root(blueprint)
    if root is not None:
        print("\nrole tree:")
        for line in role_tree_lines(blueprint, root):
            print(f"  {line}")
    if blueprint.team is not None:
        _print_team(blueprint.team_name(), blueprint.team)
    if blueprint.workflow is not None:
        print(f"\nworkflow: {_describe_node(blueprint.workflow)}")
    if blueprint.process is not None:
        _print_process(blueprint)
    if blueprint.network is not None:
        _print_network(blueprint.network_name(), blueprint.network)
    if blueprint.approvals:
        gated = ", ".join(
            called_tool_name(approval.tool) + (f" (when {approval.when})" if approval.when else "")
            for approval in blueprint.approvals
        )
        print(f"\napprovals: {gated}")
    for policy in blueprint.policies:
        parts = [f"deny {', '.join(policy.deny)}"] if policy.deny else []
        parts += [f"ask for {', '.join(policy.ask)}"] if policy.ask else []
        parts += [f"use {policy.use}"] if policy.use else []
        print(f"\npolicy ({', '.join(policy.agents) or 'all agents'}): {'; '.join(parts)}")
    if blueprint.uses_auto():
        print(f"\n{blueprint.explain()}")
    return EXIT_OK


def _print_agents(blueprint: Blueprint) -> None:
    """The agents table of `awa show`; tools and skills auto-matching adds are marked "(auto)"."""

    with_roles = any(spec.role for spec in blueprint.agents)
    matched = (
        {item.agent: item for item in blueprint.explain().agents} if blueprint.uses_auto() else {}
    )

    def listed(own: Sequence[str], auto: Sequence[str]) -> str:
        names = [*own, *(f"{name} (auto)" for name in auto if name not in own)]
        return ", ".join(names) or "-"

    rows = [
        ("agent", *(("role",) if with_roles else ()), "model", "tools", "skills", "description")
    ]
    for spec in blueprint.agents:
        report = matched.get(spec.name)
        rows.append(
            (
                spec.name,
                *((spec.role or "-",) if with_roles else ()),
                spec.model or blueprint.defaults.model or "default",
                listed(
                    [called_tool_name(ref) for ref in spec.tools],
                    [called_tool_name(ref) for ref in (report.tools if report else ())],
                ),
                listed(spec.skills, report.skills if report else ()),
                preview(spec.description or "-", 60),
            )
        )
    print_table(rows)


def _print_team(name: str, team: TeamBlueprint) -> None:
    print(f"\nteam {name}: orchestrator {team.orchestrator}")
    _print_edges("delegates", team.delegates or {team.orchestrator: team.members})
    _print_edges("peers", team.peers)
    _print_edges("handoffs", team.handoffs)
    features = {
        "messaging": team.messaging,
        "blackboard": team.blackboard,
        "task board": team.task_board,
    }
    shared = [name for name, enabled in features.items() if enabled]
    print(f"  shared: {', '.join(shared) or 'none'}")


def _print_network(name: str, network: NetworkBlueprint) -> None:
    print(f"\nnetwork {name}: lead {network.lead}")
    print(f"  teammates: {', '.join(network.teammates)}")
    if isinstance(network.peers, dict):
        _print_edges("peers", network.peers)
    else:
        print("  peers: all teammates")
    print(f"  shared: task board{', blackboard' if network.blackboard else ''}")


def _print_process(blueprint: Blueprint) -> None:
    """The process section of `awa show`: the steps with their agents, documents and checks."""

    from awa.blueprints._schema_process import describe_check

    process = blueprint.process
    assert process is not None
    print(
        f"\nprocess: {len(process.steps)} step(s), each step receives {_carry_words(process.carry)}"
    )
    by_name = {entry.name: entry for entry in blueprint.data}
    for number, step in enumerate(process.steps, 1):
        title = f" - {step.title}" if step.title else ""
        print(f"  {number}. {step.name}{title}")
        print(f"     agent: {step.agent or '(none yet)'}")
        spec = next((a for a in blueprint.agents if a.name == step.agent), None)
        documents = list(dict.fromkeys([*(spec.documents if spec else []), *step.documents]))
        for name in documents:
            entry = by_name.get(name)
            where = f"{entry.role}, {entry.path}" if entry is not None else "not declared"
            print(f"     document: {name} ({where})")
        print("     always checked: documents there and as expected; the agent gave an answer")
        for check in step.checks:
            print(f"     check: {describe_check(check)}")
        if not step.checks:
            print("     check: (only the automatic checks)")
        if step.when is not None:
            print(f"     runs only when: {step.when}")
        if step.retry is not None:
            on = " and ".join(f"a failed {kind}" for kind in step.retry.on)
            print(f"     tries: up to {step.retry.max_attempts}, again after {on}")
        if step.after_last_try != "stop" or step.retry is not None:
            print(f"     after the last try: {step.after_last_try}")
        table = blueprint.special_cases.get(step.name)
        if table is not None:
            for line in table.describe():
                print(f"     special case: {line}")


def _carry_words(carry: str) -> str:
    return "the previous step's result" if carry == "previous" else "every earlier step's result"


def _run(args: argparse.Namespace) -> int:
    from awa._internal import aio as asyncio_compat
    from awa._internal.upstream.checkpoint import StateInMemorySaver
    from awa.blueprints import Blueprint
    from awa.blueprints._resolve import load_tools, skill_registry, tools_files
    from awa.cli._preflight import preflight
    from awa.contracts.usage import cost_or_none
    from awa.runtime.context import new_run_id, use_run_id

    preflight(args.blueprint)
    blueprint = Blueprint.from_yaml(Path(args.blueprint))
    tools = load_tools(tools_files(args.tools, args.blueprint))
    deps = deps_from_args(args)
    limits = limits_from_args(args, blueprint)
    needs_checkpointer = args.thread_id is not None or bool(blueprint.needs_checkpointer())

    async def build_and_execute() -> RunResult[Any]:
        system = await blueprint.abuild(
            tools=tools,
            skill_registry=skill_registry(args.skills, args.blueprint),
            checkpointer=StateInMemorySaver() if needs_checkpointer else None,
            allow_imports=True,
        )
        with use_run_id(run_id):
            return await _execute(system, args, deps, limits)

    run_id = new_run_id()
    recorder = _trace_recorder(args, blueprint, run_id)
    with _recording(recorder) as outcome:
        result = asyncio_compat.run(build_and_execute())
        outcome.append("paused" if result.is_interrupted else "ok")
    if result.is_interrupted:
        print(
            f"The run is paused for approval (thread {result.thread_id}); run it in an "
            "interactive terminal to decide.",
            file=sys.stderr,
        )
        return EXIT_PAUSED
    cost = cost_or_none(result.usage, blueprint.price_table())
    if args.json:
        print_json(_result_json(result, cost))
    else:
        print(output_text(result.output))
        print_usage(result.agent, result.usage, duration_ms=result.duration_ms, cost=cost)
    return EXIT_OK


def _trace_recorder(
    args: argparse.Namespace, blueprint: Blueprint, run_id: str
) -> TraceRecorder | None:
    """The recorder `awa run --trace` asks for, or `None`."""

    if args.trace is None:
        return None
    from awa.observability.trace_store import DEFAULT_TRACE_DIR, TraceRecorder

    path = Path(args.trace) if args.trace else Path(DEFAULT_TRACE_DIR) / f"{run_id}.jsonl"
    return TraceRecorder(
        path,
        level=args.trace_level,
        root_run_id=run_id,
        meta={"blueprint": blueprint.name, "command": "awa run"},
        prices=blueprint.price_table(),
    )


@contextmanager
def _recording(recorder: TraceRecorder | None) -> Iterator[list[str]]:
    """Feed the run's events to `recorder`, close its trace whatever the outcome, say where it is."""

    outcome: list[str] = []
    if recorder is None:
        yield outcome
        return
    from awa.runtime.ambient import use_global_subscriber

    try:
        with use_global_subscriber(recorder):
            yield outcome
    except BaseException as exc:
        recorder.close("failed", exc)
        _trace_written(recorder)
        raise
    recorder.close(outcome[-1] if outcome else "ok")
    _trace_written(recorder)


def _trace_written(recorder: TraceRecorder) -> None:
    print(
        f"trace written to {recorder.path}; read it with: awa runs trace {recorder.path}",
        file=sys.stderr,
    )


def build_command(args: argparse.Namespace) -> int:
    """Handle `awa build`; exit code 1 below the target pass rate."""

    from awa._internal import aio as asyncio_compat
    from awa.blueprints import BuildProgress, build_project
    from awa.contracts.usage import UsageLimits
    from awa.errors import ConfigurationError

    if (args.note or args.answer or args.mask != "none") and not args.data:
        raise ConfigurationError("--note, --mask and --answer belong to --data: name your files")
    from awa.cli._preflight import preflight

    preflight(None, extra_models=[args.model])  # before any file is read or profiled
    limits = None
    if args.max_requests is not None or args.max_total_tokens is not None:
        limits = UsageLimits(max_requests=args.max_requests, max_total_tokens=args.max_total_tokens)

    def progress(step: BuildProgress) -> None:
        print(f"[{step.stage}] {step.message}", file=sys.stderr)

    notes = _notes(args.note)
    answers = _answers(args.answer)
    asking = not getattr(args, "no_input", True) and sys.stdin.isatty()
    declared = _declared_systems(args.out, args.process)
    while True:
        report = asyncio_compat.run(
            build_project(
                args.process,
                out_dir=args.out,
                model=args.model,
                max_rounds=args.rounds,
                target_pass_rate=args.target,
                usage_limits=limits,
                on_progress=progress,
                guidance=args.guidance,
                force=args.force,
                data=args.data or None,
                notes=notes,
                mask=args.mask,
                answers=answers,
                declared_systems=declared,
            )
        )
        question = report.question
        if question is None:
            break
        given = _ask_questions(question) if asking and question.status == "question" else None
        if given is None:  # the progress line `[question]` already told it, once
            wait = question.status == "question"
            print_json({**question.to_dict(), "status": "needs_input" if wait else "blocked"})
            return EXIT_PAUSED if wait else EXIT_ERROR
        answers = {**answers, **given}
    print(report.summary())
    assert report.design is not None  # without a question there is a design
    triggered = bool(report.design.blueprint.triggers)  # already inferred from the process
    if not getattr(args, "no_input", True) and sys.stdin.isatty() and not triggered:
        _offer_triggers(report.directory / "blueprint.yaml")
    return EXIT_OK if report.passed else EXIT_ERROR


def _declared_systems(out: str | None, process: str = "") -> list[Any]:
    """The systems declared in the project folder `out` (`awa data add-system`), with their
    settings exported from its `.env` for the test runs (names it does not set only), and the
    catalog connectors the description `process` names whose settings are set (read-only)."""

    from awa.blueprints._declare import merged_env, named_systems, read_declared_systems
    from awa.core.dotenv import export_env_file

    systems = read_declared_systems(out) if out is not None else []
    env_file = Path(out) / ".env" if out is not None else Path(".env")
    named = named_systems(
        process if isinstance(process, str) else "",
        merged_env(os.environ, env_file, {}),
        taken=[s.key for s in systems] + [s.connector for s in systems],
    )
    names = [name for system in [*systems, *named] for name in system.variables]
    export_env_file(env_file, os.environ, names)
    for system in systems:
        print(
            f"[systems] {system.name} ({system.kind}): live, read-only (declared in "
            f"{Path(str(out)) / 'data/SYSTEMS.json'})",
            file=sys.stderr,
        )
    for system in named:
        print(
            f"[systems] {system.name}: the description names the `{system.connector}` connector "
            "and its settings are set; the design uses it live, read-only",
            file=sys.stderr,
        )
    return [*systems, *named]


def _notes(values: list[str]) -> dict[str, str]:
    """`--note FILE=TEXT` options as a dict (a second note for a file is added to its first)."""

    from awa.errors import ConfigurationError

    notes: dict[str, str] = {}
    for value in values:
        name, separator, text = value.partition("=")
        if not separator or not name.strip() or not text.strip():
            raise ConfigurationError(f"--note needs FILE=TEXT, got {value!r}")
        notes[name.strip()] = (
            f"{notes[name.strip()]} {text.strip()}" if name.strip() in notes else text.strip()
        )
    return notes


def _answers(values: list[str]) -> dict[str, str]:
    """`--answer` options: `q1=TEXT` answers one question, plain `TEXT` the questions as a whole."""

    answers: dict[str, str] = {}
    for number, value in enumerate(values, start=1):
        name, separator, text = value.partition("=")
        if separator and re.fullmatch(r"q\d+", name.strip()):
            answers[name.strip()] = text.strip()
        else:
            answers[f"answer_{number}"] = value.strip()
    return answers


def _ask_questions(question: BuildQuestions) -> dict[str, str] | None:
    """Ask each question of a build on the terminal; `None` when nobody answers."""

    print("\nBefore anything is designed, awa needs to know:", file=sys.stderr)
    given: dict[str, str] = {}
    try:
        for item in question.questions:
            options = f" ({' / '.join(item.options)})" if item.options else ""
            print(f"\n{item.id}: {item.text}{options}", file=sys.stderr)
            answer = input("Your answer: ").strip()
            if answer:
                given[item.id] = answer
    except EOFError:
        print(file=sys.stderr)
        return None
    return given or None


def _offer_triggers(blueprint: Path) -> None:
    """Ask what should start the system, write each answer as a trigger and print how to run it."""

    from awa.blueprints import Blueprint
    from awa.blueprints.triggers import add_trigger, ask_trigger, trigger_steps

    print("\nStart it automatically? (you can do this later with `awa add trigger`)")
    while (entry := ask_trigger(input, sys.stdout)) is not None:
        add_trigger(blueprint, entry)
        print(f"added trigger {entry['name']!r} to {blueprint}")
    for step in trigger_steps(Blueprint.from_yaml_file(blueprint), str(blueprint)):
        print(f"{step.title}:\n  {step.command}")


MICROSOFT = "microsoft"
"""The word that makes `awa connect` set up Microsoft 365 (a folder of that name: ./microsoft)."""


def _connect(args: argparse.Namespace) -> int:
    import getpass

    from awa.blueprints._connect import run_connect, run_connect_database
    from awa.errors import ConfigurationError

    if args.directory == MICROSOFT:
        if args.database:
            raise ConfigurationError("connect microsoft and --database are two different steps")
        from awa.checks.connect import run_connect_microsoft

        if Path(MICROSOFT).is_dir():
            print(
                "note: a folder called microsoft is here; to connect that project, run "
                "awa connect ./microsoft"
            )
        return run_connect_microsoft(Path(args.project), teams=args.teams, people=args.people)
    if args.teams or args.people:
        raise ConfigurationError(
            "--teams and --people belong to awa connect microsoft; a project folder called "
            "microsoft is given as ./microsoft"
        )
    if args.database:
        return run_connect_database(args.directory, args.database, secret=getpass.getpass)
    return run_connect(args.directory, secret=getpass.getpass)


def print_usage(
    agent: str, usage: Usage, *, duration_ms: int | None = None, cost: float | None = None
) -> None:
    """Print one line of model usage to stderr (with the cost when the prices are known)."""

    timing = f", {duration_ms} ms" if duration_ms is not None else ""
    money = f", cost {cost:.4f}" if cost is not None else ""
    print(
        f"\n[{agent}] {usage.requests} model call(s), {usage.total_tokens} tokens "
        f"({usage.input_tokens} in, {usage.output_tokens} out){money}{timing}",
        file=sys.stderr,
    )


async def _execute(
    system: AgentLike, args: argparse.Namespace, deps: Any = None, limits: UsageLimits | None = None
) -> RunResult[Any]:
    printer = _EventPrinter(sys.stdout) if args.stream else None
    result = await _start(system, args.task, args.thread_id, printer, deps, limits)
    while result.is_interrupted and interactive():
        resume = getattr(system, "resume", None)
        if resume is None or result.thread_id is None:
            break
        try:
            decisions = {p["id"]: ask_decision(p) for p in result.interrupts}
        except EOFError:  # no answer (Ctrl-D, or input at its end): the run stays paused
            print(file=sys.stderr)
            break
        # Several pending approvals (parallel tool calls) are resumed together,
        # keyed by interrupt id; a single one takes the plain decision.
        decision: Any = next(iter(decisions.values())) if len(decisions) == 1 else decisions
        result = await resume(result.thread_id, decision, deps=deps, **limit_option(limits))
    return result


async def _start(
    system: AgentLike,
    task: str,
    thread_id: str | None,
    printer: _EventPrinter | None,
    deps: Any = None,
    limits: UsageLimits | None = None,
) -> RunResult[Any]:
    from awa.contracts.events import RunCompletedEvent
    from awa.errors import AgentRunError

    # Every system a blueprint builds (agent, team, workflow, network) can stream.
    stream = getattr(system, "stream", None)
    if printer is None or stream is None:
        return await system.run(task, thread_id=thread_id, deps=deps, **limit_option(limits))
    result: RunResult[Any] | None = None
    async for event in stream(task, thread_id=thread_id, deps=deps, **limit_option(limits)):
        printer(event)
        if isinstance(event, RunCompletedEvent):
            result = event.result
    if result is None:
        raise AgentRunError("The run ended without a result")
    return result


class _EventPrinter:
    """Prints run events as readable, depth-indented lines."""

    def __init__(self, out: TextIO) -> None:
        self._out = out
        self._streamed: set[str] = set()
        self._open_line = False

    def __call__(self, event: RunEvent) -> None:
        from awa.contracts.events import TextDeltaEvent

        if isinstance(event, TextDeltaEvent):
            self._streamed.add(event.run_id)
            self._out.write(terminal_safe(event.delta))
            self._open_line = True
            return
        line = self._describe(event)
        if line is None:
            return
        if self._open_line:
            self._out.write("\n")
            self._open_line = False
        self._out.write(f"{'  ' * event.depth}{terminal_safe(line)}\n")
        self._out.flush()

    def _describe(self, event: RunEvent) -> str | None:
        from awa.contracts import events

        if isinstance(event, events.AgentStartedEvent):
            return f"> {event.agent} started"
        if isinstance(event, events.ModelResponseEvent):
            if event.run_id in self._streamed:
                self._streamed.discard(event.run_id)
                return None
            return f"  {event.agent}: {preview(event.content)}" if event.content else None
        if isinstance(event, events.ToolCallEvent):
            arguments = json.dumps(event.arguments, default=str)
            return f"  {event.agent} -> {event.tool_name}({preview(arguments)})"
        if isinstance(event, events.ToolResultEvent):
            status = "error" if event.is_error else "ok"
            from awa.observability.events import tool_output_text

            content = tool_output_text(event.content)
            return f"  {event.agent} <- {event.tool_name} [{status}]: {preview(content)}"
        if isinstance(event, events.DelegationEvent):
            return f"  {event.agent} => {event.target} ({event.mode}): {preview(event.task)}"
        if isinstance(event, events.DelegationResultEvent):
            return (
                f"  {event.agent} <= {event.target} [{event.status}]: "
                f"{preview(event.content_preview)}"
            )
        if isinstance(event, events.HandoffEvent):
            reason = f": {preview(event.reason)}" if event.reason else ""
            return f"  {event.source} >> {event.target}{reason}"
        if isinstance(event, events.AgentMessageEvent):
            message = event.message
            recipient = message.recipient or message.topic or "all"
            return f"  {message.sender} -> {recipient} [{message.kind}]: {preview(message.subject)}"
        if isinstance(event, events.AgentFinishedEvent):
            return f"< {event.agent} finished ({event.duration_ms} ms)"
        if isinstance(event, events.ErrorEvent):
            return f"! {event.agent} failed: {event.error_type}: {preview(event.message)}"
        return None


def _result_json(result: RunResult[Any], cost: float | None = None) -> dict[str, Any]:
    from pydantic import BaseModel

    output = result.output
    return {
        "agent": result.agent,
        "output": output.model_dump(mode="json") if isinstance(output, BaseModel) else output,
        "text": result.text,
        "thread_id": result.thread_id,
        "handoff_path": list(result.handoff_path),
        "usage": result.usage.model_dump(mode="json", exclude={"by_agent"})
        | {"total_tokens": result.usage.total_tokens},
        "cost": cost,
        "duration_ms": result.duration_ms,
    }


def _describe_node(node: WorkflowNode) -> str:
    from awa.blueprints import LoopStep, MapStep, ParallelStep, SequentialStep

    if isinstance(node, str):
        return node
    if isinstance(node, SequentialStep):
        return " -> ".join(_describe_node(child) for child in node.sequential)
    if isinstance(node, ParallelStep):
        return "(" + " | ".join(_describe_node(child) for child in node.parallel) + ")"
    if isinstance(node, MapStep):
        return f"map[{_describe_node(node.map)}] parts of {node.part_size}"
    assert isinstance(node, LoopStep)
    condition = ""
    if node.until_contains is not None:
        condition = f" until {node.until_contains!r}"
    elif node.until is not None:
        condition = f" until {node.until}"
    return f"loop[{_describe_node(node.loop)}]{condition} x{node.max_iterations}"


def _print_edges(label: str, edges: dict[str, list[str]]) -> None:
    for source, targets in edges.items():
        print(f"  {label}: {source} -> {', '.join(targets)}")
