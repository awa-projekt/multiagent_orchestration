"""`awa people search|pin|list|check|unpin`: find colleagues and pin them to the project by role."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from awa.cli._common import EXIT_ERROR, EXIT_OK, Commands, add_command, print_table

if TYPE_CHECKING:
    from awa.people import PeopleDirectory, PersonRef

LOCAL_LIST = "config/people.csv"
"""The local people list `--local` reads when no `--source` is given."""
STALE_DAYS = 180
"""A pinned person not confirmed for this many days is named by `awa people check`."""


def add_people(commands: Commands) -> None:
    people = add_command(commands, "people")
    actions = people.add_subparsers(dest="people_command", required=True, metavar="action")

    def common(parser: argparse.ArgumentParser, *, source: bool = True) -> None:
        parser.add_argument(
            "--project",
            help="the project folder (default: `awa --project`, else the nearest project folder)",
        )
        if source:
            parser.add_argument(
                "--local",
                action="store_true",
                help=f"use a local list instead of Entra ID ({LOCAL_LIST})",
            )
            parser.add_argument("--source", help="the local list (CSV or YAML) for --local")

    search = actions.add_parser("search", help="find people by name (nothing is stored)")
    search.add_argument("text", help="words of the name or the start of the mail address")
    search.add_argument("--guests", action="store_true", help="also show guest accounts")
    search.add_argument("--limit", type=int, default=10, help="at most this many (10 at most)")
    common(search)
    search.set_defaults(handler=_search)

    pin = actions.add_parser("pin", help="store a person in config/people.yaml, with roles")
    pin.add_argument(
        "person", help="words of the name (as for search), an object id or name@company"
    )
    pin.add_argument("--role", action="append", default=[], help="a role the person holds")
    pin.add_argument("--pick", type=int, help="the number of the search result to pin")
    pin.add_argument("--guests", action="store_true", help="allow pinning a guest account")
    common(pin)
    pin.set_defaults(handler=_pin)

    listing = actions.add_parser("list", help="the pinned people and their roles")
    common(listing, source=False)
    listing.set_defaults(handler=_list)

    check = actions.add_parser("check", help="ask the directory whether pinned people still exist")
    common(check)
    check.set_defaults(handler=_check)

    unpin = actions.add_parser("unpin", help="remove a person and all their roles")
    unpin.add_argument("key", help="the key shown by awa people list")
    common(unpin, source=False)
    unpin.set_defaults(handler=_unpin)


def _project(args: argparse.Namespace) -> Path:
    """The project folder: this command's `--project`, else `awa --project`, else the nearest
    project folder (as every other command finds it)."""

    from awa.core.config import find_project

    return find_project(project=args.project or getattr(args, "project_folder", None))


def _entra(args: argparse.Namespace) -> PeopleDirectory:
    from awa.people import EntraPeople

    return EntraPeople(dotenv=str(_project(args) / ".env"))


def _local(args: argparse.Namespace) -> PeopleDirectory:
    from awa.people import LocalPeople

    return LocalPeople(Path(args.source) if args.source else _project(args) / LOCAL_LIST)


def _directory(args: argparse.Namespace) -> PeopleDirectory:
    return _local(args) if args.local or args.source else _entra(args)


def _rows(found: list[PersonRef]) -> list[tuple[str, ...]]:
    header: list[tuple[str, ...]] = [("#", "name", "mail", "account")]
    return header + [
        (str(n), p.name, p.mail or "(no mail)", "guest" if p.kind == "guest" else "")
        for n, p in enumerate(found, start=1)
    ]


def _search(args: argparse.Namespace) -> int:
    found = _directory(args).search(args.text, limit=args.limit, guests=args.guests)
    if not found:
        print("Nobody found. Type the beginning of a first or last name (at least 2 letters).")
        return EXIT_OK
    print_table(_rows(found))
    print(
        f'\nNothing was stored. Pin one with: awa people pin "{args.text}" --pick <#> --role <role>'
    )
    return EXIT_OK


def _choose(args: argparse.Namespace, directory: PeopleDirectory) -> PersonRef | None:
    from awa.people import EntraPeople, PeopleError
    from awa.people._ref import is_guid

    text = str(args.person).strip()
    if isinstance(directory, EntraPeople) and (is_guid(text) or "@" in text):
        return directory.lookup(text)
    found = directory.search(text, guests=args.guests)
    if not found:
        raise PeopleError(f"Nobody matches {text!r}; try awa people search with other words.")
    if args.pick is not None:
        if not 1 <= args.pick <= len(found):
            raise PeopleError(f"--pick must be between 1 and {len(found)}.")
        chosen: PersonRef = found[int(args.pick) - 1]
        return chosen
    if len(found) == 1:
        return found[0]
    print_table(_rows(found))
    print(f'\n{len(found)} people match. Choose one: awa people pin "{text}" --pick <#> ...')
    return None


def _pin(args: argparse.Namespace) -> int:
    from awa.people import PeopleError, PeopleFile, ensure_gitignored

    directory = _directory(args)
    person = _choose(args, directory)
    if person is None:
        return EXIT_ERROR
    if person.kind == "guest" and not args.guests:
        raise PeopleError(f"{person.name} is a guest account; add --guests to pin a guest.")
    if not person.usable:
        raise PeopleError(f"{person.name} is {person.state} in the directory and cannot be pinned.")
    file = PeopleFile.of_project(_project(args))
    book = file.load()
    key = book.pin(person, args.role)
    ignored = ensure_gitignored(_project(args))
    file.save(book)
    roles = ", ".join(book.roles_of(key)) or "none yet: add --role"
    print(f"Pinned {person.name} as {key} (roles: {roles}) in {file.path}.")
    if ignored:
        print("Added config/people.yaml to .gitignore: it holds names and mail addresses.")
    return EXIT_OK


def _list(args: argparse.Namespace) -> int:
    from awa.people import PeopleFile

    book = PeopleFile.of_project(_project(args)).load()
    if not book.people:
        print('Nobody is pinned yet. Find people with: awa people search "<name>"')
        return EXIT_OK
    print_table(
        [("key", "name", "mail", "from", "state", "checked", "roles")]
        + [
            (
                key,
                p.name,
                p.mail or "",
                p.source + (" (guest)" if p.kind == "guest" else ""),
                p.state,
                (p.checked_at or "never")[:10],
                ", ".join(book.roles_of(key)),
            )
            for key, p in sorted(book.people.items())
        ]
    )
    return EXIT_OK


def _stale(person: PersonRef, now: datetime) -> bool:
    if not person.checked_at:
        return True
    try:
        checked = datetime.fromisoformat(person.checked_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    checked = checked if checked.tzinfo else checked.replace(tzinfo=UTC)
    return now - checked > timedelta(days=STALE_DAYS)


def _check(args: argparse.Namespace) -> int:
    from awa.people import PeopleError, PeopleFile

    file = PeopleFile.of_project(_project(args))
    book = file.load()
    if not book.people:
        print("Nobody is pinned; nothing to check.")
        return EXIT_OK
    local = args.local or bool(args.source)
    directories: dict[str, PeopleDirectory] = {}
    rows: list[tuple[str, ...]] = [("key", "name", "state", "note")]
    problems = 0
    now = datetime.now(UTC)
    for key, person in sorted(book.people.items()):
        note = ""
        if person.source == "local" and not local:
            fresh = person
            note = "local: add --local to check against the list"
        else:
            if person.source not in directories:
                directories[person.source] = (
                    _local(args) if person.source == "local" else _entra(args)
                )
            try:
                fresh = directories[person.source].get(person)
            except PeopleError as exc:
                fresh, note = person, f"not checked: {exc}"
        if fresh.mail != person.mail:
            note = note or "mail address changed; updated"
        if not fresh.usable:
            problems += 1
            gone = "no longer exists" if fresh.state == "deleted" else "is disabled"
            note = f"{fresh.name} {gone} in the directory: left out of every role; pin someone else"
        elif _stale(fresh, now):
            note = note or f"not confirmed for {STALE_DAYS} days"
        book.replace(fresh)
        rows.append((key, fresh.name, fresh.state, note))
    file.save(book)
    print_table(rows)
    if problems:
        print(f"\n{problems} pinned person(s) cannot hold a role any more.")
        return EXIT_ERROR
    return EXIT_OK


def _unpin(args: argparse.Namespace) -> int:
    from awa.people import PeopleFile

    file = PeopleFile.of_project(_project(args))
    book = file.load()
    person = book.unpin(args.key)
    file.save(book)
    print(f"Unpinned {person.name}: the entry and all its roles are deleted from {file.path}.")
    return EXIT_OK
