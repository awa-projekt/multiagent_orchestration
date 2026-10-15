"""`.env` files: read them, update them atomically and look variables up."""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, MutableMapping
from pathlib import Path
from typing import Literal

from awa._internal.filelock import locked_file
from awa.core._envparse import ASSIGNMENT, parse_env_file
from awa.core._envparse import ENV_NAME as ENV_NAME

MAX_VALUE_CHARS = 8192
"""Longest value `write_env_file` accepts."""

PROJECT_DOTENV = ".env"
"""The `dotenv=` default of `Environment`: the project's `.env` file, found by
`awa.core.config` (the folder of the blueprint, `--project`, or the working directory)."""

_PLAIN_VALUE = re.compile(r"^[A-Za-z0-9_\-./:@+,=]*$")
_NOTE_PREFIX = "# "
_REPLACE_ATTEMPTS = 5


def read_env_file(path: str | Path) -> dict[str, str]:
    """The assignments of the `.env` file at `path` (`{}` if none); `NAME` or `NAME=` give `""`."""

    return parse_env_file(path)[0]


def export_env_file(
    path: str | Path, environ: MutableMapping[str, str], names: Iterable[str]
) -> list[str]:
    """Copy the values of `names` from the `.env` file at `path` into `environ`, where `environ`
    does not set them already; return the names copied (never the values)."""

    saved = read_env_file(path)
    copied = []
    for name in names:
        if saved.get(name) and not environ.get(name):
            environ[name] = saved[name]
            copied.append(name)
    return copied


def copy_env_file(source: str | Path, target: str | Path) -> None:
    """Copy the `.env` file `source` to the new file `target`, byte for byte and owner-only.

    Raises:
        FileExistsError: If `target` exists; it is never replaced.
    """

    data = Path(source).read_bytes()
    file = Path(target)
    descriptor = os.open(
        file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    restrict_to_owner(file)


class OverwriteRefused(ValueError):
    """`write_env_file(mode="merge")` would change the values of `names` already in the file."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        super().__init__(
            f"{', '.join(names)} already set in .env; confirm to overwrite "
            "(--yes, or confirm_overwrite=True)"
        )


def write_env_file(
    path: str | Path,
    values: Mapping[str, str],
    *,
    mode: Literal["replace", "merge"] = "replace",
    confirm_overwrite: bool = False,
    comments: Mapping[str, str] | None = None,
) -> list[str]:
    """Set `values` in the `.env` file at `path`, keeping every other line; return the names set.

    Existing assignments (also `export NAME=...`) are replaced in place, new names appended, blank
    values skipped. The file is replaced atomically, owner-only (0600; on Windows an ACL), and
    writers take turns through a lock file.

    Args:
        mode: `replace` overwrites without asking; `merge` needs `confirm_overwrite=True` to change
            a value, backs the file up to `.env.bak` first, and refuses a value with both `$` and a
            quote.
        confirm_overwrite: In `merge` mode, allow changing values already set; in either mode,
            allow replacing a set `AWA_PSEUDONYM_KEY`.
        comments: A comment line to put above a name's assignment, replacing an earlier one.

    Raises:
        OverwriteRefused: In `merge` mode, when a value would change without `confirm_overwrite`.
    """

    updates: dict[str, str] = {}
    for name, value in values.items():
        if not isinstance(name, str) or not ENV_NAME.match(name):
            raise ValueError(f"{name!r} is not a valid setting name (letters, digits and _)")
        if not isinstance(value, str) or any(c in value for c in "\r\n\0"):
            raise ValueError(f"The value for {name} must be a single line of text")
        if len(value) > MAX_VALUE_CHARS:
            raise ValueError(f"The value for {name} is longer than {MAX_VALUE_CHARS} characters")
        if value.strip():
            updates[name] = value.strip()
            if mode == "merge" and "$" in value and ("'" in value or "\\" in value):
                raise ValueError(
                    f"The value for {name} has both $ and a quote or backslash; it cannot be "
                    "written so that every reader takes it literally"
                )
    if not updates:
        return []
    file = Path(path)
    with locked_file(file):
        return _write_locked(file, updates, mode, confirm_overwrite, comments)


def _write_locked(
    file: Path,
    updates: dict[str, str],
    mode: Literal["replace", "merge"],
    confirm_overwrite: bool,
    comments: Mapping[str, str] | None,
) -> list[str]:
    """`write_env_file` once its lock is held: read, change and replace the file."""

    text = file.read_text(encoding="utf-8-sig") if file.is_file() else ""
    from awa.core.settings import PSEUDONYM_KEY_VARIABLE

    if not confirm_overwrite and PSEUDONYM_KEY_VARIABLE in updates:
        # Replacing the pseudonym key would cut every person off from their linked runs.
        if (read_env_file(file).get(PSEUDONYM_KEY_VARIABLE) or "").strip():
            updates.pop(PSEUDONYM_KEY_VARIABLE)
        if not updates:
            return []
    lines = text.splitlines()
    if mode == "merge":
        existing = read_env_file(file)
        changed = sorted(
            name for name, value in updates.items() if existing.get(name) not in (None, "", value)
        )
        if changed and not confirm_overwrite:
            raise OverwriteRefused(changed)
        if file.is_file():
            _write_private(file.with_name(f"{file.name}.bak"), text)
    notes = dict(comments or {})
    pending = dict(updates)
    output: list[str] = []
    for line in lines:
        match = ASSIGNMENT.match(line)
        if line.startswith(f"{_NOTE_PREFIX}awa:") and line.split()[-1] in notes:
            continue  # replaced by the new note above the assignment
        if match and match.group(1) in updates:
            name = match.group(1)
            # Every assignment of the name: the last one wins when the file is read.
            if name in notes:
                output.append(f"{_NOTE_PREFIX}{notes[name]}")
            output.append(_assignment(name, updates[name]))
            pending.pop(name, None)
        else:
            output.append(line)
    for name, value in pending.items():
        if name in notes:
            output.append(f"{_NOTE_PREFIX}{notes[name]}")
        output.append(_assignment(name, value))
    _write_private(file, "\n".join(output) + "\n")
    return sorted(updates)


def remove_env_names(path: str | Path, names: list[str]) -> list[str]:
    """Remove every assignment of `names` (and awa's note above it); return those that were set."""

    file = Path(path)
    if not file.is_file():
        return []
    with locked_file(file):
        return _remove_locked(file, set(names))


def _remove_locked(file: Path, wanted: set[str]) -> list[str]:
    if not file.is_file():
        return []
    lines = file.read_text(encoding="utf-8-sig").splitlines()
    kept: list[str] = []
    removed: set[str] = set()
    for line in lines:
        match = ASSIGNMENT.match(line)
        if match and match.group(1) in wanted:
            removed.add(match.group(1))
            continue
        if line.startswith(f"{_NOTE_PREFIX}awa:") and line.split()[-1] in wanted:
            continue
        kept.append(line)
    if removed:
        _write_private(file, "\n".join(kept) + ("\n" if kept else ""))
    return sorted(removed)


def _write_private(file: Path, text: str, *, pause: Callable[[float], None] = time.sleep) -> None:
    """Write `text` to `file` atomically, readable by the owner only."""

    temporary = file.with_name(f".{file.name}.tmp")
    # Created owner-only, so the secrets are never readable by others, not even briefly.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    restrict_to_owner(temporary)
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(temporary, file)
            return
        except PermissionError:  # another program (an editor, a virus scanner) holds it
            if attempt == _REPLACE_ATTEMPTS - 1:
                with contextlib.suppress(OSError):
                    temporary.unlink()
                raise PermissionError(
                    f"{file} is locked by another program; close it and try again"
                ) from None
            pause(0.1)


def restrict_to_owner(path: Path, environ: Mapping[str, str] | None = None) -> None:
    """Make `path` readable by its owner only: mode 0600, on Windows an ACL of the current user.

    On Windows this needs `icacls`; if it cannot run, the folder's permissions apply.
    """

    if os.name == "posix":
        path.chmod(0o600)  # a leftover temporary file keeps its old mode
        return
    env = os.environ if environ is None else environ
    user = env.get("USERNAME")
    if not user:
        return
    domain = env.get("USERDOMAIN")
    icacls = str(Path(env.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "icacls.exe")
    account = f"{domain}\\{user}" if domain else user
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        grant = [icacls, str(path), "/grant:r", f"{account}:(F)"]
        if subprocess.run(grant, capture_output=True, timeout=15, check=False).returncode == 0:  # noqa: S603 - a fixed system tool, no shell
            subprocess.run(  # noqa: S603
                [icacls, str(path), "/inheritance:r"], capture_output=True, timeout=15, check=False
            )


def _assignment(name: str, value: str) -> str:
    if _PLAIN_VALUE.match(value):
        return f"{name}={value}"
    if "'" not in value and "\\" not in value:  # a backslash is an escape in `.env` quotes
        return f"{name}='{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{name}="{escaped}"'


class Environment:
    """Looks up variables: `env` if given, else the process environment, then `dotenv`.

    The `.env` file is re-read when its modification time changes.

    Args:
        renames: Variable names replaced by others (`{"JIRA_URL": "JIRA2_URL"}`).
    """

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        dotenv: str | Path | None = ".env",
        renames: Mapping[str, str] | None = None,
    ) -> None:
        self._env = env
        if dotenv == PROJECT_DOTENV:
            # The project's `.env` (see `awa.core.config`), not one in the working directory.
            from awa.core.config import current

            dotenv = current().dotenv_path
        self._dotenv = Path(dotenv) if dotenv is not None else None
        self._renames = dict(renames or {})
        self._cache: tuple[float, dict[str, str]] | None = None

    def __repr__(self) -> str:
        return f"Environment(dotenv={str(self._dotenv) if self._dotenv else None!r})"

    def name(self, variable: str) -> str:
        """The name `variable` is read under (after renames)."""

        return self._renames.get(variable, variable)

    def get(self, variable: str) -> str | None:
        """The non-empty value of `variable`, or `None`."""

        name = self.name(variable)
        source = self._env if self._env is not None else os.environ
        value = source.get(name)
        if value is None or not str(value).strip():
            value = self._file().get(name)
        return value if value is not None and str(value).strip() else None

    def is_set(self, variable: str) -> bool:
        """Whether `variable` has a non-empty value."""

        return self.get(variable) is not None

    def _file(self) -> dict[str, str]:
        path = self._dotenv
        if path is None:
            return {}
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return {}
        if self._cache is None or self._cache[0] != stamp:
            self._cache = (stamp, read_env_file(path))
        return self._cache[1]
