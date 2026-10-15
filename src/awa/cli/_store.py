"""`awa store`: where runs, steps and results are kept, and moving them between SQLite and
PostgreSQL (connection strings never printed or taken as arguments)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from awa.cli._common import EXIT_CONFIRM, EXIT_ERROR, EXIT_OK, Commands, add_command, print_json


def add_store(commands: Commands) -> None:
    command = add_command(commands, "store")
    actions = command.add_subparsers(dest="action", required=True, metavar="action")

    def project(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "project", nargs="?", default=None, help="project folder (default: --project, else .)"
        )
        parser.add_argument("--schema", default="awa", help="PostgreSQL schema (default awa)")

    status = actions.add_parser("status", help="the active store, its schema and row counts")
    project(status)
    status.set_defaults(handler=_status)
    status.add_argument("--json", action="store_true", help="print the status as JSON")
    use = actions.add_parser(
        "use",
        help="make sqlite or postgres the store: move the data, verify it, switch",
        description="Move the project's runs, steps, results, thread owners and checkpoints "
        "into the other store, verify counts and checksums, then set AWA_DATA_BACKEND in .env. "
        "`use postgres` without AWA_POSTGRES_DSN starts the local PostgreSQL in Docker first "
        "(as `awa setup` does). The old store stays untouched as a backup.",
    )
    use.add_argument("to", choices=("postgres", "sqlite"), help="the store to move to")
    project(use)
    use.set_defaults(handler=_use)
    migrate = actions.add_parser("migrate", help="the move of `use`, with every option")
    migrate.add_argument(
        "--to", required=True, choices=("postgres", "sqlite"), help="the store to move to"
    )
    migrate.add_argument(
        "--dsn-env", default="AWA_POSTGRES_DSN", metavar="NAME", help="variable holding the DSN"
    )
    migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="only count what would move (copies no row; the stores are still prepared)",
    )
    migrate.add_argument(
        "--no-switch", action="store_true", help="copy and verify, but keep AWA_DATA_BACKEND"
    )
    migrate.add_argument("--json", action="store_true", help="print the result as JSON")
    project(migrate)
    migrate.set_defaults(handler=_migrate)
    upgrade = actions.add_parser("upgrade", help="apply pending schema migrations")
    project(upgrade)
    upgrade.set_defaults(handler=_upgrade)
    backup = actions.add_parser("backup", help="back up the local data (SQLite backup API)")
    backup.add_argument("target", help="a new folder for the backup files")
    project(backup)
    backup.set_defaults(handler=_backup)
    erase = actions.add_parser(
        "erase", help="delete runs for good: one run, an automation's runs, or a person's"
    )
    erase.add_argument("--automation", required=True, help="the automation (project) name")
    erase.add_argument("--tenant", default="local", help="the tenant (default local)")
    which = erase.add_mutually_exclusive_group(required=True)
    which.add_argument("--run", help="one process run")
    which.add_argument("--all", action="store_true", help="every run of the automation")
    which.add_argument("--person", help="every run this principal started or decided on")
    erase.add_argument(
        "--confirm", action="store_true", help="confirm deleting many runs (--all, --person)"
    )
    project(erase)
    erase.set_defaults(handler=_erase)
    sweep = actions.add_parser("sweep", help="delete finished runs older than the retention")
    sweep.add_argument("--days", type=_days, help="default: AWA_RUN_RETENTION_DAYS (at least 1)")
    project(sweep)
    sweep.set_defaults(handler=_sweep)


def _days(text: str) -> int:
    """`--days`: a whole number of at least 1 (0 would delete every finished run)."""

    try:
        days = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if days < 1:
        raise argparse.ArgumentTypeError("--days must be at least 1")
    return days


def _run(coro: Any) -> Any:
    from awa._internal import aio as asyncio_compat

    return asyncio_compat.run(coro, selector_loop=True)


def _say(line: str) -> None:
    print(line, flush=True)


def _folder(args: argparse.Namespace) -> Path:
    """The project folder: the positional one, else the global `--project`, else here."""

    return Path(args.project or args.project_folder or ".").resolve()


def _status(args: argparse.Namespace) -> int:
    from awa.project_setup import data_store

    found = _run(data_store.store_status(_folder(args), schema=args.schema))
    if args.json:
        print_json(found.to_dict())
        return EXIT_OK
    print(f"store: {found.backend} ({found.where})")
    print(f"schema: {found.last_migration or 'no migration applied'}")
    for table, count in found.counts.items():
        print(f"  {table:<24} {count}")
    return EXIT_OK


def _use(args: argparse.Namespace) -> int:
    from awa.project_setup import data_store

    done = _run(data_store.use_store(_folder(args), args.to, schema=args.schema, say=_say))
    return EXIT_OK if done is None or done.switched else EXIT_ERROR


def _migrate(args: argparse.Namespace) -> int:
    from awa.project_setup import data_store

    result = _run(
        data_store.migrate_store(
            _folder(args),
            args.to,
            dsn_env=args.dsn_env,
            schema=args.schema,
            dry_run=args.dry_run,
            switch=not args.no_switch,
            say=_say,
        )
    )
    if args.json:
        print_json(result.to_dict())
    else:
        for table in result.report["tables"]:
            print(
                f"  {table['name']:<24} source {table['source']}, copied {table['copied']},"
                f" present {table['present']}, target {table['target']}"
            )
    return EXIT_OK if args.dry_run or result.verified else EXIT_ERROR


def _upgrade(args: argparse.Namespace) -> int:
    from awa.project_setup import data_store

    for status in _run(data_store.upgrade_store(_folder(args), schema=args.schema)):
        print(f"{status['component']}/{status['version']:04d}_{status['name']}: {status['state']}")
    return EXIT_OK


def _backup(args: argparse.Namespace) -> int:
    from awa.project_setup import data_store

    for path in data_store.backup_store(_folder(args), args.target):
        print(f"wrote {path}")
    return EXIT_OK


def _erase(args: argparse.Namespace) -> int:
    from awa.project_setup import data_store

    if (args.all or args.person is not None) and not args.confirm:
        print("This deletes many runs for good; add --confirm to go ahead.", file=sys.stderr)
        return EXIT_CONFIRM
    erased = _run(
        data_store.erase(
            _folder(args),
            automation=args.automation,
            tenant=args.tenant,
            run=args.run,
            everything=args.all,
            person=args.person,
            schema=args.schema,
        )
    )
    print(
        f"deleted {len(erased.runs)} run(s), {len(erased.threads)} checkpoint thread(s), "
        f"{erased.traces} trace file(s)"
    )
    return EXIT_OK


def _sweep(args: argparse.Namespace) -> int:
    from awa.project_setup import data_store

    swept = _run(data_store.sweep(_folder(args), days=args.days, schema=args.schema))
    print(f"deleted {len(swept.runs)} run(s) older than the retention")
    return EXIT_OK
