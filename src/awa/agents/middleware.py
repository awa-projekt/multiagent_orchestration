"""Middleware that connects a `create_agent` graph to the active `RunContext`.

`Agent` installs `RunTrackingMiddleware` automatically; a hand-built `create_agent` graph invoked
inside `use_run_context(...)` can add it too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Annotated, Any, NotRequired

from awa._internal.upstream.agents import (
    AgentMiddleware,
    AgentState,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
    ToolCallRequest,
)
from awa._internal.upstream.graph import Command
from awa._internal.upstream.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from awa._internal.upstream.runnables import get_config
from awa._internal.util import bound
from awa.contracts.events import ModelResponseEvent, ToolCallEvent, ToolResultEvent
from awa.contracts.usage import cache_tokens
from awa.errors import MaxStepsExceededError
from awa.kernel.output import ModelRetry, structured_output_tool_names
from awa.memory.long_term import with_recalled_memories
from awa.models.errors import model_name
from awa.models.guard import ensure_model_request_allowed
from awa.models.resilience import ModelResilienceMiddleware
from awa.runtime.cancellation import checkpoint
from awa.runtime.clock import SystemClock
from awa.runtime.context import RunContext, current_run_context, tool_call_scope
from awa.runtime.engine import current_engine
from awa.runtime.loops import REPEAT_GUARDED_TOOLS, REPEAT_LIMIT, LoopState, call_signature
from awa.tools.idempotency import use_tool_call

BUDGET_NOTE_SHARE = 0.7
"""Share of `max_requests` used from which a model call is told how many calls remain."""

BUDGET_FINAL_SHARE = 0.85
"""Share of `max_requests` used from which a model call must answer: its tools are removed."""

ROOT_RESERVE = 3
"""Model calls of the run tree a delegated run leaves for the agents above it to finish."""

BUDGET_STAGES_FROM = 10
"""Smallest `max_requests` that gets budget notes and a final call: a smaller budget is an
exact plan of calls, which the run keeps to as given."""

STEP_STATE_KEY = "awa_turn_steps"
"""Agent-state key of the model calls the current turn has made (see `AgentControlMiddleware`)."""

BUDGET_STATE_KEY = "awa_budget"
"""Agent-state key of a top-level run's usage, limits and loop counters (see `budget_record`)."""

_ANNOUNCED_KEY = "awa.announced_tool_calls"
"""`RunContext.internal` key of the ids of the tool calls this run emitted a `ToolCallEvent` for."""

_CLOCK = SystemClock()
"""Measures how long each model call takes."""


class RunTrackingMiddleware(AgentMiddleware):
    """Accounts for model and tool calls in the active `RunContext` (async path only).

    Around every model call it refuses real models while requests are blocked and checks the request
    limit; a failed call still counts as a request. After a response it records usage for the model
    that answered, emits `ModelResponseEvent` and `ToolCallEvent`s, and checks limits. Every tool
    call emits a `ToolResultEvent`. Outside a run context it passes calls through.
    """

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        """Guard, count and record one model call of the active run, then pass it on."""

        ensure_model_request_allowed(request.model)
        ctx = current_run_context()
        if ctx is None:
            return await handler(request)
        stage = budget_stage(ctx)
        ctx.admit_request()
        if stage is not None and request.tools:
            request = _budget_request(request, *stage)
        started = _CLOCK.monotonic()
        try:
            response = await handler(request)
        except current_engine().control_flow:
            raise
        except Exception as exc:
            # A failed attempt was still a request to the provider; a response that failed
            # to parse (structured output) still used its tokens and is traced like any
            # other response. Its tool calls never run, so none of them is announced.
            answer = getattr(exc, "ai_message", None)
            if isinstance(answer, AIMessage):
                await record_model_response(
                    ctx,
                    answer,
                    frozenset(call["name"] for call in answer.tool_calls),
                    model_name(request.model),
                    request=request,
                    duration_ms=int((_CLOCK.monotonic() - started) * 1000),
                )
            else:
                ctx.usage.record_model_call(ctx.agent, model=model_name(request.model))
            ctx.check_limits()  # a reply that crossed a limit ends the run, it is not repaired
            raise
        finally:
            ctx.usage.request_ended()
        message = next((m for m in reversed(response.result) if isinstance(m, AIMessage)), None)
        if message is not None:
            await record_model_response(
                ctx,
                message,
                structured_output_tool_names(request.response_format),
                model_name(request.model),
                request=request,
                duration_ms=int((_CLOCK.monotonic() - started) * 1000),
            )
        ctx.check_limits()
        return response

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        """Run one tool call and emit its `ToolResultEvent` (also when the tool raised)."""

        ctx = current_run_context()
        if ctx is None:
            return await handler(request)
        call = request.tool_call
        fields = {"tool_name": call["name"], "call_id": call.get("id") or "", **ctx.event_fields()}
        if call.get("id") not in ctx.internal.get(_ANNOUNCED_KEY, ()):
            # Requested in an earlier run (it waited for an approval): the result names it.
            fields["arguments"] = dict(call["args"])
        try:
            # A run the tool starts (a delegation, an agent as a tool) records this call;
            # the tool call id and graph namespace key the tool's external writes, so a
            # resumed run that replays this call sends the same idempotency key.
            with (
                tool_call_scope(ctx.run_id, call.get("id") or ""),
                use_tool_call(call.get("id") or "", _checkpoint_namespace()),
            ):
                result = await handler(request)
        except current_engine().control_flow:
            raise
        except Exception as exc:
            await ctx.emit(
                ToolResultEvent(content=f"{type(exc).__name__}: {exc}", is_error=True, **fields)
            )
            raise
        message = _tool_message(result)
        cached = message.response_metadata.get("awa_cache") if message is not None else None
        await ctx.emit(
            ToolResultEvent(
                content=str(message.text) if message is not None else "",
                is_error=message is not None and message.status == "error",
                cache=cached if cached in ("hit", "miss") else None,
                **fields,
            )
        )
        return result


def remaining_requests(ctx: RunContext[Any]) -> tuple[int, int] | None:
    """The tightest request budget of `ctx` as `(remaining, maximum)`; `None` when unbounded.

    A delegated run leaves `ROOT_RESERVE` calls of the tree for the agents above it.
    """

    delegated = ctx.depth >= 2 or bool(ctx.budgets)
    tightest: tuple[int, int] | None = None
    limit = ctx.limits.max_requests if ctx.limits is not None else None
    if limit is not None:
        reserve = ROOT_RESERVE if delegated else 0
        tightest = (limit - ctx.usage.requests_started - reserve, max(1, limit - reserve))
    for budget in ctx.budgets:
        cap = budget.limits.max_requests
        if cap is not None:
            left = cap - budget.usage.own_requests_started
            if tightest is None or left < tightest[0]:
                tightest = (left, cap)
    return tightest


def budget_stage(ctx: RunContext[Any]) -> tuple[str, int] | None:
    """Whether the next model call runs low on budget: `("note" | "final", calls left)`, else `None`.

    `"note"` from `BUDGET_NOTE_SHARE` of the budget used, `"final"` from `BUDGET_FINAL_SHARE` or for
    the last call, when the model still writes its best answer.
    """

    budget = remaining_requests(ctx)
    if budget is None or budget[1] < BUDGET_STAGES_FROM:
        return None
    left, maximum = budget
    used = maximum - left
    if left <= 1 or used >= BUDGET_FINAL_SHARE * maximum:
        return "final", max(left, 0)
    if used >= BUDGET_NOTE_SHARE * maximum:
        return "note", left
    return None


def _budget_request(request: ModelRequest[Any], stage: str, left: int) -> ModelRequest[Any]:
    if stage == "final":
        note = (
            "The model-call budget of this run is almost used up. Do not call tools. Write "
            "your final answer now from what you have, and state plainly what is incomplete."
        )
        return request.override(
            tools=[], tool_choice=None, messages=[*request.messages, HumanMessage(note)]
        )
    note = (
        f"Budget note: about {left} model calls remain for this run. Use the fewest calls "
        "that finish the work, and make independent tool calls in one turn."
    )
    return request.override(messages=[*request.messages, HumanMessage(note)])


class StepState(AgentState[Any]):
    """The agent state with the turn's step counter, kept in every checkpoint of the thread."""

    awa_turn_steps: NotRequired[Annotated[int, PrivateStateAttr]]
    awa_budget: NotRequired[Annotated[dict[str, Any], PrivateStateAttr]]


@dataclass(frozen=True, slots=True)
class Step:
    """The model call in progress: its number in the turn and the agent's `max_steps`."""

    number: int
    max_steps: int

    @property
    def is_last(self) -> bool:
        """Whether this is the last model call `max_steps` allows."""

        return self.number >= self.max_steps


_step: ContextVar[Step | None] = ContextVar("awa_model_step", default=None)


def current_step() -> Step | None:
    """The step of the model call in progress (inside `AgentControlMiddleware`), if any."""

    return _step.get()


def _checkpoint_namespace() -> str:
    """The node path of the running LangGraph node without task ids (`""` outside a graph)."""

    try:
        config = get_config()
    except RuntimeError:  # not inside a runnable
        return ""
    namespace = str((config.get("configurable") or {}).get("checkpoint_ns") or "")
    return "|".join(part.split(":", 1)[0] for part in namespace.split("|") if part)


@dataclass(slots=True)
class _RunControl:
    """Per-run state of one agent: its system prompt, model-call budget and interrupts."""

    system_prompt: str
    # `auto_recall` context message, placed before the run's user input.
    memories: HumanMessage | None
    memory_anchor: str | None = None
    model_calls: int = 0
    # Interrupt id -> position of the tool call that raised it in its model turn.
    interrupt_positions: dict[str, int] = field(default_factory=dict)


def _cap_output(request: ToolCallRequest, result: Any) -> Any:
    """Keep a long text result within the tool profile's limit: offload (`cost`) or cut (`quality`).

    The text blocks of block content, such as an MCP result, are joined into one text block
    first; other blocks follow, bounded in number and size (`bound_blocks`).
    """

    from awa.tools.hardening import (
        OUTPUT_LIMIT_METADATA_KEY,
        bound_blocks,
        split_tool_text,
        unframe_tool_output,
        with_tool_text,
    )
    from awa.tools.offload import current_profile, limit_output

    if not isinstance(result, ToolMessage):
        return result
    if (bounded := bound_blocks(result.content)) is not result.content:
        result = result.model_copy(update={"content": bounded})
    split = split_tool_text(result.content)
    if split is None:
        return result
    text, others = split
    profile = current_profile()
    unframed = unframe_tool_output(text)
    raw = text if unframed is None else unframed[2]
    if len(raw) <= profile.max_output_chars:
        return result
    tool = getattr(request, "tool", None)
    limited_by_tool = tool is not None and OUTPUT_LIMIT_METADATA_KEY in (tool.metadata or {})
    if limited_by_tool and not profile.offload:
        return result
    name = request.tool_call["name"] if isinstance(request.tool_call, dict) else "the tool"
    content = limit_output(
        raw,
        tool=name,
        profile=profile,
        source=None if unframed is None else unframed[1],
        framed=unframed is not None,
    )
    return result.model_copy(update={"content": with_tool_text(result.content, content, others)})


class AgentControlMiddleware(AgentMiddleware):
    """Applies each run's system prompt, enforces `max_steps` and orders interrupts by tool call.

    `max_steps` bounds the model calls of one turn, output retries included. The count lives in the
    agent state (`STEP_STATE_KEY`), so it continues across resumes and restarts at zero only when new
    input starts a turn.
    """

    state_schema = StepState

    def __init__(self, agent: str, max_steps: int) -> None:
        super().__init__()
        self._agent = agent
        self._max_steps = max_steps
        self._runs: dict[str, _RunControl] = {}

    def begin(self, run_id: str, system_prompt: str, memories: HumanMessage | None = None) -> None:
        """Start controlling the run `run_id` with its system prompt and recalled memories."""

        self._runs[run_id] = _RunControl(system_prompt, memories)

    def end(self, run_id: str) -> None:
        """Forget the run `run_id`."""

        self._runs.pop(run_id, None)

    def spend_step(self, run_id: str) -> bool:
        """Count one more model call of the run, or report that `max_steps` leaves none."""

        control = self._runs.get(run_id)
        if control is None or control.model_calls >= self._max_steps:
            return False
        control.model_calls += 1
        return True

    def pause_order(self, run_id: str) -> Callable[[str], int] | None:
        """Sort key of a pause id: the position of the tool call that raised the pause."""

        control = self._runs.get(run_id)
        if control is None:
            return None
        positions = control.interrupt_positions
        return lambda pause_id: positions.get(pause_id, len(positions))

    async def abefore_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """New input starts a turn: its step count starts at zero."""

        return {STEP_STATE_KEY: 0}

    async def aafter_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        """Keep the turn's step count (and a top-level run's budget) in the state, so the
        next checkpoint holds them."""

        ctx = current_run_context()
        control = self._runs.get(ctx.run_id) if ctx is not None else None
        if ctx is None or control is None:
            return None
        update: dict[str, Any] = {STEP_STATE_KEY: control.model_calls}
        if ctx.parent_run_id is None:
            # A top-level run keeps its budget in the checkpoint: the tool calls the model
            # just asked for are counted already, so a pause in them never undercounts.
            reply = next((m for m in reversed(_messages(state)) if isinstance(m, AIMessage)), None)
            requested = reply.tool_calls if reply is not None else []
            update[BUDGET_STATE_KEY] = budget_record(ctx, requested)
        return update

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[Any]]
    ) -> Any:
        await checkpoint()
        ctx = current_run_context()
        if ctx is not None and (refused := await _loop_guard(ctx, request)) is not None:
            return refused
        try:
            return _cap_output(request, await handler(request))
        except ModelRetry as exc:
            # A tool asked the model to try again: the reason is the tool's result.
            call = request.tool_call
            return ToolMessage(
                content=str(exc),
                tool_call_id=call.get("id") or "",
                name=call["name"],
                status="error",
            )
        except current_engine().control_flow as exc:
            ctx = current_run_context()
            control = self._runs.get(ctx.run_id) if ctx is not None else None
            if control is not None:
                position = _call_position(request)
                for pause in current_engine().pauses(exc):
                    control.interrupt_positions.setdefault(pause.id, position)
            raise

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        await checkpoint()
        ctx = current_run_context()
        control = self._runs.get(ctx.run_id) if ctx is not None else None
        if control is None:
            return await handler(request)
        # A resumed or continued turn goes on from the count in its checkpoint.
        control.model_calls = max(control.model_calls, _saved_steps(request.state)) + 1
        if control.model_calls > self._max_steps:
            raise MaxStepsExceededError(
                f"Agent {self._agent!r} exceeded max_steps={self._max_steps}: it needed more "
                f"than {self._max_steps} model calls without producing a final answer"
            )
        if control.system_prompt:
            request = request.override(system_message=SystemMessage(content=control.system_prompt))
        if control.memories is not None:
            messages, control.memory_anchor = with_recalled_memories(
                request.messages, control.memories, control.memory_anchor
            )
            request = request.override(messages=messages)
        with bound(_step, Step(control.model_calls, self._max_steps)):
            return await handler(request)


async def _loop_guard(ctx: RunContext[Any], request: ToolCallRequest) -> ToolMessage | None:
    """Refuse a tool call over its `per_tool_limits`, or an identical repeat of a
    coordination call; the refusal (with a `LimitExceededEvent`) replaces the tool result."""

    call = request.tool_call
    name = call["name"]
    call_id = call.get("id") or ""
    text: str | None = None
    limits = ctx.limits.per_tool_limits if ctx.limits is not None else None
    count = ctx.loop.count_tool(name, call_id)
    cap = (limits or {}).get(name)
    if cap is not None and count > cap:
        text = (
            f"Not run: {name!r} reached its limit of {cap} calls in this run "
            f"(per_tool_limits). Continue with what you have."
        )
        ctx.report_limit_exceeded(
            limit="per_tool_limits", value=count, maximum=cap, message=f"{name}: {text}"
        )
    elif name in REPEAT_GUARDED_TOOLS:
        turn = _model_turn(request)
        repeats = ctx.loop.count_signature(ctx.run_id, call_signature(name, call["args"]), turn)
        if repeats >= REPEAT_LIMIT:
            text = (
                f"Not run: this exact {name} call was already made {repeats - 1} times. It "
                "will not run again. Use the earlier result, change the request, or answer."
            )
            ctx.report_limit_exceeded(
                limit="repeated_call", value=repeats, maximum=REPEAT_LIMIT - 1, message=text
            )
        elif repeats > 1:
            text = (
                f"Not run again: you already made this exact {name} call; its result is above "
                "in the conversation. Use it, or change the request if you need something else."
            )
            ctx.report_limit_exceeded(limit="repeated_call", value=repeats, maximum=1, message=text)
    if text is None:
        return None
    fields = {"tool_name": name, "call_id": call_id, **ctx.event_fields()}
    await ctx.emit(ToolResultEvent(content=text, is_error=True, **fields))
    return ToolMessage(content=text, tool_call_id=call_id, name=name, status="error")


def budget_record(
    ctx: RunContext[Any], requested: Sequence[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    """What a run tree has used and may use (JSON); `requested` tool calls are counted already."""

    loop = LoopState()
    loop.restore(ctx.loop.snapshot())
    for call in requested:
        loop.count_tool(call["name"], call.get("id") or "")
    return {
        "usage": ctx.usage.pool.model_dump(mode="json"),
        "limits": ctx.limits.model_dump(mode="json") if ctx.limits is not None else None,
        "loop": loop.snapshot(),
    }


def restore_budget(ctx: RunContext[Any], record: Any) -> None:
    """Continue the budget `budget_record` saved: a resume never resets a counter.

    Only a top-level run restores. A resume without `usage_limits` continues the paused run's limits
    and usage; limits given to the resume budget only the resumed part. Loop counters always
    continue.
    """

    from awa.contracts.usage import Usage, UsageLimits
    from awa.runtime.termination import caller_limits

    if ctx.parent_run_id is not None or not isinstance(record, dict):
        return
    ctx.loop.restore(record.get("loop"))
    # Limits the caller gave win; the ceilings filled in at the start do not count as given.
    if caller_limits(ctx) is not None or not isinstance(record.get("limits"), dict):
        return
    ctx.limits = UsageLimits.model_validate(record["limits"])
    if isinstance(record.get("usage"), dict):
        before = Usage.model_validate(record["usage"])
        before.merge(ctx.usage)
        ctx.usage = Usage.under(before)


def _messages(state: Any) -> list[Any]:
    return state.get("messages", []) if isinstance(state, dict) else []


def _model_turn(request: ToolCallRequest) -> int:
    """The number of the model turn that made the request's tool call (its AI messages so far)."""

    return sum(isinstance(message, AIMessage) for message in _messages(request.state))


def _saved_steps(state: Any) -> int:
    value = state.get(STEP_STATE_KEY) if isinstance(state, dict) else None
    return value if isinstance(value, int) and value > 0 else 0


def _call_position(request: ToolCallRequest) -> int:
    """Position of the request's tool call within the model turn that made it."""

    call_id = request.tool_call["id"]
    for message in reversed(_messages(request.state)):
        ids = [call["id"] for call in message.tool_calls] if isinstance(message, AIMessage) else []
        if call_id in ids:
            return ids.index(call_id)
    return 0


def node_input_summary(value: Any) -> dict[str, Any]:
    """What a LangSmith trace records as the input of awa's middleware nodes: the state
    keys and the number of messages, never their content (`TracePolicy.process_inputs`)."""

    if not isinstance(value, dict):
        return {"type": type(value).__name__}
    messages = value.get("messages")
    summary: dict[str, Any] = {"keys": sorted(str(key) for key in value)}
    if isinstance(messages, list):
        summary["messages"] = len(messages)
    return summary


def apply_trace_policy(stack: Sequence[AgentMiddleware]) -> None:
    """Give every middleware node a `TracePolicy` that records state keys instead of the conversation.

    Middleware that sets its own `trace_policy` is left as it is.
    """

    from awa._internal.upstream.graph import TracePolicy

    policy = TracePolicy(process_inputs=node_input_summary)
    for middleware in stack:
        if getattr(middleware, "trace_policy", None) is None:
            middleware.trace_policy = policy


def with_run_tracking(
    middleware: Sequence[AgentMiddleware], resilience: ModelResilienceMiddleware | None = None
) -> list[AgentMiddleware]:
    """Return `middleware` with run tracking innermost and resilience right outside it.

    So every retried or fallback attempt passes the request guard, `max_requests` and per-model
    usage, while middleware further out sees one model call.
    """

    stack = list(middleware)
    if not any(isinstance(m, RunTrackingMiddleware) for m in stack):
        stack.append(RunTrackingMiddleware())
    if resilience is not None and not any(isinstance(m, ModelResilienceMiddleware) for m in stack):
        tracking = next(i for i, m in enumerate(stack) if isinstance(m, RunTrackingMiddleware))
        stack.insert(tracking, resilience)
    return stack


async def record_model_response(
    ctx: RunContext[Any],
    message: AIMessage,
    structured_names: frozenset[str],
    configured: str | None,
    *,
    request: ModelRequest[Any] | None = None,
    duration_ms: int | None = None,
) -> None:
    """Record the usage of one model answer under the `configured` model and emit its events.

    The `ModelResponseEvent` comes first, then a `ToolCallEvent` per requested tool call
    (except the structured output tools in `structured_names`).
    """

    from awa.memory.context_editing import cleared_tool_results
    from awa.models.prompt_cache import prefix_hash

    usage = message.usage_metadata
    metadata = message.response_metadata
    model = metadata.get("model_name") or metadata.get("model")
    # Usage is keyed by the configured model, as for failed attempts: providers may
    # report another name (an Azure deployment answers with a dated model version).
    ctx.usage.record_usage_metadata(
        ctx.agent,
        dict(usage) if usage else None,
        tool_calls=len(message.tool_calls),
        model=configured or (str(model) if model else None),
    )
    fields = ctx.event_fields()
    cache_read, cache_write = cache_tokens(dict(usage) if usage else None)
    await ctx.emit(
        ModelResponseEvent(
            content=str(message.text),
            tool_call_names=tuple(call["name"] for call in message.tool_calls),
            input_tokens=usage["input_tokens"] if usage else 0,
            output_tokens=usage["output_tokens"] if usage else 0,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            model=str(model) if model else None,
            provider=_text(metadata.get("model_provider")),
            finish_reason=_text(metadata.get("finish_reason") or metadata.get("stop_reason")),
            prefix_hash=prefix_hash(request) if request is not None else None,
            replayed=metadata.get("awa_replay") == "hit",
            duration_ms=duration_ms,
            cleared_tool_results=(
                cleared_tool_results(request.messages) if request is not None else 0
            ),
            **fields,
        )
    )
    announced: set[str] = ctx.internal.setdefault(_ANNOUNCED_KEY, set())
    for call in message.tool_calls:
        if call["name"] in structured_names:
            continue
        announced.add(call.get("id") or "")
        await ctx.emit(
            ToolCallEvent(
                tool_name=call["name"],
                call_id=call.get("id") or "",
                arguments=dict(call["args"]),
                **fields,
            )
        )


def _text(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _tool_message(result: ToolMessage | Command[Any]) -> ToolMessage | None:
    if isinstance(result, ToolMessage):
        return result
    update = result.update
    messages = update.get("messages", []) if isinstance(update, dict) else []
    return next((m for m in reversed(messages) if isinstance(m, ToolMessage)), None)


__all__ = [
    "BUDGET_STAGES_FROM",
    "BUDGET_STATE_KEY",
    "STEP_STATE_KEY",
    "RunTrackingMiddleware",
    "Step",
    "StepState",
    "apply_trace_policy",
    "budget_record",
    "budget_stage",
    "current_step",
    "node_input_summary",
    "remaining_requests",
    "restore_budget",
    "with_run_tracking",
]
