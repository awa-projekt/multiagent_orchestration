"""`awa env list|get|set|unset`: show and change the settings of the project `.env` (secret
values never shown)."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from awa.cli._common import (
    EXIT_CONFIRM,
    EXIT_ERROR,
    EXIT_OK,
    Commands,
    add_command,
    preview,
    print_json,
    print_table,
)

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

    from awa.core.config import LoadedEnvironment


def add_env(commands: Commands) -> None:
    parser = add_command(
        commands,
        "env",
        description="Show and change the settings in the project .env. A value is taken from "
        "the environment, else the project .env, else the default. Secret values are never "
        "shown: only whether they are set.",
    )
    actions = parser.add_subparsers(dest="env_command", required=True, metavar="action")
    listing = actions.add_parser(
        "list",
        help="show every setting with its value, source and users",
        description="Show every setting with its value, source and users. Secret values show "
        'only "set" or "not set".',
    )
    listing.add_argument("--group", help="only this group (models, azure, storage, ...)")
    listing.add_argument("--json", action="store_true", help="print the settings as JSON")
    listing.set_defaults(handler=_list)
    get = actions.add_parser(
        "get",
        help="show one setting and where its value comes from",
        description="Show one setting and where its value comes from (env, .env:<line> or "
        "default).",
    )
    get.add_argument("name", help="the setting, e.g. AWA_DEFAULT_MODEL")
    get.add_argument("--json", action="store_true", help="print the setting as JSON")
    get.set_defaults(handler=_get)
    put = actions.add_parser(
        "set",
        help="check a value and write it to the project .env",
        description="Check the value and write it to the project .env. Secret settings never "
        "accept NAME=VALUE: type the value hidden, or pass it with --stdin. Security switches "
        "and endpoint or base-URL settings need --confirm.",
    )
    put.add_argument(
        "assignment",
        metavar="NAME[=VALUE]",
        help="the setting and its value; a secret takes only NAME (its value is asked hidden)",
    )
    put.add_argument("--stdin", action="store_true", help="read the value from standard input")
    put.add_argument(
        "--confirm", action="store_true", help="confirm a security switch or a request address"
    )
    put.set_defaults(handler=_set)
    unset = actions.add_parser("unset", help="remove a setting from the project .env")
    unset.add_argument("name", help="the setting to remove")
    unset.set_defaults(handler=_unset)


def setting_rows(group: str | None = None) -> list[dict[str, Any]]:
    """Every registry setting and every declared name, as `awa env list --json` shows them."""

    from awa.core.config import current
    from awa.core.settings_registry import REGISTRY

    loaded = current()
    rows = [setting_row(name) for name, spec in REGISTRY.items() if group in (None, spec.group)]
    if group in (None, "declared"):
        rows += [setting_row(name) for name in sorted(loaded.declared) if name not in REGISTRY]
    return rows


def setting_row(name: str, loaded: LoadedEnvironment | None = None) -> dict[str, Any]:
    """One row of `awa env list` and of the sources table of `awa doctor`, read from `loaded`
    (default: the current environment)."""

    from awa.core.config import current, mask
    from awa.core.settings_registry import spec

    loaded = loaded or current()
    found = spec(name)
    value = loaded.get(name)
    source = loaded.source_of(name)
    label = source.label
    if source.origin == "default" and (chosen := detected_profile(name)) is not None:
        shown, label = chosen
    elif source.origin == "default" and found is not None and found.default is not None:
        shown = (
            str(found.default).lower() if isinstance(found.default, bool) else str(found.default)
        )
    else:
        shown = mask(name, value) if source.origin != "env_empty" else "(empty)"
        if (
            found is not None
            and found.choices
            and shown not in (*found.choices, "not set", "(empty)")
        ):
            shown = "(invalid)"
    declared = loaded.declared.get(name)
    return {
        "name": name,
        "value": shown,
        "set": bool(value and value.strip()),
        "source": label,
        "secret": found.is_secret if found is not None else True,
        "group": found.group if found is not None else "declared",
        "used_by": list(found.used_by)
        if found is not None
        else [declared.where if declared else ""],
        "doc": found.doc
        if found is not None
        else f"named in {declared.where if declared else '?'}",
    }


def _profile_fields() -> dict[str, str]:
    from awa.core.settings_registry import SETUP_ENV

    return {SETUP_ENV[field]: field for field in ("default_model", "fast_model", "strong_model")}


def detected_profile(name: str) -> tuple[str, str] | None:
    """For an unset model profile chosen from the credentials: its model and the source
    `default (<provider> key)`; `None` otherwise."""

    field_name = _profile_fields().get(name)
    if field_name is None:
        return None
    from pydantic import ValidationError

    from awa.core.settings import Settings

    try:
        settings = Settings()
    except ValidationError:
        return None
    if settings.detected_provider is None:
        return None
    return str(getattr(settings, field_name)), f"default ({settings.detected_provider} key)"


def _list(args: argparse.Namespace) -> int:
    rows = setting_rows(args.group)
    if args.json:
        print_json(rows)
        return EXIT_OK
    if not rows:
        print(f"no settings in group {args.group!r}")
        return EXIT_ERROR
    table = [("NAME", "VALUE", "SOURCE", "USED BY")]
    table += [
        (r["name"], preview(r["value"], 48), r["source"], ", ".join(r["used_by"])) for r in rows
    ]
    print_table(table, free_last=True)
    return EXIT_OK


def _get(args: argparse.Namespace) -> int:
    from awa.core.config import current
    from awa.core.settings_registry import spec

    name = args.name.strip()
    if spec(name) is None and name not in current().declared:
        print(f"error: {_unknown_message(name)}", file=sys.stderr)
        return EXIT_ERROR
    row = setting_row(name)
    if args.json:
        print_json(row)
        return EXIT_OK
    print(f"{row['name']} = {row['value']}")
    print(f"  source:  {row['source']}")
    if row["doc"]:
        print(f"  what:    {row['doc']}")
    if any(row["used_by"]):
        print(f"  used by: {', '.join(row['used_by'])}")
    return EXIT_OK


def _unknown_message(name: str) -> str:
    import difflib

    from awa.core.settings_registry import known_names

    match = difflib.get_close_matches(name, sorted(known_names()), n=1, cutoff=0.8)
    hint = f" (did you mean {match[0]}?)" if match else ""
    return f"{name} is not a known setting{hint}"


def _set(args: argparse.Namespace) -> int:
    from awa.core.config import current
    from awa.core.dotenv import ENV_NAME
    from awa.core.settings_registry import is_denied, spec

    name, has_value, value = args.assignment.partition("=")
    name = name.strip()
    loaded = current()
    found = spec(name)
    if not ENV_NAME.match(name) or is_denied(name):
        print(f"error: {name!r} cannot be set in .env", file=sys.stderr)
        return EXIT_ERROR
    if found is None and name not in loaded.declared and name.startswith("AWA_"):
        print(f"error: {_unknown_message(name)}", file=sys.stderr)
        return EXIT_ERROR
    secret = found.is_secret if found is not None else True
    if has_value and secret:
        print(
            f"error: {name} is secret: run without =VALUE to type it hidden, or use --stdin",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if not has_value:
        read = read_secret(name, use_stdin=args.stdin)
        if read is None:
            return EXIT_ERROR
        value = read
    if found is not None and found.needs_confirmation and not args.confirm:
        print(
            f"error: {name} decides where requests go or switches a protection; run again "
            "with --confirm",
            file=sys.stderr,
        )
        return EXIT_CONFIRM
    problem = check_value(name, value)
    if problem is not None:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_ERROR
    path = project_env_file()
    note = None
    if args.confirm:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        note = {name: f"awa:confirmed {stamp} {name}"}
    from awa.core.dotenv import write_env_file

    write_env_file(path, {name: value}, mode="merge", confirm_overwrite=True, comments=note)
    print(f"{name} saved in {_shown(path)}")
    from awa.core.config import source_of

    if source_of(name).origin in ("env", "env_empty"):
        print(f"note: the environment also sets {name}; it wins over .env", file=sys.stderr)
    return EXIT_OK


def check_value(name: str, value: str) -> str | None:
    """Why `value` is not valid for `name`, or `None`; never repeats the value."""

    from pydantic import ConfigDict, TypeAdapter, ValidationError

    from awa.core.dotenv import MAX_VALUE_CHARS
    from awa.core.settings_registry import spec

    if not value.strip():
        return f"the value for {name} is empty"
    if any(c in value for c in "\r\n\0") or len(value) > MAX_VALUE_CHARS:
        return f"the value for {name} must be one line of at most {MAX_VALUE_CHARS} characters"
    found = spec(name)
    if found is None:
        return None
    config = ConfigDict(hide_input_in_errors=True)
    types: list[Any] = [found.type]
    field = _settings_field(found.name)
    if field is not None and field.metadata:
        types.append(Annotated[field.annotation, *field.metadata])
    try:
        for kind in types:
            adapter: TypeAdapter[Any] = TypeAdapter(kind, config=config)
            adapter.validate_python(value.strip(), strict=False)
    except ValidationError:
        allowed = f": one of {', '.join(found.choices)}" if found.choices else ""
        return f"{name} does not accept that value{allowed}"
    if found.name in _profile_fields():
        return _check_model(name, value.strip())
    return None


def _settings_field(name: str) -> FieldInfo | None:
    """The `Settings` field read from the variable `name`, whose bounds a value must meet."""

    from awa.core.settings import Settings

    prefix = Settings.model_config.get("env_prefix", "")
    for field_name, field in Settings.model_fields.items():
        if (field.alias or f"{prefix}{field_name}".upper()) == name:
            return field
    return None


def _check_model(name: str, value: str) -> str | None:
    from awa.core.providers import PROVIDERS, provider_of

    prefix = provider_of(value)
    if ":" not in value or not value.partition(":")[2]:
        return f"{name} must be provider:model, for example openai:gpt-6-luna"
    known = {item.name for item in PROVIDERS}
    if prefix not in known:
        return f"{name}: unknown provider {value.partition(':')[0]!r} (known: {', '.join(sorted(known))})"
    return None


def read_secret(name: str, *, use_stdin: bool) -> str | None:
    """A value typed hidden, or read from standard input with `use_stdin` (UTF-8, line break
    dropped); `None` after printing why nothing could be read."""

    if use_stdin:
        data = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read()
        text = data.decode("utf-8-sig") if isinstance(data, bytes) else data.lstrip("\ufeff")
        text = text.rstrip("\r\n")
        if not text.strip():
            print(f"error: no value for {name} on standard input", file=sys.stderr)
            return None
        return text
    if not _can_prompt():
        print(
            f"error: cannot ask for {name} here (no terminal); pass the value with --stdin",
            file=sys.stderr,
        )
        return None
    import getpass

    try:
        typed = getpass.getpass(f"{name} (hidden): ")
    except (EOFError, KeyboardInterrupt):
        print("", file=sys.stderr)
        return None
    if not typed.strip():
        print(f"error: no value typed for {name}", file=sys.stderr)
        return None
    return typed


def _can_prompt() -> bool:
    """Whether a hidden prompt works: a terminal, and not mintty on Windows (Git Bash), where
    `getpass` would echo or hang."""

    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        return False
    return not (sys.platform == "win32" and os.environ.get("TERM") and not _has_console())


def _has_console() -> bool:
    try:
        import msvcrt  # noqa: F401  # the Windows console is usable
    except ImportError:
        return False
    return os.environ.get("TERM_PROGRAM") != "mintty" and "MSYSTEM" not in os.environ


def _unset(args: argparse.Namespace) -> int:
    from awa.core.dotenv import remove_env_names

    path = project_env_file()
    removed = remove_env_names(path, [args.name.strip()])
    if not removed:
        print(f"{args.name} is not in {_shown(path)}")
        return EXIT_OK
    print(f"{args.name} removed from {_shown(path)}")
    if args.name in os.environ:
        print(f"note: the environment still sets {args.name}", file=sys.stderr)
    return EXIT_OK


def project_env_file() -> Path:
    """The `.env` of the project (`--project`, the blueprint's folder or the working folder)."""

    from awa.core.config import current

    loaded = current()
    return loaded.dotenv_file() or loaded.project_dir / ".env"


def _shown(path: Path) -> str:
    from awa.core.config import current, shown_path

    return shown_path(path, current().project_dir)
