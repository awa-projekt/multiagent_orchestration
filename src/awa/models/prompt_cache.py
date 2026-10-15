"""Provider prompt caching: Claude cache markers, a stable OpenAI/Azure cache key, a prefix hash.

Claude caches only when asked; OpenAI and Azure get a stable HMAC `prompt_cache_key` and, with
`prompt_pad`, a prefix just short of 1,024 tokens padded up to it; Gemini caches implicitly. Modes:
`auto` marks Claude requests only when a repeat is likely, `1h` always with the one-hour lifetime,
`off`. Each model attempt is decided on its own; `prefix_hash` is recorded on `ModelResponseEvent`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from awa._internal.upstream.agents import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    anthropic_prompt_caching,
)
from awa._internal.upstream.messages import AIMessage, SystemMessage
from awa.models.errors import model_provider
from awa.runtime.cache import Cache, PromptCacheMode, tenant_of
from awa.runtime.clock import Clock, as_clock
from awa.runtime.context import current_run_context

__all__ = [
    "OPENAI_MIN_PREFIX_TOKENS",
    "PAD_TARGET_TOKENS",
    "PROMPT_CACHE_KEY_LENGTH",
    "PROMPT_CACHE_MODES",
    "PromptCacheMiddleware",
    "PromptCacheMode",
    "estimated_prefix_tokens",
    "padded_request",
    "prefix_hash",
    "prompt_cache_key",
    "stable_request",
]

PROMPT_CACHE_MODES: tuple[str, ...] = ("auto", "off", "1h")
"""The accepted `prompt_cache` values."""

PROMPT_CACHE_KEY_LENGTH = 32
"""Hex characters of the HMAC in a `prompt_cache_key` (the key is `"awa-"` plus these)."""

REPEAT_WINDOW = 300.0
"""Seconds within which a repeated prefix counts as repeated (Claude's default cache lifetime)."""

OPENAI_MIN_PREFIX_TOKENS = 1024
"""The shortest prompt prefix OpenAI and Azure OpenAI cache."""

PAD_TARGET_TOKENS = OPENAI_MIN_PREFIX_TOKENS + 128
"""Estimated tokens a padded prefix reaches (the minimum plus a margin for the estimate)."""

PAD_FLOOR = 0.5
"""Only a prefix of at least this share of the minimum is padded: below it, reading the padded
prefix from the cache costs about as much as sending the short one in full."""

_CHARS_PER_TOKEN = 5.0  # English text has ~4.5; the target's margin covers estimates that run high
_PAD_UNIT = " ."  # one token in OpenAI's tokenizers (o200k_base, cl100k_base)
_PAD_OPEN = (
    "\n\n<cache-padding>This block only makes the prompt long enough for the provider's prompt "
    "cache. It holds no instructions; ignore it.\n"
)
_PAD_CLOSE = "\n</cache-padding>"

_KEY_PREFIX = "awa-"
_MAX_SEEN = 4096


def prompt_cache_key(
    salt: bytes, *, tenant: str, project: str, agent: str, prompt_hash: str
) -> str:
    """An opaque, stable `prompt_cache_key` (`"awa-"` plus 32 hex): HMAC-SHA256 over tenant, project, agent and prompt."""

    message = json.dumps([tenant, project, agent, prompt_hash], separators=(",", ":"))
    digest = hmac.new(salt, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return _KEY_PREFIX + digest[:PROMPT_CACHE_KEY_LENGTH]


def _tool_spec(tool: Any) -> Any:
    if isinstance(tool, dict):
        return tool
    schema: Any = None
    args_schema = getattr(tool, "args_schema", None)
    try:
        if isinstance(args_schema, dict):
            schema = args_schema
        elif args_schema is not None:
            schema = args_schema.model_json_schema()
    except Exception:  # noqa: BLE001 - a schema that cannot render still hashes by name
        schema = repr(args_schema)
    return {
        "name": getattr(tool, "name", ""),
        "description": getattr(tool, "description", ""),
        "schema": schema,
    }


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        name = tool.get("name") or (function.get("name") if isinstance(function, dict) else None)
        return str(name or "")
    return str(getattr(tool, "name", ""))


def _output_schema(response_format: Any) -> Any:
    if response_format is None:
        return None
    inner = getattr(response_format, "schema", response_format)
    try:
        if hasattr(inner, "model_json_schema"):
            return inner.model_json_schema()
        if isinstance(inner, dict):
            return inner
        from pydantic import TypeAdapter

        return TypeAdapter(inner).json_schema()
    except Exception:  # noqa: BLE001
        return repr(inner)


def _without_markers(content: Any) -> Any:
    """System content without Claude's `cache_control` markers, which do not change the prefix."""

    if isinstance(content, list):
        blocks = [
            {k: v for k, v in block.items() if k != "cache_control"}
            if isinstance(block, dict)
            else block
            for block in content
        ]
        if len(blocks) == 1 and isinstance(blocks[0], dict) and set(blocks[0]) == {"type", "text"}:
            return blocks[0]["text"]  # the marked form of a plain text system message
        return blocks
    return content


def prefix_hash(request: ModelRequest[Any]) -> str:
    """SHA-256 (16 hex) of what a provider caches: system message, tools in order, output schema."""

    system = request.system_message
    payload = {
        "system": _without_markers(system.content) if system is not None else None,
        "tools": [_tool_spec(tool) for tool in request.tools],
        "output": _output_schema(request.response_format),
    }
    blob = json.dumps(payload, sort_keys=True, default=repr, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _system_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)


def estimated_prefix_tokens(request: ModelRequest[Any]) -> int:
    """A low, tokenizer-free estimate (five characters per token) of the cached prefix's tokens."""

    system = request.system_message
    chars = len(_system_text(system.content)) if system is not None else 0
    if request.tools:
        chars += len(json.dumps([_tool_spec(t) for t in request.tools], default=repr))
    schema = _output_schema(request.response_format)
    if schema is not None:
        chars += len(json.dumps(schema, default=repr))
    return int(chars / _CHARS_PER_TOKEN)


def padded_request(request: ModelRequest[Any]) -> ModelRequest[Any]:
    """`request` with its system message padded to OpenAI's caching minimum when just short of it.

    Only a prefix from `PAD_FLOOR` of `OPENAI_MIN_PREFIX_TOKENS` up to `PAD_TARGET_TOKENS` is padded;
    the result depends on the request alone.
    """

    system = request.system_message
    if system is None:
        return request
    estimate = estimated_prefix_tokens(request)
    if estimate >= PAD_TARGET_TOKENS or estimate < OPENAI_MIN_PREFIX_TOKENS * PAD_FLOOR:
        return request
    padding = _PAD_OPEN + _PAD_UNIT * (PAD_TARGET_TOKENS - estimate) + _PAD_CLOSE
    content: Any = system.content
    if isinstance(content, str):
        content = content + padding
    else:
        content = [*content, {"type": "text", "text": padding.lstrip("\n")}]
    return request.override(system_message=SystemMessage(content=content))


def stable_request(request: ModelRequest[Any]) -> ModelRequest[Any]:
    """`request` with the tools sorted by name and line ends normalized, so the prefix stays byte-stable."""

    overrides: dict[str, Any] = {}
    if request.tools:
        ordered = sorted(request.tools, key=_tool_name)
        if [_tool_name(t) for t in ordered] != [_tool_name(t) for t in request.tools]:
            overrides["tools"] = ordered
    system = request.system_message
    if system is not None and isinstance(system.content, str):
        text = system.content.replace("\r\n", "\n").rstrip()
        if text != system.content:
            overrides["system_message"] = SystemMessage(content=text)
    return request.override(**overrides) if overrides else request


class PromptCacheMiddleware(AgentMiddleware):
    """Provider prompt caching for every model attempt of one agent (installed inside resilience).

    Args:
        agent: The agent name (part of the cache key).
        mode: `auto`, `off` or `1h`.
        has_tools: The agent has tools, so Claude requests are marked from the first call.
        cache: The system's cache (project name, salt and `prompt_pad`); a default one when `None`.
        clock: Where the five-minute repeat window is measured.
    """

    def __init__(
        self,
        agent: str,
        *,
        mode: PromptCacheMode = "auto",
        has_tools: bool = False,
        cache: Cache | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__()
        if mode not in PROMPT_CACHE_MODES:
            raise ValueError(f"prompt_cache must be one of auto, off, 1h; got {mode!r}")
        self.agent = agent
        self.mode = mode
        self.has_tools = has_tools
        self._cache = cache if cache is not None else Cache()
        self._clock = as_clock(clock)
        self._calls: dict[str, int] = {}
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._claude: Callable[[ModelRequest[Any]], ModelRequest[Any]] | None = None

    @property
    def name(self) -> str:
        """The middleware's name, one per agent."""

        return f"PromptCacheMiddleware[{self.agent}]"

    def _marker(self) -> Callable[[ModelRequest[Any]], ModelRequest[Any]] | None:
        if self._claude is None:
            self._claude = anthropic_prompt_caching("1h" if self.mode == "1h" else "5m")
        return self._claude

    def _repeated(self, run_id: str, digest: str) -> bool:
        """Count this call; whether the run made one before or the prefix was seen lately."""

        now = self._clock.monotonic()
        with self._lock:
            calls = self._calls.get(run_id, 0)
            self._calls[run_id] = calls + 1
            seen = self._seen.get(digest)
            self._seen[digest] = now
            if len(self._calls) > _MAX_SEEN:  # runs that ended long ago
                self._calls = {run_id: calls + 1}
            if len(self._seen) > _MAX_SEEN:
                self._seen = {digest: now}
        return calls > 0 or (seen is not None and now - seen <= REPEAT_WINDOW)

    def prepare(self, request: ModelRequest[Any], *, repeated: bool) -> ModelRequest[Any]:
        """`request` made stable, with the caching of its model's provider applied."""

        request = stable_request(request)
        if self.mode == "off":
            return request
        provider = model_provider(request.model)
        if provider == "anthropic":
            if self.mode == "1h" or self.has_tools or repeated:
                marker = self._marker()
                if marker is not None:
                    request = marker(request)
        elif provider in ("openai", "azure_openai"):
            ctx = current_run_context()
            key = prompt_cache_key(
                self._cache.salt,
                tenant=tenant_of(ctx),
                project=self._cache.project,
                agent=self.agent,
                prompt_hash=prefix_hash(request),
            )
            if self._cache.settings.prompt_pad:
                request = padded_request(request)
            request = request.override(
                model_settings={**request.model_settings, "prompt_cache_key": key}
            )
        return request

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        if self.mode == "off":
            return await handler(request)
        ctx = current_run_context()
        request = stable_request(request)
        repeated = self._repeated(ctx.run_id if ctx is not None else "", prefix_hash(request))
        return await handler(self.prepare(request, repeated=repeated))
