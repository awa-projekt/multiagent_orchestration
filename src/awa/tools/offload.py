"""Tool layer profiles and offloading of large tool results.

In the `cost` profile a result above `max_output_chars` is saved aside in memory (never on disk)
and the model gets a preview and a reference to read on with `read_tool_result`; in `quality` it is
cut. A reference resolves only inside the run tree that saved it and not after a restart.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, Field

from awa._internal.upstream.agents import AgentMiddleware, ModelRequest, ModelResponse
from awa._internal.upstream.messages import ToolMessage
from awa._internal.upstream.tools import StructuredTool
from awa.runtime.context import current_run_context
from awa.tools.hardening import frame_tool_output
from awa.tools.risk import mark_tool_risk

READ_TOOL = "read_tool_result"
"""Name of the tool that reads an offloaded result."""

MAX_STORED_CHARS = 20_000_000
"""Characters of offloaded results kept in memory before the oldest are dropped."""

_PREVIEW_MIN = 200
_REF_KEY = secrets.token_bytes(16)
_SAVED_MARK = "the full result is saved as R-"
_Call = Callable[[ModelRequest[Any]], ModelResponse[Any]]
_AsyncCall = Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]]


@dataclass(frozen=True, slots=True)
class ToolProfile:
    """The defaults of the tool layer (`AWA_TOOL_PROFILE`; each value can be set on its own).

    | profile   | max_output_chars | editing trigger | keep | clear_at_least | large result |
    |-----------|------------------|-----------------|------|----------------|--------------|
    | `cost`    | 6,000            | 10,000 tokens   | 3    | 3,000 tokens   | offloaded    |
    | `quality` | 40,000           | 40,000 tokens   | 4    | 0              | cut          |
    """

    name: str
    max_output_chars: int
    offload: bool
    editing_trigger: int
    editing_keep: int
    clear_at_least: int


PROFILES: dict[str, ToolProfile] = {
    "cost": ToolProfile("cost", 6_000, True, 10_000, 3, 3_000),
    "quality": ToolProfile("quality", 40_000, False, 40_000, 4, 0),
}
"""The built-in profiles by name."""


def current_profile() -> ToolProfile:
    """The process settings' profile, with every explicitly set value applied."""

    from awa.core.settings import get_settings

    settings = get_settings()
    profile = PROFILES[settings.tool_profile]
    overrides: dict[str, Any] = {
        "max_output_chars": settings.tool_max_output_chars,
        "editing_trigger": settings.context_editing_trigger,
        "editing_keep": settings.context_editing_keep,
        "clear_at_least": settings.context_editing_clear_at_least,
    }
    return replace(profile, **{k: v for k, v in overrides.items() if v is not None})


def offload(
    text: str, *, tool: str, limit: int, source: str | None = None, framed: bool = False
) -> str:
    """`text` if it fits `limit`, else a note with a preview and a `read_tool_result` reference.

    With `framed`, note and preview are framed again within `limit`, and so is every chunk read
    later.
    """

    if len(text) <= limit:
        return text
    ref = _STORE.put(_Saved(text, tool, source, framed))
    head = (
        f"[Large result of {tool}: {len(text)} characters{_items(text)}. The first part is "
        f"shown; the full result is saved as {ref}. Read on with "
        f"{READ_TOOL}(ref='{ref}', start=END), or call {tool} again with a narrower filter, "
        "query or page. Note the facts you need from it: older results are cleared later.]\n"
    )

    def build(size: int) -> tuple[int, str]:
        end = _cut_end(text, size)
        body = f"{head}{text[:end]}\n… [shown 0-{end} of {len(text)}; END={end}]"
        return end, _frame(body, tool=tool, source=source, framed=framed)

    return _fit(limit, build)[1]


def cut(
    text: str, *, limit: int, tool: str = "", source: str | None = None, framed: bool = False
) -> str:
    """`text` cut at `limit` with a note asking for a narrower result; `framed` frames it again."""

    if len(text) <= limit:
        return text

    def build(size: int) -> tuple[int, str]:
        body = (
            f"{text[:size]}… [cut {len(text) - size} characters; ask for a narrower result, a "
            "page or a filter]"
        )
        return size, _frame(body, tool=tool, source=source, framed=framed)

    return build(limit)[1] if not framed else _fit(limit, build)[1]


def limit_output(
    text: str,
    *,
    tool: str,
    profile: ToolProfile | None = None,
    source: str | None = None,
    framed: bool = False,
) -> str:
    """`text` within the profile's limit: offloaded (`cost`) or cut (`quality`)."""

    profile = profile or current_profile()
    limit = profile.max_output_chars
    if profile.offload:
        return offload(text, tool=tool, limit=limit, source=source, framed=framed)
    return cut(text, limit=limit, tool=tool, source=source, framed=framed)


class OffloadMiddleware(AgentMiddleware):
    """Gives an agent the `read_tool_result` tool once a result was saved aside."""

    def __init__(self) -> None:
        super().__init__()
        reader = StructuredTool.from_function(
            func=_read,
            name=READ_TOOL,
            description=(
                "Read on in a large tool result that was saved aside: pass its reference and the "
                "start the previous part names."
            ),
            args_schema=_ReadToolResultArgs,
        )
        # The saved text is what another tool returned: label it as data like the original.
        flags = {"untrusted_input": True, "private_data": True, "external_action": False}
        self.tools = [mark_tool_risk(reader, **flags)]

    def wrap_model_call(self, request: ModelRequest[Any], handler: _Call) -> ModelResponse[Any]:
        """Hide `read_tool_result` until a result was saved aside."""

        return handler(_without_read_tool(request))

    async def awrap_model_call(
        self, request: ModelRequest[Any], handler: _AsyncCall
    ) -> ModelResponse[Any]:
        """Hide `read_tool_result` until a result was saved aside."""

        return await handler(_without_read_tool(request))


def _without_read_tool(request: ModelRequest[Any]) -> ModelRequest[Any]:
    if any(
        isinstance(m, ToolMessage) and isinstance(m.content, str) and _SAVED_MARK in m.content
        for m in request.messages
    ):
        return request
    tools = [t for t in request.tools if getattr(t, "name", None) != READ_TOOL]
    return request.override(tools=tools)


def _scope() -> tuple[str, str]:
    """The run tree that owns saved results: `(tenant, root run id)`, empty without a run."""

    ctx = current_run_context()
    return (ctx.ids.tenant, ctx.root_run_id) if ctx is not None else ("", "")


@dataclass(frozen=True, slots=True)
class _Saved:
    text: str
    tool: str
    source: str | None
    framed: bool


class _Store:
    """Offloaded results by scope and reference, bounded by total size (oldest dropped first)."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: OrderedDict[tuple[tuple[str, str], str], _Saved] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def put(self, saved: _Saved) -> str:
        scope = _scope()
        material = "\x1f".join(scope) + "\x00" + saved.text
        digest = hmac.new(_REF_KEY, material.encode("utf-8", "replace"), hashlib.sha256)
        ref = "R-" + digest.hexdigest()[:12]
        with self._lock:
            if (scope, ref) in self._items:
                self._items.move_to_end((scope, ref))
                return ref
            self._items[(scope, ref)] = saved
            self._size += len(saved.text)
            while self._size > self._limit and len(self._items) > 1:
                _, dropped = self._items.popitem(last=False)
                self._size -= len(dropped.text)
        return ref

    def get(self, ref: str) -> _Saved | None:
        with self._lock:
            return self._items.get((_scope(), ref))


_STORE = _Store(MAX_STORED_CHARS)


def _items(text: str) -> str:
    """`, N items` for a JSON array (or an object holding one), else `, N lines`."""

    stripped = text.lstrip()
    if stripped[:1] in "[{":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), None)
        if isinstance(data, list):
            return f", {len(data)} items"
    lines = text.count("\n") + 1
    return f", {lines} line" if lines == 1 else f", {lines} lines"


def _cut_end(text: str, size: int) -> int:
    if size >= len(text):
        return len(text)
    cut = text.rfind("\n", size // 2, size)
    return cut + 1 if cut >= 0 else size


def _fit(limit: int, build: Callable[[int], tuple[int, str]]) -> tuple[int, str]:
    """`build(size)` for the largest `size` (at least `_PREVIEW_MIN`) whose output fits `limit`."""

    size = limit
    while True:
        end, out = build(size)
        if len(out) <= limit or size <= _PREVIEW_MIN:
            return end, out
        size = max(_PREVIEW_MIN, size - (len(out) - limit))


def _frame(body: str, *, tool: str, source: str | None, framed: bool) -> str:
    return frame_tool_output(body, tool=tool, source=source) if framed else body


class _ReadToolResultArgs(BaseModel):
    ref: str = Field(description="The reference of the saved result, such as R-1a2b3c4d5e6f.")
    start: int = Field(default=0, ge=0, description="Character to start reading at.")


def _read(ref: str, start: int = 0) -> str:
    saved = _STORE.get(ref.strip())
    if saved is None:
        return (
            f"Error: no saved result {ref!r} in this process; call the original tool again "
            "with a narrower filter."
        )
    text = saved.text
    if start >= len(text):
        return f"Error: start {start} is past the end of {ref} ({len(text)} characters)."

    def build(size: int) -> tuple[int, str]:
        end = start + _cut_end(text[start:], size)
        note = (
            f"\n… [shown {start}-{end} of {len(text)}; read on with start={end}]"
            if end < len(text)
            else f"\n[end of {ref}]"
        )
        body = text[start:end] + note
        return end, _frame(body, tool=saved.tool, source=saved.source, framed=saved.framed)

    return _fit(current_profile().max_output_chars, build)[1]


__all__ = [
    "PROFILES",
    "READ_TOOL",
    "OffloadMiddleware",
    "ToolProfile",
    "current_profile",
    "cut",
    "limit_output",
    "offload",
]
