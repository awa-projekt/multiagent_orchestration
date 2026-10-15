"""Triggers for a blueprint from a few answers: a valid `triggers:` entry and the commands that run it.

`trigger_entry` builds one entry (secrets only named, as `*_env` variables), `add_trigger`
writes it into a blueprint file, and `trigger_steps` lists what to run and send for every
trigger. `awa add trigger`, `awa ui`, `awa build` and `awa new process` use them.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from awa.blueprints._automation import AutomationBlueprint, TriggerBlueprint
from awa.blueprints.components import edit_blueprint
from awa.blueprints.schema import Blueprint
from awa.errors import AwaError, ConfigurationError

KINDS = ("webhook", "schedule", "file-drop", "mail", "queue")
"""The kinds of trigger `trigger_entry` writes."""

_ALIASES = {"file_drop": "file-drop", "mailbox": "mail"}
"""The blueprint keys of two kinds, accepted as their names too."""

DEFAULT_PORT = 8000
"""The port `awa serve` listens on unless told otherwise."""

_SOURCE = {
    "webhook": "webhook",
    "schedule": "schedule",
    "file-drop": "file_drop",
    "mail": "mailbox",
    "queue": "queue",
}
_INPUT = {
    "webhook": "Handle this request: {payload}",
    "schedule": "Run the scheduled check for {payload.date}.",
    "file-drop": "Process the file {payload.name}:\n{payload.text}",
    "mail": "New email from {payload.from}: {payload.subject}\n{payload.text}",
    "queue": "Process this message: {payload}",
}


@dataclass(frozen=True)
class Step:
    """One thing to run or know: a title, the exact command (or text) and a note."""

    title: str
    command: str
    note: str = ""


def trigger_entry(
    kind: str,
    name: str | None = None,
    *,
    input: str | None = None,
    secret_env: str | None = None,
    scheme: str = "timestamped",
    cron: str | None = None,
    every: str | None = None,
    timezone: str | None = None,
    directory: str = "inbox",
    pattern: str = "*",
    host: str | None = None,
    username_env: str = "AWA_IMAP_USER",
    password_env: str = "AWA_IMAP_PASSWORD",
    folder: str = "INBOX",
    queue: str | None = None,
) -> dict[str, Any]:
    """A valid `triggers:` entry of `kind`; options of other kinds are ignored.

    Args:
        kind: One of `KINDS`; the blueprint keys `file_drop` and `mailbox` work too.
        input: What the system is asked to do; `{payload.x}` fills in the event's values.
        secret_env: Webhook: the variable holding the secret (default `<NAME>_SECRET`).
        cron: Schedule: a cron expression (default `0 7 * * MON-FRI` without `every`).
        directory: File drop: the folder to watch, relative to the blueprint.
        host: Mail: the IMAP server (required for `mail`).
        queue: Queue: the queue's name (default: the trigger's name).

    Raises:
        ConfigurationError: If `kind` is unknown or a setting is invalid.
    """

    kind = _ALIASES.get(kind, kind)
    if kind not in KINDS:
        raise ConfigurationError(f"Unknown trigger kind {kind!r}; use one of {', '.join(KINDS)}")
    name = name or kind
    settings: dict[str, Any]
    if kind == "webhook":
        settings = {"secret_env": secret_env or _env_name(name) + "_SECRET"}
        if scheme != "timestamped":
            settings["scheme"] = scheme
    elif kind == "schedule":
        settings = {"every": every} if every else {"cron": cron or "0 7 * * MON-FRI"}
        if timezone:
            settings["timezone"] = timezone
    elif kind == "file-drop":
        settings = {"directory": directory, "pattern": pattern}
    elif kind == "mail":
        if not host:
            raise ConfigurationError("A mail trigger needs the IMAP server (host)")
        settings = {"host": host, "username_env": username_env, "password_env": password_env}
        if folder != "INBOX":
            settings["folder"] = folder
    else:
        settings = {"name": queue or name}
    entry = {"name": name, _SOURCE[kind]: settings, "input": input or _INPUT[kind]}
    try:
        TriggerBlueprint.model_validate(entry)
    except ValueError as exc:
        raise ConfigurationError(f"Invalid {kind} trigger: {exc}") from None
    return entry


def add_trigger(
    path: str | os.PathLike[str], entry: dict[str, Any], *, dry_run: bool = False
) -> str:
    """Append `entry` to the `triggers:` of the blueprint file `path` and return the new text.

    The file keeps its comments and is written only when the result is valid and its triggers
    build (`dry_run` writes nothing).

    Raises:
        ConfigurationError: If the name is taken or the result has errors.
    """

    def append(data: dict[str, Any]) -> None:
        section = data.get("automation")
        owner = section if isinstance(section, dict) and section.get("triggers") else data
        if owner.get("triggers") is None:
            owner["triggers"] = []
        if any(isinstance(i, dict) and i.get("name") == entry["name"] for i in owner["triggers"]):
            raise ConfigurationError(f"{path} already has a trigger named {entry['name']!r}")
        owner["triggers"].append(entry)

    base = Path(path).resolve().parent
    return edit_blueprint(  # a bad cron expression fails here, not at 7 a.m.
        path, append, what="trigger", dry_run=dry_run, check=lambda changed: _build(changed, base)
    )


def check_trigger(entry: dict[str, Any], base_dir: str | os.PathLike[str] = ".") -> None:
    """Build `entry` as a real trigger; raises `ConfigurationError` if it is not valid."""

    try:
        blueprint = Blueprint.from_dict(
            {"name": "check", "agents": {"check": "Check."}, "triggers": [entry]}
        )
        _build(blueprint, Path(base_dir).resolve())
    except (ValueError, ConfigurationError) as exc:
        raise ConfigurationError(f"Invalid trigger {entry.get('name')!r}: {exc}") from None


def schedule_problems(section: AutomationBlueprint) -> list[tuple[int, str]]:
    """The schedule triggers of `section` that cannot be built, as `(index, why)`.

    A cron expression, time zone or interval that the schema accepts can still be refused
    by `Schedule`; this finds those before a worker starts.
    """

    from awa.automation.blueprint import build_triggers

    problems: list[tuple[int, str]] = []
    for index, item in enumerate(section.triggers):
        if item.schedule is None:
            continue
        one = section.model_copy(update={"triggers": [item.model_copy(update={"enabled": True})]})
        try:
            build_triggers(one)
        except AwaError as exc:
            problems.append((index, str(exc)))
    return problems


def _build(blueprint: Blueprint, base_dir: Path) -> None:
    from awa.automation.blueprint import build_triggers
    from awa.automation.queues import InMemoryEventQueue

    section = blueprint.automation_section()
    if section is not None:
        queues = {item.queue.name: InMemoryEventQueue() for item in section.triggers if item.queue}
        build_triggers(section, base_dir=base_dir, queues=queues)


def trigger_steps(
    blueprint: Blueprint,
    file: str = "blueprint.yaml",
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    only: str | None = None,
    payloads: Mapping[str, str] | None = None,
) -> list[Step]:
    """What to run for the triggers of `blueprint` (all, or the one named `only`); empty without triggers.

    The first step starts the automation; then, per trigger, what a sender does and a command
    that tries it with the payload `payloads` names (else one with the fields its input uses).
    """

    section = blueprint.automation_section()
    items = [t for t in (section.triggers if section else []) if only in (None, t.name)]
    if section is None or not items:
        return []
    automation = section.name or blueprint.name
    serve = any(item.webhook for item in section.triggers)
    steps = [
        Step(
            "Start it" + (" (receives the web requests and runs everything)" if serve else ""),
            f"awa {'serve' if serve else 'worker'} {file}",
            "It runs until you stop it with Ctrl+C. Runs that need an approval wait; answer "
            "them with `awa runs decide` or on the Approvals page of `awa ui`.",
        )
    ]
    for item in items:
        steps += _steps_for(item, automation, file, f"http://{host}:{port}")
        steps.append(_try_step(item, file, payloads))
    return steps


def _try_step(item: TriggerBlueprint, file: str, given: Mapping[str, str] | None) -> Step:
    """The command that fires `item` once with a sample event read from a JSON file.

    `--payload "@NAME.json"` because Windows PowerShell 5.1 strips the double quotes inside
    an argument such as `'{"x": 1}'`.
    """

    sample = _payload_example(item, given)
    try_it = "Shows the task the system would get; leave out --dry-run to run it once."
    if sample == "{}":
        return Step(
            f"Try {item.name} without waiting for a real event",
            f"awa trigger run {file} {item.name} --dry-run",
            try_it,
        )
    event = f"{item.name}.json"
    return Step(
        f"Try {item.name} without waiting for a real event",
        f'awa trigger run {file} {item.name} --payload "@{event}" --dry-run',
        f"First save a sample event as {event} in this folder, for example {sample}. {try_it}",
    )


def _steps_for(item: TriggerBlueprint, automation: str, file: str, base: str) -> list[Step]:
    if item.webhook is not None:
        hook = item.webhook
        url = f"{base}/v1/automations/{automation}/webhooks/{item.name}"
        secret = hook.secret_env or "(unsigned: for tests only)"
        sign = {
            "timestamped": "X-Awa-Timestamp: Unix seconds now; X-Awa-Signature: sha256= followed "
            f"by the hex HMAC-SHA256 of '<timestamp>.<body>' with the secret in {secret}",
            "body": "X-Awa-Signature: sha256= followed by the hex HMAC-SHA256 of the body with "
            f"the secret in {secret}",
            "token": f"X-Awa-Token: the secret in {secret} itself",
        }[hook.scheme]
        return [
            Step(
                f"{item.name}: send a POST request to",
                url,
                f"Headers: {sign}. Set {secret} in .env.",
            )
        ]
    if item.schedule is not None:
        when = item.schedule.cron or f"every {item.schedule.every}"
        zone = f" ({item.schedule.timezone})" if item.schedule.timezone else ""
        return [
            Step(
                f"{item.name}: runs by itself", when + zone, "Nothing to send; the worker fires it."
            )
        ]
    if item.file_drop is not None:
        drop = item.file_drop
        folder = Path(file).parent / drop.directory
        return [
            Step(
                f"{item.name}: drop files ({drop.pattern}) into",
                str(folder),
                f"Handled files move to {drop.processed_dir}/, refused ones to {drop.failed_dir}/.",
            )
        ]
    if item.mailbox is not None:
        mail = item.mailbox
        return [
            Step(
                f"{item.name}: send an email to the mailbox {mail.folder} on",
                mail.host,
                f"Set {mail.username_env} and {mail.password_env} in .env.",
            )
        ]
    assert item.queue is not None  # exactly one source is set
    return [
        Step(
            f"{item.name}: a message on the queue",
            item.queue.name,
            "With a database (--dsn) any program can add messages; without one, only Python "
            "code running in the same process.",
        )
    ]


def _payload_example(item: TriggerBlueprint, given: Mapping[str, str] | None) -> str:
    """A sample event (JSON text) for `awa trigger run`: the one given, else the fields the
    input mentions."""

    if given and item.name in given:
        return given[item.name]
    fields = sorted(set(re.findall(r"\{payload\.([A-Za-z_]\w*)", item.input or "")))
    return "{" + ", ".join(f'"{name}": "..."' for name in fields) + "}"


def _env_name(name: str) -> str:
    return re.sub(r"\W+", "_", name).strip("_").upper() or "TRIGGER"


def ask_trigger(
    ask: Callable[[str], str], out: TextIO, *, taken: Collection[str] = ()
) -> dict[str, Any] | None:
    """Ask for one trigger, kind first (Enter skips), then what that kind needs; `None` when none is wanted."""

    listing = "/".join(KINDS)
    kind = ask(f"What should start it? [{listing}] (Enter: nothing, start it by hand): ").strip()
    if not kind:
        return None
    kind = _ALIASES.get(kind, kind)
    if kind not in KINDS:
        out.write(f"  not one of {listing}\n")
        return ask_trigger(ask, out, taken=taken)

    def question(label: str, default: str | None = None) -> str:
        shown = f" [{default}]" if default else ""
        while True:
            answer = ask(f"  {label}{shown}: ").strip() or (default or "")
            if answer:
                return answer

    fresh = kind
    for number in range(2, 100):
        if fresh not in taken:
            break
        fresh = f"{kind}-{number}"
    name = question("Name of the trigger", fresh)
    options: dict[str, Any] = {}
    if kind == "webhook":
        options["scheme"] = question(
            "Requests prove who sent them by a signature (timestamped) or a secret header (token)",
            "timestamped",
        )
    elif kind == "schedule":
        when = question(
            "When (cron such as '0 7 * * MON-FRI', or an interval such as 15m)", "0 7 * * MON-FRI"
        )
        options["every" if re.fullmatch(r"\d+[smhd]", when) else "cron"] = when
    elif kind == "file-drop":
        options["directory"] = question("Folder to watch", "inbox")
        options["pattern"] = question("Which files (for example *.pdf)", "*")
    elif kind == "mail":
        options["host"] = question("IMAP server, for example imap.example.com")
        options["folder"] = question("Folder to read", "INBOX")
    else:
        options["queue"] = question("Queue name", name)
    task = ask("  What should the system do with each one? (Enter: a default text): ").strip()
    return trigger_entry(kind, name, input=task or None, **options)


__all__ = [
    "DEFAULT_PORT",
    "KINDS",
    "Step",
    "add_trigger",
    "ask_trigger",
    "check_trigger",
    "schedule_problems",
    "trigger_entry",
    "trigger_steps",
]
