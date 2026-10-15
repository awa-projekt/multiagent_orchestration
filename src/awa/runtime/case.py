"""The folder of the eval case being run: `case_dir()` and `use_case_dir()`.

An evaluation gives every case a fresh copy of the example's folders, so cases never share a
file; code next to a blueprint asks `case_dir()` for the folder to use.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from pathlib import Path

from awa._internal.util import bound

_current: contextvars.ContextVar[Path | None] = contextvars.ContextVar("awa_case_dir", default=None)


def case_dir(default: str | Path | None = None) -> Path:
    """The folder of the running eval case, else `default`, else the working directory.

    Example:
        ```python
        def _out() -> Path:
            return case_dir(HERE) / "out"   # the case's copy in an eval, HERE/out otherwise
        ```
    """

    active = _current.get()
    if active is not None:
        return active
    return Path(default) if default is not None else Path.cwd()


def active_case_dir() -> Path | None:
    """The folder of the running eval case, or `None` outside one."""

    return _current.get()


def in_case_dir(base: Path, relative: str) -> Path:
    """`base / relative`, or the running case's copy of it when the case folder holds one."""

    active = _current.get()
    if active is not None and (active / relative).exists():
        return active / relative
    return base / relative


@contextlib.contextmanager
def use_case_dir(path: str | Path | None) -> Iterator[None]:
    """Make `path` the case folder inside the block (`None` leaves the current one)."""

    if path is None:
        yield
        return
    with bound(_current, Path(path)):
        yield


__all__ = ["active_case_dir", "case_dir", "in_case_dir", "use_case_dir"]
