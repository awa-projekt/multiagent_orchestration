"""Turn a process into a running multi-agent system from names and tasks.

`team` builds a `Team`, `workflow` a `Sequential` and `network` an `AgentNetwork`, all through a
`Blueprint` (`explain(system).blueprint.to_yaml()` is the equivalent). A member is a task text,
`MemberOptions` or a ready `awa.Agent`. With `auto=True` agents built from a task get the builtin
skills and tools it calls for; tools that act outside the agent are only suggested.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

from awa._internal.upstream.checkpoint import BaseCheckpointSaver, StateInMemorySaver
from awa._internal.upstream.models import BaseChatModel
from awa._internal.upstream.tools import BaseTool
from awa.agents import Agent
from awa.blueprints.auto import AgentAutoReport, AutoReport, explain, remember_report
from awa.blueprints.schema import BLUEPRINT_FORMAT_VERSION, Blueprint
from awa.contracts.cards import AGENT_NAME_PATTERN
from awa.errors import AwaError, ConfigurationError
from awa.models import ModelRegistry
from awa.skills import SkillRegistry
from awa.tools.decorator import tool_spec
from awa.tools.toolset import Toolbox

if TYPE_CHECKING:
    from awa.agents.agent import AgentTool
    from awa.contracts.protocols import AgentLike
    from awa.team import AgentNetwork, Team
    from awa.toolkits import ToolkitConfig
    from awa.workflows import Sequential

Structure = Literal["team", "workflow", "network"]
"""The structures the quick functions build."""


class MemberOptions(TypedDict, total=False):
    """Options of an agent given as a mapping instead of a task text.

    Attributes:
        task: What the agent does; also its instructions when `instructions` is not given.
        tools: Tool objects, functions, `Toolbox`es or references such as `"builtin:read_csv"`.
        capabilities: Capability tags (network teammates claim tasks by them).
        auto: Auto-match skills and tools for this agent (overrides the function's `auto`).
        max_steps: Maximum model calls per run.
    """

    task: str
    instructions: str
    tools: Sequence[str | AgentTool]
    skills: Sequence[str]
    model: str
    capabilities: Sequence[str]
    auto: bool
    max_steps: int


Member = str | MemberOptions | Agent[Any, Any]
"""An agent of a quick system: a task text, `MemberOptions`, or a ready `Agent`."""

_OPTION_KEYS = frozenset(MemberOptions.__annotations__)
_TEAM_LEAD = (
    "Coordinates the team, delegates each part of the work to the right member and writes the "
    "final answer."
)
_NETWORK_LEAD = (
    "Leads the network, plans the work as tasks for the teammates and writes the final answer."
)
_SLUG = re.compile(r"[^a-z0-9]+")


def team(
    name: str,
    *,
    members: Mapping[str, Member],
    lead: str | Agent[Any, Any] = "lead",
    description: str = "",
    model: str | BaseChatModel | None = None,
    auto: bool = True,
    toolkits: ToolkitConfig | None = None,
    approvals: Sequence[str] = (),
    peers: Mapping[str, Sequence[str]] | None = None,
    task_board: bool = False,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    skill_registry: SkillRegistry | None = None,
) -> Team:
    """Build a `Team`: an orchestrator that plans, delegates to the members and answers.

    Args:
        name: Name of the process, e.g. `"Invoice processing"` (system name
            `invoice_processing`).
        members: Member name to task text, `MemberOptions` or `Agent`.
        lead: The orchestrator's name (a member of that name becomes the orchestrator), or an
            `Agent`. An orchestrator not in `members` coordinates with a generated task.
        description: What the process achieves; defaults to `name`.
        model: Model of every agent without its own: a profile, `provider:model` or a chat model.
        auto: Give agents the builtin skills and tools their tasks call for.
        toolkits: Options of the builtin toolkits (`{"files": {"root": "./data"}}`);
            builtin tools of an unconfigured toolkit are only suggested.
        approvals: Tool names whose calls wait for a human decision.
        peers: Members that consult or message each other directly.
        task_board: Give the team a shared `TaskBoard` the orchestrator plans on.
        checkpointer: An in-memory one is used when approvals need one and none is given.
        skill_registry: Skills to resolve and match; the builtin skills by default.

    Raises:
        ConfigurationError: If a name or member is invalid.
        BlueprintError: If the system cannot be built.

    Example:
        ```python
        desk = quick.team(
            "Invoice processing",
            lead="coordinator",
            members={"extractor": "Reads incoming invoices and extracts the fields.",
                     "checker": "Validates the totals against the purchase order."},
        )
        result = desk.run_sync("Process invoice INV-1042.")
        print(quick.explain(desk))  # which skills and tools each agent got, and why
        ```
    """

    agents, structure = _layout("team", _named(members), lead)
    structure["peers"] = {key: list(value) for key, value in (peers or {}).items()}
    structure["task_board"] = task_board
    return cast(
        "Team",
        _build(
            "team",
            name,
            agents,
            structure,
            description=description,
            model=model,
            auto=auto,
            toolkits=toolkits,
            approvals=approvals,
            checkpointer=checkpointer,
            skill_registry=skill_registry,
        ),
    )


def workflow(
    name: str,
    *,
    steps: Mapping[str, Member],
    carry: Literal["previous", "all"] = "previous",
    description: str = "",
    model: str | BaseChatModel | None = None,
    auto: bool = True,
    toolkits: ToolkitConfig | None = None,
    approvals: Sequence[str] = (),
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    skill_registry: SkillRegistry | None = None,
) -> Sequential:
    """Build a `Sequential` workflow: the steps run in order, each on the previous result.

    The other parameters are those of `team`.

    Args:
        steps: Step name to task text, `MemberOptions` or `Agent`, in order.
        carry: `"previous"` passes each step the previous step's text;
            `"all"` the request plus every earlier step's text.

    Raises:
        ConfigurationError: If a name or step is invalid.
        BlueprintError: If the system cannot be built.

    Example:
        ```python
        workflow = quick.workflow(
            "Contract review",
            steps={"reader": "Extracts the clauses of the contract.",
                   "writer": "Writes a short summary for the legal team."},
        )
        ```
    """

    agents, structure = _layout("workflow", _named(steps))
    structure["carry"] = carry
    return cast(
        "Sequential",
        _build(
            "workflow",
            name,
            agents,
            structure,
            description=description,
            model=model,
            auto=auto,
            toolkits=toolkits,
            approvals=approvals,
            checkpointer=checkpointer,
            skill_registry=skill_registry,
        ),
    )


def network(
    name: str,
    *,
    teammates: Mapping[str, Member],
    lead: str | Agent[Any, Any] = "lead",
    description: str = "",
    model: str | BaseChatModel | None = None,
    auto: bool = True,
    toolkits: ToolkitConfig | None = None,
    approvals: Sequence[str] = (),
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    skill_registry: SkillRegistry | None = None,
) -> AgentNetwork:
    """Build an `AgentNetwork`: a lead plans tasks that teammates work on concurrently.

    Teammates claim tasks by capability; one without `capabilities` gets its own name as its
    capability. `teammates` and the other parameters work as `members` and those of `team`.

    Raises:
        ConfigurationError: If a name or teammate is invalid.
        BlueprintError: If the system cannot be built.

    Example:
        ```python
        research = quick.network(
            "Market scan",
            teammates={"pricing": "Compares competitor prices.",
                       "reviews": "Summarizes customer reviews of competitors."},
        )
        ```
    """

    agents, structure = _layout("network", _named(teammates), lead)
    return cast(
        "AgentNetwork",
        _build(
            "network",
            name,
            agents,
            structure,
            description=description,
            model=model,
            auto=auto,
            toolkits=toolkits,
            approvals=approvals,
            checkpointer=checkpointer,
            skill_registry=skill_registry,
        ),
    )


def blueprint(
    structure: Structure,
    name: str,
    agents: Mapping[str, str | MemberOptions],
    *,
    lead: str | None = None,
    description: str = "",
    model: str | None = None,
    auto: bool = True,
    toolkits: ToolkitConfig | None = None,
    approvals: Sequence[str] = (),
) -> Blueprint:
    """Return the blueprint `team`, `workflow` or `network` would build, without building it.

    Tools and agents given as objects cannot be written to a blueprint; name tools in
    `MemberOptions.tools` and pass the objects to `Blueprint.build(tools=...)`.

    Args:
        agents: Agent name to task text or `MemberOptions`; for a workflow, in step order.
        lead: The orchestrator of a team or the lead of a network (default `"lead"`).

    Raises:
        ConfigurationError: If a name, agent or tool is invalid.

    Example:
        ```python
        spec = quick.blueprint("workflow", "Contract review",
                               {"reader": "Extracts clauses.", "writer": "Summarizes them."})
        Path("contract_review.yaml").write_text(spec.to_yaml())
        ```
    """

    specs = _named(agents)
    if structure == "workflow" and lead is not None:
        raise ConfigurationError("A workflow has no lead")
    specs, data = _layout(structure, specs, lead or "lead")
    result, tools, prebuilt = _plan(
        structure, name, specs, data, description, auto, toolkits, approvals, model
    )
    if tools:
        raise ConfigurationError(
            "quick.blueprint cannot write tool objects; name the tools and pass the objects to "
            "Blueprint.build(tools=...)"
        )
    if prebuilt:
        raise ConfigurationError(
            f"quick.blueprint cannot write Agent objects ({', '.join(sorted(prebuilt))}); give "
            "their task text or MemberOptions instead"
        )
    return result


def _plan(
    structure: Structure,
    name: str,
    agents: Mapping[str, Member],
    data: dict[str, Any],
    description: str,
    auto: bool,
    toolkits: ToolkitConfig | None,
    approvals: Sequence[str],
    model: str | None,
) -> tuple[Blueprint, dict[str, BaseTool], dict[str, Agent[Any, Any]]]:
    """The quick system as a blueprint, plus the tool objects and `Agent`s it cannot hold."""

    system = _slug(name, what="process name")
    tools: dict[str, BaseTool] = {}
    prebuilt: dict[str, Agent[Any, Any]] = {}
    entries: list[dict[str, Any]] = []
    for agent_name, member in agents.items():
        entry = _entry(agent_name, member, tools, prebuilt)
        if structure == "network" and agent_name in data["teammates"]:
            entry.setdefault("capabilities", [agent_name])
        entries.append(entry)
    document: dict[str, Any] = {
        "format_version": BLUEPRINT_FORMAT_VERSION,
        "name": system,
        "description": description or (name if name != system else ""),
        "agents": entries,
        structure: data,
        "approvals": [{"tool": tool} for tool in approvals],
        "toolkits": {key: dict(value) for key, value in (toolkits or {}).items()},
        "auto": auto,
    }
    if model is not None:
        document["defaults"] = {"model": model}
    return Blueprint.from_dict(document), tools, prebuilt


def _build(
    structure: Structure,
    name: str,
    agents: dict[str, Member],
    data: dict[str, Any],
    *,
    description: str,
    model: str | BaseChatModel | None,
    auto: bool,
    toolkits: ToolkitConfig | None,
    approvals: Sequence[str],
    checkpointer: BaseCheckpointSaver[Any] | None,
    skill_registry: SkillRegistry | None,
) -> AgentLike:
    from awa.blueprints._build import build_blueprint

    spec, tools, prebuilt = _plan(
        structure,
        name,
        agents,
        data,
        description,
        auto,
        toolkits,
        approvals,
        model if isinstance(model, str) else None,
    )
    registry: ModelRegistry | None = None
    if isinstance(model, BaseChatModel):
        registry = ModelRegistry()
        registry.register("default", model)
    if checkpointer is None and _needs_checkpointer(spec, tools):
        checkpointer = StateInMemorySaver()
    system = build_blueprint(
        spec,
        tools=tools,
        model_registry=registry,
        skill_registry=skill_registry,
        checkpointer=checkpointer,
        prebuilt=prebuilt,
    )
    remember_report(system, _with_prebuilt_notes(explain(system), prebuilt))
    return system


def _needs_checkpointer(blueprint: Blueprint, tools: Mapping[str, BaseTool]) -> bool:
    if blueprint.needs_checkpointer():
        return True
    specs = (tool_spec(tool) for tool in tools.values())
    return any(spec is not None and spec.approval != "never" for spec in specs)


def _with_prebuilt_notes(report: AutoReport, prebuilt: Mapping[str, Agent[Any, Any]]) -> AutoReport:
    if not prebuilt:
        return report
    agents: list[AgentAutoReport] = []
    for agent in report.agents:
        given = prebuilt.get(agent.agent)
        if given is not None:
            agent = replace(
                agent,
                explicit_tools=tuple(t.name for t in given.tools),
                explicit_skills=tuple(given.card.skills),
                note="given as an Agent: used as it is",
            )
        agents.append(agent)
    return replace(report, agents=tuple(agents))


def _layout(
    structure: Structure, agents: dict[str, Member], lead: str | Agent[Any, Any] = "lead"
) -> tuple[dict[str, Member], dict[str, Any]]:
    """Return the agents (orchestrator or lead first) and the structure section of the blueprint."""

    if structure == "workflow":
        if not agents:
            raise ConfigurationError("A workflow needs at least one step")
        return agents, {"sequential": list(agents)}
    if isinstance(lead, Agent):
        if lead.name in agents and agents[lead.name] is not lead:
            raise ConfigurationError(f"The lead {lead.name!r} is also given as another agent")
        name = lead.name
        agents = {name: lead, **{key: value for key, value in agents.items() if key != name}}
    else:
        name = lead if AGENT_NAME_PATTERN.fullmatch(lead) else _slug(lead, what="lead name")
        if name not in agents:
            agents = {name: _TEAM_LEAD if structure == "team" else _NETWORK_LEAD, **agents}
    others = [agent for agent in agents if agent != name]
    if structure == "team":
        return agents, {"orchestrator": name, "members": others}
    return agents, {"lead": name, "teammates": others}


def _entry(
    name: str, member: Member, tools: dict[str, BaseTool], prebuilt: dict[str, Agent[Any, Any]]
) -> dict[str, Any]:
    """The blueprint entry of one agent; tool objects and `Agent`s are collected aside."""

    if isinstance(member, Agent):
        if member.name != name:
            raise ConfigurationError(f"Agent {member.name!r} is given under the name {name!r}")
        prebuilt[name] = member
        return {"name": name, "description": member.description, "auto": False}
    if isinstance(member, str):
        return {"name": name, "description": member}
    if not isinstance(member, Mapping):
        raise ConfigurationError(
            f"Agent {name!r} must be a task text, a mapping of options or an Agent, got "
            f"{type(member).__name__}"
        )
    unknown = sorted(set(member) - _OPTION_KEYS)
    if unknown:
        raise ConfigurationError(
            f"Unknown option(s) {', '.join(unknown)} for agent {name!r} "
            f"(known: {', '.join(sorted(_OPTION_KEYS))})"
        )
    options: dict[str, Any] = dict(member)
    entry: dict[str, Any] = {"name": name, "description": options.pop("task", "")}
    references: list[str] = []
    for item in options.pop("tools", ()):
        references.extend(_tool_references(item, tools))
    if references:
        entry["tools"] = references
    for key in ("skills", "capabilities"):
        if key in options:
            entry[key] = list(options.pop(key))
    entry.update(options)
    return entry


def _named(agents: Mapping[str, Member]) -> dict[str, Member]:
    """`agents` under valid agent names (`"Invoice checker"` becomes `invoice_checker`)."""

    named: dict[str, Member] = {}
    for key, member in agents.items():
        if not isinstance(key, str):
            raise ConfigurationError(f"Agent names must be text, got {key!r}")
        name = key if AGENT_NAME_PATTERN.fullmatch(key) else _slug(key, what="agent name")
        if name in named:
            raise ConfigurationError(f"Two agents are named {name!r}")
        named[name] = member
    return named


def _tool_references(item: Any, tools: dict[str, BaseTool]) -> list[str]:
    from awa.blueprints._resolve import to_tool

    if isinstance(item, str):
        return [item]
    if isinstance(item, Toolbox):
        found: list[BaseTool] = item.tools()
    else:
        try:
            found = [to_tool(item, source=repr(item))]
        except AwaError as exc:
            raise ConfigurationError(str(exc)) from exc
    for tool in found:
        known = tools.get(tool.name)
        if known is not None and known is not tool:
            raise ConfigurationError(
                f"Two different tools are named {tool.name!r}; rename or prefix one of them"
            )
        tools[tool.name] = tool
    return [tool.name for tool in found]


def _slug(text: str, *, what: str) -> str:
    """`text` as a valid system or agent name (`Invoice processing` -> `invoice_processing`)."""

    slug = _SLUG.sub("_", text.strip().lower()).strip("_")[:48].rstrip("_")
    if not slug or not slug[0].isalpha():
        raise ConfigurationError(
            f"Invalid {what} {text!r}: it needs to start with a letter, e.g. 'Invoice processing'"
        )
    return slug


__all__ = [
    "Member",
    "MemberOptions",
    "Structure",
    "blueprint",
    "explain",
    "network",
    "team",
    "workflow",
]
