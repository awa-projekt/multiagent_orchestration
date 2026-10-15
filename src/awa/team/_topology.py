"""Team topology: who may delegate to, consult, and hand off to whom."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from awa.errors import TopologyError

MemberKind = Literal["agent", "team", "workflow", "other"]
"""How a member is run: an `Agent`, a nested `Team`, a workflow, or any other `AgentLike`."""

EdgeKind = Literal["delegates", "peers", "handoffs"]


@dataclass(frozen=True)
class MemberInfo:
    """What the topology needs to know about one agent of a team."""

    name: str
    kind: MemberKind
    has_output_type: bool = False
    nested: Topology | None = None

    @property
    def is_agent(self) -> bool:
        """Whether the member is an `Agent` (and can therefore receive team tools)."""

        return self.kind == "agent"


@dataclass(frozen=True)
class Topology:
    """The validated, immutable edges of one team; `peers` is already symmetric when configured so."""

    team: str
    orchestrator: MemberInfo
    members: tuple[MemberInfo, ...]
    delegates: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    peers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    handoffs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def agents(self) -> Iterator[MemberInfo]:
        """The orchestrator, then every member."""

        yield self.orchestrator
        yield from self.members

    def all_names(self) -> tuple[str, ...]:
        """The team name and every agent name in the team, including nested teams."""

        names = [self.team]
        for info in self.agents():
            if info.nested is not None:
                names.extend(info.nested.all_names())
            else:
                names.append(info.name)
        return tuple(names)

    def delegates_of(self, name: str) -> tuple[str, ...]:
        """Members `name` may delegate to."""

        return self.delegates.get(name, ())

    def peers_of(self, name: str) -> tuple[str, ...]:
        """Peers `name` may consult and message."""

        return self.peers.get(name, ())

    def handoffs_of(self, name: str) -> tuple[str, ...]:
        """Agents `name` may transfer control to."""

        return self.handoffs.get(name, ())

    def reachable(self, name: str) -> tuple[str, ...]:
        """Every agent `name` can contact through any edge, in first-seen order."""

        targets = (*self.delegates_of(name), *self.peers_of(name), *self.handoffs_of(name))
        return tuple(dict.fromkeys(targets))

    def active_agents(self) -> tuple[str, ...]:
        """Agents that can hold control: the orchestrator and every handoff target."""

        targets = (t for targets in self.handoffs.values() for t in targets)
        return tuple(dict.fromkeys((self.orchestrator.name, *targets)))

    def as_dict(self) -> dict[str, dict[str, list[str]]]:
        """The edges as plain data: `{"delegates": ..., "peers": ..., "handoffs": ...}`."""

        return {
            "delegates": {source: list(t) for source, t in self.delegates.items()},
            "peers": {source: list(t) for source, t in self.peers.items()},
            "handoffs": {source: list(t) for source, t in self.handoffs.items()},
        }

    def to_mermaid(self) -> str:
        """Render the team as a Mermaid flowchart; see `Team.to_mermaid`."""

        ids: dict[int, str] = {}
        lines = ["flowchart TD"]
        _render_team(self, lines, ids, indent="    ")
        return "\n".join(lines)


def build_topology(
    team: str,
    orchestrator: MemberInfo,
    members: Sequence[MemberInfo],
    *,
    delegates: Mapping[str, Sequence[str]] | None,
    peers: Mapping[str, Sequence[str]] | None,
    symmetric_peers: bool,
    handoffs: Mapping[str, Sequence[str]] | None,
    label: str = "Team",
) -> Topology:
    """Validate a team's declaration and return its `Topology`.

    Args:
        delegates: Delegation edges; `None` lets the orchestrator delegate to every member.
        label: What the structure is called in error messages.

    Raises:
        TopologyError: If the declaration is inconsistent (see `Team`).
    """

    prefix = f"{label} {team!r}:"
    if not members:
        raise TopologyError(f"{prefix} a team needs at least one member.")
    if any(member.name == orchestrator.name for member in members):
        raise TopologyError(
            f"{prefix} the orchestrator {orchestrator.name!r} must not also be listed as a member."
        )
    _check_unique_names(prefix, team, orchestrator, members)

    by_name = {info.name: info for info in (orchestrator, *members)}
    member_names = {member.name for member in members}
    known = ", ".join(by_name)

    def known_agent(kind: EdgeKind, name: str) -> MemberInfo:
        info = by_name.get(name)
        if info is None:
            raise TopologyError(
                f"{prefix} {kind} lists unknown agent {name!r}. Known agents: {known}."
            )
        return info

    def source_agent(kind: EdgeKind, name: str) -> MemberInfo:
        info = known_agent(kind, name)
        if not info.is_agent:
            raise TopologyError(
                f"{prefix} {name!r} is a {info.kind} and cannot have outgoing {kind} edges; "
                "only Agents receive team tools."
            )
        return info

    def agent_target(kind: EdgeKind, source: str, target: str, rule: str) -> None:
        info = known_agent(kind, target)
        if not info.is_agent:
            raise TopologyError(
                f"{prefix} {kind} edge {source!r} -> {target!r} is invalid: {target!r} is a "
                f"{info.kind}, and {rule}."
            )

    default_delegates = {orchestrator.name: [member.name for member in members]}
    delegate_edges = _normalize(
        prefix, "delegates", default_delegates if delegates is None else delegates
    )
    for source, targets in delegate_edges.items():
        source_agent("delegates", source)
        for target in targets:
            known_agent("delegates", target)
            if target not in member_names:
                raise TopologyError(
                    f"{prefix} delegates edge {source!r} -> {target!r} targets the orchestrator; "
                    "delegation targets must be members."
                )

    peer_edges = _normalize(prefix, "peers", peers or {})
    for source, targets in peer_edges.items():
        for name in (source, *targets):
            if name == orchestrator.name:
                raise TopologyError(
                    f"{prefix} peers edges connect members only; {name!r} is the orchestrator "
                    "and delegates instead."
                )
        source_agent("peers", source)
        for target in targets:
            agent_target("peers", source, target, "peers must be Agents")
    if symmetric_peers:
        peer_edges = _symmetric(peer_edges)

    handoff_edges = _normalize(prefix, "handoffs", handoffs or {})
    for source, targets in handoff_edges.items():
        if source_agent("handoffs", source).has_output_type:
            raise TopologyError(
                f"{prefix} {source!r} declares an output_type and cannot hand off; an agent "
                "that transfers control ends without producing structured output."
            )
        for target in targets:
            agent_target(
                "handoffs",
                source,
                target,
                "handoff targets must be Agents because they take over the conversation",
            )

    return Topology(
        team=team,
        orchestrator=orchestrator,
        members=tuple(members),
        delegates=delegate_edges,
        peers=peer_edges,
        handoffs=handoff_edges,
    )


def _check_unique_names(
    prefix: str, team: str, orchestrator: MemberInfo, members: Sequence[MemberInfo]
) -> None:
    seen: set[str] = {team}
    for info in (orchestrator, *members):
        names = info.nested.all_names() if info.nested is not None else (info.name,)
        for name in names:
            if name in seen:
                raise TopologyError(
                    f"{prefix} the name {name!r} is used more than once; agent and team names "
                    "must be unique across the team, including nested teams."
                )
            seen.add(name)


def _normalize(
    prefix: str, kind: EdgeKind, edges: Mapping[str, Sequence[str]]
) -> dict[str, tuple[str, ...]]:
    normalized: dict[str, tuple[str, ...]] = {}
    for source, targets in edges.items():
        if isinstance(targets, str):
            raise TopologyError(
                f"{prefix} {kind}[{source!r}] must be a sequence of agent names, not the string "
                f"{targets!r}."
            )
        unique = tuple(dict.fromkeys(targets))
        if source in unique:
            raise TopologyError(f"{prefix} {kind} edge {source!r} -> {source!r} points to itself.")
        if unique:
            normalized[source] = unique
    return normalized


def _symmetric(edges: Mapping[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    graph: dict[str, list[str]] = {source: list(targets) for source, targets in edges.items()}
    for source, targets in edges.items():
        for target in targets:
            reverse = graph.setdefault(target, [])
            if source not in reverse:
                reverse.append(source)
    return {source: tuple(targets) for source, targets in graph.items()}


def _render_team(topology: Topology, lines: list[str], ids: dict[int, str], *, indent: str) -> None:
    """Append the nodes and edges of `topology` (recursing into nested teams)."""

    def node_id(info: MemberInfo) -> str:
        return ids.setdefault(id(info), f"n{len(ids)}")

    for info in topology.agents():
        nid = node_id(info)
        if info.nested is not None:
            lines.append(f'{indent}subgraph {nid}["{info.name} (team)"]')
            _render_team(info.nested, lines, ids, indent=indent + "    ")
            lines.append(f"{indent}end")
        elif info is topology.orchestrator:
            lines.append(f'{indent}{nid}(["{info.name}"])')
        elif info.kind in ("workflow", "other"):
            lines.append(f'{indent}{nid}[["{info.name}"]]')
        else:
            lines.append(f'{indent}{nid}["{info.name}"]')

    by_name = {info.name: node_id(info) for info in topology.agents()}
    for source, targets in topology.delegates.items():
        lines.extend(f"{indent}{by_name[source]} --> {by_name[target]}" for target in targets)
    rendered: set[tuple[str, str]] = set()
    for source, targets in topology.peers.items():
        for target in targets:
            if (target, source) in rendered:
                continue
            rendered.add((source, target))
            arrow = "<-.->" if source in topology.peers.get(target, ()) else "-.->"
            lines.append(f"{indent}{by_name[source]} {arrow} {by_name[target]}")
    for source, targets in topology.handoffs.items():
        lines.extend(
            f"{indent}{by_name[source]} ==>|handoff| {by_name[target]}" for target in targets
        )
