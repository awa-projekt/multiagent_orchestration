"""The blueprint schema: a declarative description of a multi-agent process.

A blueprint names the roles of a process, how they collaborate (team, network, workflow or
process) and which tool calls need human approval. It round-trips through YAML/JSON and
builds into a runnable system with `Blueprint.build`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, Unpack

from pydantic import Field, PrivateAttr, ValidationError, field_validator, model_validator

from awa._internal.serialize import dump_yaml, load_yaml
from awa.blueprints._automation import AutomationBlueprint, TriggerBlueprint
from awa.blueprints._mcp import is_mcp_reference, parse_mcp_reference, uses_tool
from awa.blueprints._schema_agent import (
    AgentBlueprint,
    BlueprintDefaults,
    BlueprintModelSettings,
    DelegationBlueprint,
    HandoffHistoryMode,
    Role,
    SkillSource,
    ToolSource,
)
from awa.blueprints._schema_data import DataBlueprint
from awa.blueprints._schema_limits import LimitsBlueprint
from awa.blueprints._schema_load import (
    BLUEPRINT_FORMAT_VERSION,
    INLINE_YAML,
    MAX_BLUEPRINT_BYTES,
    agent_entry,
    format_version_problem,
    issue_path,
    raw_top_level_scalar,
    read_blueprint_text,
)
from awa.blueprints._schema_policy import ApprovalBlueprint, PolicyBlueprint
from awa.blueprints._schema_process import (
    ApprovalCheckBlueprint,
    ProcessBlueprint,
    ProcessStepBlueprint,
    RuleCheckBlueprint,
    SchemaCheckBlueprint,
    StepCheckBlueprint,
)
from awa.blueprints._schema_refine import DescribeBlueprint, RefineBlueprint, Refinement
from awa.blueprints._schema_spec import _Spec
from awa.blueprints._schema_structure import (
    LoopStep,
    MapStep,
    NetworkBlueprint,
    NetworkLimitsBlueprint,
    ParallelStep,
    SequentialStep,
    Structure,
    TeamBlueprint,
    TeamLimitsBlueprint,
    WorkflowNode,
    workflow_references,
)
from awa.blueprints.errors import BlueprintError, BlueprintIssue
from awa.mcp import MCPServer
from awa.skills import SkillRegistry
from awa.tools.risk import ToolRisk
from awa.workflows._special_cases import CASE_TABLE_NAME, SpecialCases

if TYPE_CHECKING:
    from awa._internal.upstream.checkpoint import BaseCheckpointSaver
    from awa.blueprints.auto import AutoReport
    from awa.contracts.protocols import AgentLike
    from awa.contracts.usage import PriceTable, UsageLimits
    from awa.data import DataProfile
    from awa.messaging import BaseBlackboard, MessageBus
    from awa.models import ModelRegistry
    from awa.runtime.cache import Cache
    from awa.tasks import BaseTaskBoard


class AgentBuildOptions(TypedDict, total=False):
    """The keyword options of `Blueprint.build_agent`; `BuildOptions` adds the shared objects.

    Attributes:
        tools: Tools the blueprint's tool names resolve against, loaded MCP tools included.
        model_registry: Registry for model profile names; other references stay lazy.
        skill_registry: Registry for skill names. Defaults to the builtin skills.
        checkpointer: Enables threads and human approvals. Required when the blueprint
            declares approvals.
        allow_imports: Allow `"package.module:attribute"` references to be imported. Importing
            runs module code, so enable this only for trusted blueprints.
        cache: The system's caches, shared by all its agents; built from `defaults.cache` and
            the `AWA_CACHE_*` variables when omitted.
    """

    tools: ToolSource | None
    model_registry: ModelRegistry | None
    skill_registry: SkillRegistry | None
    checkpointer: BaseCheckpointSaver[Any] | None
    allow_imports: bool
    cache: Cache | None


class BuildOptions(AgentBuildOptions, total=False):
    """The keyword options of `Blueprint.build`, `Blueprint.abuild`, `load` and `aload`.

    The shared objects replace the new in-memory ones of the network, or of a team that turns
    the feature on (`task_board: true`, `blackboard: true`, `messaging`).
    """

    task_board: BaseTaskBoard | None
    blackboard: BaseBlackboard | None
    bus: MessageBus | None


class Blueprint(_Spec):
    """A declarative multi-agent process that builds into a runnable system.

    Short forms: `agents` may map names to tasks, `team: <orchestrator>` and `network: <lead>`
    make the other agents members or teammates, and `workflow: [a, b]` runs steps in order.

    Attributes:
        format_version: Blueprint file format, `"awa/v1"`. An unknown format is rejected
            instead of misread.
        version: Version of the process; yours to bump. Unrelated to `format_version`.
        entrypoint: `"team"`, `"workflow"`, `"network"` or an agent name. When omitted: the
            team, else the workflow, else the network, else the only agent.
        process: Steps with an agent, documents and checks; an alternative to `workflow`.
        policies: Run-time tool policies (deny, ask, or your own function).
        mcp_servers: MCP servers whose tools agents reference as `mcp:<server>[/<tool>]`;
            loaded by `abuild`.
        tool_risks: Risk annotations by tool reference, for the excessive-agency warning.
        toolkits: Options of the builtin toolkits by name, e.g. `{"files": {"root": "./data"}}`.
            Relative paths resolve against the blueprint's folder (see `source`). Builtin
            tools that change the outside world wait for approval unless the toolkit sets
            `requires_approval: false`.
        connectors: Options of connector instances by name, for `connector:<name>.<operation>`
            tools. Credentials come from environment variables; write operations wait for
            approval unless an instance sets `requires_approval: false`.
        triggers: What starts runs automatically; declared here or in `automation`, not both.
        automation: How triggered runs are processed (input, dedup, concurrency, retries,
            SLAs, notifications).
        limits: What one run may use at most; a run that reaches a limit fails. Applied by
            the CLI commands and the web app (see `usage_limits`).
        prices: Price per million tokens by model name; needed for `limits.max_cost`. awa
            ships no prices.
        auto: Match builtin skills and tools to every agent's task (see `explain`).
        data: The files the process works on and what it relies on in them; checked by
            `check(data=...)` and `awa data check`.
        special_cases: Rule tables by step name; checked and shown (`awa show cases`), not
            applied while the system runs.
        components: Built-in parts switched on or off by kind and name, e.g.
            `{"parser": {"pdf": "off"}}`. Switching a `guard.*` part off is reported by `check`.
    """

    format_version: str = BLUEPRINT_FORMAT_VERSION
    name: str
    description: str = ""
    version: str = "1.0.0"
    defaults: BlueprintDefaults = Field(default_factory=BlueprintDefaults)
    agents: list[AgentBlueprint] = Field(min_length=1)
    entrypoint: str | None = None
    team: TeamBlueprint | None = None
    workflow: WorkflowNode | None = None
    process: ProcessBlueprint | None = None
    network: NetworkBlueprint | None = None
    approvals: list[ApprovalBlueprint] = Field(default_factory=list)
    policies: list[PolicyBlueprint] = Field(default_factory=list)
    mcp_servers: list[MCPServer] = Field(default_factory=list)
    tool_risks: dict[str, ToolRisk] = Field(default_factory=dict)
    toolkits: dict[str, dict[str, Any]] = Field(default_factory=dict)
    connectors: dict[str, dict[str, Any]] = Field(default_factory=dict)
    triggers: list[TriggerBlueprint] = Field(default_factory=list)
    automation: AutomationBlueprint | None = None
    limits: LimitsBlueprint | None = None
    prices: dict[str, dict[str, Any]] = Field(default_factory=dict)
    auto: bool = False
    data: list[DataBlueprint] = Field(default_factory=list)
    special_cases: dict[str, SpecialCases] = Field(default_factory=dict)
    components: dict[str, dict[str, Any]] = Field(default_factory=dict)

    _source: Path | None = PrivateAttr(default=None)

    @field_validator("components", mode="before")
    @classmethod
    def _components(cls, value: Any) -> Any:
        from awa.plugins.switches import parse_section

        if value is None:
            return {}
        _, problems = parse_section(value, where="components")
        if problems:
            raise ValueError("; ".join(problems))
        return value

    def component_entries(self) -> dict[str, Any]:
        """The `components:` section as switch entries by `KIND.NAME` (see `awa.plugins.switches`)."""

        from awa.plugins.switches import parse_section

        return parse_section(self.components, where="components")[0]

    @model_validator(mode="before")
    @classmethod
    def _short_forms(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        data = dict(data)
        agents = data.get("agents")
        if isinstance(agents, Mapping):
            agents = [agent_entry(name, value) for name, value in agents.items()]
            data["agents"] = agents
        entries = agents if isinstance(agents, list) else []
        names = [
            str(entry["name"])
            for entry in entries
            if isinstance(entry, Mapping) and isinstance(entry.get("name"), str)
        ]
        # `team: <orchestrator>` / `network: <lead>`: the other agents join.
        for section, head, others in (
            ("team", "orchestrator", "members"),
            ("network", "lead", "teammates"),
        ):
            value = data.get(section)
            if isinstance(value, str):
                value = {head: value}
            if isinstance(value, Mapping) and others not in value and head in value:
                value = {**value, others: [n for n in names if n != value[head]]}
                if section == "network" and entries:
                    # Teammates claim tasks by capability; one without any gets its own name.
                    data["agents"] = entries = [
                        {**entry, "capabilities": [entry["name"]]}
                        if isinstance(entry, Mapping)
                        and entry.get("name") in value[others]
                        and "capabilities" not in entry
                        else entry
                        for entry in entries
                    ]
            if value is not None:
                data[section] = value
        if isinstance(data.get("workflow"), list):
            data["workflow"] = {"sequential": data["workflow"]}
        return data

    def __eq__(self, other: object) -> bool:
        # Where a blueprint was loaded from is not part of its content.
        if not isinstance(other, Blueprint):
            return NotImplemented
        return type(self) is type(other) and self.__dict__ == other.__dict__

    __hash__ = None  # type: ignore[assignment]  # compared by value and mutable: unhashable

    @field_validator("format_version")
    @classmethod
    def _supported_format(cls, value: str) -> str:
        problem = format_version_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value

    @field_validator("version", mode="before")
    @classmethod
    def _version_as_text(cls, value: Any) -> Any:
        # YAML reads `version: 1.0` as a float; `from_yaml` restores the text
        # as written, plain data gets `str(value)`.
        if isinstance(value, int | float) and not isinstance(value, bool):
            return str(value)
        return value

    @field_validator("special_cases")
    @classmethod
    def _case_table_names(cls, value: dict[str, SpecialCases]) -> dict[str, SpecialCases]:
        for name in value:
            if not CASE_TABLE_NAME.fullmatch(name):
                raise ValueError(
                    f"special_cases name {name!r} is not usable; use letters, digits, _ and -, "
                    "starting with a letter (the name of the step the cases are for)"
                )
        return value

    @field_validator("prices")
    @classmethod
    def _valid_prices(cls, value: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        from awa.contracts.usage import PriceTable
        from awa.errors import ConfigurationError

        try:
            PriceTable.from_mapping(value, source="prices")
        except ConfigurationError as exc:
            raise ValueError(str(exc)) from None
        return value

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Blueprint:
        """Validate a blueprint from plain data (as parsed from YAML or JSON).

        Raises:
            BlueprintError: If the data does not match the schema or names an unknown
                `format_version`.
        """

        if not isinstance(data, Mapping):
            raise BlueprintError(
                f"A blueprint must be a mapping at the top level, got {type(data).__name__}"
            )
        problem = format_version_problem(data.get("format_version", BLUEPRINT_FORMAT_VERSION))
        if problem is not None:
            raise BlueprintError(problem)
        try:
            return cls.model_validate(dict(data))
        except ValidationError as exc:
            issues = [
                BlueprintIssue("error", issue_path(err["loc"]), err["msg"]) for err in exc.errors()
            ]
            raise BlueprintError("Invalid blueprint", issues) from None

    @classmethod
    def from_yaml_file(cls, path: str | os.PathLike[str]) -> Blueprint:
        """Load a blueprint from a YAML or JSON file of at most `MAX_BLUEPRINT_BYTES`.

        MCP servers are pinned by an `mcp.lock.json` next to the file when one exists.

        Raises:
            BlueprintError: If the file cannot be read or the data does not match the schema.
        """

        return cls._load_file(path)

    @classmethod
    def from_yaml_text(cls, text: str) -> Blueprint:
        """Load a blueprint from YAML (or JSON) text; only safe YAML is read.

        Raises:
            BlueprintError: If the text is too large, malformed, or does not match the schema.
        """

        if not isinstance(text, str):
            raise TypeError(f"from_yaml_text needs a str, got {type(text).__name__}")
        if len(text) > MAX_BLUEPRINT_BYTES or len(text.encode("utf-8")) > MAX_BLUEPRINT_BYTES:
            raise BlueprintError(
                f"The blueprint text is larger than MAX_BLUEPRINT_BYTES "
                f"({MAX_BLUEPRINT_BYTES:,} bytes)"
            )
        return cls._parse(text)

    @classmethod
    def from_yaml(cls, source: str | os.PathLike[str]) -> Blueprint:
        """Load a blueprint from a path or from YAML text.

        A `PathLike` is a file. A string with a line break, or one line starting with `{` or
        reading `key: value`, is YAML text; any other string is a path, never parsed as YAML.

        Raises:
            BlueprintError: As for `from_yaml_file` or `from_yaml_text`.
        """

        if isinstance(source, os.PathLike):
            return cls.from_yaml_file(source)
        if not isinstance(source, str):
            raise TypeError(f"from_yaml needs a str or a path, got {type(source).__name__}")
        if "\n" in source or not source.strip() or INLINE_YAML.match(source):
            return cls.from_yaml_text(source)
        return cls._load_file(
            source,
            hint="; a one-line string is read as a path, use Blueprint.from_yaml_text for YAML text",
        )

    @classmethod
    def _load_file(cls, path: str | os.PathLike[str], *, hint: str = "") -> Blueprint:
        from awa.blueprints._mcp import attach_mcp_locks

        blueprint = attach_mcp_locks(cls._parse(read_blueprint_text(Path(path), hint=hint)), path)
        blueprint._source = Path(path).resolve()
        return _with_schema_files(blueprint)

    @property
    def source(self) -> Path | None:
        """The file the blueprint was loaded from; relative file references resolve against its folder."""

        return self._source

    @classmethod
    def _parse(cls, text: str) -> Blueprint:
        try:
            data = load_yaml(text, what="blueprint")
        except ValueError as exc:
            raise BlueprintError(str(exc)) from None
        if isinstance(data, dict) and isinstance(data.get("version"), int | float):
            # YAML reads `version: 1.10` as the float 1.1: keep the text as written.
            try:
                data["version"] = raw_top_level_scalar(text, "version")
            except KeyError:  # set through a merge key (`<<: *base`)
                data["version"] = str(data["version"])
        return cls.from_dict(data if data is not None else {})

    def to_dict(self) -> dict[str, Any]:
        """The blueprint as JSON-compatible data without defaults; `format_version` always comes first."""

        data: dict[str, Any] = self.model_dump(mode="json", exclude_defaults=True, by_alias=True)
        return {"format_version": self.format_version, **data}

    def to_yaml(self) -> str:
        """Return the blueprint as YAML; multi-line text uses block style."""

        return dump_yaml(self.to_dict())

    def agent(self, name: str) -> AgentBlueprint:
        """Return the agent named `name`; raises `KeyError` if there is none."""

        for spec in self.agents:
            if spec.name == name:
                return spec
        raise KeyError(name)

    def resolved_entrypoint(self) -> str | None:
        """Return the effective entrypoint, or `None` when it cannot be inferred."""

        if self.entrypoint is not None:
            return self.entrypoint
        if self.team is not None:
            return "team"
        if self.workflow is not None or self.process is not None:
            return "workflow"
        if self.network is not None:
            return "network"
        from awa.blueprints._roles import role_root

        if (root := role_root(self)) is not None:
            return root
        if len(self.agents) == 1:
            return self.agents[0].name
        return None

    def team_name(self) -> str:
        """Return the effective team name (`team.name`, else the blueprint name)."""

        return (self.team.name if self.team is not None else None) or self.name

    def workflow_name(self) -> str:
        """Return the effective workflow name (its top-level `name`, else the blueprint name)."""

        node = self.workflow
        if isinstance(node, str):
            return node
        return (node.name if node is not None else None) or self.name

    def network_name(self) -> str:
        """Return the effective network name (`network.name`, else the blueprint name)."""

        return (self.network.name if self.network is not None else None) or self.name

    def structure_named(self, name: str) -> Structure | None:
        """Return which declared structure a team member or workflow step `name` refers to.

        Agent names take precedence; `check` reports a structure named like an agent.
        """

        if any(spec.name == name for spec in self.agents):
            return None
        if self.team is not None and name == self.team_name():
            return "team"
        if self.network is not None and name == self.network_name():
            return "network"
        if self.has_workflow_structure() and name == self.workflow_name():
            return "workflow"
        return None

    def has_workflow_structure(self) -> bool:
        """Whether a `process`, or a `workflow` that is more than one agent name, is declared."""

        return self.process is not None or (
            self.workflow is not None and not isinstance(self.workflow, str)
        )

    def structure_references(self, structure: Structure) -> list[str]:
        """Return the names a structure refers to: members, steps, lead and teammates."""

        if structure == "team" and self.team is not None:
            return [self.team.orchestrator, *self.team.members]
        if structure == "workflow" and self.workflow is not None:
            return [name for name, _ in workflow_references(self.workflow)]
        if structure == "workflow" and self.process is not None:
            return [step.agent for step in self.process.steps]
        if structure == "network" and self.network is not None:
            return [self.network.lead, *self.network.teammates]
        return []

    def used_structures(self) -> list[Structure]:
        """Return the structures the entrypoint runs (itself, and those nested in it)."""

        entrypoint = self.resolved_entrypoint()
        pending: list[Structure] = []
        if entrypoint in ("team", "workflow", "network"):
            pending.append(entrypoint)  # type: ignore[arg-type]  # one of the three names above
        used: list[Structure] = []
        while pending:
            structure = pending.pop()
            if structure in used:
                continue
            used.append(structure)
            for name in self.structure_references(structure):
                nested = self.structure_named(name)
                if nested is not None and nested not in used:
                    pending.append(nested)
        return used

    def uses_team(self) -> bool:
        """Return whether the entrypoint runs the team (directly or nested)."""

        return self.team is not None and "team" in self.used_structures()

    def uses_network(self) -> bool:
        """Return whether the entrypoint runs the network (directly or nested)."""

        return self.network is not None and "network" in self.used_structures()

    def active_agents(self) -> list[str]:
        """Return the names of the agents the entrypoint uses, in blueprint order."""

        entrypoint = self.resolved_entrypoint()
        used: set[str] = set()
        if entrypoint not in (None, "team", "workflow", "network"):
            used.add(entrypoint)
        for structure in self.used_structures():
            used.update(self.structure_references(structure))
        from awa.blueprints._roles import role_members

        used.update(role_members(self))
        return [spec.name for spec in self.agents if spec.name in used]

    def approval_agents(self) -> list[str]:
        """Return the active agents that use an approval-gated tool, gated by default ones included."""

        from awa.blueprints._resolve import (
            builtin_requires_approval,
            connector_requires_approval,
            is_builtin_reference,
            is_connector_reference,
        )

        def gated_by_default(reference: str) -> bool:
            if is_builtin_reference(reference):
                return builtin_requires_approval(reference, self.toolkits)
            return is_connector_reference(reference) and connector_requires_approval(
                reference, self.connectors, base
            )

        base = self.source.parent if self.source is not None else None
        gated = {approval.tool for approval in self.approvals}
        names: list[str] = []
        for name in self.active_agents():
            spec = self.agent(name)
            declared = any(uses_tool(spec.tools, reference) for reference in gated)
            if declared or any(gated_by_default(reference) for reference in spec.tools):
                names.append(name)
        return names

    def step_approval_agents(self) -> list[str]:
        """Return the agents of process steps that can ask a person (they need a checkpointer)."""

        if self.process is None or "workflow" not in self.used_structures():
            return []
        names: list[str] = []
        for step in self.process.steps:
            asks = (
                any(isinstance(check, ApprovalCheckBlueprint) for check in step.checks)
                or step.name in self.special_cases
                or step.after_last_try == "ask"
            )
            if asks:
                names.append(step.agent)
        return list(dict.fromkeys(names))

    def needs_checkpointer(self) -> bool:
        """Whether a run can pause for a person: an approval-gated tool, an `ask` policy or a step approval."""

        return bool(self.approval_agents() or self.policy_agents() or self.step_approval_agents())

    def uses_auto(self) -> bool:
        """Return whether auto-matching is on for any agent."""

        return any(spec.auto if spec.auto is not None else self.auto for spec in self.agents)

    def policy_agents(self) -> list[str]:
        """Return the active agents an `ask` policy can pause (they need a checkpointer)."""

        return [
            name
            for name in self.active_agents()
            if any(policy.ask and policy.applies_to(name) for policy in self.policies)
        ]

    def explain(self, *, skills: SkillSource | None = None) -> AutoReport:
        """Report the skills and builtin tools auto-matching gives each agent, and why.

        Args:
            skills: The skills to match from; the builtin skills by default.
        """

        from awa.blueprints.auto import blueprint_auto_report

        return blueprint_auto_report(self, skills=skills)

    def automation_section(self) -> AutomationBlueprint | None:
        """The automation of the blueprint (`automation:` with the top-level `triggers:`), if any."""

        if self.automation is None and not self.triggers:
            return None
        section = self.automation or AutomationBlueprint()
        if self.triggers and not section.triggers:
            section = section.model_copy(update={"triggers": list(self.triggers)})
        return section

    def price_table(self) -> PriceTable | None:
        """The `prices` as a `PriceTable`, or `None` when the blueprint lists none."""

        if not self.prices:
            return None
        from awa.contracts.usage import PriceTable

        return PriceTable.from_mapping(self.prices, source="prices")

    def usage_limits(self) -> UsageLimits | None:
        """The `limits` as `UsageLimits` (with the `prices`), or `None` without `limits`.

        Pass them as `usage_limits=` to `run`, `stream` and `resume`; `build` applies nothing.

        Raises:
            BlueprintError: If `limits.max_cost` is set but `prices` is empty.
        """

        if self.limits is None:
            return None
        prices = self.price_table()
        if self.limits.max_cost is not None and prices is None:
            raise BlueprintError(
                "limits.max_cost needs `prices:` with the price of every model the blueprint uses"
            )
        return self.limits.usage_limits(prices)

    def to_mermaid(self) -> str:
        """Render the entrypoint's topology or workflow as a Mermaid flowchart."""

        from awa.blueprints._diagram import blueprint_to_mermaid

        return blueprint_to_mermaid(self)

    def active_mcp_servers(self) -> list[MCPServer]:
        """Return the declared MCP servers the active agents reference, in declaration order."""

        used = {
            parse_mcp_reference(reference)[0]
            for name in self.active_agents()
            for reference in self.agent(name).tools
            if is_mcp_reference(reference)
        }
        return [server for server in self.mcp_servers if server.name in used]

    def check(
        self,
        *,
        tools: ToolSource | None = None,
        skills: SkillSource | None = None,
        data: DataProfile | None = None,
    ) -> list[BlueprintIssue]:
        """Validate the blueprint deterministically and return every error and warning.

        Args:
            tools: The tools available at build time; tool names are not checked when `None`.
            skills: The skills available at build time. Defaults to the builtin skills.
            data: The profile of the files of the `data:` section; the section is checked only
                when it is given.

        Raises:
            BlueprintError: If `tools` contains an entry that is not a tool.
        """

        from awa.blueprints._check import check_blueprint

        return check_blueprint(self, tools=tools, skills=skills, data=data)

    def build_agent(self, name: str, **options: Unpack[AgentBuildOptions]) -> AgentLike:
        """Build one role as a standalone agent, without team, workflow or network wiring.

        Raises:
            BlueprintError: If the blueprint has errors, `name` is not one of its agents, or the
                agent cannot be built.
        """

        from awa.blueprints._build import build_blueprint_agent

        return build_blueprint_agent(self, name, **options)

    def build(self, **options: Unpack[BuildOptions]) -> AgentLike:
        """Build the runnable system: an `Agent`, a `Team`, a workflow or an `AgentNetwork`.

        Building opens no connection: MCP tools must already be in `tools` (or use `abuild`).

        Raises:
            BlueprintError: If `check` reports errors, a reference cannot be resolved, or the
                options do not fit the blueprint.

        Example:
            ```python
            blueprint = Blueprint.from_yaml_file("triage.yaml")
            result = await blueprint.build().run("My invoice is wrong.")
            ```
        """

        from awa.blueprints._build import build_blueprint

        return build_blueprint(self, **options)

    async def abuild(self, **options: Unpack[BuildOptions]) -> AgentLike:
        """Load the tools of the referenced MCP servers (one short-lived session per call), then `build`.

        The blueprint is checked before any connection opens. To keep sessions open, load the
        tools with `MCPToolset(blueprint.active_mcp_servers())` and call `build`.

        Raises:
            BlueprintError: If `check` reports errors, an MCP server cannot be reached, an MCP
                tool name collides with a tool in `tools`, or `build` fails.
            OptionalDependencyError: If MCP servers are used and the `mcp` extra is missing.
        """

        from awa.blueprints._build import abuild_blueprint

        return await abuild_blueprint(self, **options)


def schema_reference(schema: Mapping[str, Any] | None) -> str | None:
    """The file an `output_schema` of the form `{$ref: <path>}` names, else `None`."""

    if isinstance(schema, Mapping) and set(schema) == {"$ref"}:
        reference = schema["$ref"]
        if isinstance(reference, str) and not reference.startswith("#"):
            return reference
    return None


def _with_schema_files(blueprint: Blueprint) -> Blueprint:
    """`blueprint` with every `output_schema: {$ref: <file>}` replaced by the file's schema in its folder."""

    assert blueprint.source is not None
    folder = blueprint.source.parent
    agents = []
    for position, spec in enumerate(blueprint.agents):
        reference = schema_reference(spec.output_schema)
        if reference is None:
            agents.append(spec)
            continue
        where = f"agents[{position}].output_schema"
        target = (folder / reference).resolve()
        if not target.is_relative_to(folder):
            raise BlueprintError(
                f"{where}: $ref {reference!r} points outside the blueprint's folder; "
                "keep schema files beside the blueprint"
            )
        try:
            loaded = load_yaml(read_blueprint_text(target), what="output schema")
        except (BlueprintError, ValueError) as exc:
            raise BlueprintError(f"{where}: $ref {reference!r} cannot be read: {exc}") from None
        if not isinstance(loaded, dict):
            raise BlueprintError(f"{where}: $ref {reference!r} does not hold a JSON schema object")
        agents.append(spec.model_copy(update={"output_schema": loaded}))
    if all(new is old for new, old in zip(agents, blueprint.agents, strict=True)):
        return blueprint
    resolved = blueprint.model_copy(update={"agents": agents})
    resolved._source = blueprint.source
    return resolved


__all__ = [
    "AgentBuildOptions",
    "BuildOptions",
    "BLUEPRINT_FORMAT_VERSION",
    "MAX_BLUEPRINT_BYTES",
    "AgentBlueprint",
    "ApprovalBlueprint",
    "ApprovalCheckBlueprint",
    "Blueprint",
    "BlueprintDefaults",
    "BlueprintModelSettings",
    "DelegationBlueprint",
    "DescribeBlueprint",
    "HandoffHistoryMode",
    "LimitsBlueprint",
    "LoopStep",
    "MapStep",
    "NetworkBlueprint",
    "NetworkLimitsBlueprint",
    "ParallelStep",
    "PolicyBlueprint",
    "ProcessBlueprint",
    "ProcessStepBlueprint",
    "RefineBlueprint",
    "Refinement",
    "Role",
    "RuleCheckBlueprint",
    "SchemaCheckBlueprint",
    "SequentialStep",
    "StepCheckBlueprint",
    "SkillSource",
    "Structure",
    "TeamBlueprint",
    "TeamLimitsBlueprint",
    "ToolSource",
    "WorkflowNode",
    "workflow_references",
]
