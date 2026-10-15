"""Apply an agent's `describe:`, `refine:` and `refinements:` while the blueprint is built.

`refine:` maps onto plain LangChain objects: model fields per attempt, prompt caching from
`anthropic.cache_ttl`, middleware classes by name, and `refine.py` hooks beside the blueprint
(imported only with `allow_imports=True`).
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awa._internal.upstream.agents import (
    AgentMiddleware,
    ContextEditingMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    builtin_middleware,
)
from awa._internal.upstream.messages import AIMessage
from awa._internal.upstream.models import BaseChatModel
from awa._internal.upstream.tools import BaseTool
from awa._internal.util import is_within
from awa.blueprints.errors import BlueprintError
from awa.models.errors import model_provider
from awa.refine import MARK, HookKind, RefineContext

if TYPE_CHECKING:
    from awa.blueprints._schema_agent import AgentBlueprint
    from awa.blueprints._schema_refine import RefineBlueprint
    from awa.runtime.cache import PromptCacheMode

HOOKS_FILE = "refine.py"
"""The hooks file beside a blueprint."""

CONTEXT_MANAGEMENT_BETA = "context-management-2025-06-27"
"""The Anthropic beta that server-side context editing (`context_management`) needs."""


class RefineMiddleware(AgentMiddleware):
    """`refine:` fields applied to each model attempt by the attempt's provider.

    `per_attempt = True` places it inside the resilience middleware, so every retry and fallback
    is decided on its own. With `anthropic.context_management` the server clears old tool
    results on Claude attempts, so this middleware does the client-side editing for the others.
    """

    per_attempt = True

    def __init__(self, agent: str, refine: RefineBlueprint) -> None:
        super().__init__()
        self.agent = agent
        self.refine = refine
        self.replaces_context_editing = bool(refine.anthropic.get("context_management"))
        self._client_editing: ContextEditingMiddleware | None = None
        if self.replaces_context_editing:
            from awa.memory.context_editing import from_settings

            editing = from_settings()
            self._client_editing = editing.middleware() if editing is not None else None

    @property
    def name(self) -> str:
        return f"RefineMiddleware[{self.agent}]"

    def prepare(self, request: ModelRequest[Any]) -> ModelRequest[Any]:
        """`request` with the fields of its model's provider on the model or the call."""

        update = self.refine.section(model_provider(request.model))
        if not update:
            return request
        if update.get("context_management"):
            betas = list(update.get("betas") or getattr(request.model, "betas", None) or [])
            if CONTEXT_MANAGEMENT_BETA not in betas:
                update["betas"] = [*betas, CONTEXT_MANAGEMENT_BETA]
        fields = getattr(type(request.model), "model_fields", {})
        on_model = {key: value for key, value in update.items() if key in fields}
        on_call = {key: value for key, value in update.items() if key not in fields}
        model = request.model.model_copy(update=on_model) if on_model else request.model
        settings = {**request.model_settings, **on_call}
        return request.override(model=model, model_settings=settings)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]:
        prepared = self.prepare(request)
        if self._client_editing is not None and model_provider(request.model) != "anthropic":
            return await self._client_editing.awrap_model_call(prepared, handler)
        return await handler(prepared)


@dataclass(frozen=True)
class Hooks:
    """The `awa.refine` hooks of one `refine.py`, in definition order."""

    entries: tuple[tuple[HookKind, tuple[str, ...], Callable[..., Any]], ...] = ()

    def of(self, kind: HookKind, agent: str) -> list[Callable[..., Any]]:
        return [
            hook
            for k, agents, hook in self.entries
            if k == kind and (not agents or agent in agents)
        ]


def load_hooks(base: Path | None, *, allow_imports: bool) -> Hooks:
    """The hooks in `base/refine.py`; none without the file.

    Raises:
        BlueprintError: If the file exists but `allow_imports` is false, or cannot be imported.
    """

    path = base / HOOKS_FILE if base is not None else None
    if path is None or not path.is_file():
        return Hooks()
    if not allow_imports:
        raise BlueprintError(
            f"{path} holds refinement hooks; pass allow_imports=True to run its code "
            "(awa run and awa eval do)"
        )
    name = f"_awa_refine_{abs(hash(str(path.resolve())))}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise BlueprintError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - user code: any failure is a blueprint error
        sys.modules.pop(name, None)
        raise BlueprintError(f"Cannot import {path}: {type(exc).__name__}: {exc}") from exc
    entries = [
        (*mark, value)
        for value in vars(module).values()
        if callable(value) and isinstance(mark := getattr(value, MARK, None), tuple)
    ]
    return Hooks(tuple(entries))


@dataclass
class Refined:
    """What `AgentRefiner.prepare` gives the agent: its model, extra middleware, cache mode."""

    model: Any
    middleware: list[Any] = field(default_factory=list)
    prompt_cache: PromptCacheMode | None = None


class AgentRefiner:
    """Applies `refine:` and the hooks of one blueprint, agent by agent."""

    def __init__(self, project: str, base: Path | None, *, allow_imports: bool) -> None:
        self.project = project
        self.base = base
        self.hooks = load_hooks(base, allow_imports=allow_imports)

    def prepare(self, spec: AgentBlueprint, model: Any, resolve: Callable[[], Any]) -> Refined:
        """The agent's model and middleware with `spec.refine` and the hooks; `resolve` is called only when a hook needs the model."""

        refine = spec.refine
        refined = Refined(model=model)
        if refine is not None:
            ttl = refine.anthropic.get("cache_ttl")
            if ttl is not None:
                refined.prompt_cache = "1h" if ttl == "1h" else "auto"
            refined.middleware = [_middleware(entry) for entry in refine.middleware]
            if refine.section("anthropic") or refine.section("openai") or refine.section("google"):
                refined.middleware.append(RefineMiddleware(spec.name, refine))
        ctx = RefineContext(project=self.project, agent=spec.name, model=_reference(model))
        model_hooks = self.hooks.of("model", spec.name)
        if model_hooks:
            instance = resolve()
            for hook in model_hooks:
                instance = _called(hook, instance, ctx)
                if not isinstance(instance, BaseChatModel):
                    raise BlueprintError(
                        f"Hook {hook.__name__} must return a chat model, got "
                        f"{type(instance).__name__} (configure it with model.model_copy(update=...))"
                    )
            refined.model = instance
        for hook in self.hooks.of("agent", spec.name):
            result = _called(hook, list(refined.middleware), ctx)
            if not isinstance(result, Sequence):
                raise BlueprintError(f"Hook {hook.__name__} must return a list of middleware")
            refined.middleware = list(result)
        return refined

    def instructions(self, spec: AgentBlueprint) -> str | None:
        """The text of `describe.instructions_file`, or `None` without one."""

        file = spec.describe.instructions_file if spec.describe is not None else None
        if not file:
            return None
        if self.base is None:
            raise BlueprintError(
                f"Agent {spec.name!r} reads its instructions from {file!r}; load the blueprint "
                "from its file so the path resolves"
            )
        path = self.base / file
        if not is_within(path, self.base):
            raise BlueprintError(
                f"Agent {spec.name!r}: the instructions file {file!r} lies outside the "
                "blueprint's folder"
            )
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise BlueprintError(f"Agent {spec.name!r}: cannot read {path}: {exc}") from exc


def refinement_note(spec: AgentBlueprint) -> str:
    """The accepted refinement lines as one instructions section, or `""`."""

    if not spec.refinements:
        return ""
    lines = "\n".join(f"- {line.text.strip()}" for line in spec.refinements)
    return f"## Learned from test runs\n{lines}"


def refined_tools(spec: AgentBlueprint, tools: list[BaseTool]) -> list[BaseTool]:
    """`tools` with the descriptions of `describe.tool_docs` (copies; the shared ones stay)."""

    docs = spec.describe.tool_docs if spec.describe is not None else {}
    if not docs:
        return tools
    unknown = sorted(set(docs) - {tool.name for tool in tools})
    if unknown:
        raise BlueprintError(
            f"Agent {spec.name!r}: describe.tool_docs names tools it does not have: "
            + ", ".join(unknown)
        )
    return [
        tool.model_copy(update={"description": docs[tool.name]}) if tool.name in docs else tool
        for tool in tools
    ]


def _middleware(entry: dict[str, dict[str, Any]]) -> Any:
    ((name, kwargs),) = entry.items()
    target = builtin_middleware(name)
    if target is None:  # the schema checked it; the installed packages may differ at build
        raise BlueprintError(f"{name!r} is not a LangChain agent middleware")
    try:
        return target(**(kwargs or {}))
    except Exception as exc:  # noqa: BLE001 - upstream constructors raise anything
        raise BlueprintError(f"Cannot build {name}: {type(exc).__name__}: {exc}") from exc


def _called(hook: Callable[..., Any], value: Any, ctx: RefineContext) -> Any:
    try:
        result = hook(value, ctx)
    except Exception as exc:  # noqa: BLE001 - user code: any failure is a blueprint error
        raise BlueprintError(
            f"Refinement hook {getattr(hook, '__name__', hook)!r} of agent {ctx.agent!r} "
            f"failed: {type(exc).__name__}: {exc}"
        ) from exc
    if result is None:
        raise BlueprintError(
            f"Refinement hook {getattr(hook, '__name__', hook)!r} returned nothing; return the "
            "refined object"
        )
    return result


def _reference(model: Any) -> str | None:
    return model if isinstance(model, str) else None
