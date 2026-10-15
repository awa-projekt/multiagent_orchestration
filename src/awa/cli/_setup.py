"""`awa setup`: model provider and key, database and tracing, into the project `.env`."""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any, Literal

from awa.cli._common import (
    EXIT_CONFIG,
    EXIT_CONFIRM,
    EXIT_ERROR,
    EXIT_OK,
    Commands,
    add_command,
    print_json,
    print_table,
)

if TYPE_CHECKING:
    from awa.project_setup import Choice, SectionName, SetupPlan

_PROFILE = "only this service: db or tracing (repeatable; default: those set up)"


def add_setup(commands: Commands) -> None:
    parser = add_command(
        commands,
        "setup",
        description="Set up awa for this project: model provider and key, database and "
        "tracing. Values are written to the project .env and are never printed. "
        "Without a terminal and without options, the state table and the options to use are "
        "printed. To check the result, run awa doctor.",
    )
    parser.add_argument(
        "--provider",
        help="anthropic, openai, azure_openai, google_genai, mistralai, groq or ollama",
    )
    parser.add_argument(
        "--key-stdin", action="store_true", help="read the provider's key from standard input"
    )
    parser.add_argument("--endpoint", help="Azure OpenAI: the resource endpoint")
    parser.add_argument("--deployment", help="Azure OpenAI: the deployment name")
    parser.add_argument("--host", help="Ollama: where it listens (default http://localhost:11434)")
    parser.add_argument("--test", action="store_true", help="make one tiny model call to check")
    parser.add_argument(
        "--database",
        choices=("sqlite", "postgres-docker", "url"),
        help="where runs and checkpoints are kept: a SQLite file, PostgreSQL in Docker, or "
        "your PostgreSQL (url)",
    )
    parser.add_argument(
        "--database-url-stdin",
        action="store_true",
        help="read the PostgreSQL address for --database url from standard input",
    )
    parser.add_argument(
        "--tracing",
        choices=("off", "langfuse-docker", "url"),
        help="tracing: off, Langfuse in Docker, or your Langfuse (url)",
    )
    parser.add_argument("--langfuse-url", help="--tracing url: the Langfuse address")
    parser.add_argument("--langfuse-public-key", help="--tracing url: the public key")
    parser.add_argument(
        "--langfuse-secret-stdin",
        action="store_true",
        help="--tracing url: read the secret key from standard input",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="accept overwrites and start local services without asking",
    )
    parser.add_argument("--json", action="store_true", help="print the state as JSON")
    parser.set_defaults(handler=_setup)
    actions = parser.add_subparsers(dest="setup_command", metavar="action")
    services = actions.add_parser(
        "services",
        help="start, stop or show this project's local services in Docker",
        description="This project's local services in Docker (PostgreSQL, Langfuse). "
        "Ports are bound to 127.0.0.1 only; only this project's containers are addressed.",
    )
    service_actions = services.add_subparsers(
        dest="services_command", required=True, metavar="action"
    )
    up = service_actions.add_parser("up", help="start this project's local services")
    up.add_argument("--profile", action="append", choices=("db", "tracing"), help=_PROFILE)
    up.add_argument(
        "--wait-timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="how long to wait until the services answer (default 300)",
    )
    up.set_defaults(handler=_services_up)
    down = service_actions.add_parser(
        "down",
        help="stop this project's local services (data is kept unless --volumes)",
        description="Stop this project's local services. Data is kept unless --volumes is "
        "given and you type the project name to confirm (or pass it with --confirm).",
    )
    down.add_argument("--profile", action="append", choices=("db", "tracing"), help=_PROFILE)
    down.add_argument("--volumes", action="store_true", help="also delete the services' data")
    down.add_argument("--confirm", metavar="NAME", help="the project name, for --volumes")
    down.set_defaults(handler=_services_down)
    status = service_actions.add_parser("status", help="show this project's local services")
    status.add_argument("--json", action="store_true", help="print JSON")
    status.set_defaults(handler=_services_status)


def _plan() -> SetupPlan:
    from awa.core.config import current
    from awa.project_setup import SetupPlan

    return SetupPlan(current().project_dir)


def _print_status(plan: SetupPlan, *, as_json: bool) -> None:
    states = plan.status()
    if as_json:
        print_json([state.model_dump(mode="json") for state in states])
        return
    rows = [("SECTION", "STATE", "DETAIL")]
    rows += [(s.section, s.state, s.detail) for s in states]
    print_table(rows, free_last=True)


def _usage_hint() -> None:
    print(
        "\nTo set up without questions:\n"
        "  awa setup --provider openai --key-stdin < key.txt      (or anthropic, azure_openai, ...)\n"
        "  awa setup --database sqlite|postgres-docker|url [--database-url-stdin]\n"
        "  awa setup --tracing off|langfuse-docker|url\n"
        "Check the result with: awa doctor"
    )


def _setup(args: argparse.Namespace) -> int:
    from awa.core.dotenv import OverwriteRefused
    from awa.project_setup import SetupError

    stdin_options = [args.key_stdin, args.database_url_stdin, args.langfuse_secret_stdin]
    if sum(bool(option) for option in stdin_options) > 1:
        print("error: only one of the --...-stdin options can read standard input", file=sys.stderr)
        return EXIT_ERROR
    for option, value, selector, chosen in _dependent_options(args):
        if value and not chosen:
            print(f"error: {option} needs {selector}", file=sys.stderr)
            return EXIT_ERROR
    plan = _plan()
    given = (
        any(value is not None for value in (args.provider, args.database, args.tracing))
        or args.test
    )
    if not given:
        if _interactive() and not args.json:
            return _ask(plan, yes=args.yes)
        _print_status(plan, as_json=args.json)
        if not args.json:
            _usage_hint()
        return EXIT_OK
    try:
        code = _apply(plan, args)
    except (OverwriteRefused, SetupError) as exc:
        return _refused(exc)
    _print_status(plan, as_json=args.json)
    return code


def _dependent_options(args: argparse.Namespace) -> list[tuple[str, Any, str, Any]]:
    """`(option, value, selector, selected)` of every option that only works with its selector."""

    provider, database, tracing = args.provider, args.database, args.tracing
    return [
        ("--key-stdin", args.key_stdin, "--provider", provider),
        ("--endpoint", args.endpoint, "--provider", provider),
        ("--deployment", args.deployment, "--provider", provider),
        ("--host", args.host, "--provider", provider),
        ("--database-url-stdin", args.database_url_stdin, "--database", database),
        ("--langfuse-url", args.langfuse_url, "--tracing", tracing),
        ("--langfuse-public-key", args.langfuse_public_key, "--tracing", tracing),
        ("--langfuse-secret-stdin", args.langfuse_secret_stdin, "--tracing", tracing),
    ]


def _refused(error: Exception) -> int:
    """Print why the choices were not applied and return the exit code for it."""

    from awa.core.dotenv import OverwriteRefused

    print(f"error: {error}", file=sys.stderr)
    return EXIT_CONFIRM if isinstance(error, OverwriteRefused) else EXIT_CONFIG


def _apply(plan: SetupPlan, args: argparse.Namespace) -> int:
    from awa.cli._env import read_secret

    confirm = args.yes
    if args.provider is not None:
        choices: dict[str, Any] = {
            "provider": args.provider,
            "endpoint": args.endpoint,
            "deployment": args.deployment,
            "host": args.host,
        }
        from awa.core.providers import provider

        spec = provider(args.provider)
        needs_key = spec is not None and spec.name != "ollama" and not spec.complete(plan.value)
        if args.key_stdin or (needs_key and spec is not None):
            label = (spec.key_variable if spec is not None else None) or "the key"
            key = read_secret(label, use_stdin=args.key_stdin)
            if key is None:
                return EXIT_ERROR
            choices["key"] = key
        _with_overwrite(plan, "provider", choices, confirm)
        print(f"provider {args.provider} saved in .env", file=sys.stderr)
    if args.test:
        plan.test()
        print("the test call answered", file=sys.stderr)
    if args.database is not None:
        choices = {"database": args.database}
        if args.database == "url":
            url = read_secret("the PostgreSQL address", use_stdin=args.database_url_stdin)
            if url is None:
                return EXIT_ERROR
            choices["url"] = url
        _with_overwrite(plan, "database", choices, confirm)
    if args.tracing is not None:
        choices = {"tracing": args.tracing}
        if args.tracing == "url":
            secret = read_secret("LANGFUSE_SECRET_KEY", use_stdin=args.langfuse_secret_stdin)
            if secret is None:
                return EXIT_ERROR
            choices.update(
                url=args.langfuse_url, public_key=args.langfuse_public_key, secret_key=secret
            )
        if args.tracing == "langfuse-docker":
            warning = _memory_warning(plan)
            if warning:
                print(f"warning: {warning}", file=sys.stderr)
        _with_overwrite(plan, "tracing", choices, confirm)
    wanted = plan.wanted_profiles(database=args.database, tracing=args.tracing)
    if wanted:
        if args.yes or (_interactive() and _yes(f"Start {', '.join(wanted)} in Docker now?")):
            print(plan.start(wanted), file=sys.stderr)
        else:
            print(
                f"next: awa setup services up {' '.join('--profile ' + w for w in wanted)}",
                file=sys.stderr,
            )
    return EXIT_OK


def _with_overwrite(
    plan: SetupPlan, section: SectionName, choices: dict[str, Any], confirm: bool
) -> None:
    from awa.core.dotenv import OverwriteRefused

    try:
        plan.apply(section, choices, confirm_overwrite=confirm)
    except OverwriteRefused as exc:
        if not _interactive() or not _yes(
            f"{', '.join(exc.names)} already set in .env; overwrite?"
        ):
            raise
        plan.apply(section, choices, confirm_overwrite=True)


def _memory_warning(plan: SetupPlan) -> str | None:
    try:
        warning: str | None = plan.services.langfuse_memory_warning()
    except Exception:  # noqa: BLE001 - only a hint; Docker problems are reported when starting
        return None
    return warning


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _yes(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _choose(question: str, options: list[Choice], default: str) -> str:
    for number, option in enumerate(options, start=1):
        print(f"  {number}. {option.value:<16} {option.text}")
    try:
        answer = input(f"{question} [{default}] ").strip()
    except EOFError:
        return default
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        return str(options[int(answer) - 1].value)
    return answer or default


def _ask(plan: SetupPlan, *, yes: bool) -> int:
    """The terminal dialog: section by section, Enter keeps what is there."""

    from awa.core.dotenv import OverwriteRefused
    from awa.project_setup import SetupError

    _print_status(plan, as_json=False)
    print()
    namespace = argparse.Namespace(
        provider=None, key_stdin=False, endpoint=None, deployment=None, host=None, test=False,
        database=None, database_url_stdin=False, tracing=None, langfuse_url=None,
        langfuse_public_key=None, langfuse_secret_stdin=False, yes=yes, json=False,
    )  # fmt: skip
    current = plan.provider()
    print("Model provider:")
    namespace.provider = _choose(
        "Provider (number or name)",
        plan.options("provider"),
        current.name if current else "anthropic",
    )
    if namespace.provider == "azure_openai":
        namespace.endpoint = (
            input("Azure endpoint (https://<resource>.openai.azure.com/): ").strip() or None
        )
        namespace.deployment = input("Deployment name: ").strip() or None
    namespace.test = _yes("Test the key with one tiny call (costs a few tokens)?")
    print("\nDatabase:")
    namespace.database = _changed(plan, "database", "Database")
    print("\nTracing:")
    namespace.tracing = _changed(plan, "tracing", "Tracing")
    if namespace.tracing == "url":
        namespace.langfuse_url = input("Langfuse address: ").strip()
        namespace.langfuse_public_key = input("Langfuse public key: ").strip()
    try:
        code = _apply(plan, namespace)
    except (OverwriteRefused, SetupError) as exc:
        return _refused(exc)
    print()
    _print_status(plan, as_json=False)
    print("\nCheck the result with: awa doctor")
    return code


def _changed(plan: SetupPlan, section: Literal["database", "tracing"], label: str) -> str | None:
    """The answer for a section, or `None` when Enter kept what the project has."""

    current = plan.current_choice(section)
    answer = _choose(label, plan.options(section), current)
    return answer if answer != current else None


def _profiles(args: argparse.Namespace, plan: SetupPlan) -> list[str]:
    if args.profile:
        return list(dict.fromkeys(args.profile))
    configured = [p for p, port in plan.configured_ports().items() if port]
    return configured or ["db"]


def _services_up(args: argparse.Namespace) -> int:
    from awa.project_setup import SetupError
    from awa.project_setup.services import ServicesError

    plan = _plan()
    try:
        print(plan.start(_profiles(args, plan), wait_timeout=args.wait_timeout))
    except (ServicesError, SetupError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    return EXIT_OK


def _services_down(args: argparse.Namespace) -> int:
    from awa.project_setup.services import PROFILES, ServicesError

    plan = _plan()
    services = plan.services
    if args.volumes:
        typed = args.confirm
        if typed is None and _interactive():
            try:
                typed = input(f"This deletes the data. Type the project name ({services.name}): ")
            except EOFError:
                typed = None
        if (typed or "").strip() != services.name:
            print(
                f"error: --volumes deletes the data; confirm with --confirm {services.name}",
                file=sys.stderr,
            )
            return EXIT_CONFIRM
    try:
        result = services.down(args.profile or list(PROFILES), volumes=args.volumes)
    except ServicesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if result.returncode != 0:
        print(f"error: docker compose down failed: {result.stderr.strip()[-300:]}", file=sys.stderr)
        return EXIT_ERROR
    print(f"stopped {services.name}" + (" and deleted its data" if args.volumes else ""))
    return EXIT_OK


def _services_status(args: argparse.Namespace) -> int:
    from awa.project_setup.services import ServicesError

    plan = _plan()
    services = plan.services
    try:
        rows = services.status()
    except ServicesError as exc:
        if args.json:
            print_json({"project": services.name, "error": str(exc), "services": []})
        else:
            print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.json:
        print_json({"project": services.name, "services": rows})
        return EXIT_OK
    if not rows:
        print(f"no local services for {services.name} (awa setup --database postgres-docker ...)")
        return EXIT_OK
    table = [("SERVICE", "STATE", "HEALTH", "PORTS")]
    table += [(r["service"], r["state"], r["health"], ", ".join(r["ports"])) for r in rows]
    print(f"project {services.name}")
    print_table(table, free_last=True)
    return EXIT_OK
