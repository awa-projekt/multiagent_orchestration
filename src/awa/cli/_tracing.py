"""Langfuse tracing of the `awa` commands that run systems, switched on by
`AWA_LANGFUSE_TRACING=true`."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from awa.contracts.usage import PriceTable
    from awa.observability.langfuse import LangfuseConnection

TRACED_COMMANDS = frozenset(
    {"run", "chat", "serve", "worker", "trigger", "eval", "ui", "mcp", "build", "data"}
)
"""The commands whose runs `AWA_LANGFUSE_TRACING` sends to Langfuse."""

_active: ContextVar[LangfuseConnection | None] = ContextVar("awa_langfuse_connection", default=None)


def active_connection() -> LangfuseConnection | None:
    """The Langfuse connection of the running command, `None` when it is not traced."""

    return _active.get()


@contextmanager
def langfuse_tracing(args: Any) -> Iterator[None]:
    """Send the runs of the command `args` names to Langfuse when the setting is on.

    When tracing cannot start, one warning says why and the command runs without it.

    Raises:
        ConfigurationError: Tracing cannot start and `AWA_LANGFUSE_STRICT` is on.
        OptionalDependencyError: The extra `langfuse` is missing and `AWA_LANGFUSE_STRICT` is on.
    """

    command = getattr(args, "command", None)
    if command not in TRACED_COMMANDS:
        yield
        return
    from awa.core.settings import Settings
    from awa.errors import AwaError

    try:
        settings = Settings()
    except Exception:  # noqa: BLE001 - the command itself reports invalid settings
        yield
        return
    if not settings.langfuse_tracing:
        yield
        return
    from awa.plugins.switches import is_off

    if is_off("tracer.langfuse"):
        print(
            "tracing to Langfuse: off (tracer.langfuse is switched off for this project)",
            file=sys.stderr,
        )
        yield
        return
    from awa.observability.langfuse import connect_langfuse
    from awa.runtime import subscribe_globally

    metadata: dict[str, str | bool | int | float] = {"command": str(command)}
    tags = ["awa", f"awa {command}"]
    name, version, prices = _blueprint_facts(getattr(args, "blueprint", None))
    if name is not None:
        metadata["blueprint"] = name
        tags.append(f"blueprint:{name}")
    try:
        connection = connect_langfuse(
            public_key=_secret(settings.langfuse_public_key),
            secret_key=_secret(settings.langfuse_secret_key),
            base_url=settings.langfuse_base_url,
            capture_content=settings.langfuse_capture_content,
            tags=tags,
            metadata=metadata,
            release=settings.langfuse_release,
            environment=settings.langfuse_environment,
            version=version,
            prices=prices,
        )
    except AwaError as exc:
        if settings.langfuse_strict:
            raise
        print(
            f"warning: Langfuse tracing is off for this command: {exc} "
            "(set AWA_LANGFUSE_STRICT=true to stop instead)",
            file=sys.stderr,
        )
        yield
        return
    unsubscribe = subscribe_globally(connection.subscriber)
    token = _active.set(connection)
    print(f"tracing to Langfuse: {connection.endpoint}", file=sys.stderr)
    try:
        yield
    finally:
        _active.reset(token)
        unsubscribe()
        connection.shutdown()


def _secret(value: Any) -> str | None:
    return None if value is None else str(value.get_secret_value()) or None


def _blueprint_facts(path: Any) -> tuple[str | None, str | None, PriceTable | None]:
    """The `name:`, `version:` and `prices:` of the blueprint file `path` (each `None` when it
    cannot be read; the command itself reports a broken blueprint)."""

    import yaml

    from awa.contracts.usage import PriceTable

    if not isinstance(path, str):
        return None, None, None
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None, None, None
    if not isinstance(data, dict):
        return None, None, None
    name = data.get("name")
    version = data.get("version")
    prices: PriceTable | None = None
    if isinstance(data.get("prices"), dict) and data["prices"]:
        try:
            prices = PriceTable.from_mapping(data["prices"], source="prices")
        except Exception:  # noqa: BLE001 - invalid prices are reported by the command
            prices = None
    return (
        name if isinstance(name, str) and name else None,
        str(version) if isinstance(version, (str, int, float)) and str(version) else None,
        prices,
    )
