"""The excessive-agency ("lethal trifecta") check of `Blueprint.check`; see `awa.tools.risk`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from awa._internal.upstream.tools import BaseTool
from awa.blueprints._mcp import (
    MCP_PREFIX,
    is_mcp_reference,
    loaded_tools,
    mcp_tool_name,
    parse_mcp_reference,
    uses_tool,
)
from awa.blueprints._resolve import (
    ToolCatalog,
    builtin_requires_approval,
    builtin_risk,
    connector_operations,
    is_builtin_reference,
    is_connector_reference,
    is_import_reference,
)
from awa.blueprints.errors import BlueprintIssue
from awa.blueprints.schema import AgentBlueprint, Blueprint
from awa.tools.decorator import tool_spec
from awa.tools.risk import ToolRisk, infer_tool_risk

_MAX_LISTED = 5


@dataclass(frozen=True)
class _AgentTool:
    """One tool an agent can call, with its resolved risk."""

    label: str
    risk: ToolRisk
    gated: bool


def risk_issues(blueprint: Blueprint, catalog: ToolCatalog | None) -> list[BlueprintIssue]:
    """Return the excessive-agency warnings and the unused `tool_risks` entries."""

    issues: list[BlueprintIssue] = []
    for key in blueprint.tool_risks:
        if not any(_mentions(blueprint, spec.tools, key) for spec in blueprint.agents):
            issues.append(
                BlueprintIssue("warning", f"tool_risks.{key}", f"no agent uses tool {key!r}")
            )
    for index, spec in enumerate(blueprint.agents):
        tools = agent_tools(blueprint, spec, catalog)
        untrusted = [tool.label for tool in tools if tool.risk.untrusted_input]
        actions = [tool.label for tool in tools if tool.risk.external_action and not tool.gated]
        if not untrusted or not actions:
            continue
        private = [tool.label for tool in tools if tool.risk.private_data]
        message = (
            f"agent {spec.name!r} reads untrusted content ({_names(untrusted)}) and can act "
            f"without approval ({_names(actions)})"
        )
        if private:
            message += (
                f", and it reads private data ({_names(private)}): all three parts of the "
                "'lethal trifecta'; instructions injected into that content could make it "
                "leak the data or act for an attacker"
            )
        else:
            message += "; instructions injected into that content could trigger those actions"
        message += (
            ". Gate the actions with an approval, split reading and acting between agents, "
            "or correct the tools' risks in tool_risks if this is a false alarm"
        )
        issues.append(BlueprintIssue("warning", f"agents[{index}].tools", message))
    return issues


def ungated_action_issues(
    blueprint: Blueprint, catalog: ToolCatalog | None
) -> list[BlueprintIssue]:
    """Errors for every tool that sends or changes something outside and that nothing gates.

    Each error names the tool and how to gate it; a tool without outside effect says so in
    `tool_risks`.
    """

    issues: list[BlueprintIssue] = []
    for index, spec in enumerate(blueprint.agents):
        for tool in agent_tools(blueprint, spec, catalog):
            if not tool.risk.external_action or tool.gated:
                continue
            issues.append(
                BlueprintIssue(
                    "error",
                    f"agents[{index}].tools",
                    f"agent {spec.name!r} can run {tool.label}, which sends something out or "
                    "changes something outside, with no approval: a person must decide before "
                    f"it runs. Add `{tool.label}` to the blueprint's approvals (with a `when:` "
                    "condition if only some calls need a person), and tell the agent to call "
                    "it when the step is due; the approval makes the run wait by itself",
                )
            )
    return issues


def acting_tools(blueprint: Blueprint, name: str, catalog: ToolCatalog | None) -> list[str]:
    """The references with which `name` acts outside, gated or not; a team or network counts every agent."""

    specs = [spec for spec in blueprint.agents if spec.name == name]
    if not specs and blueprint.structure_named(name) is not None:
        specs = list(blueprint.agents)
    return list(
        dict.fromkeys(
            tool.label
            for spec in specs
            for tool in agent_tools(blueprint, spec, catalog)
            if tool.risk.external_action
        )
    )


def agent_tool_risks(
    blueprint: Blueprint, spec: AgentBlueprint, catalog: ToolCatalog | None = None
) -> list[tuple[str, ToolRisk, bool]]:
    """Every tool `spec` can call as `(reference, risk, gated)`: its resolved risk, and whether
    an approval, a policy or the tool itself makes a person decide before a call runs."""

    return [(tool.label, tool.risk, tool.gated) for tool in agent_tools(blueprint, spec, catalog)]


def agent_tools(
    blueprint: Blueprint, spec: AgentBlueprint, catalog: ToolCatalog | None
) -> list[_AgentTool]:
    tools: list[_AgentTool] = []
    for reference in dict.fromkeys(spec.tools):
        if is_builtin_reference(reference):
            tools.extend(_builtin_tools(blueprint, spec.name, reference))
        elif is_connector_reference(reference):
            tools.extend(_connector_tools(blueprint, spec.name, reference))
        elif is_mcp_reference(reference):
            tools.extend(_mcp_tools(blueprint, spec.name, reference, catalog))
        else:
            tools.append(_plain_tool(blueprint, spec.name, reference, catalog))
    return tools


def _builtin_tools(blueprint: Blueprint, agent: str, reference: str) -> list[_AgentTool]:
    builtin = builtin_risk(reference, blueprint.toolkits)
    if builtin is None:
        return []  # reported as an error by the reference check
    name = reference.removeprefix("builtin:")
    risk = infer_tool_risk(
        name,
        declared=blueprint.tool_risks.get(reference),
        metadata={"awa_risk": builtin.model_dump(exclude_none=True)},
    )
    gated = (
        _approved(blueprint, reference)
        or builtin_requires_approval(reference, blueprint.toolkits)
        or _policy_gated(blueprint, agent, name, risk)
    )
    return [_AgentTool(reference, risk, gated)]


def _plain_tool(
    blueprint: Blueprint, agent: str, reference: str, catalog: ToolCatalog | None
) -> _AgentTool:
    """A tool from the catalog or an import reference."""

    tool = catalog.tools.get(reference) if catalog is not None else None
    if tool is not None:
        name = tool.name
    elif is_import_reference(reference):
        name = reference.rpartition(":")[2].rpartition(".")[2]
    else:
        name = reference
    risk = infer_tool_risk(
        tool if tool is not None else name, declared=blueprint.tool_risks.get(reference)
    )
    gated = (
        _approved(blueprint, reference)
        or _gates_itself(tool)
        or _policy_gated(blueprint, agent, name, risk)
    )
    return _AgentTool(reference, risk, gated)


def _mcp_tools(
    blueprint: Blueprint, agent: str, reference: str, catalog: ToolCatalog | None
) -> list[_AgentTool]:
    """The MCP tools `reference` stands for: one tool, the loaded ones, or the whole server."""

    server_name, tool_name = parse_mcp_reference(reference)
    server = next((s for s in blueprint.mcp_servers if s.name == server_name), None)
    if server is None or not server_name:
        return []  # reported as an error by the reference check
    whole = f"{MCP_PREFIX}{server_name}"
    available = loaded_tools(list(catalog.tools.values()), server_name) if catalog else []
    if tool_name is not None:
        loaded = next((t for t in available if mcp_tool_name(t) == tool_name), None)
        return [_mcp_tool(blueprint, agent, reference, server_name, tool_name, loaded)]
    if available:
        return [
            _mcp_tool(blueprint, agent, f"{whole}/{name}", server_name, name, t)
            for t in available
            if server.selects(name := mcp_tool_name(t))
        ]
    # Tools not loaded: only what the server is (external) is known.
    risk = infer_tool_risk(
        "", declared=blueprint.tool_risks.get(whole), metadata={"mcp_server": server_name}
    )
    gated = _approved(blueprint, whole) or _policy_gated(blueprint, agent, "", risk)
    return [_AgentTool(whole, risk, gated)]


def _connector_tools(blueprint: Blueprint, agent: str, reference: str) -> list[_AgentTool]:
    """The connector tools `reference` stands for, with their risks and gates."""

    from awa.connectors._engine import READ_RISK, WRITE_RISK

    instance, _, operation = reference.removeprefix("connector:").partition(".")
    base = blueprint.source.parent if blueprint.source is not None else None
    options = blueprint.connectors.get(instance) or {}
    default_gate = options.get("requires_approval", True) is not False
    whole = f"connector:{instance}"
    resolved = connector_operations(reference, blueprint.connectors, base)
    if resolved is None:
        if not operation:
            return [_AgentTool(reference, READ_RISK, False)]
        risk = infer_tool_risk(operation, declared=blueprint.tool_risks.get(reference))
        gated = (
            _approved(blueprint, reference, whole)
            or (default_gate and bool(risk.external_action))
            or _policy_gated(blueprint, agent, operation, risk)
        )
        return [_AgentTool(reference, risk, gated)]
    tools = []
    for op in resolved[1]:
        label = f"{whole}.{op.name}"
        declared = blueprint.tool_risks.get(label) or blueprint.tool_risks.get(whole)
        base_risk = WRITE_RISK if op.write else READ_RISK
        risk = declared.merged_with(base_risk) if declared is not None else base_risk
        gated = (
            _approved(blueprint, label, whole)
            or (op.write and default_gate)
            or _policy_gated(blueprint, agent, f"{instance}_{op.name}", risk)
        )
        tools.append(_AgentTool(label, risk, gated))
    return tools


def _gates_itself(tool: BaseTool | None) -> bool:
    """Whether the tool waits for approval itself (`awa.tool(approval=...)`)."""

    spec = tool_spec(tool) if tool is not None else None
    return spec is not None and spec.approval != "never"


def _policy_gated(blueprint: Blueprint, agent: str, name: str, risk: ToolRisk) -> bool:
    """Whether a blueprint policy stops the agent's calls to the tool before they run."""

    flags = set(risk.flags())
    for policy in blueprint.policies:
        if policy.applies_to(agent) and ((name and name in policy.deny) or flags & set(policy.ask)):
            return True
    return False


def _approved(blueprint: Blueprint, *labels: str) -> bool:
    """Whether the blueprint declares an approval for one of `labels`."""

    return any(approval.tool in labels for approval in blueprint.approvals)


def _mcp_tool(
    blueprint: Blueprint,
    agent: str,
    label: str,
    server: str,
    tool_name: str,
    loaded: BaseTool | None,
) -> _AgentTool:
    whole = f"{MCP_PREFIX}{server}"
    specific = blueprint.tool_risks.get(f"{whole}/{tool_name}")
    general = blueprint.tool_risks.get(whole)
    declared = specific.merged_with(general) if specific is not None else general
    if loaded is not None:
        risk = infer_tool_risk(loaded, declared=declared)
    else:
        metadata = {"mcp_server": server, "mcp_tool": tool_name}
        risk = infer_tool_risk(tool_name, declared=declared, metadata=metadata)
    name = loaded.name if loaded is not None else tool_name
    gated = (
        _approved(blueprint, f"{whole}/{tool_name}", whole)
        or _gates_itself(loaded)
        or _policy_gated(blueprint, agent, name, risk)
    )
    return _AgentTool(label, risk, gated)


def _mentions(blueprint: Blueprint, tools: Sequence[str], key: str) -> bool:
    """Whether an agent listing `tools` uses the tool (or server/connector) `key` names."""

    if uses_tool(tools, key) or any(tool.startswith(f"{key}/") for tool in tools):
        return True
    instance, _, operation = key.partition(".")
    if not (is_connector_reference(key) and operation and instance in tools):
        return False
    # `connector:<name>` stands for the read operations (all of them when only listed at run time).
    base = blueprint.source.parent if blueprint.source is not None else None
    resolved = connector_operations(instance, blueprint.connectors, base)
    return resolved is None or any(op.name == operation for op in resolved[1])


def _names(labels: Sequence[str]) -> str:
    shown = ", ".join(labels[:_MAX_LISTED])
    return shown if len(labels) <= _MAX_LISTED else f"{shown}, … ({len(labels)} tools)"
