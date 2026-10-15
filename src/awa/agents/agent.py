"""`Agent`: a named, typed LLM agent built on LangChain's `create_agent`."""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeAlias
from weakref import WeakKeyDictionary

from pydantic import PydanticUserError, TypeAdapter
from typing_extensions import TypeVar

from awa._internal.state_types import register_state_type
from awa._internal.util import bound, elapsed_ms
from awa.contracts.cards import AgentCard, validate_agent_name
from awa.contracts.usage import UsageLimits
from awa.errors import (
    AgentRunError,
    MaxStepsExceededError,
    NoPendingInterruptError,
    StructuredOutputError,
)
from awa.runtime._run import RunApi, RunRequest, StreamStart, resolve_thread, run_observed
from awa.runtime.context import EventSubscriber, RunContext, current_run_context, new_run_context
from awa.runtime.engine import AgentSpec, Engine, EngineOutcome, Resume, RunOptions, current_engine

# LangChain and LangGraph load when an agent is built with tools or first runs, so
# `from awa import Agent` stays fast.
if TYPE_CHECKING:
    from awa._internal.upstream.agents import AgentMiddleware
    from awa._internal.upstream.checkpoint import BaseCheckpointSaver, BaseStore
    from awa._internal.upstream.messages import AIMessage, BaseMessage, HumanMessage
    from awa._internal.upstream.models import BaseChatModel
    from awa._internal.upstream.tools import BaseTool
    from awa.agents.hooks import AgentHooks
    from awa.agents.middleware import AgentControlMiddleware
    from awa.contracts.protocols import AgentInput
    from awa.contracts.results import RunResult
    from awa.guardrails import Guardrail
    from awa.memory.compaction import Compaction, CompactionLog
    from awa.memory.long_term import AgentMemory
    from awa.models import ModelRegistry, ModelSettings
    from awa.runtime.cache import Cache, PromptCacheMode
    from awa.skills import Skill, SkillRegistry
    from awa.tools.policy import ToolPolicy, ToolPolicyMiddleware
    from awa.tools.toolset import Toolbox

AgentDepsT = TypeVar("AgentDepsT", default=Any)
"""Type of the dependencies an agent's tools and instructions receive."""

AgentOutputT = TypeVar("AgentOutputT", default=str)
"""Type of an agent's output: `str`, or its declared `output_type`."""

Instructions = str | Callable[[RunContext[AgentDepsT]], str | Awaitable[str]]
"""Static instructions, or a (sync or async) function of the run context."""

AgentTool: TypeAlias = "BaseTool | Callable[..., Any] | Toolbox"
"""What `Agent(tools=...)` accepts: tools, plain functions and toolboxes."""

ToolPolicySpec: TypeAlias = "ToolPolicyMiddleware | ToolPolicy | Sequence[ToolPolicy]"
"""What `Agent(tool_policy=...)` accepts: a `ToolPolicyMiddleware`, or the policies for one."""


@dataclass(eq=False)
class _Override:
    """Model and deps replacing an agent's own inside an `Agent.override` block."""

    model: str | BaseChatModel | None
    deps: Any


_overrides: ContextVar[Mapping[object, _Override] | None] = ContextVar(
    "awa_agent_overrides", default=None
)


class Agent(RunApi[AgentDepsT, AgentOutputT], Generic[AgentDepsT, AgentOutputT]):
    """A named LLM agent with tools, instructions, skills and typed output.

    Building an agent is cheap and needs no credentials: the model is resolved and the graph
    compiled on the first run. An `Agent` is immutable; `clone` or `with_tools` derive variants.

    Example:
        ```python
        from awa import Agent

        def search(query: str) -> str:
            '''Search the knowledge base.'''
            return lookup(query)

        researcher = Agent("researcher", description="Finds and cites evidence.",
                           instructions="Answer only from evidence you retrieved.", tools=[search])
        result = await researcher.run("What is hybrid search?")
        ```

    Args:
        name: Unique agent name (letters, digits, `_`, `-`; starts with a letter).
        instructions: System prompt, or a sync/async function of the `RunContext` evaluated at the
            start of every run.
        description: What the agent is for; other agents read it when deciding whom to delegate to.
        model: A chat model, a model profile name (see `ModelRegistry`), a `provider:model` string,
            or `None` for the `default` profile.
        model_settings: Construction settings for models given as a string, and the resilience
            policy (`retry`, `fallbacks`, `circuit_breaker`) for every model request.
        model_registry: Registry resolving profile names; defaults to `ModelRegistry.default()`.
        tools: LangChain tools, plain functions (the docstring becomes the description), and
            `Toolbox`es, whose tools are added in their place.
        output_type: A pydantic model, dataclass or `TypedDict` the agent must return; unset, the
            output is the final text. On Python 3.11, use `typing_extensions.TypedDict`.
        skills: Skills, or names of skills in `skill_registry`.
        skill_registry: Registry to resolve skill names in; defaults to `SkillRegistry.builtin()`.
        skill_mode: `"inline"` appends every skill's instructions to the system prompt;
            `"on_demand"` appends only an index and gives tools to load skills when needed.
        output_guardrails: Checks on the validated `output_type` object, otherwise the final text;
            one with `on_fail="retry"` first sends its reason back to the model.
        output_validators: Your own checks of the final output, `fn(output)` or `fn(output, ctx)`,
            raising `ModelRetry("reason")` to ask again; a returned value replaces the output.
        output_retries: Most extra answers per turn after a rejected output, shared by schema
            errors, validators and retry guardrails (default 2); they count in `max_steps`.
        hooks: `AgentHooks` or event callbacks receiving the events of this agent's runs, nested
            runs included.
        tool_policy: Decides per tool call whether it runs, pauses for a human, changes or is
            refused (see `awa.tools.policy`); sees every call before `middleware`. `Ask` decisions
            need a checkpointer.
        max_steps: Maximum number of model calls per run.
        store: LangGraph store for cross-thread memory, available to tools.
        memory: Long-term memory the agent manages itself with `remember`, `recall` and `forget`;
            with `auto_recall`, relevant memories are recalled into a context message before the
            user input.
        compaction: Summarize older conversation turns when the conversation outgrows a token budget.
        prompt_cache: Provider prompt caching: `"auto"`, `"1h"` or `"off"`; `None` takes the mode
            of `cache`, else `"auto"`.
        cache: The caches shared with the rest of the system (`awa.runtime.Cache`): read-only tool
            results.

    Raises:
        SkillNotFoundError: If a skill name is not in the registry.
    """

    def __init__(
        self,
        name: str,
        *,
        instructions: Instructions[AgentDepsT] = "",
        description: str = "",
        model: str | BaseChatModel | None = None,
        model_settings: ModelSettings | None = None,
        model_registry: ModelRegistry | None = None,
        tools: Sequence[AgentTool] = (),
        output_type: type[AgentOutputT] | None = None,
        skills: Sequence[str | Skill] = (),
        skill_registry: SkillRegistry | None = None,
        skill_mode: Literal["inline", "on_demand"] = "inline",
        capabilities: Sequence[str] = (),
        tags: Sequence[str] = (),
        role: str | None = None,
        input_guardrails: Sequence[Guardrail] = (),
        output_guardrails: Sequence[Guardrail] = (),
        output_validators: Sequence[Callable[..., Any]] = (),
        output_retries: int = 2,
        hooks: Sequence[AgentHooks | EventSubscriber] = (),
        middleware: Sequence[AgentMiddleware[Any, Any, Any]] = (),
        tool_policy: ToolPolicySpec | None = None,
        max_steps: int = 25,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        store: BaseStore | None = None,
        metadata: Mapping[str, Any] | None = None,
        version: str = "1.0.0",
        memory: AgentMemory | None = None,
        compaction: Compaction | None = None,
        prompt_cache: PromptCacheMode | None = None,
        cache: Cache | None = None,
    ) -> None:
        validate_agent_name(name)
        if prompt_cache is not None and prompt_cache not in ("auto", "off", "1h"):
            raise ValueError(f"prompt_cache must be 'auto', 'off' or '1h', got {prompt_cache!r}")
        if max_steps < 1:
            raise ValueError(f"max_steps must be at least 1, got {max_steps}")
        if output_retries < 0:
            raise ValueError(f"output_retries must be at least 0, got {output_retries}")
        if skill_mode not in ("inline", "on_demand"):
            raise ValueError(f"skill_mode must be 'inline' or 'on_demand', got {skill_mode!r}")
        constructs = model_settings is not None and model_settings.has_construction_settings
        if constructs and _is_model_instance(model):
            raise ValueError(
                "model_settings only apply to models given as a string; configure the model "
                "instance directly"
            )

        self._options: dict[str, Any] = {
            "instructions": instructions,
            "description": description,
            "model": model,
            "model_settings": model_settings,
            "model_registry": model_registry,
            "tools": tuple(tools),
            "output_type": output_type,
            "skills": tuple(skills),
            "skill_registry": skill_registry,
            "skill_mode": skill_mode,
            "capabilities": tuple(capabilities),
            "tags": tuple(tags),
            "role": role,
            "input_guardrails": tuple(input_guardrails),
            "output_guardrails": tuple(output_guardrails),
            "output_validators": tuple(output_validators),
            "output_retries": output_retries,
            "hooks": tuple(hooks),
            "middleware": tuple(middleware),
            "tool_policy": tool_policy,
            "max_steps": max_steps,
            "checkpointer": checkpointer,
            "store": store,
            "metadata": dict(metadata or {}),
            "version": version,
            "memory": memory,
            "compaction": compaction,
            "prompt_cache": prompt_cache,
            "cache": cache,
        }
        self._name = name
        self._description = description
        self._instructions = instructions
        self._model = model
        self._model_settings = model_settings
        self._model_registry = model_registry
        self._output_type = output_type
        if isinstance(output_type, type):
            register_state_type(output_type)  # a saved run revives it as this class
        self._output_adapter = _output_adapter(output_type) if output_type is not None else None
        self._input_guardrails = list(input_guardrails)
        self._output_guardrails = list(output_guardrails)
        self._output_validators = tuple(output_validators)
        self._output_retries = output_retries
        self._hooks: tuple[EventSubscriber, ...] = tuple(hooks)
        self._middleware = tuple(middleware)
        self._tool_policy = _policy_middleware(tool_policy)
        self._max_steps = max_steps
        self._checkpointer = checkpointer
        self._store = store
        self._memory = memory
        self._compaction = compaction
        self._compaction_log: CompactionLog = {}
        self._cache = cache
        self._prompt_cache: PromptCacheMode = prompt_cache or (
            cache.settings.prompt_mode if cache is not None else "auto"
        )

        self._skill_mode = skill_mode
        self._skills: tuple[Skill, ...] = _resolve_skills(skills, skill_registry)
        self._skill_registry: SkillRegistry | None = None
        skill_tools: list[BaseTool] = []
        if self._skills:
            from awa.skills import SkillRegistry

            self._skill_registry = SkillRegistry(self._skills)
            skill_tools = _skill_tools(self._skill_registry, self._skills, skill_mode)

        memory_tools = memory.tools() if memory is not None else []
        self._tools: tuple[BaseTool, ...] = (*_as_tools(tools), *skill_tools, *memory_tools)
        names = [t.name for t in self._tools]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"Agent {name!r} has duplicate tool names: {', '.join(duplicates)}")

        self._static_prompt = (
            self._compose_prompt(instructions) if isinstance(instructions, str) else None
        )
        self._card = AgentCard(
            name=name,
            description=description,
            capabilities=tuple(capabilities),
            skills=tuple(s.name for s in self._skills),
            tools=(*names, *_middleware_tool_names(self._middleware, names)),
            model=_describe_model(model),
            role=role,
            tags=tuple(tags),
            output_schema=self._output_adapter.json_schema() if self._output_adapter else None,
            version=version,
            metadata=dict(metadata or {}),
        )
        self._engine: Engine | None = None
        self._graph: Any = None
        self._override_graphs: WeakKeyDictionary[_Override, Any] = WeakKeyDictionary()
        self._identity = object()
        self._replay_graphs: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()

    @property
    def name(self) -> str:
        """The agent's unique name."""

        return self._name

    @property
    def description(self) -> str:
        """What the agent is for."""

        return self._description

    @property
    def card(self) -> AgentCard:
        """The agent's public description (tools, skills, capabilities, output schema)."""

        return self._card

    @property
    def tools(self) -> tuple[BaseTool, ...]:
        """Every tool the agent can call, including skill tools in `on_demand` mode."""

        return self._tools

    @property
    def tool_policy(self) -> ToolPolicyMiddleware | None:
        """The middleware applying the agent's tool policies, if it has any."""

        return self._tool_policy

    @property
    def output_type(self) -> type[AgentOutputT] | None:
        """The declared structured output type, or `None` for text output."""

        return self._output_type

    @property
    def memory(self) -> AgentMemory | None:
        """The agent's long-term memory, if it has one."""

        return self._memory

    @property
    def compaction(self) -> Compaction | None:
        """The agent's context compaction settings, if any."""

        return self._compaction

    @cached_property
    def _control(self) -> AgentControlMiddleware:
        from awa.agents.middleware import AgentControlMiddleware

        return AgentControlMiddleware(self._name, self._max_steps)

    def __repr__(self) -> str:
        return f"Agent(name={self._name!r}, model={self._card.model!r}, tools={len(self._tools)})"

    async def run(
        self,
        input: AgentInput,
        *,
        deps: AgentDepsT | None = None,
        history: Sequence[BaseMessage | dict[str, Any]] | None = None,
        thread_id: str | None = None,
        usage_limits: UsageLimits | None = None,
        timeout: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunResult[AgentOutputT]:
        """Run the agent to completion.

        Inside another run (a tool, `as_tool`), the run is nested: it shares the enclosing run's usage,
        limits, events and deadline, and inherits its `deps` unless new ones are given.

        Args:
            input: A user prompt or a list of messages.
            deps: Dependencies available to tools and dynamic instructions via the `RunContext`.
            thread_id: Conversation thread to continue and persist (requires a checkpointer); created
                when omitted with a checkpointer.
            usage_limits: Limits for the whole run tree; only on a top-level run.
            timeout: Wall-clock seconds for the run, nested runs included; a nested timeout can only
                shorten the enclosing deadline.
            metadata: Request-scoped values merged into `RunContext.metadata` and inherited by nested
                runs; by convention `awa.request_id`, `awa.tenant` and `awa.actor`.

        Returns:
            `output` is the validated structured output when `output_type` is set, otherwise the final
            text; `None` when the run paused (see `RunResult.interrupts`).

        Raises:
            ConfigurationError: If `thread_id` is given without a checkpointer, or `usage_limits` for a
                nested run.
            RunTimeoutError: If `timeout` or `UsageLimits.max_duration` expired.
            RunCancelledError: If the run was stopped with `awa.runtime.cancel`.
            MaxStepsExceededError: If the run needs more than `max_steps` model calls.
            UsageLimitExceededError: If the run tree exceeds `usage_limits`.
            GuardrailTripwireError: If a guardrail blocks the input or output.
            StructuredOutputError: If the model never returned the `output_type`.
            ModelResolutionError: If the model cannot be resolved, or real model requests are blocked.
            AgentRunError: If the run is interrupted with no checkpointer to pause on.
        """

        thread = self._resolve_thread(thread_id)
        ctx = self._new_context(deps, usage_limits, metadata)
        return await self._execute(
            ctx, *self._graph_input(input, history), thread_id=thread, timeout=timeout
        )

    def _stream_task(self, request: RunRequest) -> tuple[RunContext[Any], StreamStart]:
        thread = self._resolve_thread(request.thread_id)
        ctx = self._new_context(request.deps, request.usage_limits, request.metadata)
        graph_input, input_text, history_length = self._graph_input(request.input, request.history)

        def begin(subscriber: EventSubscriber) -> Coroutine[Any, Any, RunResult[AgentOutputT]]:
            return self._execute(
                ctx,
                graph_input,
                input_text,
                history_length,
                thread_id=thread,
                timeout=request.timeout,
                stream_text=True,
                subscribers=(subscriber,),
            )

        return ctx, begin

    async def resume(
        self,
        thread_id: str | None,
        decision: Any,
        *,
        deps: AgentDepsT | None = None,
        usage_limits: UsageLimits | None = None,
        timeout: float | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> RunResult[AgentOutputT]:
        """Resume a run that paused on an interrupt, or continue a stopped one.

        A cancelled or timed-out run keeps its thread at the last completed step; without a pending
        interrupt, `resume` continues from there and `decision` is ignored (pass `None`).

        Args:
            decision: The value returned to the pending `interrupt(...)`; with several, a mapping of
                interrupt id (from `RunResult.interrupts`) to decision.

        Raises:
            ConfigurationError: If the agent has no checkpointer.
            NoPendingInterruptError: If `thread_id` is `None` (the run kept no thread).
            RunTimeoutError: If `timeout` expired.
            RunCancelledError: If the run was stopped with `awa.runtime.cancel`.
        """

        if thread_id is None:
            raise NoPendingInterruptError(f"Agent {self.name!r} has no thread to resume")
        thread = self._resolve_thread(thread_id)
        ctx = self._new_context(deps, usage_limits, metadata)
        return await self._execute(
            ctx, Resume(decision), None, 0, thread_id=thread, timeout=timeout
        )

    def as_tool(self, *, name: str | None = None, description: str | None = None) -> BaseTool:
        """Expose the agent as a nested-run tool returning its final text, or the JSON of its output.

        Args:
            name: Tool name; defaults to the agent name.
            description: Tool description; defaults to the agent description.
        """

        from awa.agents.boundary import as_tool_for

        return as_tool_for(
            self, self._output_text, noun="agent", name=name, description=description
        )

    def clone(self, **overrides: Any) -> Agent[AgentDepsT, AgentOutputT]:
        """Return a new, independent agent with the same configuration except the keyword `overrides`."""

        return Agent(**{"name": self._name, **self._options, **overrides})

    def with_tools(
        self,
        *tools: AgentTool,
        extra_instructions: str = "",
        middleware: Sequence[AgentMiddleware[Any, Any, Any]] = (),
    ) -> Agent[AgentDepsT, AgentOutputT]:
        """Return a variant with additional tools, instructions and middleware, sharing this identity.

        Args:
            extra_instructions: Text appended to the (rendered) instructions.
        """

        instructions: Instructions[AgentDepsT] = self._instructions
        if extra_instructions:
            instructions = _append_instructions(instructions, extra_instructions)
        return self._variant(
            tools=(*self._options["tools"], *tools),
            instructions=instructions,
            middleware=(*self._options["middleware"], *middleware),
        )

    def _variant(self, **overrides: Any) -> Agent[AgentDepsT, AgentOutputT]:
        """A `clone` sharing this agent's identity, so `override` applies to both."""

        variant = self.clone(**overrides)
        variant._identity = self._identity
        return variant

    @contextmanager
    def override(
        self, *, model: str | BaseChatModel | None = None, deps: AgentDepsT | None = None
    ) -> Iterator[None]:
        """Temporarily replace the model and/or deps of runs started inside the block (for tests).

        It also applies when the agent runs nested (a delegated team member). A `with_tools` variant
        shares its source's identity, a `clone` is never affected. `None` keeps the current value;
        nested blocks stack.

        Example:
            ```python
            with researcher.override(model=FakeChatModel(["stub answer"])):
                result = await team.run("question")
            ```
        """

        current = _overrides.get() or {}
        previous = current.get(self._identity) or _Override(model=None, deps=None)
        entry = _Override(
            model=model if model is not None else previous.model,
            deps=deps if deps is not None else previous.deps,
        )
        with bound(_overrides, {**current, self._identity: entry}):
            yield

    def _spec_attributes(self) -> dict[str, str | int | float | bool]:
        """What tracing shows of the agent spec on the agent's span: the `awa.agent.*` entries
        of the card's metadata (scalars only) and the hash of the static system prompt."""

        from awa.runtime._run import AGENT_SPEC_METADATA_PREFIX

        values: dict[str, str | int | float | bool] = {
            key: value
            for key, value in self._card.metadata.items()
            if key.startswith(AGENT_SPEC_METADATA_PREFIX)
            and isinstance(value, str | int | float | bool)
        }
        if self._static_prompt is not None:
            digest = hashlib.sha256(self._static_prompt.encode("utf-8")).hexdigest()[:16]
            values[f"{AGENT_SPEC_METADATA_PREFIX}prompt_hash"] = digest
        return values

    def _compose_prompt(self, base: str) -> str:
        """`base` with the skill instructions (or index) and the memory note appended."""

        prompt = base
        if self._skill_registry is not None:
            if self._skill_mode == "inline":
                from awa.skills import compose_prompt

                prompt = compose_prompt(prompt, list(self._skills), self._skill_registry)
            else:
                index = self._skill_registry.render_index([s.name for s in self._skills])
                prompt = _joined(prompt, index)
        if self._memory is not None:
            prompt = _joined(prompt, self._memory.instructions)
        return prompt

    async def _recalled_memories(
        self, ctx: RunContext[Any], input_text: str | None
    ) -> HumanMessage | None:
        """The `auto_recall` context message for this run, if anything was recalled."""

        memory = self._memory
        if memory is None or not memory.auto_recall or not input_text:
            return None
        recalled = await memory.context_block(
            input_text, owner=memory.owner_for_run(ctx), limit=memory.auto_recall
        )
        if not recalled:
            return None
        from awa.memory.long_term import recalled_memories_message

        return recalled_memories_message(recalled, run_id=ctx.run_id)

    async def _system_prompt(self, ctx: RunContext[Any]) -> str:
        if self._static_prompt is not None:
            return self._static_prompt
        return self._compose_prompt(await _render_instructions(self._instructions, ctx))

    def _compiled_graph(self, override: _Override | None) -> Any:
        """The engine handle this run uses, built once per engine, override and replay."""

        from awa.models.replay import active_replay

        engine = current_engine()
        if engine is not self._engine:
            self._engine = engine
            self._graph = None
            self._override_graphs.clear()
            self._replay_graphs.clear()
        replay = active_replay()
        if replay is not None:
            # Inside a replay scope (evaluations and tests) the model answers from recorded
            # responses: a graph of its own, never the one live runs use.
            graph = self._replay_graphs.get(replay)
            if graph is None:
                chosen = override.model if override is not None and override.model else self._model
                graph = self._build_graph(replay.bind(self._resolve_model(chosen)))
                self._replay_graphs[replay] = graph
            return graph
        if override is not None and override.model is not None:
            graph = self._override_graphs.get(override)
            if graph is None:
                graph = self._build_graph(self._resolve_model(override.model))
                self._override_graphs[override] = graph
            return graph
        if self._graph is None:
            self._graph = self._build_graph(self._resolve_model(self._model))
        return self._graph

    def _resolve_model(self, model: str | BaseChatModel | None) -> BaseChatModel:
        """`model` as a chat model; `model_settings` apply unless it is an instance."""

        from awa.models import resolve_model

        settings = None if _is_model_instance(model) else self._model_settings
        return resolve_model(model, settings=settings, registry=self._model_registry)

    def _build_graph(self, model: BaseChatModel) -> Any:
        from awa.agents._stack import build_stack

        spec = AgentSpec(
            name=self._name,
            model=model,
            tools=self._tools,
            middleware=build_stack(self, model),
            response_format=self._output_type,
            checkpointer=self._checkpointer,
            store=self._store,
        )
        return current_engine().build_agent(spec)

    def _spend_step(self) -> bool:
        """Count a model call for an in-call output retry, or report that no step is left."""

        ctx = current_run_context()
        return ctx is not None and self._control.spend_step(ctx.run_id)

    def _guard_layers(self) -> list[Any]:
        """This agent's layers every nested run inherits: its tool policy, boundary hooks."""

        from awa.agents.boundary import AgentCallGuard

        own: list[Any] = [self._tool_policy] if self._tool_policy is not None else []
        return [*own, *(m for m in self._middleware if isinstance(m, AgentCallGuard))]

    def _active_override(self) -> _Override | None:
        return (_overrides.get() or {}).get(self._identity)

    def _resolve_thread(self, thread_id: str | None) -> str | None:
        return resolve_thread(f"Agent {self._name!r}", self._checkpointer, thread_id)

    def _new_context(
        self,
        deps: AgentDepsT | None,
        usage_limits: UsageLimits | None,
        metadata: Mapping[str, Any] | None,
    ) -> RunContext[Any]:
        override = self._active_override()
        if override is not None and override.deps is not None:
            deps = override.deps
        return new_run_context(self._name, deps, usage_limits, metadata)

    @staticmethod
    def _graph_input(
        input: AgentInput, history: Sequence[BaseMessage | dict[str, Any]] | None
    ) -> tuple[dict[str, Any], str, int]:
        from awa._internal.upstream.messages import HumanMessage, convert_to_messages

        messages = (
            [HumanMessage(content=input)] if isinstance(input, str) else convert_to_messages(input)
        )
        prior = convert_to_messages(history) if history else []
        return {"messages": [*prior, *messages]}, _input_text(messages), len(prior)

    async def _execute(
        self,
        ctx: RunContext[Any],
        graph_input: dict[str, Any] | Resume,
        input_text: str | None,
        history_length: int,
        *,
        thread_id: str | None,
        timeout: float | None = None,
        stream_text: bool = False,
        subscribers: Sequence[EventSubscriber] = (),
    ) -> RunResult[AgentOutputT]:
        engine = current_engine()
        resumable = engine.in_thread()

        async def work(started: float) -> RunResult[AgentOutputT]:
            try:
                return await self._invoke(
                    ctx, graph_input, input_text, history_length, thread_id, stream_text, started
                )
            except engine.control_flow as exc:
                if resumable or not engine.pauses(exc):
                    raise
                # `run_observed` reports this as the run's error instead of a pause.
                raise AgentRunError(
                    f"Agent {self._name!r} was interrupted inside a nested run, but no "
                    "enclosing graph has a checkpointer to pause on. Give the top-level "
                    "agent, team, workflow or network a checkpointer to use "
                    "human-in-the-loop in nested agents."
                ) from exc

        return await run_observed(
            ctx,
            work,
            kind="agent",
            input_text=input_text or "",
            output_text=self._output_text,
            timeout=timeout,
            subscribers=(*self._hooks, *subscribers),
            thread_id=thread_id,
            role=self._card.role,
            spec=self._spec_attributes(),
        )

    async def _invoke(
        self,
        ctx: RunContext[Any],
        graph_input: dict[str, Any] | Resume | None,
        input_text: str | None,
        history_length: int,
        thread_id: str | None,
        stream_text: bool,
        started: float,
    ) -> RunResult[AgentOutputT]:
        from awa.agents._stack import rejected_error
        from awa.agents.boundary import enter_guards
        from awa.kernel.output import OutputRejected
        from awa.tools.risk import graph_durability

        engine = current_engine()
        # The guard layers of this agent apply to every run it starts (deny wins).
        enter_guards(ctx, self._guard_layers())
        graph = self._compiled_graph(self._active_override())
        system_prompt = await self._system_prompt(ctx)
        graph_input, input_text = self._guarded_input(graph_input, input_text)
        memories = await self._recalled_memories(ctx, input_text)
        graph_input, start = await self._continue_thread(
            ctx, graph, graph_input, history_length, thread_id
        )

        self._control.begin(ctx.run_id, system_prompt, memories)
        try:
            options = RunOptions(
                run_name=self._name,
                thread_id=thread_id,
                step_limit=10 * (self._max_steps + 1),
                durable_writes=graph_durability(self, self._name, self._checkpointer),
                stream_text=stream_text,
                pause_order=self._control.pause_order(ctx.run_id),
            )
            outcome = await engine.run(graph, graph_input, options)
        except OutputRejected as exc:
            raise rejected_error(self._name, exc) from exc
        except Exception as exc:
            if not engine.is_step_limit(exc):
                raise
            raise MaxStepsExceededError(
                f"Agent {self._name!r} exceeded max_steps={self._max_steps}: the run did not "
                "converge within its step budget"
            ) from exc
        finally:
            self._control.end(ctx.run_id)
            compactions = self._compaction_log.pop(ctx.run_id, None)
        if compactions:
            from awa.memory.compaction import shift_start

            # Older messages were replaced by a summary; find this run's new ones again.
            start = shift_start(start, compactions)
        if outcome.pending and thread_id is None and not engine.in_thread():
            # Without a thread the paused run could never be resumed.
            raise AgentRunError(
                f"Agent {self._name!r} was interrupted, but it has no checkpointer to pause "
                "on. Give the agent a checkpointer to use human-in-the-loop interrupts."
            )
        return self._build_result(ctx, outcome, start, thread_id, started)

    def _guarded_input(
        self, graph_input: dict[str, Any] | Resume | None, input_text: str | None
    ) -> tuple[dict[str, Any] | Resume | None, str | None]:
        """The input after the input guardrails, which may block it or sanitize its text."""

        if input_text is None or not self._input_guardrails:
            return graph_input, input_text
        from awa.guardrails import run_input_guardrails

        checked = run_input_guardrails(input_text, self._input_guardrails, raise_on_block=True)
        if checked.replacement is None or not isinstance(graph_input, dict):
            return graph_input, input_text
        # A guardrail sanitized the input (for example redacted personal data).
        text = str(checked.replacement)
        messages = replace_last_human_text(graph_input["messages"], text)
        return {**graph_input, "messages": messages}, text

    async def _continue_thread(
        self,
        ctx: RunContext[Any],
        graph: Any,
        graph_input: dict[str, Any] | Resume | None,
        history_length: int,
        thread_id: str | None,
    ) -> tuple[dict[str, Any] | Resume | None, int]:
        """The graph input and where this run's new messages start, after the saved thread."""

        engine = current_engine()
        start = history_length
        carried: list[Any] = []
        if thread_id is not None:
            saved = await engine.state(graph, thread_id)
            carried = list(saved.values.get("messages", []))
            start += len(carried)
            if isinstance(graph_input, Resume) and not saved.pending and saved.has_next:
                # Resuming a run that was stopped mid-run: continue from its last step.
                graph_input = None
            if not isinstance(graph_input, dict) and ctx.parent_run_id is None:
                from awa.agents.middleware import BUDGET_STATE_KEY, restore_budget

                # A resumed or continued run goes on with the usage, limits and loop
                # counters of the run it continues: a resume never resets a budget.
                restore_budget(ctx, saved.values.get(BUDGET_STATE_KEY))
        if isinstance(graph_input, dict):
            from awa.kernel.tool_guard import unanswered_tool_calls

            # A tool call left without a result is answered by a message of its own before
            # the run's new messages (`RepairToolCallsMiddleware`).
            carried += graph_input["messages"][:history_length]
            start += unanswered_tool_calls(carried)
        return graph_input, start

    def _build_result(
        self,
        ctx: RunContext[Any],
        outcome: EngineOutcome,
        start: int,
        thread_id: str | None,
        started: float,
    ) -> RunResult[AgentOutputT]:
        from awa._internal.upstream.messages import AIMessage
        from awa.contracts.results import RunResult

        state = outcome.values
        messages: list[BaseMessage] = list(state.get("messages", []))
        interrupts = [{"id": pause.id, "value": pause.value} for pause in outcome.pending]
        reply = next((m for m in reversed(messages[start:]) if isinstance(m, AIMessage)), None)
        text = str(reply.text) if reply is not None else ""
        output: Any = None
        if not interrupts:
            if self._output_type is not None:
                if state.get("structured_response") is None:
                    raise StructuredOutputError(
                        f"Agent {self._name!r} finished without returning a "
                        f"{self._output_type.__name__}"
                    )
                output = state["structured_response"]
            elif reply is None:
                raise AgentRunError(
                    f"Agent {self._name!r} finished without a model response. If you resumed a "
                    "thread, it has probably finished already or has no pending approval; "
                    "start a new run instead of resuming it"
                )
            else:
                output = text
        result: RunResult[AgentOutputT] = RunResult(
            run_id=ctx.run_id,
            agent=self._name,
            output=output,
            text=text,
            messages=messages,
            new_messages_start=start,
            usage=ctx.usage.snapshot(),
            duration_ms=elapsed_ms(started),
            handoff_path=(self._name,),
            thread_id=thread_id,
            interrupts=interrupts,
        )
        if not interrupts and self._output_guardrails:
            from awa.guardrails import run_output_guardrails

            checked = run_output_guardrails(
                result.output, self._output_guardrails, raise_on_block=True
            )
            if checked.replacement is not None:
                result = self._replace_output(result, reply, checked.replacement)
        return result

    def _replace_output(
        self, result: RunResult[AgentOutputT], reply: AIMessage | None, replacement: Any
    ) -> RunResult[AgentOutputT]:
        """`result` with the output a guardrail sanitized (the thread keeps the original)."""

        if self._output_type is not None or not isinstance(replacement, str):
            return result.model_copy(update={"output": replacement})
        messages = list(result.messages)
        if reply is not None:
            index = next(i for i in reversed(range(len(messages))) if messages[i] is reply)
            messages[index] = _with_text(reply, replacement)
        return result.model_copy(
            update={"output": replacement, "text": replacement, "messages": messages}
        )

    def _output_text(self, result: RunResult[Any]) -> str:
        if self._output_adapter is None or result.output is None:
            return result.text
        return self._output_adapter.dump_json(result.output).decode()


def _output_adapter(output_type: type[Any]) -> TypeAdapter[Any]:
    """Build the validator for `output_type`, explaining the Python 3.11 TypedDict rule."""
    try:
        return TypeAdapter(output_type)
    except PydanticUserError as exc:
        if exc.code != "typed-dict-version":
            raise
        raise TypeError(
            f"output_type {getattr(output_type, '__name__', output_type)!r} uses "
            "typing.TypedDict, which pydantic cannot validate on Python < 3.12; define it "
            "with `from typing_extensions import TypedDict` instead"
        ) from exc


def _resolve_skills(
    skills: Sequence[str | Skill], registry: SkillRegistry | None
) -> tuple[Skill, ...]:
    if not skills:
        return ()
    from awa.skills import Skill, SkillRegistry

    source = registry if registry is not None else SkillRegistry.builtin()
    resolved: dict[str, Skill] = {}
    for item in skills:
        skill = item if isinstance(item, Skill) else source.get(item)
        resolved.setdefault(skill.name, skill)
    return tuple(resolved.values())


def _skill_tools(registry: SkillRegistry, skills: Sequence[Skill], mode: str) -> list[BaseTool]:
    """Skill tools: `load_skill` in on-demand mode, `read_skill_resource` only when
    a skill bundles resource files (and in on-demand mode, where the catalog names it)."""

    from awa.skills import create_skill_tools

    tools: list[BaseTool] = []
    if mode == "on_demand":
        loader = create_skill_tools(registry, allowed=[s.name for s in skills])
        tools += [t for t in loader if t.name == "load_skill"]
    with_resources = [s.name for s in skills if s.resources]
    if with_resources:
        reader = create_skill_tools(registry, allowed=with_resources)
        tools += [t for t in reader if t.name == "read_skill_resource"]
    return tools


def _as_tools(tools: Sequence[AgentTool]) -> list[BaseTool]:
    """`tools` as LangChain tools: functions converted, `Toolbox`es expanded."""

    if not tools:
        return []
    from awa._internal.upstream.tools import BaseTool
    from awa._internal.upstream.tools import tool as as_langchain_tool
    from awa.tools.toolset import Toolbox

    converted: list[BaseTool] = []
    for item in tools:
        if isinstance(item, Toolbox):
            converted.extend(item.tools())
        elif isinstance(item, BaseTool):
            converted.append(item)
        elif callable(item):
            converted.append(as_langchain_tool(item))
        else:
            raise TypeError(f"Expected a BaseTool, function or Toolbox, got {type(item).__name__}")
    return converted


def _middleware_tool_names(
    middleware: Sequence[AgentMiddleware[Any, Any, Any]], known: list[str]
) -> list[str]:
    """Names of the tools middleware adds to the agent, other than `known` ones."""

    if not middleware:
        return []
    from awa._internal.upstream.tools import BaseTool

    return [
        t.name
        for m in middleware
        for t in getattr(m, "tools", ())
        if isinstance(t, BaseTool) and t.name not in known
    ]


def _policy_middleware(value: ToolPolicySpec | None) -> ToolPolicyMiddleware | None:
    """`tool_policy` as one `ToolPolicyMiddleware` (`None` for no policy)."""

    if value is None:
        return None
    from awa.tools.policy import ToolPolicyMiddleware

    if isinstance(value, ToolPolicyMiddleware):
        return value
    if callable(value):
        return ToolPolicyMiddleware(value)
    if isinstance(value, Sequence) and not isinstance(value, str):
        policies = list(value)
        for policy in policies:
            if not callable(policy):
                raise TypeError(
                    f"tool_policy entries must be ToolPolicy functions, got {type(policy).__name__}"
                )
        return ToolPolicyMiddleware(*policies) if policies else None
    raise TypeError(
        "tool_policy must be a ToolPolicyMiddleware, a ToolPolicy function or a sequence of "
        f"them, got {type(value).__name__}"
    )


def _is_model_instance(model: str | BaseChatModel | None) -> bool:
    if model is None or isinstance(model, str):
        return False
    from awa._internal.upstream.models import BaseChatModel

    return isinstance(model, BaseChatModel)


def _describe_model(model: str | BaseChatModel | None) -> str:
    if model is None:
        return "default"
    if isinstance(model, str):
        return model
    for attribute in ("model_name", "model", "model_id"):
        value = getattr(model, attribute, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


async def _render_instructions(instructions: Instructions[Any], ctx: RunContext[Any]) -> str:
    if isinstance(instructions, str):
        return instructions
    rendered = instructions(ctx)
    if inspect.isawaitable(rendered):
        rendered = await rendered
    return rendered


def _append_instructions(instructions: Instructions[Any], extra: str) -> Instructions[Any]:
    if isinstance(instructions, str):
        return _joined(instructions, extra)

    async def combined(ctx: RunContext[Any]) -> str:
        return _joined(await _render_instructions(instructions, ctx), extra)

    return combined


def _joined(text: str, extra: str) -> str:
    """`extra` appended to `text` as a new paragraph (just `extra` when `text` is empty)."""

    return f"{text}\n\n{extra}" if text else extra


def replace_last_human_text(messages: Sequence[BaseMessage], text: str) -> list[BaseMessage]:
    """`messages` with the text of the last user message replaced by `text`."""

    from awa._internal.upstream.messages import HumanMessage

    replaced = list(messages)
    for index in reversed(range(len(replaced))):
        if isinstance(replaced[index], HumanMessage):
            replaced[index] = _with_text(replaced[index], text)
            break
    return replaced


def _with_text(message: BaseMessage, text: str) -> BaseMessage:
    """A copy of `message` whose text is `text`; non-text content blocks are kept."""

    content = message.content
    if isinstance(content, str):
        return message.model_copy(update={"content": text})
    others = [
        block
        for block in content
        if not (isinstance(block, str) or (isinstance(block, dict) and block.get("type") == "text"))
    ]
    return message.model_copy(update={"content": [{"type": "text", "text": text}, *others]})


def _input_text(messages: Sequence[BaseMessage]) -> str:
    from awa._internal.upstream.messages import HumanMessage

    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.text)
    return ""


__all__ = ["Agent", "AgentDepsT", "AgentOutputT", "AgentTool", "Instructions", "ToolPolicySpec"]
