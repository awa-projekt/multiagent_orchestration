"""`awa serve`: build a blueprint and serve it as an HTTP API, with its triggers (only webhooks
and events with `--dsn`, the rest left to `awa worker`)."""

from __future__ import annotations

import argparse
import sys

from awa.cli._automation import CHECKPOINTS_FILE, DSN_ENV, env_dsn, local_store
from awa.cli._common import (
    EXIT_OK,
    LOOPBACK_HOSTS,
    Commands,
    add_blueprint_argument,
    add_command,
    add_limit_options,
    add_tools_options,
    limits_from_args,
)
from awa.core.config import env_value
from awa.core.settings import ALLOWED_HOSTS_VARIABLE

_LOG_LEVELS = ("critical", "error", "warning", "info", "debug", "trace")


def _allowed_hosts(args: argparse.Namespace) -> list[str]:
    """The `--allowed-host` values; none given: the names in `AWA_ALLOWED_HOSTS`."""

    if args.allowed_hosts:
        return list(args.allowed_hosts)
    return [
        name.strip()
        for name in (env_value(ALLOWED_HOSTS_VARIABLE) or "").split(",")
        if name.strip()
    ]


def add_serve(commands: Commands) -> None:
    parser = add_command(commands, "serve")
    add_blueprint_argument(parser)
    add_tools_options(parser)
    add_limit_options(parser, "request and automation run")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="port to listen on (default 8000)")
    parser.add_argument(
        "--allowed-host",
        action="append",
        dest="allowed_hosts",
        metavar="HOST",
        help="host name the server answers besides its own (repeatable; default: the "
        "comma-separated AWA_ALLOWED_HOSTS; without an API key other Host headers are refused)",
    )
    parser.add_argument(
        "--docs",
        action="store_true",
        help="serve the OpenAPI document at /openapi.json (needs the API key when one is set)",
    )
    parser.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help="allow a non-loopback --host without an API key (anyone who can reach the port "
        "can run the system)",
    )
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument(
        "--api-key-env",
        metavar="VAR",
        help="environment variable holding the API key clients must send as a bearer token",
    )
    auth.add_argument(
        "--api-keys-file",
        metavar="FILE",
        help="JSON file mapping API key digests (or key variables) to principals and tenants",
    )
    parser.add_argument(
        "--cors-origin",
        action="append",
        default=[],
        dest="cors_origins",
        metavar="ORIGIN",
        help="browser origin allowed to call the API (repeatable)",
    )
    parser.add_argument(
        "--thread-scope",
        choices=("tenant", "principal", "shared"),
        default="tenant",
        help="who may use a thread: its tenant (default), its creator, or anyone",
    )
    parser.add_argument(
        "--rate-limit",
        metavar="N/SECONDS",
        type=_rate_limit_argument,
        help="requests (runs and resumes) per principal, e.g. 60/60",
    )
    parser.add_argument(
        "--max-concurrency",
        type=_positive_int,
        metavar="N",
        help="runs in flight at once; further requests get 503",
    )
    parser.add_argument(
        "--approver-role",
        action="append",
        default=[],
        dest="approver_roles",
        metavar="ROLE",
        help="role needed to answer approvals (repeatable; default: any caller)",
    )
    parser.add_argument(
        "--drain-timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="seconds runs in flight get to finish at shutdown (default 30)",
    )
    parser.add_argument(
        "--max-body-bytes",
        type=_positive_int,
        metavar="N",
        help="largest request body accepted (default 1 MiB)",
    )
    parser.add_argument(
        "--log-format",
        choices=("text", "json"),
        default="text",
        help="text (default) or JSON lines with request ids",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="PostgreSQL DSN keeping automation runs and paused runs, shared with "
        f"`awa worker` and `awa runs` (default: ${DSN_ENV}; without one they are kept in "
        ".awa/ next to the blueprint)",
    )
    parser.add_argument(
        "--schema", default="awa", help="PostgreSQL schema of the run tables (default awa)"
    )
    parser.add_argument(
        "--store",
        default=None,
        help="without a DSN: the local data file (default .awa/awa.db next to the "
        f"blueprint; the checkpoints are kept beside it in {CHECKPOINTS_FILE})",
    )
    parser.add_argument(
        "--log-level", default="info", choices=_LOG_LEVELS, help="uvicorn log level"
    )
    parser.set_defaults(handler=_serve)


def _rate_limit_argument(text: str) -> str:
    from awa.server._blueprint_app import parse_rate_limit

    try:
        parse_rate_limit(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return text


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}")
    return value


def _serve(args: argparse.Namespace) -> int:
    """`awa serve`: build the app and run uvicorn until interrupted."""

    from awa.blueprints import Blueprint
    from awa.cli._preflight import preflight
    from awa.server._app import import_uvicorn, run_uvicorn
    from awa.server._blueprint_app import ServeConfig, build_blueprint_app

    dsn = args.dsn or env_dsn()
    # Before the port is bound: a missing key or variable stops here with one line each.
    preflight(args.blueprint, triggers=True, dsn=dsn, api_key_env=args.api_key_env)
    from awa.cli._plugins import warn_security_off

    warn_security_off("serve")
    limits = limits_from_args(args, Blueprint.from_yaml_file(args.blueprint))
    store_path = None if dsn else str(local_store(args.store, args.blueprint).resolve())
    config = ServeConfig(
        blueprint=str(args.blueprint),
        tools=list(args.tools),
        skills=list(args.skills),
        api_key_env=args.api_key_env,
        cors_origins=list(args.cors_origins),
        api_keys_file=args.api_keys_file,
        thread_scope=args.thread_scope,
        rate_limit=args.rate_limit,
        max_concurrency=args.max_concurrency,
        approver_roles=list(args.approver_roles),
        drain_timeout=args.drain_timeout,
        max_body_bytes=args.max_body_bytes,
        json_logs=args.log_format == "json",
        dsn=dsn,
        schema=args.schema,
        store_path=store_path,
        host=args.host,
        allowed_hosts=_allowed_hosts(args),
        docs=args.docs,
        usage_limits=None if limits is None else limits.model_dump(mode="json"),
    )
    options = config.app_options()
    unauthenticated = options["api_key"] is None and options["authenticator"] is None
    if unauthenticated and args.host not in LOOPBACK_HOSTS:
        if not args.allow_unauthenticated:
            from awa.errors import ConfigurationError

            raise ConfigurationError(
                f"awa serve without an API key serves on this computer only; --host {args.host} "
                "would let anyone who can reach the port run the system. Use --api-key-env or "
                "--api-keys-file, or add --allow-unauthenticated if that is intended."
            )
        print(
            f"warning: serving on {args.host} without an API key; anyone who can reach the "
            "port can run the system. Use --api-key-env or --api-keys-file.",
            file=sys.stderr,
        )
    _print_banner(config.blueprint, args.host, args.port)
    _explain_automation(config.blueprint, store_path=store_path)
    # psycopg's async driver cannot use Windows' default event loop.
    selector = bool(dsn) and sys.platform == "win32"
    uvicorn = import_uvicorn()
    app = build_blueprint_app(
        config.blueprint,
        tools=config.tools,
        skills=config.skills,
        dsn=config.dsn,
        schema=config.schema,
        store_path=config.store_path,
        **options,
    )
    run_uvicorn(
        uvicorn,
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        json_logs=config.json_logs,
        selector_loop=selector,
    )
    return EXIT_OK


def _print_banner(blueprint: str, host: str, port: int) -> None:
    """Tell what is served and where: the system's name and every trigger's address."""

    from awa.blueprints import Blueprint
    from awa.server._blueprint_app import served_name

    spec = Blueprint.from_yaml_file(blueprint)
    base = f"http://{f'[{host}]' if ':' in host else host}:{port}"  # where it really listens
    system = served_name(spec)
    print(f"awa serves {spec.name!r} on {base}", file=sys.stderr)
    print(f"  system:     POST {base}/v1/systems/{system}/runs", file=sys.stderr)
    section = spec.automation_section()
    name = (section.name if section else None) or spec.name
    for item in section.triggers if section else ():
        where = f"POST {base}/v1/automations/{name}/webhooks/{item.name}" if item.webhook else ""
        print(f"  trigger:    {item.name} ({item.kind}) {where}".rstrip(), file=sys.stderr)
    if section is not None and system != name:
        print(
            f"  note:       the system is called {system!r} in /v1/systems, the automation {name!r}",
            file=sys.stderr,
        )


def _explain_automation(blueprint: str, *, store_path: str | None) -> None:
    """Tell which triggers the server runs, and where the runs are kept."""

    from awa.blueprints import Blueprint

    section = Blueprint.from_yaml_file(blueprint).automation_section()
    if section is None:
        return
    if store_path is not None:
        from pathlib import Path

        from awa.automation.store import hold_triggers

        with hold_triggers(Path(store_path)):  # fail now, not in the server's startup
            pass
        print(
            "automation: the server runs every trigger and executes the runs; runs and paused "
            f"runs are kept in {store_path} (shared with `awa worker` and `awa runs` on this "
            f"machine; --dsn or ${DSN_ENV} shares them across machines)",
            file=sys.stderr,
        )
        return
    print(
        "automation: the server receives webhooks and events and executes the runs they start",
        file=sys.stderr,
    )
    elsewhere = [
        f"{t.name} ({t.kind})" for t in section.triggers if t.enabled and t.webhook is None
    ]
    if elsewhere:
        print(
            f"automation: {', '.join(elsewhere)} run under `awa worker {blueprint} --dsn ...`",
            file=sys.stderr,
        )
