"""Turn a checked `Blueprint` into runnable agents, a team, a workflow or a network."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from itertools import count
from pathlib import Path
from typing import Any, Unpack

from awa._internal.logging import get_logger
from awa._internal.upstream.checkpoint import BaseCheckpointSaver
from awa._internal.upstream.models import BaseChatModel
from awa._internal.upstream.tools import BaseTool
from awa.agents import Agent
from awa.blueprints._agent_facts import effective_instructions, effective_skills, trace_attributes
from awa.blueprints._build_process import build_process
from awa.blueprints._build_roles import (
    RoleCompiler,
    role_max_steps,
    role_model_settings,
    role_output_type,
    role_policies,
)
from awa.blueprints._check import check_blueprint, check_expanded, with_auto
from awa.blueprints._mcp import is_mcp_reference, loaded_tools, mcp_tool_name, parse_mcp_reference
from awa.blueprints._process_documents import documents_note
from awa.blueprints._refine import AgentRefiner, refined_tools, refinement_note
from awa.blueprints._resolve import (
    ToolCatalog,
    build_builtin,
    build_connector,
    builtin_entry,
    import_callable,
    import_object,
    is_builtin_reference,
    is_connector_reference,
    is_import_reference,
    to_tool,
)
from awa.blueprints._roles import role_root
from awa.blueprints.auto import AutoReport, remember_report
from awa.blueprints.errors import BlueprintError, BlueprintIssue
from awa.blueprints.json_schema import model_from_json_schema
from awa.blueprints.schema import (
    AgentBlueprint,
    AgentBuildOptions,
    Blueprint,
    BuildOptions,
    LoopStep,
    MapStep,
    NetworkBlueprint,
    ParallelStep,
    SequentialStep,
    Structure,
    TeamBlueprint,
    ToolSource,
    WorkflowNode,
)
from awa.contracts.protocols import AgentLike
from awa.contracts.results import RunResult
from awa.errors import AwaError, ConfigurationError
from awa.mcp import MCPServer
from awa.memory import AgentMemory, Compaction
from awa.messaging import BaseBlackboard, MessageBus
from awa.models import ModelSettings, resolve_model
from awa.plugins.switches import ComponentError, is_off, using_blueprint
from awa.runtime.cache import Cache
from awa.runtime.case import in_case_dir
from awa.skills import SkillRegistry
from awa.tasks import BaseTaskBoard
from awa.team import NetworkLimits, Team, TeamLimits
from awa.team.network import AgentNetwork
from awa.tools import create_tool_approval_wrapper, harden_tool
from awa.tools.approval import ApprovalCondition
from awa.tools.decorator import tool_spec
from awa.tools.policy import ToolPolicy, ask_for_risk, deny_tools
from awa.workflows import Loop, Map, Parallel, Sequential

_log = get_logger("blueprints")

_PATH_OPTIONS = frozenset({"root", "path"})
"""Toolkit options holding a file or folder (`files.root`, `knowledge.path`, ...)."""


def build_blueprint(
    blueprint: Blueprint,
    *,
    prebuilt: Mapping[str, AgentLike] | None = None,
    **options: Unpack[BuildOptions],
) -> AgentLike:
    """Build `blueprint` (see `Blueprint.build`); `prebuilt` agents are used as they are."""

    _check_options(options, BuildOptions)
    skills = options.get("skill_registry")
    with using_blueprint(blueprint.component_entries()):
        expanded, report = _expanded(blueprint, options.get("tools"), skills)
        system = _Builder(
            expanded,
            options,
            prebuilt=prebuilt,
            task_board=options.get("task_board"),
            blackboard=options.get("blackboard"),
            bus=options.get("bus"),
        ).build()
    remember_report(system, report or blueprint.explain(skills=skills))
    return system


def build_blueprint_agent(
    blueprint: Blueprint, name: str, **options: Unpack[AgentBuildOptions]
) -> AgentLike:
    """Build one role of `blueprint`; see `Blueprint.build_agent`."""

    _check_options(options, AgentBuildOptions)
    try:
        spec = blueprint.agent(name)
    except KeyError:
        known = ", ".join(agent.name for agent in blueprint.agents)
        raise BlueprintError(
            f"Blueprint {blueprint.name!r} has no agent {name!r} (agents: {known})"
        ) from None
    with using_blueprint(blueprint.component_entries()):
        expanded, _ = _expanded(blueprint, options.get("tools"), options.get("skill_registry"))
        builder = _Builder(expanded, options)
        return builder._agent(expanded.agent(spec.name), checkpointer=options.get("checkpointer"))


async def abuild_blueprint(blueprint: Blueprint, **options: Unpack[BuildOptions]) -> AgentLike:
    """Load the MCP tools `blueprint` references, then build it; see `Blueprint.abuild`."""

    _check_options(options, BuildOptions)
    servers = blueprint.active_mcp_servers()
    if servers:
        _raise_errors(blueprint, check_blueprint(blueprint, skills=options.get("skill_registry")))
        options["tools"] = await _with_mcp_tools(options.get("tools"), servers)
    return build_blueprint(blueprint, **options)


def _check_options(options: Mapping[str, Any], allowed: Any) -> None:
    """Raise `BlueprintError` for a keyword option the `TypedDict` `allowed` does not declare."""

    unknown = sorted(set(options) - set(allowed.__optional_keys__) - set(allowed.__required_keys__))
    if unknown:
        known = ", ".join(sorted(allowed.__optional_keys__ | allowed.__required_keys__))
        raise BlueprintError(f"Unknown build option(s) {', '.join(unknown)} (known: {known})")


def _raise_errors(blueprint: Blueprint, issues: Sequence[BlueprintIssue]) -> None:
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise BlueprintError(f"Blueprint {blueprint.name!r} cannot be built", errors)


def _expanded(
    blueprint: Blueprint, tools: ToolSource | None, skills: SkillRegistry | None
) -> tuple[Blueprint, AutoReport | None]:
    """`blueprint` with its auto-matches added and checked, and the auto report."""

    expanded, report = with_auto(blueprint, skills)
    _raise_errors(
        blueprint,
        check_expanded(expanded, report, tools=tools if tools is not None else [], skills=skills),
    )
    return expanded, report


async def _with_mcp_tools(
    tools: ToolSource | None, servers: Sequence[MCPServer]
) -> dict[str, BaseTool]:
    from awa.mcp import MCPError, load_mcp_tools

    merged = dict(ToolCatalog.from_source(tools).tools)
    try:
        mcp_tools = await load_mcp_tools(servers)
    except MCPError as exc:
        raise BlueprintError(f"Cannot load MCP tools: {exc}") from exc
    for tool in mcp_tools:
        if tool.name in merged:
            raise BlueprintError(
                f"MCP tool {tool.name!r} has the same name as another tool; set a distinct "
                "tool_prefix on its server"
            )
        merged[tool.name] = tool
    return merged


class _TextContains:
    """Loop stop condition: the iteration's final text contains a marker."""

    def __init__(self, marker: str) -> None:
        self._marker = marker

    def __call__(self, result: RunResult[Any]) -> bool:
        return self._marker in result.text

    def __repr__(self) -> str:
        return f"until_contains({self._marker!r})"


def _never(_result: RunResult[Any]) -> bool:
    return False


class _Builder:
    def __init__(
        self,
        blueprint: Blueprint,
        options: AgentBuildOptions,
        *,
        prebuilt: Mapping[str, AgentLike] | None = None,
        task_board: BaseTaskBoard | None = None,
        blackboard: BaseBlackboard | None = None,
        bus: MessageBus | None = None,
    ) -> None:
        self._bp = blueprint
        self._prebuilt = dict(prebuilt or {})
        self._task_board = task_board
        self._blackboard = blackboard
        self._bus = bus
        self._catalog = ToolCatalog.from_source(options.get("tools"))
        self._models = options.get("model_registry")
        self._skill_registry = options.get("skill_registry")
        self._checkpointer = options.get("checkpointer")
        self._allow_imports = options.get("allow_imports", False)
        self._entrypoint = blueprint.resolved_entrypoint()
        self._role_root = role_root(blueprint)
        self._tools: dict[str, BaseTool] = {}
        self._agents: dict[str, AgentLike] = {}
        self._structures: dict[Structure, AgentLike] = {}
        self._names = count(1)
        self._memories: dict[str, AgentMemory] = {}
        self._given_cache = options.get("cache")
        self._built_cache: Cache | None = None
        self._refiner = AgentRefiner(blueprint.name, self._base, allow_imports=self._allow_imports)

    @property
    def _cache(self) -> Cache:
        """The system's caches: the one given, else one from `defaults.cache` (built once)."""

        if self._given_cache is not None:
            return self._given_cache
        if self._built_cache is None:
            from awa.persistence.cache import build_cache

            source = self._bp.source
            self._built_cache = build_cache(
                self._bp.defaults.cache,
                project=self._bp.name,
                base=source.parent if source is not None else None,
            )
        return self._built_cache

    def build(self) -> AgentLike:
        self._check_options()
        for name in self._bp.active_agents():
            prebuilt = self._prebuilt.get(name)
            self._agents[name] = prebuilt or self._agent(self._bp.agent(name))
        if self._entrypoint in ("team", "workflow", "network"):
            return self._structure(self._entrypoint)  # type: ignore[arg-type]  # one of the three names above
        if self._role_root is not None:
            return self._role_system(self._role_root)
        assert self._entrypoint is not None
        return self._agents[self._entrypoint]

    def _role_system(self, root: str) -> AgentLike:
        """The `Team` and `Checked` units of the role tree led by `root`."""

        try:
            return RoleCompiler(
                self._bp, root, self._agents, self._native, checkpointer=self._checkpointer
            ).build()
        except (AwaError, ValueError, TypeError) as exc:
            raise BlueprintError(f"Cannot build the role tree of {self._bp.name!r}: {exc}") from exc

    def _structure(self, structure: Structure) -> AgentLike:
        """Build a team, workflow or network once (nested structures first)."""

        built = self._structures.get(structure)
        if built is not None:
            return built
        if structure == "team":
            assert self._bp.team is not None
            built = self._build_team(self._bp.team)
        elif structure == "network":
            assert self._bp.network is not None
            built = self._build_network(self._bp.network)
        elif self._bp.workflow is None:
            assert self._bp.process is not None
            built = build_process(
                self._bp,
                self._bp.process.steps,
                member=lambda step: self._member(step.agent),
                base=self._base,
                name=self._bp.name,
                description=self._bp.description,
                checkpointer=self._persists("workflow"),
                cache=self._cache,
            )
        else:
            built = self._node(self._bp.workflow, top_level=True)
        self._structures[structure] = built
        return built

    def _member(self, name: str) -> AgentLike:
        """An agent, or the team, workflow or network a member or step names."""

        structure = self._bp.structure_named(name)
        return self._structure(structure) if structure is not None else self._agents[name]

    def _persists(self, name: str) -> BaseCheckpointSaver[Any] | None:
        """Only the entrypoint (a role tree's root team) gets the checkpointer; nested runs persist on its thread."""

        if self._role_root is not None and name == self._role_root:
            return None
        return self._checkpointer if self._entrypoint == name else None

    def _check_options(self) -> None:
        self._check_shared_state()
        needs_approval = list(
            dict.fromkeys(
                [
                    *self._bp.approval_agents(),
                    *self._bp.policy_agents(),
                    *self._bp.step_approval_agents(),
                ]
            )
        )
        if needs_approval and self._checkpointer is None:
            raise BlueprintError(
                f"Blueprint {self._bp.name!r} declares approvals (used by "
                f"{', '.join(needs_approval)}), which pause the run until a human decides; "
                "pass a checkpointer, e.g. awa.persistence.StateInMemorySaver()"
            )

    def _check_shared_state(self) -> None:
        """Shared-state objects go to the network if one is used, else where the team uses them."""

        given = {"task_board": self._task_board, "blackboard": self._blackboard, "bus": self._bus}
        network = self._bp.network if self._bp.uses_network() else None
        spec = self._bp.team if self._bp.uses_team() else None
        for option, value in given.items():
            if value is None:
                continue
            if network is not None:
                if option == "blackboard" and not network.blackboard:
                    raise BlueprintError(
                        f"Blueprint {self._bp.name!r}: blackboard= was given but the network "
                        "sets blackboard: false"
                    )
                continue
            if spec is None:
                raise BlueprintError(
                    f"Blueprint {self._bp.name!r} has no team or network; do not pass {option}="
                )
            enabled = {
                "task_board": spec.task_board,
                "blackboard": spec.blackboard,
                "bus": spec.messaging,
            }
            if not enabled[option]:
                feature = "messaging" if option == "bus" else option
                raise BlueprintError(
                    f"Blueprint {self._bp.name!r}: {option}= was given but the team sets "
                    f"{feature}: false"
                )

    def _native(self, name: str) -> Agent[Any, Any]:
        """An agent that must be an `awa.Agent` (team and network roles; `check` ensures it)."""

        agent = self._agents[name]
        if not isinstance(agent, Agent):
            raise BlueprintError(f"Agent {name!r} is not an awa Agent and cannot fill this role")
        return agent

    def _agent(
        self, spec: AgentBlueprint, *, checkpointer: BaseCheckpointSaver[Any] | None = None
    ) -> AgentLike:
        defaults = self._bp.defaults
        model, settings = self._model(spec)
        refined = self._refiner.prepare(
            spec, model, lambda: self._model_instance(spec, model, settings)
        )
        output_type = (
            model_from_json_schema(spec.output_schema, name=f"{spec.name}_output")
            if spec.output_schema is not None
            else None
        )
        try:
            return Agent(
                spec.name,
                instructions=self._instructions(spec),
                description=spec.description,
                model=refined.model,
                model_settings=settings,
                tools=refined_tools(spec, self._agent_tools(spec)),
                middleware=refined.middleware,
                prompt_cache=refined.prompt_cache,
                output_type=role_output_type(spec) or output_type,
                skills=effective_skills(self._bp, spec, self._catalog),
                skill_registry=self._skill_registry,
                skill_mode=spec.skill_mode or defaults.skill_mode,
                capabilities=spec.capabilities,
                tags=spec.tags,
                max_steps=role_max_steps(spec, defaults.max_steps),
                checkpointer=checkpointer or self._persists(spec.name),
                version=self._bp.version,
                metadata=trace_attributes(spec),
                memory=self._memory(spec),
                compaction=(
                    Compaction(**spec.compaction.model_dump()) if spec.compaction else None
                ),
                tool_policy=[*self._tool_policies(spec), *role_policies(spec)] or None,
                cache=self._cache,
                role="orchestrator" if spec.role == "orchestrator" else None,
            )
        except BlueprintError:
            raise
        except (AwaError, ValueError, TypeError) as exc:
            raise BlueprintError(f"Cannot build agent {spec.name!r}: {exc}") from exc

    def _instructions(self, spec: AgentBlueprint) -> str:
        """The agent's instructions and role rules, with the list of the documents it always has."""

        own = self._refiner.instructions(spec)
        if own is not None:
            spec = spec.model_copy(update={"instructions": own})
        text = effective_instructions(self._bp, spec, self._catalog)
        notes = [note for note in (documents_note(self._bp, spec), refinement_note(spec)) if note]
        return "\n\n".join([text, *notes])

    def _model_instance(
        self, spec: AgentBlueprint, model: str | BaseChatModel | None, settings: Any
    ) -> BaseChatModel:
        """The agent's chat model as an instance (for a `refine_model` hook)."""

        if isinstance(model, BaseChatModel):
            return model
        try:
            return resolve_model(model, settings=settings, registry=self._models)
        except AwaError as exc:
            raise BlueprintError(f"Cannot resolve the model of agent {spec.name!r}: {exc}") from exc

    def _tool_policies(self, spec: AgentBlueprint) -> list[ToolPolicy]:
        """The blueprint's `policies:` that apply to the agent, in declaration order."""

        policies: list[ToolPolicy] = []
        for policy in self._bp.policies:
            if not policy.applies_to(spec.name):
                continue
            if policy.deny:
                policies.append(
                    deny_tools(policy.deny, reason=policy.reason)
                    if policy.reason
                    else deny_tools(policy.deny)
                )
            if policy.ask:
                policies.append(ask_for_risk(*policy.ask, reason=policy.reason))
            if policy.use is not None:
                policies.append(self._import(policy.use, "Tool policy"))
        return policies

    def _memory(self, spec: AgentBlueprint) -> AgentMemory | None:
        """One shared `AgentMemory` per scope and one store for all of them."""

        if spec.memory is None:
            return None
        options = spec.memory
        key = f"{options.scope}:{options.auto_recall}:{options.require_user}"
        if key not in self._memories:
            store = next(iter(self._memories.values())).store if self._memories else None
            self._memories[key] = AgentMemory(
                store,
                scope=options.scope,
                auto_recall=options.auto_recall,
                require_user=options.require_user,
            )
        return self._memories[key]

    def _model(
        self, spec: AgentBlueprint
    ) -> tuple[str | BaseChatModel | None, ModelSettings | None]:
        reference = spec.model or self._bp.defaults.model
        base = self._bp.defaults.model_settings
        settings = base.merge(spec.model_settings) if base is not None else spec.model_settings
        settings = role_model_settings(spec, settings)
        if self._models is None or (reference or "default") not in self._models:
            return reference, settings
        try:
            if settings is not None:
                # Profiles bound to a model instance are already configured.
                with suppress(AwaError):
                    return resolve_model(reference, settings=settings, registry=self._models), None
            return resolve_model(reference, registry=self._models), None
        except AwaError as exc:
            raise BlueprintError(f"Cannot resolve the model of agent {spec.name!r}: {exc}") from exc

    def _agent_tools(self, spec: AgentBlueprint) -> list[BaseTool]:
        tools: list[BaseTool] = []
        for reference in spec.tools:
            if (off := self._switched_off(reference)) is not None:
                _log.warning(
                    "Agent %r runs without %s: %s is switched off for this project "
                    "(remove it from the blueprint's components:)",
                    spec.name,
                    reference,
                    off,
                )
                continue
            if is_mcp_reference(reference):
                tools.extend(self._mcp_tools(reference, spec.name))
            elif reference.startswith("plugin:"):
                tools.extend(self._plugin_tools(reference))
            elif is_connector_reference(reference):
                tools.extend(self._connector_tools(reference))
            else:
                tools.append(self._tool(reference))
        return tools

    def _switched_off(self, reference: str) -> str | None:
        """The component (`tool.<name>`, `toolkit.<section>`, `connector.<name>`) that switches
        `reference` off, or `None`."""

        if is_builtin_reference(reference):
            entry = builtin_entry(reference)
            return entry.switched_off if entry is not None else None
        if is_connector_reference(reference):
            instance = reference.removeprefix("connector:").partition(".")[0]
            name = str((self._bp.connectors.get(instance) or {}).get("use") or instance)
            key = f"connector.{name}"
            try:
                return key if is_off(key) else None
            except ComponentError:
                return None
        return None

    def _connector_tools(self, reference: str) -> list[BaseTool]:
        """The tools of `connector:<name>[.<operation>]`, gated by blueprint approvals."""

        from awa.connectors import CONNECTOR_METADATA_KEY

        instance = reference.removeprefix("connector:").partition(".")[0]
        approved = {approval.tool for approval in self._bp.approvals}
        loaded = build_connector(
            reference, self._bp.connectors, base=self._base, ungated=_ungated(instance, approved)
        )
        return [
            self._cached(
                f"connector:{(tool.metadata or {}).get(CONNECTOR_METADATA_KEY, tool.name)}",
                tool,
                f"connector:{instance}",
            )
            for tool in loaded
        ]

    def _plugin_tools(self, reference: str) -> list[BaseTool]:
        """The tools of an installed plugin (`plugin:<dist>[/<name>]`), approval-gated."""

        from awa.plugins import load_reference
        from awa.tools.importing import from_langchain

        try:
            loaded = from_langchain(load_reference(reference), requires_approval=False)
        except AwaError as exc:
            raise BlueprintError(f"Cannot load {reference!r}: {exc}") from exc
        dist = reference.removeprefix("plugin:").split("/", 1)[0]
        return [
            self._cached(f"plugin:{dist}/{tool.name}", tool, reference, f"plugin:{dist}")
            for tool in loaded
        ]

    def _mcp_tools(self, reference: str, agent: str) -> list[BaseTool]:
        server, name = parse_mcp_reference(reference)
        available = loaded_tools(list(self._catalog.tools.values()), server)
        if not available:
            raise BlueprintError(
                f"Agent {agent!r} uses {reference!r}, but no tools of MCP server {server!r} were "
                "passed; build with 'await blueprint.abuild(...)', or pass the tools of "
                "'MCPToolset(blueprint.active_mcp_servers())' as tools="
            )
        if name is not None:
            selected = [tool for tool in available if mcp_tool_name(tool) == name]
            if not selected:
                known = ", ".join(sorted(mcp_tool_name(tool) for tool in available))
                raise BlueprintError(
                    f"Agent {agent!r} uses {reference!r}, but MCP server {server!r} has no tool "
                    f"{name!r} (loaded: {known})"
                )
            available = selected
        return [
            self._cached(f"mcp:{server}/{mcp_tool_name(tool)}", tool, f"mcp:{server}")
            for tool in available
        ]

    def _cached(self, key: str, tool: BaseTool, *wider: str) -> BaseTool:
        """The tool cached under `key`, gated on first use by `key` or a `wider` reference."""

        if key not in self._tools:
            self._tools[key] = self._gate(tool, (key, *wider))
        return self._tools[key]

    def _tool(self, reference: str) -> BaseTool:
        cached = self._tools.get(reference)
        if cached is not None:
            return cached
        if is_builtin_reference(reference):
            gated = any(approval.tool == reference for approval in self._bp.approvals)
            tool = build_builtin(reference, self._toolkits(), gated=gated)
        elif is_import_reference(reference):
            if not self._allow_imports:
                raise BlueprintError(
                    f"Tool {reference!r} is an import reference; pass allow_imports=True to "
                    "import it (only for trusted blueprints)"
                )
            tool = to_tool(import_object(reference, base=self._base), source=f"tool {reference!r}")
            tool = self._limited(tool)
        else:
            tool = self._limited(self._catalog.tools[reference])
        return self._cached(reference, tool)

    def _limited(self, tool: BaseTool) -> BaseTool:
        """A blueprint's own tool with the default call timeout (`defaults.tool_timeout`),
        unless the tool sets one itself."""

        seconds = self._bp.defaults.tool_timeout
        spec = tool_spec(tool)
        if seconds is None or tool.args_schema is None or (spec and spec.timeout is not None):
            return tool
        return harden_tool(tool, timeout=seconds, on_error="raise")

    def _gate(self, tool: BaseTool, references: Sequence[str]) -> BaseTool:
        """Wrap `tool` with the approval of the first of `references` that has one."""

        approval = next(
            (a for ref in references for a in self._bp.approvals if a.tool == ref), None
        )
        if approval is None:
            return tool
        if approval.condition is not None:
            try:
                # A gate on a whole connector or server shares the condition among
                # its tools; a name one of them lacks is null there.
                condition = ApprovalCondition(approval.condition).for_tool(
                    tool, strict=approval.tool == references[0]
                )
            except ConfigurationError as exc:
                raise BlueprintError(
                    f"Approval of {approval.tool!r}: {exc} (tool {tool.name!r})"
                ) from exc
            return create_tool_approval_wrapper(tool, requires_approval=condition)
        if approval.when is not None:
            predicate = self._import(approval.when, "Approval predicate")
            return create_tool_approval_wrapper(tool, requires_approval=predicate)
        return create_tool_approval_wrapper(tool)

    def _toolkits(self) -> dict[str, dict[str, Any]]:
        """The `toolkits:` section with relative paths resolved against the blueprint's folder."""

        base = self._base
        if base is None:
            return self._bp.toolkits
        resolved: dict[str, dict[str, Any]] = {}
        for section, options in self._bp.toolkits.items():
            if not isinstance(options, dict):
                resolved[section] = options
                continue
            resolved[section] = {
                key: (
                    str(in_case_dir(base, value))
                    if key in _PATH_OPTIONS
                    and isinstance(value, str)
                    and not Path(value).is_absolute()
                    else value
                )
                for key, value in options.items()
            }
        return resolved

    @property
    def _base(self) -> Path | None:
        """Folder relative file references resolve against (the blueprint's)."""

        source = self._bp.source
        return source.parent if source is not None else None

    def _build_team(self, spec: TeamBlueprint) -> Team:
        limits = (
            TeamLimits(**spec.limits.model_dump(exclude_none=True))
            if spec.limits is not None
            else None
        )
        # Shared objects passed to `build` belong to the network when there is one.
        own = not self._bp.uses_network()
        try:
            return Team(
                self._bp.team_name(),
                orchestrator=self._native(spec.orchestrator),
                members=[self._member(name) for name in spec.members],
                description=spec.description or self._bp.description,
                delegates=spec.delegates,
                peers=spec.peers,
                symmetric_peers=spec.symmetric_peers,
                handoffs=spec.handoffs,
                handoff_history=spec.handoff_history,
                messaging=spec.messaging,
                blackboard=(
                    self._blackboard
                    if self._blackboard is not None and spec.blackboard and own
                    else spec.blackboard
                ),
                task_board=(
                    self._task_board
                    if self._task_board is not None and spec.task_board and own
                    else spec.task_board
                ),
                bus=self._bus if own else None,
                limits=limits,
                checkpointer=self._persists("team"),
                version=self._bp.version,
            )
        except (AwaError, ValueError, TypeError) as exc:
            raise BlueprintError(f"Cannot build team {self._bp.team_name()!r}: {exc}") from exc

    def _build_network(self, spec: NetworkBlueprint) -> AgentNetwork:
        limits = (
            NetworkLimits(**spec.limits.model_dump(exclude_none=True))
            if spec.limits is not None
            else None
        )
        name = self._bp.network_name()
        try:
            return AgentNetwork(
                name,
                lead=self._native(spec.lead),
                teammates=[self._native(teammate) for teammate in spec.teammates],
                description=spec.description or self._bp.description,
                task_board=self._task_board,
                bus=self._bus,
                blackboard=(
                    self._blackboard
                    if self._blackboard is not None and spec.blackboard
                    else spec.blackboard
                ),
                peers=spec.peers,
                teammates_create_tasks=spec.teammates_create_tasks,
                limits=limits,
                checkpointer=self._persists("network"),
                version=self._bp.version,
            )
        except (AwaError, ValueError, TypeError) as exc:
            raise BlueprintError(f"Cannot build network {name!r}: {exc}") from exc

    def _node(self, node: WorkflowNode, *, top_level: bool = False) -> AgentLike:
        if isinstance(node, str):
            return self._member(node)
        name = node.name or (self._bp.name if top_level else None)
        checkpointer = self._persists("workflow") if top_level else None
        description = node.description or (self._bp.description if top_level else "")
        try:
            if isinstance(node, SequentialStep):
                return Sequential(
                    name or f"sequential-{next(self._names)}",
                    [self._node(child) for child in node.sequential],
                    description=description,
                    carry=node.carry,
                    checkpointer=checkpointer,
                )
            if isinstance(node, ParallelStep):
                return Parallel(
                    name or f"parallel-{next(self._names)}",
                    [self._node(child) for child in node.parallel],
                    description=description,
                    fail_fast=node.fail_fast,
                    max_concurrency=node.max_concurrency,
                    checkpointer=checkpointer,
                )
            if isinstance(node, MapStep):
                return Map(
                    name or f"map-{next(self._names)}",
                    self._node(node.map),
                    part_size=node.part_size,
                    max_concurrency=node.max_concurrency,
                    description=description,
                    checkpointer=checkpointer,
                )
            return Loop(
                name or f"loop-{next(self._names)}",
                self._node(node.loop),
                until=self._until(node),
                max_iterations=node.max_iterations,
                description=description,
                checkpointer=checkpointer,
            )
        except ValueError as exc:
            raise BlueprintError(f"Cannot build workflow step {name!r}: {exc}") from exc

    def _until(self, node: LoopStep) -> Callable[[RunResult[Any]], bool]:
        if node.until is not None:
            return self._import(node.until, "Loop condition")
        if node.until_contains is not None:
            return _TextContains(node.until_contains)
        return _never

    def _import(self, reference: str, what: str) -> Callable[..., Any]:
        return import_callable(
            reference, allow_imports=self._allow_imports, what=what, base=self._base
        )


def _ungated(instance: str, approved: set[str]) -> frozenset[str]:
    """Operations of `instance` whose approval the blueprint adds (they must not pause twice)."""

    if f"connector:{instance}" in approved:
        return frozenset({"*"})
    prefix = f"connector:{instance}."
    return frozenset(tool.removeprefix(prefix) for tool in approved if tool.startswith(prefix))
