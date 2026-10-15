"""Context compaction: summarize older turns when a conversation outgrows its budget.

Summary calls are model requests of the run: they count in `RunResult.usage`, are refused while
real model requests are blocked, and are held to the run's `UsageLimits`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from awa._internal.retry import backoff_delay, retry_on_transient
from awa._internal.upstream.agents import (
    DEFAULT_SUMMARY_PROMPT,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    SummarizationMiddleware,
)
from awa._internal.upstream.graph import REMOVE_ALL_MESSAGES, Command, Runtime
from awa._internal.upstream.messages import AIMessage, AnyMessage, RemoveMessage
from awa._internal.upstream.models import BaseChatModel, LanguageModelInput
from awa._internal.upstream.runnables import RunnableConfig, RunnableLambda
from awa.errors import AwaError, ModelProviderError
from awa.memory.long_term import RECALLED_MEMORIES_ID_PREFIX
from awa.models.errors import classify_model_error, model_name
from awa.models.guard import ensure_model_request_allowed
from awa.runtime.context import current_run_context
from awa.runtime.engine import current_engine

SUMMARY_ATTEMPTS = 3
"""Attempts per summary before its error fails the run (each attempt counts as a request)."""

CompactionLog = dict[str, list[tuple[int, int]]]
"""Per run id: `(messages replaced, summary messages inserted)` for each compaction."""


@dataclass(frozen=True, slots=True)
class Compaction:
    """When and how an agent summarizes older conversation turns.

    Before a model call over `max_tokens`, every message but the last `keep_last_messages` becomes
    one summary; a tool call is never cut from its results. With a checkpointer the thread keeps the
    compacted conversation. A provider context overflow triggers one immediate summary and retry.

    Attributes:
        max_tokens: Approximate conversation size in tokens that triggers a summary.
        keep_last_messages: Most recent messages kept verbatim.
        model: Model writing the summary (instance, profile name or `provider:model`); `None` uses
            the agent's own model.
        summary_prompt: Must contain `{messages}` and no other placeholder (literal braces as `{{`
            and `}}`); defaults to LangChain's summarization prompt.
        trim_tokens_to_summarize: Upper bound on the older turns sent to the summary model.

    Raises:
        ValueError: If `max_tokens` or `keep_last_messages` is below 1, or `summary_prompt` lacks
            `{messages}` or has other placeholders.

    Example:
        ```python
        agent = Agent(
            "analyst",
            checkpointer=StateInMemorySaver(),
            compaction=Compaction(max_tokens=12_000, keep_last_messages=12),
        )
        ```
    """

    max_tokens: int = 8000
    keep_last_messages: int = 20
    model: str | BaseChatModel | None = None
    summary_prompt: str | None = None
    trim_tokens_to_summarize: int | None = 4000

    def __post_init__(self) -> None:
        if self.max_tokens < 1:
            raise ValueError(f"max_tokens must be at least 1, got {self.max_tokens}")
        if self.keep_last_messages < 1:
            raise ValueError(
                f"keep_last_messages must be at least 1, got {self.keep_last_messages}"
            )
        if self.summary_prompt is not None:
            if "{messages}" not in self.summary_prompt:
                raise ValueError("summary_prompt must contain the {messages} placeholder")
            try:
                # Formatted mid-run before every summary: fail here instead.
                self.summary_prompt.format(messages="")
            except (KeyError, IndexError, ValueError) as exc:
                raise ValueError(
                    "summary_prompt may only contain the {messages} placeholder; write "
                    f"literal braces as '{{{{' and '}}}}' ({type(exc).__name__}: {exc})"
                ) from None

    def middleware(
        self, model: BaseChatModel, *, log: CompactionLog | None = None
    ) -> CompactionMiddleware:
        """Build the middleware summarizing with `model`; `log` records compactions per run id."""

        return CompactionMiddleware(
            model,
            trigger=("tokens", self.max_tokens),
            keep=("messages", self.keep_last_messages),
            summary_prompt=self.summary_prompt or DEFAULT_SUMMARY_PROMPT,
            trim_tokens_to_summarize=self.trim_tokens_to_summarize,
            log=log,
        )


class CompactionMiddleware(SummarizationMiddleware):
    """`SummarizationMiddleware` whose summary calls are model requests of the run.

    Each attempt passes the model-request guard, counts under the running agent and the summary
    model, and is held to `UsageLimits` (`max_requests` before, tokens and cost after). Transient
    failures are retried up to `SUMMARY_ATTEMPTS` times in total; awa errors never are.
    """

    retry_delay: float = 1.0
    """Seconds before the second attempt; doubled for each further one (with jitter)."""

    def __init__(
        self, model: BaseChatModel, *, log: CompactionLog | None = None, **kwargs: Any
    ) -> None:
        super().__init__(model, **kwargs)
        self._log = log
        # Replaces the plain `model.with_retry()` of the base class, which would
        # bypass the run's accounting and limits.
        self._summary_model = RunnableLambda(self._summarize)

    async def _summarize(self, prompt: LanguageModelInput, config: RunnableConfig) -> AIMessage:
        for attempt in range(1, SUMMARY_ATTEMPTS):
            try:
                return await self._attempt(prompt, config)
            except (AwaError,) + current_engine().control_flow:
                raise
            except Exception as error:
                if not retry_on_transient(error):
                    raise
                await asyncio.sleep(
                    backoff_delay(attempt - 1, initial=self.retry_delay, jitter=0.5)
                )
        return await self._attempt(prompt, config)

    async def _attempt(self, prompt: LanguageModelInput, config: RunnableConfig) -> AIMessage:
        ensure_model_request_allowed(self.model)
        ctx = current_run_context()
        if ctx is None:
            return await self.model.ainvoke(prompt, config=config)
        ctx.admit_request()
        try:
            response = await self.model.ainvoke(prompt, config=config)
        except current_engine().control_flow:
            raise
        except Exception:
            # A failed attempt was still a request to the provider.
            ctx.usage.record_model_call(ctx.agent, model=model_name(self.model))
            raise
        finally:
            ctx.usage.request_ended()
        usage = response.usage_metadata
        reported = response.response_metadata.get("model_name") or response.response_metadata.get(
            "model"
        )
        # Keyed by the configured model, like failed attempts (see RunTrackingMiddleware).
        ctx.usage.record_usage_metadata(
            ctx.agent,
            dict(usage) if usage else None,
            model=model_name(self.model) or (str(reported) if reported else None),
        )
        ctx.check_limits()
        return response

    async def abefore_model(self, state: Any, runtime: Runtime[Any]) -> dict[str, Any] | None:
        """Summarize old messages when needed and log the compaction under the run id."""

        before: list[AnyMessage] = list(state["messages"])
        update = await super().abefore_model(state, runtime)
        if update is not None:
            known = {id(message) for message in before}
            inserted = update["messages"][1:]  # after RemoveMessage(REMOVE_ALL_MESSAGES)
            kept = sum(1 for message in inserted if id(message) in known)
            self._record_compaction(len(before) - kept, len(inserted) - kept)
        return update

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | ExtendedModelResponse[Any]:
        """Summarize and retry once when the provider reports a context overflow.

        The older turns are summarized at once (the regular cut, or half the conversation when that
        keeps nothing); a second overflow, or one with nothing to summarize, is raised. Messages
        only the request carries (recalled memories, those other middleware added) are sent
        again but never saved; the kept turns are saved as the thread holds them.
        """

        try:
            return await handler(request)
        except Exception as error:
            if not _is_context_overflow(error):
                raise
            recalled = [m for m in request.messages if _recalled(m)]
            messages = [m for m in request.messages if not _recalled(m)]
            self._ensure_message_ids(messages)
            cutoff = self._overflow_cutoff(messages)
            if cutoff <= 0:
                raise
            older, preserved = self._partition_messages(messages, cutoff)
            summary = self._build_new_messages(await self._acreate_summary(older))
            retry = [*summary, *recalled, *preserved]
            response = await handler(request.override(messages=retry))
        self._record_compaction(cutoff, len(summary))
        saved = _saved(request, preserved)
        return ExtendedModelResponse(
            model_response=response,
            command=Command(update={"messages": self._replacement(summary, saved, response)}),
        )

    def _overflow_cutoff(self, messages: list[AnyMessage]) -> int:
        cutoff = self._determine_cutoff_index(messages)
        if cutoff <= 0 and len(messages) > 1:
            cutoff = self._find_safe_cutoff(messages, len(messages) // 2)
        return cutoff

    def _record_compaction(self, replaced: int, inserted: int) -> None:
        ctx = current_run_context()
        if self._log is not None and ctx is not None:
            self._log.setdefault(ctx.run_id, []).append((replaced, inserted))

    @staticmethod
    def _replacement(
        summary: list[Any], preserved: list[AnyMessage], response: ModelResponse[Any]
    ) -> list[Any]:
        # The agent writes the model's reply before middleware commands (which gives
        # it an id), so the replacement repeats it after the summary and kept turns.
        reply = list(response.result)
        return [RemoveMessage(id=REMOVE_ALL_MESSAGES), *summary, *preserved, *reply]


def overflow_middleware(
    model: BaseChatModel, *, log: CompactionLog | None = None
) -> CompactionMiddleware:
    """A `CompactionMiddleware` for agents without `Compaction`: acts only on a context overflow."""

    return CompactionMiddleware(model, keep=("messages", 20), log=log)


def shift_start(start: int, compactions: list[tuple[int, int]]) -> int:
    """Map the index of a run's first new message across `(replaced, inserted)` compactions.

    Returns:
        The first surviving message after `start`, or the first kept message when new messages
        were summarized too.
    """

    for replaced, inserted in compactions:
        start = start - replaced + inserted if start >= replaced else inserted
    return start


def _recalled(message: AnyMessage) -> bool:
    return (message.id or "").startswith(RECALLED_MEMORIES_ID_PREFIX)


def _is_context_overflow(error: BaseException) -> bool:
    passing = (AwaError,) + current_engine().control_flow
    if isinstance(error, passing) and not isinstance(error, ModelProviderError):
        return False
    classified = classify_model_error(error)
    return classified is not None and classified.kind == "context_overflow"


__all__ = [
    "SUMMARY_ATTEMPTS",
    "Compaction",
    "CompactionLog",
    "CompactionMiddleware",
    "overflow_middleware",
    "shift_start",
]


def _saved(request: ModelRequest[Any], messages: list[AnyMessage]) -> list[AnyMessage]:
    """The thread's own versions of `messages`, without those only the request carries."""

    state = request.state
    thread = state.get("messages", []) if isinstance(state, dict) else []
    by_id = {message.id: message for message in thread if message.id}
    return [by_id[message.id] for message in messages if message.id in by_id]
