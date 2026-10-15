"""The parser extension point: how a file kind gets profiled."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.util import find_spec

from awa._internal.util import name_taken
from awa.data._errors import DataError
from awa.data._model import FileProfile, Flag, Limits, Privacy, ProfileEvent, make_flag
from awa.data.ports import Parser
from awa.plugins import REGISTRY, KindView, current_project
from awa.runtime.clock import Clock, SystemClock

BUILTIN_MODULES = ("_tabular", "_excel", "_pdf", "_docx", "_text", "_mail", "_json", "_sqlite")
"""The modules that provide the built-in parsers; each lists its kinds in `KINDS`."""


class ParseTimeout(Exception):
    """Raised by `ParseContext.check_deadline` when a file takes longer than the limit."""


@dataclass
class ParseContext:
    """What a parser `profile_<kind>(path, ctx)` gets besides the path.

    A parser fills `profile` and returns it, never raises for bad content (`fail`), never executes
    anything from the file, stays inside `limits` (calling `check_deadline()` in long loops) and
    reports a missing optional library with `library_missing`.

    Attributes:
        profile: The file's profile, identity filled in.
        deadline: `clock.monotonic()` value after which parsing must stop, or `None`.
        child: Profiles an embedded file (mail attachment), see `child_profile`.
        embedded_budget: Embedded files still to profile, shared by a file and everything nested
            in it (`None`: the parser's own limit applies).
    """

    limits: Limits
    privacy: Privacy
    file_id: str
    profile: FileProfile
    deadline: float | None
    emit: Callable[[ProfileEvent], None]
    child: Callable[[str, bytes], FileProfile] | None = None
    clock: Clock = field(default_factory=SystemClock)
    embedded_budget: list[int] | None = None

    def table_id(self, number: int) -> str:
        """Id of the `number`-th table (1-based) of this file, for example `f1.t2`."""

        return f"{self.file_id}.t{number}"

    def check_deadline(self) -> None:
        """Raise `ParseTimeout` when the time limit of this file is used up."""

        if self.deadline is not None and self.clock.monotonic() > self.deadline:
            raise ParseTimeout(f"parsing took longer than {self.limits.parse_timeout_s:g} s")

    def progress(self, message: str) -> None:
        """Report a parse step (never put file content in the message)."""

        self.emit(ProfileEvent(file=self.file_id, stage="parse", message=message))

    def flag(self, code: str, message: str, where: str | None = None) -> Flag:
        """Add a flag to the file (severity from the code family) and return it."""

        flag = make_flag(code, message, where or self.file_id)
        self.profile.flags.append(flag)
        return flag

    def fail(self, code: str, message: str) -> FileProfile:
        """Mark the file as unreadable (`status: error`) with a `DF-E-...` flag; return it."""

        self.profile.status = "error"
        self.flag(code, message)
        return self.profile

    def library_missing(self, module: str, extra: str = "files") -> bool:
        """True (and the file marked `error` with an install hint) if `module` is not installed."""

        if find_spec(module) is not None:
            return False
        from awa._internal.install import install_hint

        self.fail(
            "DF-E-MISSING-EXTRA",
            f"reading {self.profile.kind} files needs the {extra!r} extra: {install_hint(extra)}",
        )
        return True

    def child_profile(self, name: str, data: bytes) -> FileProfile:
        """Profile an embedded file as `<file_id>.a<N>`, same limits, three levels deep at most."""

        if self.child is None:
            raise RuntimeError("this context cannot profile embedded files")
        return self.child(name, data)


PARSERS: MutableMapping[str, Parser] = KindView(REGISTRY, "parsers", current_project)
"""Registered parsers by file kind (built-in and installed ones are loaded on first use)."""


def register_parser(kind: str, parser: Parser, *, replace: bool = False) -> None:
    """Handle files of `kind` with `parser` for the rest of the process.

    An installed package registers one through the `awa.parsers` entry-point group (name = kind).

    Raises:
        DataError: If a parser is already registered for `kind` and `replace` is false.

    Example:
        ```python
        def profile_xls(path, ctx):
            ...  # fill ctx.profile with your own library
            return ctx.profile

        register_parser("xls", profile_xls)
        ```
    """

    if kind in PARSERS and not replace:
        raise DataError(name_taken("parser", kind))
    _put(kind, parser)


def unregister_parser(kind: str) -> None:
    """Stop reading files of `kind` (they are then profiled as `unsupported`).

    Raises:
        DataError: If no parser is registered for `kind`.
    """

    if kind not in PARSERS:
        raise DataError(f"no parser is registered for {kind!r}")
    del PARSERS[kind]
    _mark_python(kind, None)


def parser_for(kind: str) -> Parser | None:
    """The parser for `kind`, or `None` when unread or switched off in the blueprint."""

    from awa.plugins.switches import choice, replacement

    picked = choice(f"parser.{kind}")
    if picked.is_off:
        return None
    if (own := replacement(picked)) is not None:
        return own  # type: ignore[no-any-return]
    return PARSERS.get(kind)


@contextmanager
def parsers_override(parsers: Mapping[str, Parser]) -> Iterator[None]:
    """Use `parsers` (kind to parser) inside a `with` block and restore the registry afterwards.

    The registry is process-wide: do not use this while other threads profile files.
    """

    from awa.plugins.switches import python_entries, python_object, set_python

    switches = {
        key: (entry, python_object(key))
        for key, entry in python_entries().items()
        if key.startswith("parser.")
    }
    with REGISTRY.preserved():
        for kind, parser in parsers.items():
            _put(kind, parser)
        try:
            yield
        finally:
            for key in python_entries():
                if key.startswith("parser.") and key not in switches:
                    set_python(key, None)
            for key, (entry, obj) in switches.items():
                set_python(key, entry, obj)


def builtin_parsers() -> list[tuple[str, Parser, str]]:
    """The built-in parsers as `(kind, parser, summary)` rows, the provider of the registry."""

    found: list[tuple[str, Parser, str]] = []
    for name in BUILTIN_MODULES:
        full = f"awa.data.{name}"
        if find_spec(full) is None:
            continue
        module = importlib.import_module(full)
        found += [
            (kind, getattr(module, f"profile_{kind}"), f"Reads {kind} files.")
            for kind in module.KINDS
        ]
    return found


def _put(kind: str, parser: Parser) -> None:
    replaces = kind in PARSERS
    PARSERS[kind] = parser
    _mark_python(kind, "replaced" if replaces else "on")


def _mark_python(kind: str, state: str | None) -> None:
    """Tell the component switches that Python decided `kind` in this process (Python wins)."""

    from awa.plugins.switches import Entry, set_python

    key = f"parser.{kind}"
    if state is None:
        set_python(key, None)
    elif state == "replaced":
        set_python(key, Entry(use="python:register_parser"), PARSERS.get(kind))
    else:
        set_python(key, Entry(state="on"))
