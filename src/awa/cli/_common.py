"""What every `awa` command shares: exit codes, error handling, options and output."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:
    from awa.blueprints import Blueprint
    from awa.contracts.usage import PriceTable, UsageLimits

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PAUSED = 2
"""A run is left paused for an approval, or a tool call needs one (`awa tools call`)."""
EXIT_CONFIRM = 3
"""A change needs a confirmation that was not given (`--confirm`, or a typed name)."""
EXIT_UNSUPPORTED = 4
"""A blueprint uses features the export does not carry over (`awa export`); nothing was written."""
EXIT_CONFIG = 78
"""The configuration is incomplete or wrong (sysexits EX_CONFIG): a missing key or variable."""

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
"""Hosts that only accept connections from this computer."""

Commands: TypeAlias = "argparse._SubParsersAction[argparse.ArgumentParser]"
"""The `add_subparsers()` result that commands are added to."""


def run_command(args: argparse.Namespace) -> int:
    """Run a parsed command's handler and return the exit code (one line per error, 130 on
    Ctrl-C)."""

    from awa.cli._preflight import PreflightFailed
    from awa.cli._tracing import langfuse_tracing
    from awa.errors import AwaError

    verbose = getattr(args, "verbose", False)
    _salt_project(args)
    try:
        with _tool_files_scope(args), langfuse_tracing(args):
            code: int = args.handler(args)
    except PreflightFailed as exc:
        print(exc.report.render(), file=sys.stderr)
        return EXIT_CONFIG
    except AwaError as exc:
        if verbose:
            traceback.print_exc()
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        # Provider and tool failures: keep the message, show the traceback on request.
        if verbose:
            traceback.print_exc()
        hint = "" if verbose else " (run with --verbose for the traceback)"
        print(f"error: {type(exc).__name__}: {exc}{hint}", file=sys.stderr)
        return EXIT_ERROR
    return code


_COMMANDS_WITHOUT_TOOL_FILES = frozenset({"doctor", "env", "setup", "check", "store", "people"})
"""Commands that load no Python file of a project: they skip `file_modules_scope` and its imports."""


def _tool_files_scope(args: argparse.Namespace) -> contextlib.AbstractContextManager[None]:
    """Make a command see only the tools files it loads itself (see `--tools`)."""

    if getattr(args, "command", None) in _COMMANDS_WITHOUT_TOOL_FILES:
        return contextlib.nullcontext()
    from awa.blueprints._resolve import file_modules_scope

    return file_modules_scope()


_SALTED_COMMANDS = frozenset({"run", "chat", "eval", "serve", "worker", "trigger", "mcp"})


def _salt_project(args: argparse.Namespace) -> None:
    """Give a project with a `.env` its cache salt (`AWA_CACHE_SALT`) when it has none, so the
    keys of provider prompt caching stay stable across processes (the value is never shown)."""

    blueprint = getattr(args, "blueprint", None)
    if getattr(args, "command", None) not in _SALTED_COMMANDS or not isinstance(blueprint, str):
        return
    folder = Path(blueprint).resolve().parent
    if not (folder / ".env").is_file():
        return
    from awa.cli._cache import ensure_cache_salt

    # A read-only .env only makes cache keys stable per process.
    with contextlib.suppress(OSError, ValueError):
        ensure_cache_salt(folder)


def prepare_environment(args: argparse.Namespace) -> int | None:
    """Load the project `.env` for a command line and export the names awa knows; returns
    `EXIT_CONFIG` when `AWA_STRICT_SETTINGS` turns a warning into an error."""

    from awa.core import config
    from awa.errors import ConfigurationError

    config.set_skip_preflight(bool(getattr(args, "skip_preflight", False)))
    start, files = _blueprint_start(args)
    allow = [
        value
        for name, value in vars(args).items()
        if name.endswith("_env") and isinstance(value, str) and value.strip()
    ]
    try:
        loaded = config.load_environment(
            start,
            project=getattr(args, "project_folder", None),
            export=True,
            files=files,
            allow=allow,
        )
    except (OSError, ConfigurationError) as exc:  # unreadable or not UTF-8: warn and go on
        print(f"warning: cannot read the project .env: {exc}", file=sys.stderr)
        return None
    printed = config.warn_unknown(loaded)
    if printed and config.strict():
        print(
            "error: AWA_STRICT_SETTINGS is on: correct or remove the unknown names above",
            file=sys.stderr,
        )
        return EXIT_CONFIG
    if config.preflight_skipped():
        print("warning: --skip-preflight: the configuration checks are skipped", file=sys.stderr)
    return None


def _blueprint_start(args: argparse.Namespace) -> tuple[Path | None, list[Path]]:
    """Where the project search starts: the blueprint the command names, else the folder."""

    for name in ("blueprint", "target", "project", "where"):
        value = getattr(args, name, None)
        if not isinstance(value, str) or not value or ":" in Path(value).name:
            continue
        path = Path(value)
        if path.is_dir():
            return path, []
        if path.is_file():
            return path.parent, [path]
    return None, []


def blueprint_path(value: str) -> Path:
    """The blueprint file `value` names: the file itself, or a project folder's `blueprint.yaml`."""

    path = Path(value)
    inside = path / "blueprint.yaml"
    return inside if path.is_dir() and inside.is_file() else path


def blueprint_argument(value: str) -> str:
    """The argparse `type=` of every blueprint argument: a file stays, a project folder
    becomes its `blueprint.yaml`, so each command takes both the same way."""

    return str(blueprint_path(value))


def add_blueprint_argument(parser: argparse.ArgumentParser) -> None:
    """Add the positional `blueprint`: a blueprint file, or a project folder (`blueprint_argument`)."""

    parser.add_argument(
        "blueprint", type=blueprint_argument, help="blueprint file (YAML or JSON) or project folder"
    )


def add_command(commands: Commands, name: str, **options: Any) -> argparse.ArgumentParser:
    """Add the command `name` with its help line from `awa.cli.COMMANDS`."""

    from awa.cli import COMMANDS

    parser: argparse.ArgumentParser = commands.add_parser(name, help=COMMANDS[name][1], **options)
    return parser


def add_tools_options(parser: argparse.ArgumentParser) -> None:
    """Add `--tools` and `--skills` (see `awa.blueprints._resolve.load_tools`)."""

    parser.add_argument(
        "--tools",
        action="append",
        default=[],
        metavar="FILES",
        help="more tool files or modules, comma-separated (repeatable); the tools.py next to "
        "the blueprint is always loaded",
    )
    parser.add_argument(
        "--skills",
        action="append",
        default=[],
        metavar="DIR",
        help="more skill folders (repeatable); skills/ next to the blueprint is always loaded",
    )


def add_deps_options(parser: argparse.ArgumentParser) -> None:
    """Add `--deps` and `--user-id` (see `deps_from_args`)."""

    parser.add_argument(
        "--deps",
        metavar="JSON",
        help="deps for every run: a JSON object, or @file.json (tools read them from the "
        "run context)",
    )
    parser.add_argument(
        "--user-id",
        metavar="ID",
        help="the user the runs act for, added to the deps as user_id (user-scoped memory)",
    )


_LIMIT_OPTIONS = ("max_requests", "max_total_tokens", "max_tool_calls", "max_cost", "max_duration")


def add_limit_options(
    parser: argparse.ArgumentParser, what: str = "run", *, widen: bool = False
) -> None:
    """Add `--max-requests`, `--max-total-tokens`, `--max-tool-calls`, `--max-cost` and
    `--max-duration`: limits for each `what`, never looser than the blueprint's `limits:`
    (with `widen`: never tighter, see `limits_from_args`)."""

    note = f"for each {what}; " + (
        "a larger value in the blueprint's limits wins"
        if widen
        else "can only tighten the blueprint's limits"
    )
    parser.add_argument("--max-requests", type=int, metavar="N", help=f"model calls {note}")
    parser.add_argument(
        "--max-total-tokens", type=int, metavar="N", help=f"prompt plus completion tokens {note}"
    )
    parser.add_argument("--max-tool-calls", type=int, metavar="N", help=f"tool calls {note}")
    parser.add_argument(
        "--max-cost", type=float, metavar="AMOUNT", help=f"cost (needs prices) {note}"
    )
    parser.add_argument("--max-duration", metavar="TIME", help=f"longest time, e.g. 5m, {note}")


def limits_from_args(
    args: argparse.Namespace,
    blueprint: Blueprint,
    *,
    prices: PriceTable | None = None,
    widen: bool = False,
) -> UsageLimits | None:
    """The limits of `blueprint` (`limits:`), tightened by the options of `add_limit_options`.

    Args:
        prices: Prices for `--max-cost` when the blueprint lists none (`awa eval --prices`).
        widen: Take the larger value of every limit both set instead of the smaller one, so one
            setting for many blueprints never cuts the budget a blueprint declares.

    Raises:
        ConfigurationError: If an option is not positive or `--max-cost` has no prices.
    """

    from awa._internal.util import to_timedelta
    from awa.contracts.usage import UsageLimits, merge_limits
    from awa.errors import ConfigurationError

    ceiling = blueprint.usage_limits()
    given = {name: getattr(args, name, None) for name in _LIMIT_OPTIONS}
    if all(value is None for value in given.values()):
        return ceiling
    for name, value in given.items():
        if isinstance(value, int | float) and not value > 0:
            raise ConfigurationError(f"--{name.replace('_', '-')} must be greater than 0")
    seconds = None
    if given["max_duration"] is not None:
        try:
            duration = to_timedelta(given["max_duration"], what="--max-duration")
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from None
        seconds = duration.total_seconds() if duration is not None else None
        if seconds is not None and not seconds > 0:
            raise ConfigurationError("--max-duration must be greater than 0")
    known = (ceiling.prices if ceiling is not None else None) or blueprint.price_table() or prices
    if given["max_cost"] is not None and known is None:
        raise ConfigurationError(
            "--max-cost needs prices: list them under `prices:` in the blueprint"
            + (" or give --prices FILE" if prices is None and hasattr(args, "prices") else "")
        )
    requested = UsageLimits(
        max_requests=given["max_requests"],
        max_total_tokens=given["max_total_tokens"],
        max_tool_calls=given["max_tool_calls"],
        max_cost=given["max_cost"],
        prices=known,
        max_duration=seconds,
    )
    return _widest(requested, ceiling) if widen else merge_limits(requested, ceiling)


def _widest(requested: UsageLimits, declared: UsageLimits | None) -> UsageLimits:
    """Every limit at the larger of the two values (a limit set only once stays)."""

    from awa.contracts.usage import UsageLimits

    if declared is None:
        return requested
    values: dict[str, Any] = {}
    for name in (
        "max_requests", "max_input_tokens", "max_output_tokens", "max_total_tokens",
        "max_tool_calls", "max_duration",
    ):  # fmt: skip
        bounds = [v for v in (getattr(requested, name), getattr(declared, name)) if v is not None]
        values[name] = max(bounds, default=None)
    same_prices = requested.prices == declared.prices
    if requested.max_cost is not None and declared.max_cost is not None and same_prices:
        values["max_cost"], values["prices"] = (
            max(requested.max_cost, declared.max_cost),
            declared.prices,
        )
    else:
        source = declared if declared.max_cost is not None else requested
        values["max_cost"], values["prices"] = source.max_cost, source.prices
    return UsageLimits(**values)


def json_argument(text: str, option: str) -> Any:
    """The JSON value of an option: the text itself, or the content of FILE for `@FILE`.

    `@FILE` works in every shell (PowerShell 5.1 strips double quotes inside an argument).

    Raises:
        ConfigurationError: If the file cannot be read or the text is not valid JSON.
    """

    from awa.errors import ConfigurationError

    raw = text
    if text.startswith("@"):
        try:
            raw = Path(text[1:]).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ConfigurationError(f"Cannot read {option} file {text[1:]!r}: {exc}") from None
    try:
        return json.loads(raw)
    except ValueError as exc:
        where = f"{option} file {text[1:]!r}" if text.startswith("@") else option
        hint = (
            ""
            if text.startswith("@")
            else '; in Windows PowerShell put the JSON in a file and pass it as "@file.json"'
        )
        raise ConfigurationError(f"{where} is not valid JSON: {exc}{hint}") from None


def limit_option(limits: UsageLimits | None) -> dict[str, Any]:
    """`{"usage_limits": limits}` for the `run`, `stream` and `resume` of a system, empty without limits."""

    return {} if limits is None else {"usage_limits": limits}


def deps_from_args(args: argparse.Namespace) -> Any:
    """The `deps` given with `--deps` and `--user-id`, or `None`.

    Raises:
        ConfigurationError: If `--deps` is not valid JSON, its file cannot be read, or it is not
            an object while `--user-id` is given.
    """

    from awa.errors import ConfigurationError

    text: str | None = getattr(args, "deps", None)
    user_id: str | None = getattr(args, "user_id", None)
    deps: Any = None
    if text is not None:
        deps = json_argument(text, "--deps")
    if user_id is not None:
        if deps is None:
            deps = {}
        if not isinstance(deps, dict):
            raise ConfigurationError("--user-id needs --deps to be a JSON object")
        deps = {**deps, "user_id": user_id}
    return deps


def interactive() -> bool:
    """Whether a person can answer on this terminal (standard input is one)."""

    return sys.stdin.isatty()


def ask_decision(pending: dict[str, Any]) -> dict[str, str]:
    """Ask on the terminal whether to approve the paused step `pending`; `y` approves."""

    value = pending.get("value")
    if isinstance(value, dict):  # the Python resume hint is for programs, not for this terminal
        value = {key: item for key, item in value.items() if key != "instructions"}
    print("\nApproval needed:", file=sys.stderr)
    print(json.dumps(value, indent=2, default=str), file=sys.stderr)
    print("Answer here: y approves the step; anything else rejects it.", file=sys.stderr)
    answer = input("Approve? [y/N] ").strip().lower()
    if answer in ("y", "yes"):
        return {"action": "approve"}
    note = input("Reason for rejecting (optional): ").strip()
    return {"action": "reject", "note": note or "Rejected by the operator."}


def print_table(rows: Sequence[Sequence[str]], *, free_last: bool = False) -> None:
    """Print rows as left-aligned columns, the first row the header (`free_last`: last unpadded)."""

    measured = len(rows[0]) - 1 if free_last else len(rows[0])
    widths = [max(len(row[i]) for row in rows) for i in range(measured)]
    for row in rows:
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=False)]
        print("  ".join([*cells, *row[measured:]]).rstrip())


def print_json(value: Any) -> None:
    """Print `value` as indented JSON (what `--json` prints)."""

    print(json.dumps(value, indent=2, default=str))


def preview(value: Any, limit: int = 160) -> str:
    """`value` on one line (JSON unless it is text), cut to `limit` characters."""

    from awa._internal.util import preview as cut  # imported on use: `awa --help` stays light

    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return cut(text, limit, flatten=True)


def output_text(output: Any) -> str:
    """A run's output as text: models and other values as indented JSON."""

    from pydantic import BaseModel

    if isinstance(output, BaseModel):
        return output.model_dump_json(indent=2)
    if isinstance(output, str):
        return output
    return json.dumps(output, indent=2, default=str)
