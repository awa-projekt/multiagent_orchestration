"""The `awa` command line: `main`, `build_parser` and `ChatSession` (behind `awa chat`).

Each command lives in a private module of this package, imported only when that command runs,
so `awa --help` loads nothing but `argparse`.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from awa.cli._chat import ChatSession

COMMANDS: dict[str, tuple[str, str]] = {
    "init": ("_init", "create a starter project from a template; its tests run offline"),
    "validate": ("_blueprint", "check a blueprint for errors without running it"),
    "show": ("_blueprint", "describe a blueprint's agents, tools, triggers and flow"),
    "run": ("_blueprint", "give the system one task and print the answer"),
    "chat": ("_chat", "talk to the system in the terminal; approvals are asked inline"),
    "build": ("_blueprint", "[preview] build a tested project from a description (files: --data)"),
    "export": ("_export", "eject a one-agent project into plain LangChain and LangGraph code"),
    "connect": ("_blueprint", "[preview] set up a project's external systems, setting by setting"),
    "eval": ("_eval", "run the eval cases and report the pass rate (exit 1 below the target)"),
    "serve": ("_serve", "[preview] offer the system as a web API with its webhooks and triggers"),
    "mcp": ("_mcp", "offer the system to MCP clients; pin the tools of its MCP servers"),
    "brief": ("_brief", "[preview] write a brief for a coding assistant from the project folder"),
    "doctor": ("_doctor", "check the installation, keys and settings, and say what to fix"),
    "env": ("_env", "show and change the settings in the project .env (secrets never shown)"),
    "setup": ("_setup", "set the model key; optionally database and tracing"),
    "check": ("_check", "[preview] check the Microsoft 365 setup with small live reads"),
    "add": ("_add", "[preview] add a trigger to a blueprint; import tools, graphs and skills"),
    "plugins": ("_plugins", "list, show, check and switch plugins and parts"),
    "new": ("_new", "write a blueprint, tool, toolbox, parser, connector or skill from a template"),
    "tools": ("_new", "list the tools of a Python file or the builtins, or call one"),
    "worker": ("_automation", "[preview] run the triggers and queued runs until stopped"),
    "trigger": ("_automation", "[preview] list a blueprint's triggers or fire one once to try it"),
    "runs": ("_automation", "[preview] list, approve, retry, cancel and trace process runs"),
    "connectors": ("_connectors", "list, show and test the connectors to your external systems"),
    "ui": ("_ui", "[preview] open the web app: set up, add files, describe, connect, test, run"),
    "skills": ("_skills", "list or show the skills"),
    "data": ("_data", "[preview] inspect your files, check them against a project, run it on them"),
    "cache": ("_cache", "show or clear what the caches hold, or write the project's cache salt"),
    "people": ("_people", "[preview] find colleagues in Entra ID and pin them to roles"),
    "store": ("_store", "[preview] where runs are kept: status, sqlite or postgres, backup, erase"),
}
"""Every command: the module of this package that defines it (`add_<command>`) and its help.
A help line starting with `[preview]` marks a command without a stability promise yet."""

GROUPS: dict[str, tuple[str, ...]] = {
    "Start": ("ui", "build", "init", "setup"),
    "Build": ("new", "add", "connect", "brief", "people", "export"),
    "Check": ("validate", "show", "eval", "data", "doctor", "check"),
    "Run": ("chat", "run", "worker", "serve", "mcp"),
    "Operate": ("runs", "trigger", "connectors", "skills", "tools", "plugins"),
    "Settings": ("env", "cache", "store"),
}
"""How `awa --help` groups the commands, in this order. A command in no group joins the last
group."""

START = """Three ways to start:
  1. Web app:   awa ui --project invoice-desk
  2. Terminal:  awa build "<your process>" --out invoice-desk [--data <your files>]
  3. Template:  awa init my-desk --template support

Then, in the project folder (a model key is needed: awa setup sets it, awa doctor checks it):
  awa chat blueprint.yaml                    try it
  awa eval blueprint.yaml evals/cases.yaml   test it

Let it run by itself:
  awa add trigger blueprint.yaml             asks what starts it and prints the command:
    awa worker blueprint.yaml                schedule, file drop, email or queue
    awa serve blueprint.yaml                 web requests (webhooks), plus all triggers
  awa runs decide RUN_ID --approve           answer an approval (or Approvals in awa ui)

More: awa <action> --help for every option."""
"""The epilog of `awa --help`: the three ways to start and the next steps."""

PREVIEW_COMMANDS = frozenset(
    name for name, (_, text) in COMMANDS.items() if text.startswith("[preview]")
)
"""The commands that carry no stability promise yet (see `docs/about/stability.md`)."""

PREVIEW_SUBCOMMANDS = ("mcp serve",)
"""Subcommands of a stable command that carry no stability promise yet."""


def main(argv: Sequence[str] | None = None) -> int:
    """Run the `awa` command line (`argv` defaults to `sys.argv[1:]`) and return the exit code.

    Exit codes: 0 on success, 1 on errors, 2 when a run is left paused for an approval that could
    not be asked for, 3 when a confirmation is missing, 4 when `awa export` refuses a blueprint,
    78 when the configuration is incomplete. Errors print as one line; `--verbose` adds the
    traceback.
    """

    import sys

    _tolerate_unencodable_output(sys.stdout, sys.stderr)
    arguments = list(sys.argv[1:] if argv is None else argv)
    args = build_parser(arguments).parse_args(arguments)
    from awa.cli._common import prepare_environment, run_command

    refused = prepare_environment(args)
    if refused is not None:
        return refused
    return run_command(args)


def _tolerate_unencodable_output(*streams: Any) -> None:
    """Print `?` for text the stream's encoding (a cp1252 console) cannot hold, never crash."""

    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(ValueError, OSError):  # a closed stream stays as it is
                reconfigure(errors="replace")


class _GroupedHelp(argparse.ArgumentParser):
    """The top-level parser: `--help` lists the commands in `GROUPS`, then the epilog `START`."""

    def format_help(self) -> str:
        formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        formatter.add_text(self.description)
        parts = [formatter.format_help()]
        placed = {name for names in GROUPS.values() for name in names}
        last = list(GROUPS)[-1]
        for title, names in GROUPS.items():
            names = (*names, *(n for n in COMMANDS if n not in placed)) if title == last else names
            rows = "\n".join(
                f"  {name:<11} {COMMANDS[name][1]}" for name in names if name in COMMANDS
            )
            parts.append(f"{title}:\n{rows}\n")
        formatter = self._get_formatter()
        formatter.start_section("options")
        formatter.add_arguments(self._optionals._group_actions)
        formatter.end_section()
        parts.append(formatter.format_help())
        return "\n".join(parts) + f"\n{START}\n"


def _selected_command(argv: Sequence[str]) -> str | None:
    """The command `argv` names: its first word that is neither an option nor an option's value."""

    words = iter(argv)
    for word in words:
        if word == "--project":
            next(words, None)
        elif not word.startswith("-"):
            return word
    return None


def _finish_subcommands(parser: argparse.ArgumentParser) -> None:
    """Give every subcommand its description from `awa.cli._descriptions` when it sets none,
    starting with `[preview]` for a preview command, and let `--verbose` also follow the command
    (`awa validate x --verbose`): every subcommand accepts it unless it has its own; it is hidden
    there and shares the top-level value."""

    from awa.cli._descriptions import DESCRIPTIONS

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in {id(p): p for p in action.choices.values()}.values():
                path = sub.prog.partition(" ")[2]
                if not sub.description:
                    sub.description = DESCRIPTIONS.get(path)
                preview = path in PREVIEW_COMMANDS or path in PREVIEW_SUBCOMMANDS
                if preview and sub.description and not sub.description.startswith("[preview]"):
                    sub.description = f"[preview] {sub.description}"
                if "--verbose" not in sub._option_string_actions:
                    sub.add_argument(
                        "--verbose",
                        action="store_true",
                        default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS,
                    )
                _finish_subcommands(sub)


def build_parser(argv: Sequence[str] | None = None) -> argparse.ArgumentParser:
    """The `awa` parser, complete only for the command `argv` names (for all without `argv`)."""

    parser = _GroupedHelp(
        prog="awa",
        description="AI agents for business processes, with hard limits in code, approvals, eval "
        "cases and a one-agent exit to plain LangGraph.",
    )
    parser.add_argument("--version", action=_VersionAction)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show tracebacks for errors (works before or after the command)",
    )
    parser.add_argument(
        "--project",
        dest="project_folder",
        metavar="DIR",
        help="project folder (default: the nearest folder at or above the current one with "
        "blueprint.yaml, pyproject.toml [tool.awa] or .awa/)",
    )
    # Hidden on purpose (docs/reference/cli.md): for CI images that set variables late.
    parser.add_argument("--skip-preflight", action="store_true", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(
        dest="command", required=True, metavar="action", parser_class=argparse.ArgumentParser
    )
    selected = None if argv is None else _selected_command(argv)
    for name, (module, help_text) in COMMANDS.items():
        if argv is None or name == selected:
            getattr(importlib.import_module(f"awa.cli.{module}"), f"add_{name}")(commands)
        else:
            commands.add_parser(name, help=help_text)
    _finish_subcommands(parser)
    return parser


class _VersionAction(argparse.Action):
    """`--version`, reading the installed version only when it is asked for."""

    def __init__(self, option_strings: Sequence[str], dest: str, **kwargs: Any) -> None:
        kwargs.update(nargs=0, default=argparse.SUPPRESS)
        kwargs.setdefault("help", "show the installed version and exit")
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser: argparse.ArgumentParser, *args: Any, **kwargs: Any) -> None:
        from awa._internal.version import __version__

        print(f"awa {__version__}")
        parser.exit()


def __getattr__(name: str) -> Any:
    if name == "ChatSession":
        from awa.cli._chat import ChatSession

        return ChatSession
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "COMMANDS",
    "PREVIEW_COMMANDS",
    "PREVIEW_SUBCOMMANDS",
    "ChatSession",
    "build_parser",
    "main",
]
