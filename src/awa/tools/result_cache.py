"""Reuse the results of read-only tools: the same lookup in one run runs once.

Only tools declared read-only (`external_action` false), without approval and called with
arguments only are cached. When an acting tool runs, earlier results of the run are set aside.
Errors are never cached.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import itertools
import json
import os
import re
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator, Mapping
from pathlib import Path
from types import CodeType
from typing import Any

from awa._internal.upstream.agents import AgentMiddleware, ToolCallRequest
from awa._internal.upstream.graph import Command
from awa._internal.upstream.messages import ToolMessage
from awa._internal.upstream.tools import BaseTool
from awa.runtime.cache import Cache, cache_key, canonical_json, tenant_of
from awa.runtime.context import RunContext, current_run_context
from awa.runtime.ids import UuidIds
from awa.tools.risk import declared_tool_risk, infer_tool_risk

DATA_ROOT_METADATA_KEY = "awa_data_root"
"""Tool metadata key naming the folder a builtin file tool reads from."""

DATA_STATE_METADATA_KEY = "awa.data_state"
"""Run metadata key of a caller's data-state token (a version, an ETag, a change counter)."""

IDENTITY_METADATA_KEY = "awa_identity"
"""Tool metadata key: whose rights the tool's credentials carry (`user`, `app`), if known."""

_MAX_HASHED_BYTES = 256 * 1024 * 1024
"""Files larger than this make a call uncacheable (hashing would cost more than it saves)."""

_MAX_LISTED_ENTRIES = 5000
"""Folders with more entries than this make a call uncacheable."""

_RUN_SCOPE_TTL = 3600.0
"""Seconds a result cached for one run is kept at most (runs rarely last longer)."""

_SECRET_NAME = re.compile(
    r"secret|token|password|passwd|api_?key|credential|auth|cookie|session|private|dsn|conn",
    re.IGNORECASE,
)
_APPROVAL_GATE = "awa_approval_gate"
_MAX_RUNS = 4096
_PROCESS_KEY = UuidIds().new_id("cache-key-").encode("utf-8")  # random, never stored


def is_cacheable(tool: BaseTool) -> bool:
    """Whether results of `tool` may be reused (see the module docstring)."""

    metadata = tool.metadata or {}
    if metadata.get(_APPROVAL_GATE) or metadata.get("awa_requires_approval"):
        return False
    from awa.tools.decorator import tool_spec

    spec = tool_spec(tool)
    if spec is not None and spec.injects_context:
        return False
    declared = declared_tool_risk(tool)
    return declared is not None and declared.external_action is False


def tool_fingerprint(tool: BaseTool) -> str:
    """The version and configuration of `tool`, without secrets, as one digest."""

    schema: Any = None
    try:
        if isinstance(tool.args_schema, dict):
            schema = tool.args_schema
        elif tool.args_schema is not None:
            schema = tool.args_schema.model_json_schema()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - a schema that cannot render still has a name
        schema = repr(tool.args_schema)
    metadata = {
        key: value
        for key, value in (tool.metadata or {}).items()
        if not _SECRET_NAME.search(str(key))
    }
    functions = [getattr(tool, "func", None), getattr(tool, "coroutine", None)]
    return cache_key(
        tool.name,
        tool.description,
        schema,
        json.loads(canonical_json(metadata)),
        [_describe(f) for f in functions if f is not None],
        type(tool).__qualname__,
    )


class ToolResultCacheMiddleware(AgentMiddleware):
    """Answers repeated calls of read-only tools from the system's cache.

    The key covers the tenant, the tool's name, code, description and schema, its arguments, a
    secret-free configuration fingerprint and the data state (files read, acting tools run). Results
    live for the run, or `tools_ttl` seconds across runs keyed by the run's principal; a run without
    a principal does not share results of tools that sign in as a person. Async only: synchronous
    graph invocation raises `NotImplementedError`.

    Args:
        cache: The system's cache (its `tools` switch, store and `tools_ttl`).
        tools: The agent's tools; only those `is_cacheable` accepts are cached.
    """

    def __init__(self, cache: Cache, tools: list[BaseTool]) -> None:
        super().__init__()
        self._cache = cache
        self._fingerprints = {
            tool.name: tool_fingerprint(tool) for tool in tools if is_cacheable(tool)
        }
        self._tools = {tool.name: tool for tool in tools}

    async def _key(
        self, ctx: RunContext[Any], tool: BaseTool, args: Mapping[str, Any]
    ) -> str | None:
        files: str | None = ""
        root = (tool.metadata or {}).get(DATA_ROOT_METADATA_KEY)
        if isinstance(root, str) and root:
            # Hashing the named files can take long: it must not hold up the event loop.
            files = await asyncio.to_thread(_data_state, root, {**_schema_defaults(tool), **args})
            if files is None:
                return None
        ttl = self._cache.settings.tools_ttl
        tenant = tenant_of(ctx)
        principal = ctx.ids.actor or ""  # frozen at the start; metadata can be changed later
        if ttl and not principal and (tool.metadata or {}).get(IDENTITY_METADATA_KEY) == "user":
            ttl = None  # someone's own data, and nobody to tell whose: this run only
        acted = _ACTIONS.epoch(f"tenant:{tenant}" if ttl else f"run:{ctx.root_run_id}")
        return cache_key(
            tenant,
            tool.name,
            self._fingerprints[tool.name],
            json.loads(canonical_json(dict(args))),
            str(ctx.metadata.get(DATA_STATE_METADATA_KEY, "")),
            files,
            acted,
            f"principal:{principal}" if ttl else ctx.root_run_id,
        )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        ctx = current_run_context()
        call = request.tool_call
        name = call["name"]
        tool = request.tool or self._tools.get(name)
        if ctx is None or tool is None or not self._cache.uses("tools"):
            return await handler(request)
        if name not in self._fingerprints:
            try:
                return await handler(request)
            finally:
                if infer_tool_risk(tool).external_action:
                    _ACTIONS.acted(f"run:{ctx.root_run_id}", f"tenant:{tenant_of(ctx)}")
        key = await self._key(ctx, tool, call.get("args") or {})
        if key is None:
            return await handler(request)
        tenant = tenant_of(ctx)
        stored = await self._cache.aget("tools", tenant, key)
        if stored is not None:
            reused = _restore(stored, call)
            if reused is not None:
                return reused
        result = await handler(request)
        if isinstance(result, ToolMessage) and result.status != "error" and result.artifact is None:
            value = _store(result)
            if value is not None:
                ttl = self._cache.settings.tools_ttl or _RUN_SCOPE_TTL
                await self._cache.aput("tools", tenant, key, value, ttl=ttl)
                result.response_metadata = {**result.response_metadata, "awa_cache": "miss"}
        return result


def _schema_defaults(tool: BaseTool) -> dict[str, Any]:
    """The default arguments of `tool`, so a call that leaves a path out names its default."""

    fields = getattr(tool.args_schema, "model_fields", None)
    if not isinstance(fields, Mapping):
        return {}
    defaults = {name: getattr(field, "default", None) for name, field in fields.items()}
    return {
        name: value
        for name, value in defaults.items()
        if isinstance(value, str | list | tuple | dict)
    }


def _store(message: ToolMessage) -> bytes | None:
    try:
        return json.dumps({"content": message.content}, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return None


def _restore(value: bytes, call: Mapping[str, Any]) -> ToolMessage | None:
    try:
        content = json.loads(value.decode("utf-8"))["content"]
    except (ValueError, KeyError, UnicodeDecodeError):
        return None
    return ToolMessage(
        content=content,
        tool_call_id=call.get("id") or "",
        name=call["name"],
        response_metadata={"awa_cache": "hit"},
    )


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def _inside(root: Path, text: str) -> Path | None:
    if not text or len(text) > 1024 or "\0" in text:
        return None
    try:
        candidate = (root / text.replace("\\", "/")).resolve()
    except (OSError, ValueError):
        return None
    if candidate != root and root not in candidate.parents:
        return None
    return candidate if candidate.exists() else None


def _file_token(path: Path) -> str | None:
    stat = path.stat()
    if stat.st_size > _MAX_HASHED_BYTES:
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return f"{stat.st_size}:{stat.st_mtime_ns}:{digest.hexdigest()}"


def _folder_token(path: Path) -> str | None:
    entries: list[str] = []
    unreadable: list[OSError] = []
    for current, folders, files in os.walk(path, onerror=unreadable.append):
        folders.sort()
        entries.extend(f"{Path(current, name).relative_to(path)}/:dir" for name in folders)
        for name in sorted(files):
            stat = (Path(current) / name).stat()
            entries.append(
                f"{Path(current, name).relative_to(path)}:{stat.st_size}:{stat.st_mtime_ns}"
            )
            if len(entries) > _MAX_LISTED_ENTRIES:
                return None
    return None if unreadable else hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()


def _data_state(root: str, arguments: Mapping[str, Any]) -> str | None:
    """The token of the files and folders under `root` that `arguments` name.

    Every string argument that names an existing entry inside `root` adds that entry's state;
    when none does, the state of the root folder itself (a call that lists or searches it).
    `None` when an entry is too large, a folder too big, or reading fails.
    """

    try:
        base = Path(root).resolve()
        parts: list[str] = []
        for text in sorted(set(_strings(dict(arguments)))):
            path = _inside(base, text)
            if path is None:
                continue
            token = _folder_token(path) if path.is_dir() else _file_token(path)
            if token is None:
                return None
            parts.append(f"{path.relative_to(base) if path != base else '.'}={token}")
        if not parts:
            token = _folder_token(base) if base.is_dir() else None
            if token is None:
                return None
            parts.append(f".={token}")
    except OSError:
        return None
    return "|".join(parts)


def _keyed(text: str) -> str:
    """A digest of `text` that means nothing outside this process (no secret can be guessed)."""

    return hmac.new(_PROCESS_KEY, text.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


class _Actions:
    """How often acting tools ran per root run and tenant; eviction only turns hits into misses."""

    def __init__(self) -> None:
        self._last: OrderedDict[str, int] = OrderedDict()
        self._counter = itertools.count(1)
        self._floor = 0
        self._lock = threading.Lock()

    def acted(self, *scopes: str) -> None:
        with self._lock:
            for scope in scopes:
                self._last[scope] = next(self._counter)
                self._last.move_to_end(scope)
            while len(self._last) > _MAX_RUNS:
                _, number = self._last.popitem(last=False)
                self._floor = max(self._floor, number)

    def epoch(self, scope: str) -> int:
        with self._lock:
            return self._last.get(scope, self._floor)


_ACTIONS = _Actions()


def _scalar(value: Any) -> bool:
    return isinstance(value, str | int | float | bool) or value is None


def _describe(function: Any, depth: int = 0) -> list[Any]:
    """The code and secret-free closure settings of `function`, following wrapped functions."""

    code = getattr(function, "__code__", None)
    if code is None:
        return [type(function).__qualname__]
    consts = [c for c in code.co_consts if _scalar(c)]
    blob = code.co_code + repr(consts).encode("utf-8") + repr(code.co_names).encode("utf-8")
    described: list[Any] = [
        f"{getattr(function, '__module__', '')}.{code.co_qualname}",
        hashlib.sha256(blob).hexdigest()[:16],
    ]
    settings: dict[str, Any] = {}
    for name, value in _settings_of(function, code):
        if _SECRET_NAME.search(name):  # a secret or a connection factory: only a keyed digest
            settings[name] = _private(value, depth)
        elif _scalar(value):
            settings[name] = value
        elif hasattr(value, "__fspath__"):
            settings[name] = str(value)
        elif depth < 4 and isinstance(value, BaseTool):
            settings[name] = [
                _describe(f, depth + 1)
                for f in (getattr(value, "func", None), getattr(value, "coroutine", None))
                if f is not None
            ]
        elif depth < 4 and callable(value) and hasattr(value, "__code__"):
            settings[name] = _describe(value, depth + 1)
        elif isinstance(value, list | tuple | frozenset) and all(_scalar(v) for v in value):
            settings[name] = (
                sorted(map(repr, value)) if isinstance(value, frozenset) else list(value)
            )
    described.append(settings)
    return described


def _settings_of(function: Any, code: CodeType) -> list[tuple[str, Any]]:
    values: list[tuple[str, Any]] = []
    for name, cell in zip(
        code.co_freevars, getattr(function, "__closure__", None) or (), strict=False
    ):
        try:
            values.append((name, cell.cell_contents))
        except ValueError:  # an empty cell
            continue
    owner = getattr(function, "__self__", None)
    if owner is not None and hasattr(owner, "__dict__"):
        values += sorted(vars(owner).items())
    return values


def _private(value: Any, depth: int) -> str:
    if _scalar(value):
        return "private:" + _keyed(repr(value))
    if depth < 4 and callable(value) and hasattr(value, "__code__"):
        return "private:" + _keyed(canonical_json(_describe(value, depth + 1)))
    return "object:" + _keyed(str(id(value)))


__all__ = [
    "DATA_ROOT_METADATA_KEY",
    "DATA_STATE_METADATA_KEY",
    "IDENTITY_METADATA_KEY",
    "ToolResultCacheMiddleware",
    "is_cacheable",
    "tool_fingerprint",
]
