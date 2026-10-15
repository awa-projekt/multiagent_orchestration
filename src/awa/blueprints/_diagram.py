"""Flow diagrams of blueprints: one node/edge model, rendered as Mermaid here and as SVG in the UI."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from typing import Literal

from awa.blueprints._mcp import uses_tool
from awa.blueprints._schema_process import ApprovalCheckBlueprint
from awa.blueprints.schema import (
    Blueprint,
    LoopStep,
    MapStep,
    NetworkBlueprint,
    ParallelStep,
    SequentialStep,
    TeamBlueprint,
    WorkflowNode,
)

Shape = Literal["lead", "agent", "group", "terminal"]
"""`lead`: orchestrator or network lead; `agent`: an agent; `group`: a nested team, workflow
or network; `terminal`: the `input` and `output` ends of a workflow."""

EdgeStyle = Literal["solid", "thick", "dotted", "both"]
"""`solid`: delegation or step; `thick`: handoff; `dotted`: one-way peer or loop back;
`both`: two-way peers."""

_ARROWS: dict[str, str] = {"solid": "-->", "thick": "==>", "dotted": "-.->", "both": "<-.->"}
_BOXES: dict[str, str] = {
    "terminal": "([{}])",
    "lead": '(["{}"])',
    "group": '[["{}"]]',
    "agent": '["{}"]',
}


@dataclass(frozen=True)
class DiagramNode:
    """A box of the diagram; `lines` is its label: the name, then notes such as approvals."""

    id: str
    lines: tuple[str, ...]
    shape: Shape = "agent"


@dataclass(frozen=True)
class DiagramEdge:
    """An arrow between two node ids (a target may name no node: Mermaid then adds one)."""

    source: str
    target: str
    style: EdgeStyle = "solid"
    label: str = ""


@dataclass(frozen=True)
class Diagram:
    """Nodes and edges in drawing order; `TD` flows top-down, `LR` left to right."""

    direction: Literal["TD", "LR"]
    items: tuple[DiagramNode | DiagramEdge, ...]

    @property
    def nodes(self) -> list[DiagramNode]:
        return [item for item in self.items if isinstance(item, DiagramNode)]

    @property
    def edges(self) -> list[DiagramEdge]:
        return [item for item in self.items if isinstance(item, DiagramEdge)]


def blueprint_diagram(blueprint: Blueprint) -> Diagram:
    """The diagram of the entrypoint of `blueprint`.

    Solid arrows delegate, dotted arrows are peer or messaging edges, thick labeled arrows
    handoffs; workflow steps flow from `input` to `output`, loops have a dotted back edge. Nested
    structures are group nodes, and agents name their approval-gated tools.
    """

    entrypoint = blueprint.resolved_entrypoint()
    if entrypoint == "team" and blueprint.team is not None:
        return Diagram("TD", tuple(_team(blueprint, blueprint.team)))
    if entrypoint == "network" and blueprint.network is not None:
        return Diagram("TD", tuple(_network(blueprint, blueprint.network)))
    if entrypoint == "workflow" and blueprint.workflow is not None:
        return _Workflow(blueprint).diagram(blueprint.workflow)
    if entrypoint == "workflow" and blueprint.process is not None:
        return _process(blueprint)
    from awa.blueprints._roles import role_root

    if entrypoint is not None and role_root(blueprint) == entrypoint:
        from awa.blueprints._diagram_roles import role_diagram

        return role_diagram(blueprint, entrypoint)
    name = entrypoint or blueprint.name
    return Diagram("TD", (DiagramNode("a0", node_label(blueprint, name)),))


def diagram_to_mermaid(diagram: Diagram) -> str:
    """Write `diagram` as a Mermaid flowchart."""

    lines = [f"flowchart {diagram.direction}"]
    for item in diagram.items:
        if isinstance(item, DiagramNode):
            # `"` would end the quoted node label (tool names in approval notes are free text).
            box = _BOXES[item.shape].format("<br/>".join(item.lines).replace('"', "'"))
            lines.append(f"    {item.id}{box}")
        else:
            # `|` would end a Mermaid edge label and `"` a quoted string.
            label = item.label.replace('"', "'").replace("|", "/")
            arrow = _ARROWS[item.style] + (f"|{label}|" if label else "")
            lines.append(f"    {item.source} {arrow} {item.target}")
    return "\n".join(lines)


def blueprint_to_mermaid(blueprint: Blueprint) -> str:
    """Render the entrypoint of `blueprint` as a Mermaid flowchart (see `blueprint_diagram`)."""

    return diagram_to_mermaid(blueprint_diagram(blueprint))


def _team(blueprint: Blueprint, team: TeamBlueprint) -> list[DiagramNode | DiagramEdge]:
    names = list(dict.fromkeys([team.orchestrator, *team.members]))
    ids = {name: f"a{index}" for index, name in enumerate(names)}

    def node(name: str) -> str:
        return ids.get(name, name)

    items: list[DiagramNode | DiagramEdge] = [
        DiagramNode(ids[team.orchestrator], node_label(blueprint, team.orchestrator), "lead")
    ]
    items += [_member(blueprint, ids[name], name) for name in names[1:]]
    delegates = (
        team.delegates if team.delegates is not None else {team.orchestrator: list(team.members)}
    )
    for source, targets in delegates.items():
        items += [DiagramEdge(node(source), node(target)) for target in targets]
    drawn: set[frozenset[str]] = set()
    for source, targets in team.peers.items():
        for target in targets:
            pair = frozenset((source, target))
            if pair in drawn:
                continue
            drawn.add(pair)
            both = team.symmetric_peers or source in team.peers.get(target, [])
            items.append(DiagramEdge(node(source), node(target), "both" if both else "dotted"))
    for source, targets in team.handoffs.items():
        items += [DiagramEdge(node(source), node(target), "thick", "handoff") for target in targets]
    return items


def _network(blueprint: Blueprint, network: NetworkBlueprint) -> list[DiagramNode | DiagramEdge]:
    names = list(dict.fromkeys([network.lead, *network.teammates]))
    ids = {name: f"a{index}" for index, name in enumerate(names)}
    lead, teammates = ids[network.lead], names[1:]
    items: list[DiagramNode | DiagramEdge] = [
        DiagramNode(lead, node_label(blueprint, network.lead), "lead")
    ]
    items += [DiagramNode(ids[name], node_label(blueprint, name)) for name in teammates]
    items += [DiagramEdge(lead, ids[name], "solid", "tasks") for name in teammates]
    if network.peers == "all":
        pairs = [(a, b) for i, a in enumerate(teammates) for b in teammates[i + 1 :]]
    else:
        drawn: set[frozenset[str]] = set()
        pairs = []
        for source, targets in network.peers.items():
            for target in targets:
                pair = frozenset((source, target))
                if pair not in drawn and source in ids and target in ids:
                    drawn.add(pair)
                    pairs.append((source, target))
    items += [DiagramEdge(ids[a], ids[b], "both") for a, b in pairs]
    return items


def _process(blueprint: Blueprint) -> Diagram:
    """A process: the steps in a row from `input` to `output`, each with its number, title,
    agent and a note on its checks (an approval is named)."""

    process = blueprint.process
    assert process is not None
    items: list[DiagramNode | DiagramEdge] = [
        DiagramNode("input", ("input",), "terminal"),
        DiagramNode("output", ("output",), "terminal"),
    ]
    previous = "input"
    for number, step in enumerate(process.steps, 1):
        node_id = f"s{number}"
        asks = any(isinstance(check, ApprovalCheckBlueprint) for check in step.checks)
        rules = sum(1 for check in step.checks if not isinstance(check, ApprovalCheckBlueprint))
        notes = []
        if rules:
            notes.append(f"{rules} check{'s' if rules != 1 else ''}")
        if asks:
            notes.append("approval")
        lines = (f"{number} {step.title or step.name}", step.agent or "no agent yet")
        if blueprint.structure_named(step.agent) is not None:
            shape: Shape = "group"
        else:
            shape = "agent"
        items.append(
            DiagramNode(
                node_id, (*lines, ", ".join(notes) if notes else "automatic checks only"), shape
            )
        )
        items.append(DiagramEdge(previous, node_id))
        previous = node_id
    items.append(DiagramEdge(previous, "output"))
    return Diagram("LR", tuple(items))


def _member(blueprint: Blueprint, node_id: str, name: str) -> DiagramNode:
    """A team member or workflow step: an agent, or a group for a nested structure."""

    structure = blueprint.structure_named(name)
    if structure is not None:
        return DiagramNode(node_id, (f"{structure} {name}",), "group")
    return DiagramNode(node_id, node_label(blueprint, name))


class _Workflow:
    def __init__(self, blueprint: Blueprint) -> None:
        self._bp = blueprint
        self._ids = count(1)
        self._items: list[DiagramNode | DiagramEdge] = [
            DiagramNode("input", ("input",), "terminal"),
            DiagramNode("output", ("output",), "terminal"),
        ]

    def diagram(self, node: WorkflowNode) -> Diagram:
        entries, exits = self._node(node)
        self._connect(["input"], entries)
        self._connect(exits, ["output"])
        return Diagram("LR", tuple(self._items))

    def _node(self, node: WorkflowNode) -> tuple[list[str], list[str]]:
        if isinstance(node, str):
            node_id = f"n{next(self._ids)}"
            self._items.append(_member(self._bp, node_id, node))
            return [node_id], [node_id]
        if isinstance(node, SequentialStep):
            entries: list[str] = []
            exits: list[str] = []
            for index, child in enumerate(node.sequential):
                child_entries, child_exits = self._node(child)
                if index == 0:
                    entries = child_entries
                else:
                    self._connect(exits, child_entries)
                exits = child_exits
            return entries, exits
        if isinstance(node, ParallelStep):
            branches = [self._node(child) for child in node.parallel]
            return (
                [i for branch_entries, _ in branches for i in branch_entries],
                [i for _, branch_exits in branches for i in branch_exits],
            )
        if isinstance(node, MapStep):
            return self._map(node)
        return self._loop(node)

    def _map(self, node: MapStep) -> tuple[list[str], list[str]]:
        """A `for each` box fanning out to the step run per part of the list."""

        node_id = f"n{next(self._ids)}"
        per = "each item" if node.part_size == 1 else f"parts of {node.part_size}"
        self._items.append(DiagramNode(node_id, (f"for {per}",), "group"))
        entries, exits = self._node(node.map)
        self._connect([node_id], entries)
        return [node_id], exits

    def _loop(self, node: LoopStep) -> tuple[list[str], list[str]]:
        entries, exits = self._node(node.loop)
        if node.until_contains is not None:
            condition = f"until {node.until_contains!r}"
        elif node.until is not None:
            condition = f"until {node.until}"
        else:
            condition = "repeat"
        label = f"{condition}, max {node.max_iterations}"
        self._items += [
            DiagramEdge(source, target, "dotted", label) for source in exits for target in entries
        ]
        return entries, exits

    def _connect(self, sources: list[str], targets: list[str]) -> None:
        self._items += [DiagramEdge(source, target) for source in sources for target in targets]


def node_label(blueprint: Blueprint, agent: str) -> tuple[str, ...]:
    """The label lines of an agent's box: its name, then the approvals it waits for."""

    gated = [
        approval.tool
        for approval in blueprint.approvals
        if any(
            spec.name == agent and uses_tool(spec.tools, approval.tool) for spec in blueprint.agents
        )
    ]
    return (agent, f"approval: {', '.join(gated)}") if gated else (agent,)
