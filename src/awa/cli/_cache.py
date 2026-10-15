"""`awa cache stats|clear|salt`: the project's cache store, emptying it, and its cache salt."""

from __future__ import annotations

import argparse
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

from awa.cli._common import EXIT_ERROR, EXIT_OK, Commands, add_command

if TYPE_CHECKING:
    from awa.runtime.ports import CacheStore


def add_cache(commands: Commands) -> None:
    command = add_command(commands, "cache")
    actions = command.add_subparsers(dest="action", required=True, metavar="action")
    for name, text in (
        ("stats", "show what the cache store holds"),
        ("clear", "delete cache entries"),
        ("salt", "write a random AWA_CACHE_SALT into the project's .env"),
    ):
        action = actions.add_parser(name, help=text)
        action.add_argument(
            "project", nargs="?", default=".", help="project folder or blueprint (default .)"
        )
        if name == "clear":
            action.add_argument("--tenant", help="only this tenant")
            action.add_argument("--kind", help="only this kind (tools, steps, examples, plans)")
            action.add_argument(
                "--replay", action="store_true", help="the recordings of awa eval --replay"
            )
        if name in ("stats", "clear"):
            action.add_argument(
                "--replay-store", metavar="FILE", help="recordings file (default .awa/replay.db)"
            )
        if name == "salt":
            action.add_argument(
                "--force", action="store_true", help="replace a salt that is already set"
            )
    command.set_defaults(handler=_cache)


def _folder(value: str) -> tuple[Path, Path | None]:
    path = Path(value).resolve()
    if path.is_file():
        return path.parent, path
    blueprint = path / "blueprint.yaml"
    return path, blueprint if blueprint.is_file() else None


def ensure_cache_salt(folder: Path, *, force: bool = False) -> bool:
    """Write a random `AWA_CACHE_SALT` into `folder/.env` when it has none (the settings read it
    from there on). Returns whether a salt was written. The value is never printed."""

    from awa.core.dotenv import read_env_file, write_env_file
    from awa.core.settings import CACHE_SALT_VARIABLE, get_settings

    env = folder / ".env"
    if not force and (read_env_file(env).get(CACHE_SALT_VARIABLE) or "").strip():
        return False
    salt = secrets.token_urlsafe(32)
    write_env_file(env, {CACHE_SALT_VARIABLE: salt})
    get_settings.cache_clear()
    return True


def _store(args: argparse.Namespace, folder: Path, blueprint: Path | None) -> CacheStore | None:
    from awa.core.settings import get_settings
    from awa.persistence.cache import SqliteCacheStore, open_cache_store
    from awa.runtime.cache import CacheSettings

    if getattr(args, "replay", False) or getattr(args, "replay_store", None):
        path = Path(args.replay_store) if args.replay_store else folder / ".awa" / "replay.db"
        return SqliteCacheStore(path) if path.is_file() else None
    settings = CacheSettings()
    if blueprint is not None:
        from awa.blueprints import Blueprint

        settings = Blueprint.from_yaml(blueprint).defaults.cache
    environment = get_settings()
    settings = settings.with_environment(environment)
    if args.action == "stats":
        salt = "set" if environment.cache_salt is not None else "not set (awa cache salt)"
        print("; ".join(f"{name}: {value}" for name, value in settings.summary()))
        print(f"AWA_CACHE_SALT: {salt}")
    if settings.store == "memory":
        print("The cache store is `memory`: it lives inside one process, nothing is kept.")
        return None
    return open_cache_store(settings, base=folder)


def _cache(args: argparse.Namespace) -> int:
    folder, blueprint = _folder(args.project)
    if args.action == "salt":
        written = ensure_cache_salt(folder, force=args.force)
        print(
            f"wrote AWA_CACHE_SALT into {folder / '.env'}"
            if written
            else f"AWA_CACHE_SALT is already set in {folder / '.env'} (--force replaces it)"
        )
        return EXIT_OK
    store = _store(args, folder, blueprint)
    if store is None:
        if getattr(args, "replay", False) or getattr(args, "replay_store", None):
            print("No recordings of awa eval --replay found.")
        return EXIT_OK
    try:
        if args.action == "stats":
            lines = store.stats()
            if not lines:
                print("The cache store is empty.")
                return EXIT_OK
            print("| tenant | project | kind | entries | bytes | hits |")
            print("| --- | --- | --- | ---: | ---: | ---: |")
            for line in lines:
                print(
                    f"| {line.tenant} | {line.project} | {line.kind} | {line.entries} | "
                    f"{line.bytes} | {line.hits} |"
                )
            return EXIT_OK
        kind = "replay" if args.replay else args.kind
        deleted = store.clear(tenant=args.tenant, kind=kind)
        print(f"deleted {deleted} cache entr{'y' if deleted == 1 else 'ies'}")
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - one line for a broken store
        print(f"error: the cache store failed: {exc}")
        return EXIT_ERROR
    finally:
        store.close()
