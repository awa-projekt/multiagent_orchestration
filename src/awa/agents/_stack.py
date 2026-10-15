"""An agent's middleware, written once: the order (`build_stack`) and the output checks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from awa._internal.util import preview
from awa.errors import OutputRejectedError, StructuredOutputError
from awa.kernel.output import (
    ModelRetry,
    OutputRejected,
    OutputValidatorMiddleware,
    RetryKind,
    Validator,
    takes_runtime,
)
from awa.runtime.context import current_run_context

if TYPE_CHECKING:
    from awa._internal.upstream.agents import AgentMiddleware
    from awa._internal.upstream.models import BaseChatModel
    from awa.agents.agent import Agent
    from awa.guardrails import Guardrail


def build_stack(agent: Agent[Any, Any], model: BaseChatModel) -> list[AgentMiddleware]:
    """The middleware of `agent`'s graph for `model`, outermost first.

    Order: dangling tool call repair, run control, inherited call guards, tool policy, the agent's
    own middleware, the output retry, compaction (or the context-overflow retry), context editing,
    model resilience, the tool layer, run tracking, then caching and the trace policy.
    """

    from awa.agents.boundary import InheritedGuardMiddleware
    from awa.agents.middleware import apply_trace_policy, with_run_tracking
    from awa.kernel.tool_guard import RepairToolCallsMiddleware
    from awa.memory.context_editing import from_settings
    from awa.models import build_resilience_middleware
    from awa.tools.resilience import tool_layer_middleware

    policy = [agent._tool_policy] if agent._tool_policy is not None else []
    middleware: list[AgentMiddleware] = [
        RepairToolCallsMiddleware(),
        agent._control,
        InheritedGuardMiddleware(),
        *policy,
        *agent._middleware,
        *output_checks(agent),
    ]
    if agent._compaction is not None:
        summary_model = agent._compaction.model
        summarizer = model if summary_model is None else agent._resolve_model(summary_model)
        middleware.append(agent._compaction.middleware(summarizer, log=agent._compaction_log))
    else:
        from awa.memory.compaction import overflow_middleware

        # No size trigger: only a provider's context-overflow error summarizes and retries.
        middleware.append(overflow_middleware(model, log=agent._compaction_log))
    editing = from_settings()
    # A middleware that edits context on the server (a blueprint's `refine:`) replaces it.
    if editing is not None and not any(
        getattr(m, "replaces_context_editing", False) for m in agent._middleware
    ):
        middleware.append(editing.middleware())
    resilience = build_resilience_middleware(
        agent._model, agent._model_settings, agent._model_registry
    )
    outer, inner = tool_layer_middleware(list(agent._tools))
    tracked = [*with_run_tracking([*middleware, *outer], resilience), *inner]
    stack = with_caching(agent, tracked)
    apply_trace_policy(stack)
    return stack


def with_caching(agent: Agent[Any, Any], stack: list[AgentMiddleware]) -> list[AgentMiddleware]:
    """`stack` with prompt caching right outside run tracking (so per attempt, inside
    retries and fallbacks) and the read-only tool result cache right inside it."""

    from awa.agents.middleware import RunTrackingMiddleware
    from awa.models.prompt_cache import PromptCacheMiddleware

    tracking = next(i for i, m in enumerate(stack) if isinstance(m, RunTrackingMiddleware))
    prompt = PromptCacheMiddleware(
        agent._name, mode=agent._prompt_cache, has_tools=bool(agent._tools), cache=agent._cache
    )
    inner: list[AgentMiddleware] = []
    if agent._cache is not None and agent._cache.uses("tools"):
        from awa.tools.result_cache import ToolResultCacheMiddleware

        # Also for agents with only acting tools: their actions invalidate the cached
        # reads of other agents in the same run.
        if agent._tools:
            inner.append(ToolResultCacheMiddleware(agent._cache, list(agent._tools)))
    # Middleware marked `per_attempt` (a blueprint's `refine:`) also decides per attempt.
    attempt = [m for m in stack[:tracking] if getattr(m, "per_attempt", False)]
    outer = [m for m in stack[:tracking] if not getattr(m, "per_attempt", False)]
    rest = [stack[tracking], *inner, *stack[tracking + 1 :]]
    return [*outer, prompt, *attempt, *rest]


class GuardrailRetry(ModelRetry):
    """A retry-mode guardrail rejected `output`."""

    def __init__(self, guardrail: Guardrail, output: Any, reason: str) -> None:
        super().__init__(f"{guardrail.name}: {reason}")
        self.guardrail = guardrail
        self.output = output


class _GuardrailCheck:
    """A retry-mode guardrail as an output validator."""

    def __init__(self, guardrail: Guardrail) -> None:
        self.__name__ = guardrail.name
        self._guardrail = guardrail

    def __call__(self, output: Any) -> None:
        from awa.guardrails import run_guardrails

        result = run_guardrails(output, [self._guardrail])  # a check that raises blocks
        if result.blocked:
            raise GuardrailRetry(self._guardrail, output, result.reason or "")


def _with_run_context(validator: Validator) -> Validator:
    """`validator` with its `runtime` argument replaced by the current `RunContext`."""

    if not takes_runtime(validator):
        return validator

    def checked(output: Any, runtime: Any) -> Any:
        return validator(output, current_run_context())

    checked.__name__ = str(getattr(validator, "__name__", None) or type(validator).__name__)
    return checked


def output_checks(agent: Agent[Any, Any]) -> list[AgentMiddleware]:
    """The output validator middleware of `agent`: empty when it has nothing to check."""

    from awa.guardrails import retries_on_fail

    validators: list[Validator] = [
        _GuardrailCheck(g) for g in agent._output_guardrails if retries_on_fail(g)
    ]
    validators += [_with_run_context(v) for v in agent._output_validators]
    if agent._output_type is None and not validators:
        return []
    return [
        OutputValidatorMiddleware(
            validators,
            max_retries=agent._output_retries,
            response_format=agent._output_type,
            spend_step=agent._spend_step,
            on_retry=_emit_retry,
        )
    ]


def _emit_retry(check: str, kind: RetryKind, attempt: int, max_retries: int) -> None:
    from awa.contracts.events import OutputRetryEvent

    ctx = current_run_context()
    if ctx is not None:
        fields = {"check": check, "attempt": attempt, "max_retries": max_retries, "kind": kind}
        ctx.events.emit_later(OutputRetryEvent(**fields, **ctx.event_fields()))


def rejected_error(agent: str, rejected: OutputRejected) -> Exception:
    """The error for an output that stayed rejected: what the caller of `run` sees."""

    problems = preview("; ".join(rejected.problems), 600)
    if rejected.kind == "schema":
        return StructuredOutputError(
            f"Agent {agent!r} returned a response that does not match its output type: {problems}"
        )
    for cause in rejected.causes:
        if isinstance(cause, GuardrailRetry):
            from awa.guardrails import GuardrailTripwireError, run_output_guardrails

            try:  # the final block, with its event, as for a guardrail that never retries
                run_output_guardrails(cause.output, [cause.guardrail], raise_on_block=True)
            except GuardrailTripwireError as error:
                return error
    return OutputRejectedError(
        f"Agent {agent!r} could not produce an output that passes its checks after "
        f"{rejected.attempts} retries: {problems}",
        problems=rejected.problems,
        attempts=rejected.attempts,
    )
