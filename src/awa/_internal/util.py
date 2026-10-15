"""Small helpers used across packages: call signatures, factories, lazy exports, previews,
registration messages, watcher lists, context variable scopes, file locks."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeAlias, TypeVar

from awa.errors import AdapterError

T = TypeVar("T")

_IDENTIFIER_PATH = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*"
_IMPORT_PATH = re.compile(rf"^{_IDENTIFIER_PATH}(?::{_IDENTIFIER_PATH})?$")

DurationLike: TypeAlias = timedelta | float | int | str
"""A duration: a `timedelta`, seconds, or text such as `"90s"`, `"15m"`, `"2h"`, `"3d"`, `"1w"`
or `"1d12h"`."""


_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d|w)")


def accepts_keyword(function: Callable[..., Any], name: str) -> bool:
    """Whether `function(name=...)` is accepted (a parameter `name`, or `**kwargs`)."""

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def importable(module_name: str) -> bool:
    """Whether `module_name` can be found, without importing it."""

    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


async def call_factory(value: Any, keep: type | tuple[type, ...]) -> Any:
    """`value` itself, or what it builds when it is a factory (sync or async).

    Anything callable that is not an instance of `keep` is called without arguments; the caller
    checks the type of the result.
    """

    if callable(value) and not isinstance(value, keep):
        value = value()
        if inspect.isawaitable(value):
            value = await value
    return value


def homes(modules: Mapping[str, str]) -> dict[str, str]:
    """Turn `{module: "Name other_name"}` into the `{name: module}` table of `lazy_attribute`."""

    return {name: module for module, names in modules.items() for name in names.split()}


def lazy_attribute(namespace: dict[str, Any], homes: Mapping[str, str], name: str) -> Any:
    """Import `name` for a package's PEP 562 `__getattr__` from its module in `homes`, and cache it."""

    package = namespace["__name__"]
    if name in homes:
        value = getattr(importlib.import_module(homes[name]), name)
    elif (
        "__path__" in namespace
        and not name.startswith("__")
        and importlib.util.find_spec(f"{package}.{name}") is not None
    ):
        value = importlib.import_module(f"{package}.{name}")
    else:
        raise AttributeError(f"module {package!r} has no attribute {name!r}")
    namespace[name] = value
    return value


def is_within(path: Path, root: Path) -> bool:
    """Whether `path` lies inside `root` (or is it) once resolved; an unresolvable path is not."""

    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):  # unreadable, or a link loop
        return False


def preview(text: str, limit: int = 200, *, flatten: bool = False, ellipsis: str = "...") -> str:
    """`text` cut to at most `limit` characters, ending in `ellipsis` when cut.

    With `flatten`, every run of whitespace first becomes one space.

    Example:
        ```python
        preview("a long answer", 8)  # 'a lon...'
        ```
    """

    if limit < len(ellipsis):
        raise ValueError("limit must be at least the length of the ellipsis")
    value = str(text)
    if flatten:
        value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - len(ellipsis)] + ellipsis


def name_taken(noun: str, name: str) -> str:
    """The message for registering a name that is in use, the same at every extension point."""

    return f"a {noun} named {name!r} is already registered; pass replace=True to replace it"


def elapsed_ms(started: float) -> int:
    """Milliseconds since `started`, a `time.perf_counter()` reading."""

    return int((time.perf_counter() - started) * 1000)


def add_callback(callbacks: list[T], callback: T) -> Callable[[], None]:
    """Append `callback` to `callbacks`; return a function that removes it again."""

    callbacks.append(callback)

    def remove() -> None:
        if callback in callbacks:
            callbacks.remove(callback)

    return remove


async def call_watchers(
    watchers: Sequence[Callable[[Any], Awaitable[None] | None]],
    value: Any,
    logger: logging.Logger,
    what: str,
    *args: Any,
) -> None:
    """Call every watcher (sync or async) with `value`; errors are logged as `what % args`."""

    for watcher in list(watchers):
        try:
            outcome = watcher(value)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:  # noqa: BLE001 - watchers must not fail the change they observe
            logger.exception(what, *args)


def _normalize_import_path(path: str) -> str:
    """`path` as `"module:attribute"`, a dotted path split at its longest importable module."""

    if not _IMPORT_PATH.fullmatch(path):
        raise AdapterError(
            f"Invalid import path {path!r}: expected 'package.module:attribute' or "
            "'package.module.Attribute'"
        )
    if ":" in path:
        return path
    parts = path.split(".")
    for split in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:split])
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            missing = exc.name
            if missing is not None and not (
                module_name == missing or module_name.startswith(f"{missing}.")
            ):
                # The module exists but one of its own imports is missing.
                raise AdapterError(f"Cannot import {path!r}: {exc}") from None
            continue
        except ImportError as exc:
            raise AdapterError(f"Cannot import {path!r}: {exc}") from None
        return f"{module_name}:{'.'.join(parts[split:])}"
    raise AdapterError(f"Cannot import {path!r}: no module {parts[0]!r}")


def import_path(path: str) -> Any:
    """Import the object an import path names; this runs the module's code, so pass only trusted paths.

    Raises:
        AdapterError: If the path is malformed or cannot be imported.

    Example:
        ```python
        factory = import_path("my_tools.github:make_tools")
        ```
    """

    module_name, _, attribute_path = _normalize_import_path(path).partition(":")
    try:
        target: Any = importlib.import_module(module_name)
        for attribute in attribute_path.split("."):
            target = getattr(target, attribute)
    except (ImportError, AttributeError) as exc:
        raise AdapterError(f"Cannot import {path!r}: {exc}") from None
    return target


def to_timedelta(value: DurationLike | None, *, what: str = "duration") -> timedelta | None:
    """Convert a `DurationLike` to a `timedelta` (`None` stays `None`)."""

    if value is None:
        return None
    if isinstance(value, timedelta):
        result = value
    elif isinstance(value, bool):
        raise ValueError(f"{what} must be a duration, got {value!r}")
    elif isinstance(value, int | float | str):
        seconds = _seconds(value, what) if isinstance(value, str) else float(value)
        try:
            result = timedelta(seconds=seconds)
        except OverflowError:
            raise ValueError(f"{what} is too long: {value!r}") from None
    else:
        raise ValueError(f"{what} must be a duration, got {type(value).__name__}")
    if result < timedelta(0):
        raise ValueError(f"{what} must not be negative")
    return result


def _seconds(value: str, what: str) -> float:
    """The seconds a duration text names: a number, or units such as `"1d12h"`."""

    text = value.strip().lower().replace(" ", "")
    if not text:
        raise ValueError(f"{what} must not be empty")
    try:
        return float(text)
    except ValueError:
        pass
    position = 0
    seconds = 0.0
    for match in _DURATION.finditer(text):
        if match.start() != position:
            break
        seconds += float(match.group(1)) * _UNITS[match.group(2)]
        position = match.end()
    if position != len(text):
        raise ValueError(
            f"{what} {value!r} is not a duration; use seconds or e.g. '90s', '15m', "
            "'2h', '3d', '1w'"
        )
    return seconds


@contextmanager
def bound(var: ContextVar[T], value: T) -> Iterator[T]:
    """Set `var` to `value` inside the block and restore the previous value afterwards."""

    token = var.set(value)
    try:
        yield value
    finally:
        var.reset(token)
