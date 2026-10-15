"""Deterministic validation of a `Blueprint` (see `Blueprint.check`)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awa.blueprints._check_process import check_process
from awa.blueprints._check_roles import role_issues
from awa.blueprints._mcp import (
    is_mcp_reference,
    loaded_tools,
    mcp_tool_name,
    parse_mcp_reference,
    uses_tool,
)
from awa.blueprints._resolve import (
    ToolCatalog,
    builtin_entry,
    builtin_issues,
    connector_issues,
    connector_missing,
    connector_operations,
    connector_options,
    is_builtin_reference,
    is_connector_reference,
    is_import_reference,
    is_valid_import_reference,
    skill_names,
)
from awa.blueprints._risk import risk_issues
from awa.blueprints._schema_agent import AgentMemoryBlueprint
from awa.blueprints.auto import AutoReport, apply_auto
from awa.blueprints.errors import BlueprintError, BlueprintIssue, Severity
from awa.blueprints.json_schema import model_from_json_schema
from awa.blueprints.schema import (
    Blueprint,
    LoopStep,
    MapStep,
    NetworkBlueprint,
    ParallelStep,
    SequentialStep,
    SkillSource,
    Structure,
    TeamBlueprint,
    ToolSource,
    WorkflowNode,
    schema_reference,
    workflow_references,
)
from awa.contracts.cards import AGENT_NAME_PATTERN
from awa.errors import AwaError, ConfigurationError
from awa.tools.approval import ApprovalCondition

if TYPE_CHECKING:
    from awa.data import DataProfile

_RESERVED_NAMES = ("team", "workflow", "network")

_CONNECTOR_MENTION = re.compile(r"connector:[a-z][a-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*")


def check_blueprint(
    blueprint: Blueprint,
    *,
    tools: ToolSource | None = None,
    skills: SkillSource | None = None,
    data: DataProfile | None = None,
) -> list[BlueprintIssue]:
    """Return every issue of `blueprint` (see `Blueprint.check`); with auto-matching, ungranted suggestions are warnings."""

    expanded, report = with_auto(blueprint, skills)
    return check_expanded(expanded, report, tools=tools, skills=skills, data=data)


def with_auto(
    blueprint: Blueprint, skills: SkillSource | None
) -> tuple[Blueprint, AutoReport | None]:
    """`blueprint` with its auto-matches added, and the report (`None` without auto)."""

    if not blueprint.uses_auto():
        return blueprint, None
    return apply_auto(blueprint, skills=skills)


def check_expanded(
    blueprint: Blueprint,
    report: AutoReport | None,
    *,
    tools: ToolSource | None = None,
    skills: SkillSource | None = None,
    data: DataProfile | None = None,
) -> list[BlueprintIssue]:
    """The issues of a blueprint whose auto-matches `with_auto` already added."""

    issues = _Checker(blueprint, tools, skills, data).run()
    if report is not None:
        issues.extend(_auto_issues(report))
    return issues


def _auto_issues(report: AutoReport) -> list[BlueprintIssue]:
    issues: list[BlueprintIssue] = []
    for index, agent in enumerate(report.agents):
        if not agent.suggested:
            continue
        listing = "; ".join(f"{match.name}: {match.note}" for match in agent.suggested)
        issues.append(
            BlueprintIssue(
                "warning",
                f"agents[{index}]",
                f"auto-matching suggests tools for {agent.agent!r} but does not grant them "
                f"({listing}). Add the ones the process needs to the agent's tools (and "
                "toolkits), or set auto: false on the agent",
            )
        )
    return issues


class _Checker:
    def __init__(
        self,
        blueprint: Blueprint,
        tools: ToolSource | None,
        skills: SkillSource | None,
        data: DataProfile | None = None,
    ) -> None:
        self._bp = blueprint
        self._data = data
        self._catalog = ToolCatalog.from_source(tools) if tools is not None else None
        self._skills = skill_names(skills)
        self._agent_names = [spec.name for spec in blueprint.agents]
        self._agents = set(self._agent_names)
        self._issues: list[BlueprintIssue] = []

    def run(self) -> list[BlueprintIssue]:
        self._name(self._bp.name, "name", "blueprint name")
        self._check_mcp_servers()
        self._check_toolkits()
        self._check_connectors()
        self._check_instruction_references()
        self._check_agents()
        self._check_entrypoint()
        if self._bp.team is not None:
            self._check_team(self._bp.team)
        if self._bp.workflow is not None:
            self._check_workflow(self._bp.workflow)
        self._issues.extend(
            check_process(
                self._bp, structure_names=self._structure_names("workflow"), data=self._data
            )
        )
        if self._bp.network is not None:
            self._check_network(self._bp.network)
        self._check_structures()
        self._check_usage()
        self._check_approvals()
        self._check_policies()
        self._check_special_cases()
        self._issues.extend(risk_issues(self._bp, self._catalog))
        self._issues.extend(role_issues(self._bp, self._catalog))
        self._check_components()
        self._check_automation()
        self._check_limits()
        if self._bp.data and self._data is not None:
            from awa.blueprints._check_data import check_data

            self._issues.extend(check_data(self._bp, self._data))
        return self._issues

    def _check_components(self) -> None:
        """Governance findings: security parts switched off, parts that have no effect, plugins
        of the project that do not take effect."""

        from awa.plugins import list_plugins
        from awa.plugins.switches import PARTS, SECURITY

        for key, entry in self._bp.component_entries().items():
            where = f"components.{key}"
            if key in SECURITY and entry.state == "off":
                self._add(
                    "warning",
                    where,
                    f"governance: {key} is switched off. What it does: {PARTS[key]}",
                )
            elif key.startswith("vector_store."):
                self._add(
                    "warning", where, f"{key} has no effect: vector stores are not switchable"
                )
        if self._bp.source is not None:
            for info in list_plugins(project=self._bp.source.parent):
                if info.status == "clash" or info.status.startswith("shadowed"):
                    self._add(
                        "warning",
                        f"plugins.{info.kind}.{info.name}",
                        f"the {info.kind} plugin {info.name!r} ({info.source}) is {info.status}; "
                        "pass replace=True to register() to let it take the place of the "
                        "lower one",
                    )

    def _check_automation(self) -> None:
        automation = self._bp.automation
        if automation is not None and automation.triggers and self._bp.triggers:
            self._add(
                "error",
                "triggers",
                "declare triggers at the top level or under automation, not both",
            )
        section = self._bp.automation_section()
        if section is None:
            return
        for problem in section.check():
            location, _, message = problem.partition(": ")
            self._add("warning", location if message else "automation", message or problem)
        from awa.blueprints.triggers import schedule_problems

        for index, message in schedule_problems(section):
            self._add("error", f"automation.triggers[{index}].schedule", message)
        for index, trigger in enumerate(section.triggers):
            template = trigger.input or section.input or ""
            if trigger.file_drop is not None and "payload.processed_path" in template:
                self._add(
                    "warning",
                    f"automation.triggers[{index}].input",
                    "the file reaches processed_path only after its run succeeds; the run "
                    "reads it at {payload.path}",
                )

    def _check_limits(self) -> None:
        limits = self._bp.limits
        section = self._bp.automation_section()
        if limits is None:
            if section is not None and section.triggers:
                self._add(
                    "warning",
                    "limits",
                    "the blueprint has triggers but no `limits:`; every triggered run may use as "
                    "many model calls, tokens and as much money as it needs. Add limits "
                    "(max_requests, max_total_tokens, max_cost)",
                )
            return
        if limits.max_cost is not None:
            self._check_prices()

    def _check_prices(self) -> None:
        prices = self._bp.price_table()
        if prices is None:
            self._add(
                "error",
                "limits.max_cost",
                "max_cost needs `prices:` with the price of every model the blueprint uses",
            )
            return
        active = set(self._bp.active_agents())
        defaults = _default_profiles()
        for index, spec in enumerate(self._bp.agents):
            if spec.name not in active:
                continue
            reference = spec.model or self._bp.defaults.model or "default"
            named = ":" in reference
            model = reference if named else defaults.get(reference)
            path = f"agents[{index}].model"
            if model is None:
                self._add(
                    "warning",
                    path,
                    f"cannot tell which model the profile {reference!r} names, so its price is "
                    "not checked; list the model's own name under `prices:`",
                )
                continue
            try:
                prices.price_for(model)
            except AwaError:
                # A profile may be redefined in code, so only a model written out is certain.
                self._add(
                    "error" if named else "warning",
                    path,
                    f"max_cost is set but agent {spec.name!r} uses the model {model!r}"
                    + ("" if named else f" (profile {reference!r})")
                    + ", which has no entry under `prices:`; add its price (the cost of a run "
                    "could not be counted)",
                )

    def _add(self, severity: Severity, path: str, message: str) -> None:
        self._issues.append(BlueprintIssue(severity, path, message))

    def _name(self, name: str, path: str, what: str) -> bool:
        if AGENT_NAME_PATTERN.fullmatch(name):
            return True
        self._add(
            "error",
            path,
            f"invalid {what} {name!r}: must start with a letter, contain only letters, "
            "digits, '_' or '-', and be at most 48 characters long",
        )
        return False

    def _known(self, name: str, path: str, allowed: Sequence[str], what: str) -> bool:
        if name in allowed:
            return True
        self._add("error", path, f"unknown {what} {name!r} (known: {_listing(allowed)})")
        return False

    def _check_agents(self) -> None:
        seen: set[str] = set()
        for index, spec in enumerate(self._bp.agents):
            path = f"agents[{index}]"
            if self._name(spec.name, f"{path}.name", "agent name"):
                if spec.name in _RESERVED_NAMES:
                    self._add(
                        "error", f"{path}.name", f"{spec.name!r} is a reserved entrypoint name"
                    )
                elif spec.name in seen:
                    self._add("error", f"{path}.name", f"duplicate agent name {spec.name!r}")
            seen.add(spec.name)
            self._check_tools(spec.tools, f"{path}.tools")
            for position, skill in enumerate(spec.skills):
                skill_path = f"{path}.skills[{position}]"
                if spec.skills.index(skill) != position:
                    self._add("error", skill_path, f"skill {skill!r} is listed twice")
                elif skill not in self._skills:
                    self._add(
                        "error",
                        skill_path,
                        f"unknown skill {skill!r} (known: {_listing(sorted(self._skills))})",
                    )
            if schema_reference(spec.output_schema) is not None:
                self._add(
                    "error",
                    f"{path}.output_schema",
                    "a schema file ($ref) is read only when the blueprint is loaded from its "
                    "file; load it with Blueprint.from_yaml_file, or write the schema inline",
                )
            elif spec.output_schema is not None:
                try:
                    model_from_json_schema(spec.output_schema, name=f"{spec.name}_output")
                except BlueprintError as exc:
                    self._add("error", f"{path}.output_schema", str(exc))
            if spec.memory is not None:
                self._check_memory(spec.name, spec.memory, f"{path}.memory")

    def _check_memory(self, agent: str, memory: AgentMemoryBlueprint, path: str) -> None:
        team, network = self._bp.team, self._bp.network
        in_team = (team is not None and agent in (team.orchestrator, *team.members)) or (
            network is not None and agent in (network.lead, *network.teammates)
        )
        if memory.scope == "team" and not in_team:
            self._add(
                "error",
                f"{path}.scope",
                f"memory scope 'team' needs {agent!r} to be the orchestrator or a "
                "member of the team (or the lead or a teammate of the network)",
            )
        if (memory.scope == "user" or memory.require_user) and memory.user_id is None:
            self._add(
                "warning",
                f"{path}.user_id",
                f"memory of {agent!r} is per user, but the blueprint names no source "
                "of the user id: served to several users (awa serve), every run fails "
                "without one. Set memory.user_id to 'principal' (the authenticated "
                "caller) or 'deps' (sent by the client)",
            )

    def _check_tools(self, tools: Sequence[str], path: str) -> None:
        for position, reference in enumerate(tools):
            tool_path = f"{path}[{position}]"
            if tools.index(reference) != position:
                self._add("error", tool_path, f"tool {reference!r} is listed twice")
            elif is_mcp_reference(reference):
                self._check_mcp_reference(reference, tools, tool_path)
            elif is_builtin_reference(reference):
                for problem in builtin_issues(reference, self._bp.toolkits):
                    self._add("error", tool_path, problem)
            elif is_connector_reference(reference):
                for problem in connector_issues(reference, self._bp.connectors, self._base):
                    self._add("error", tool_path, problem)
            elif reference.startswith("plugin:"):
                from awa.plugins import reference_problem

                if (plugin_problem := reference_problem(reference)) is not None:
                    self._add("error", tool_path, plugin_problem)
            elif is_import_reference(reference):
                if not is_valid_import_reference(reference):
                    self._add("error", tool_path, _invalid_import(reference))
            elif self._catalog is None:
                if builtin_entry(reference) is not None:
                    self._add("warning", tool_path, _builtin_hint(reference))
            elif reference not in self._catalog.tools:
                known = _listing(sorted(self._catalog.tools))
                hint = f"; {_builtin_hint(reference)}" if builtin_entry(reference) else ""
                self._add("error", tool_path, f"unknown tool {reference!r} (known: {known}){hint}")

    def _check_toolkits(self) -> None:
        if not self._bp.toolkits:
            return
        from awa.toolkits import catalog

        tools = catalog()
        for path, message in tools.config_problems(self._bp.toolkits):
            self._add("error", path, message)
        entries = [
            builtin_entry(reference)
            for spec in self._bp.agents
            for reference in spec.tools
            if is_builtin_reference(reference)
        ]
        used = {entry.toolkit.name for entry in entries if entry is not None}
        for section in self._bp.toolkits:
            inherited = section == "files" and used & {"data", "documents"}
            if section in tools.toolkits() and section not in used and not inherited:
                self._add(
                    "warning",
                    f"toolkits.{section}",
                    f"toolkit {section!r} is configured but no agent uses its builtin tools",
                )

    @property
    def _base(self) -> Path | None:
        source = self._bp.source
        return source.parent if source is not None else None

    def _check_connectors(self) -> None:
        from awa.connectors._catalog import config_problems, split_reference

        references = {
            reference: spec.name
            for spec in self._bp.agents
            for reference in spec.tools
            if is_connector_reference(reference)
        }
        used = {split_reference(reference)[0] for reference in references}
        for section in self._bp.connectors:
            if section not in used:
                resolved_options = connector_options(section, self._bp.connectors, self._base)
                for problem in config_problems(section, resolved_options):
                    self._add("error", f"connectors.{section}", problem)
                self._add(
                    "warning",
                    f"connectors.{section}",
                    f"connector {section!r} is declared but no agent uses its tools",
                )
        gated = {approval.tool for approval in self._bp.approvals}
        warned: set[str] = set()
        for reference, agent in references.items():
            instance, _ = split_reference(reference)
            options = self._bp.connectors.get(instance) or {}
            missing = connector_missing(reference, self._bp.connectors, self._base)
            if missing and instance not in warned:
                shown = f"{instance} --blueprint <file>" if options.get("use") else instance
                warned.add(instance)
                self._add(
                    "warning",
                    f"connectors.{instance}",
                    f"connector {instance!r} is not configured: set {', '.join(missing)} "
                    f"(see 'awa connectors show {shown}'); its tools fail until then",
                )
            resolved = connector_operations(reference, self._bp.connectors, self._base)
            if resolved is None or options.get("requires_approval", True) is not False:
                continue
            for operation in resolved[1]:
                tool = f"connector:{instance}.{operation.name}"
                if operation.write and not {tool, f"connector:{instance}"} & gated:
                    self._add(
                        "warning",
                        f"connectors.{instance}.requires_approval",
                        f"agent {agent!r} can run {tool}, which "
                        "changes or sends data, without approval (requires_approval: false); "
                        "add an approval for it or remove the option",
                    )

    def _check_instruction_references(self) -> None:
        """Warn about `connector:<instance>.<operation>` in instructions that names no tool
        (left over after `awa connect` replaced a stand-in by the real system)."""

        for spec in self._bp.agents:
            for reference in sorted(set(_CONNECTOR_MENTION.findall(spec.instructions or ""))):
                instance = reference.removeprefix("connector:").split(".", 1)[0]
                if instance not in self._bp.connectors or reference in spec.tools:
                    continue
                if connector_issues(reference, self._bp.connectors, self._base):
                    self._add(
                        "warning",
                        f"agents.{spec.name}.instructions",
                        f"the instructions mention {reference}, which connector {instance!r} "
                        "does not have; name one of the agent's tools instead",
                    )

    def _check_mcp_servers(self) -> None:
        names = [server.name for server in self._bp.mcp_servers]
        referenced = {
            parse_mcp_reference(reference)[0]
            for spec in self._bp.agents
            for reference in spec.tools
            if is_mcp_reference(reference)
        }
        for index, name in enumerate(names):
            path = f"mcp_servers[{index}].name"
            if names.index(name) != index:
                self._add("error", path, f"duplicate MCP server name {name!r}")
            elif name not in referenced:
                self._add(
                    "warning",
                    f"mcp_servers[{index}]",
                    f"MCP server {name!r} is declared but no agent uses its tools",
                )

    def _check_mcp_reference(self, reference: str, tools: Sequence[str], path: str) -> None:
        server_name, tool = parse_mcp_reference(reference)
        if not server_name or tool == "" or (tool is not None and "/" in tool):
            self._add(
                "error",
                path,
                f"invalid MCP reference {reference!r}: expected 'mcp:<server>' or "
                "'mcp:<server>/<tool>'",
            )
            return
        server = next((s for s in self._bp.mcp_servers if s.name == server_name), None)
        if server is None:
            known = _listing([s.name for s in self._bp.mcp_servers])
            self._add(
                "error",
                path,
                f"unknown MCP server {server_name!r} (declared in mcp_servers: {known})",
            )
        elif tool is not None and f"mcp:{server_name}" in tools:
            self._add("error", path, f"{reference!r} is already included by 'mcp:{server_name}'")
        elif tool is not None and not server.selects(tool):
            self._add(
                "error",
                path,
                f"tool {tool!r} is left out by the include/exclude filters of MCP server "
                f"{server_name!r}",
            )

    def _check_entrypoint(self) -> None:
        entrypoint = self._bp.resolved_entrypoint()
        if entrypoint is None:
            self._add(
                "error",
                "entrypoint",
                "cannot infer the entrypoint: declare a team, a workflow or a network, or "
                "set 'entrypoint' to one of the agents",
            )
        elif (
            entrypoint in _RESERVED_NAMES
            and getattr(self._bp, entrypoint) is None
            and not (entrypoint == "workflow" and self._bp.process is not None)
        ):
            self._add(
                "error",
                "entrypoint",
                f"entrypoint is {entrypoint!r} but no {entrypoint} is declared",
            )
        elif entrypoint not in _RESERVED_NAMES and entrypoint not in self._agents:
            self._add(
                "error",
                "entrypoint",
                f"unknown entrypoint {entrypoint!r}: use 'team', 'workflow', 'network' or an "
                "agent name",
            )

    def _check_team(self, team: TeamBlueprint) -> None:
        if team.name is not None:
            self._name(team.name, "team.name", "team name")
        if self._bp.team_name() in self._agents:
            self._add(
                "error", "team.name", f"team name {self._bp.team_name()!r} is also an agent name"
            )
        self._known(team.orchestrator, "team.orchestrator", self._agent_names, "agent")
        if not team.members:
            self._add("error", "team.members", "a team needs at least one member")
        nested = self._structure_names(exclude="team")
        for index, member in enumerate(team.members):
            path = f"team.members[{index}]"
            if not self._known(
                member, path, [*self._agent_names, *nested], "member" if nested else "agent"
            ):
                continue
            if member == team.orchestrator:
                self._add("error", path, "the orchestrator cannot also be a member")
            elif team.members.index(member) != index:
                self._add("error", path, f"member {member!r} is listed twice")
            elif member in self._agents and not self._bp.agent(member).description:
                self._add(
                    "warning",
                    path,
                    f"member {member!r} has no description; collaborators choose whom to "
                    "involve by description",
                )
        members = [
            m for m in dict.fromkeys(team.members) if m != team.orchestrator and m in self._agents
        ]
        everyone = [team.orchestrator, *members] if team.orchestrator in self._agents else members
        delegable = [*members, *(m for m in dict.fromkeys(team.members) if m in nested)]
        self._check_edges(team.delegates or {}, "team.delegates", everyone, delegable)
        for source in team.peers:
            if source == team.orchestrator:
                self._add(
                    "error",
                    f"team.peers.{source}",
                    f"peer edges connect members only; {source!r} is the orchestrator and "
                    "delegates instead",
                )
        peers = {
            source: targets for source, targets in team.peers.items() if source != team.orchestrator
        }
        self._check_edges(peers, "team.peers", members, members)
        self._check_edges(team.handoffs, "team.handoffs", everyone, everyone)
        for source in team.handoffs:
            if source in self._agents and self._bp.agent(source).output_schema is not None:
                self._add(
                    "error",
                    f"team.handoffs.{source}",
                    f"{source!r} declares an output_schema, so it cannot transfer control",
                )

    def _check_edges(
        self, edges: dict[str, list[str]], path: str, sources: Sequence[str], targets: Sequence[str]
    ) -> None:
        for source, destinations in edges.items():
            source_path = f"{path}.{source}"
            if not self._known(source, source_path, sources, "team agent"):
                continue
            for index, target in enumerate(destinations):
                target_path = f"{source_path}[{index}]"
                if target == source:
                    self._add("error", target_path, f"{source!r} cannot target itself")
                elif self._known(target, target_path, targets, "target") and (
                    destinations.index(target) != index
                ):
                    self._add("warning", target_path, f"target {target!r} is listed twice")

    def _check_workflow(self, workflow: WorkflowNode) -> None:
        allowed = [*self._agent_names, *self._structure_names("workflow")]
        for name, path in workflow_references(workflow):
            self._known(name, path, allowed, "workflow step")
        self._check_node(workflow, "workflow")

    def _check_node(self, node: WorkflowNode, path: str) -> None:
        if isinstance(node, str):
            return
        if node.name is not None:
            self._name(node.name, f"{path}.name", "workflow name")
        if isinstance(node, SequentialStep):
            for index, child in enumerate(node.sequential):
                self._check_node(child, f"{path}.sequential[{index}]")
        elif isinstance(node, ParallelStep):
            names = [child if isinstance(child, str) else child.name for child in node.parallel]
            for index, child in enumerate(node.parallel):
                label = names[index]
                if label is not None and names.index(label) != index:
                    self._add(
                        "error",
                        f"{path}.parallel[{index}]",
                        f"parallel branch {label!r} appears twice; branch names must be unique",
                    )
                self._check_node(child, f"{path}.parallel[{index}]")
        elif isinstance(node, MapStep):
            self._check_node(node.map, f"{path}.map")
        else:
            self._check_loop(node, path)

    def _check_loop(self, node: LoopStep, path: str) -> None:
        if node.until is not None and node.until_contains is not None:
            self._add("error", path, "set either 'until' or 'until_contains', not both")
        if node.until is not None and not is_valid_import_reference(node.until):
            self._add("error", f"{path}.until", _invalid_import(node.until))
        if node.until is None and node.until_contains is None:
            self._add(
                "warning",
                path,
                f"the loop has no stop condition and always runs {node.max_iterations} times",
            )
        self._check_node(node.loop, f"{path}.loop")

    def _check_network(self, network: NetworkBlueprint) -> None:
        if network.name is not None:
            self._name(network.name, "network.name", "network name")
        if self._bp.network_name() in self._agents:
            self._add(
                "error",
                "network.name",
                f"network name {self._bp.network_name()!r} is also an agent name",
            )
        self._known(network.lead, "network.lead", self._agent_names, "agent")
        if not network.teammates:
            self._add("error", "network.teammates", "a network needs at least one teammate")
        for index, teammate in enumerate(network.teammates):
            path = f"network.teammates[{index}]"
            if not self._known(teammate, path, self._agent_names, "agent"):
                continue
            if teammate == network.lead:
                self._add("error", path, "the lead cannot also be a teammate")
            elif network.teammates.index(teammate) != index:
                self._add("error", path, f"teammate {teammate!r} is listed twice")
            elif not self._bp.agent(teammate).capabilities:
                self._add(
                    "warning",
                    path,
                    f"teammate {teammate!r} has no capabilities; it can only take tasks "
                    "assigned to it by name",
                )
        if isinstance(network.peers, dict):
            teammates = [t for t in dict.fromkeys(network.teammates) if t in self._agents]
            for source, targets in network.peers.items():
                source_path = f"network.peers.{source}"
                if not self._known(source, source_path, teammates, "teammate"):
                    continue
                for index, target in enumerate(targets):
                    target_path = f"{source_path}[{index}]"
                    if target == source:
                        self._add("error", target_path, f"{source!r} cannot target itself")
                    else:
                        self._known(target, target_path, teammates, "teammate")

    def _structure_names(self, exclude: Structure) -> list[str]:
        """Names a team member or workflow step may use for the other structures."""

        names: list[str] = []
        if exclude != "team" and self._bp.team is not None:
            names.append(self._bp.team_name())
        if exclude != "network" and self._bp.network is not None:
            names.append(self._bp.network_name())
        if exclude != "workflow" and self._bp.has_workflow_structure():
            names.append(self._bp.workflow_name())
        return names

    def _check_structures(self) -> None:
        """A team, workflow or network must not contain itself, directly or through another."""

        specs: dict[Structure, object] = {
            "team": self._bp.team,
            "workflow": self._bp.workflow or self._bp.process,
            "network": self._bp.network,
        }
        declared = [structure for structure, spec in specs.items() if spec is not None]
        for structure in declared:
            seen: set[Structure] = set()
            pending = [structure]
            while pending:
                current = pending.pop()
                for name in self._bp.structure_references(current):
                    nested = self._bp.structure_named(name)
                    if nested == structure:
                        self._add(
                            "error",
                            structure,
                            f"the {structure} contains itself (through {current} member "
                            f"{name!r}); give the nested {structure} its own name, or remove "
                            "the reference",
                        )
                        pending = []
                        break
                    if nested is not None and nested not in seen:
                        seen.add(nested)
                        pending.append(nested)

    def _check_usage(self) -> None:
        if self._bp.resolved_entrypoint() is None:
            return
        if self._bp.team is not None and not self._bp.uses_team():
            self._add("warning", "team", "the team is declared but the entrypoint does not use it")
        used = self._bp.used_structures()
        if self._bp.workflow is not None and "workflow" not in used:
            self._add(
                "warning", "workflow", "the workflow is declared but the entrypoint does not use it"
            )
        if self._bp.process is not None and "workflow" not in used:
            self._add(
                "warning",
                "process",
                "the process is declared but the entrypoint does not use it; remove the "
                "'entrypoint' line, or set it to 'workflow' to run the process",
            )
        if self._bp.network is not None and "network" not in used:
            self._add(
                "warning", "network", "the network is declared but the entrypoint does not use it"
            )
        active = set(self._bp.active_agents())
        for index, spec in enumerate(self._bp.agents):
            if spec.name not in active:
                fix = (
                    "; put it on a step of the process or remove it"
                    if self._bp.process is not None
                    else ""
                )
                self._add(
                    "warning",
                    f"agents[{index}]",
                    f"agent {spec.name!r} is not used by the entrypoint{fix}",
                )

    def _check_approvals(self) -> None:
        seen: set[str] = set()
        network = self._bp.network
        # A network pauses for approvals only as the entrypoint (its agents run
        # outside any enclosing run's graph).
        nested_network = (
            {network.lead, *network.teammates}
            if network is not None
            and self._bp.uses_network()
            and self._bp.resolved_entrypoint() != "network"
            else set()
        )
        for index, approval in enumerate(self._bp.approvals):
            path = f"approvals[{index}]"
            if approval.tool in seen:
                self._add("error", f"{path}.tool", f"tool {approval.tool!r} has two approvals")
            seen.add(approval.tool)
            if approval.condition is not None:
                if _reference_like(approval.condition):
                    self._add("error", f"{path}.when", _invalid_import(approval.when))
                else:
                    self._check_condition(approval.tool, approval.condition, f"{path}.when")
            owners = [spec.name for spec in self._bp.agents if uses_tool(spec.tools, approval.tool)]
            if not owners:
                self._add("error", f"{path}.tool", f"no agent uses tool {approval.tool!r}")
                continue
            for owner in owners:
                if owner in nested_network:
                    self._add(
                        "error",
                        f"{path}.tool",
                        f"agent {owner!r} works in the network, which pauses for approval "
                        "only as the entrypoint; make the network the entrypoint, or move "
                        "the approval-gated work into the team or workflow",
                    )

    def _check_special_cases(self) -> None:
        """Special cases bound to process steps, retries, `after_last_try` and `when`."""

        from awa.blueprints._check_routing import check_routing

        self._issues.extend(check_routing(self._bp, self._catalog))

    def _check_condition(self, reference: str, condition: str, path: str) -> None:
        """Check that the condition parses and reads only arguments of the gated tool(s)."""

        try:
            parsed = ApprovalCondition(condition)
        except ConfigurationError as exc:
            self._add(
                "error",
                path,
                f"{exc}; write an expression over the tool's arguments such as "
                "'amount >= 5000', or an import reference 'package.module:predicate'",
            )
            return
        arguments = self._arguments(reference)
        if arguments is None:
            return
        unknown = parsed.unknown_names(arguments)
        if unknown:
            known = ", ".join(sorted(arguments)) or "none"
            self._add(
                "error",
                path,
                f"condition {condition!r} reads {', '.join(unknown)}, which the arguments of "
                f"{reference!r} do not have (arguments: {known})",
            )

    def _arguments(self, reference: str) -> set[str] | None:
        """The argument names of the tool(s) `reference` names; `None` if known only at build time."""

        if is_connector_reference(reference):
            instance, _, operation = reference.removeprefix("connector:").partition(".")
            if operation:
                resolved = connector_operations(reference, self._bp.connectors, self._base)
                operations = resolved[1] if resolved is not None else None
            else:
                operations = _instance_operations(instance, self._bp.connectors, self._base)
            names = {param.name for op in operations or () for param in op.params}
            return names or None  # operations built by code declare no parameters
        if is_builtin_reference(reference):
            entry = builtin_entry(reference)
            if entry is None or entry.problems(self._bp.toolkits):
                return None
            try:
                builtin = entry.create(self._bp.toolkits, requires_approval=False)
            except Exception:  # noqa: BLE001 - reported by the reference checks
                return None
            return set(builtin.args)
        if self._catalog is None:
            return None
        if is_mcp_reference(reference):
            server, name = parse_mcp_reference(reference)
            tools = [
                tool
                for tool in loaded_tools(list(self._catalog.tools.values()), server)
                if name is None or mcp_tool_name(tool) == name
            ]
            return {argument for tool in tools for argument in tool.args} if tools else None
        named = self._catalog.tools.get(reference)
        return set(named.args) if named is not None else None

    def _check_policies(self) -> None:
        for index, policy in enumerate(self._bp.policies):
            path = f"policies[{index}]"
            if not (policy.deny or policy.ask or policy.use):
                self._add("error", path, "a policy needs deny, ask or use")
            if policy.use is not None and not is_valid_import_reference(policy.use):
                self._add(
                    "error",
                    f"{path}.use",
                    f"invalid import reference {policy.use!r}: expected 'package.module:policy' "
                    "or 'path/to/file.py:policy'",
                )
            for position, name in enumerate(policy.agents):
                self._known(name, f"{path}.agents[{position}]", self._agent_names, "agent")


def _default_profiles() -> dict[str, str]:
    """The models of the profiles `default`, `fast` and `strong` (empty when settings fail)."""

    from awa.core.settings import Settings

    try:
        settings = Settings()
    except Exception:  # noqa: BLE001 - an unreadable environment only skips this check
        return {}
    return {
        "default": settings.default_model,
        "fast": settings.fast_model,
        "strong": settings.strong_model,
    }


def _builtin_hint(reference: str) -> str:
    return f"{reference!r} is a builtin tool; write 'builtin:{reference}' to use it"


def _invalid_import(reference: object) -> str:
    return (
        f"invalid import reference {reference!r}: expected 'package.module:attribute' or "
        "'path/to/file.py:attribute'"
    )


def _reference_like(text: str) -> bool:
    """Whether `text` is meant as an import reference (a colon, and nothing an expression has)."""

    return ":" in text and not any(char in text for char in " '\"()=<>!")


def _instance_operations(
    instance: str, connectors: Mapping[str, Any], base: Path | None
) -> list[Any] | None:
    """Every operation of connector instance `instance` (`None` when only the system lists them)."""

    from awa.connectors._catalog import connector_context

    try:
        ctx = connector_context(
            instance, env={}, config=connector_options(instance, connectors, base)
        )
        return None if ctx.spec.dynamic else list(ctx.operations())
    except AwaError:
        return None


def _listing(names: Sequence[str]) -> str:
    return ", ".join(names) if names else "none"
