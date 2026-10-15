"""The role tree of a blueprint: who leads whom (`sub_agents`), who judges and who reviews."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from awa.blueprints.schema import AgentBlueprint, Blueprint

DEFAULT_MAX_DEPTH = 3
"""Levels below the root a role tree may have unless the root's `delegation` says otherwise."""

DEFAULT_MAX_PARALLEL = 20
"""Delegations one compiled team runs at once unless the root's `delegation` says otherwise."""

DEFAULT_MAX_ITERATIONS = 3
"""Times a judge may send a worker's output back unless the parent's `max_iterations` says so."""


def uses_role_tree(blueprint: Blueprint) -> bool:
    """Whether any agent declares `sub_agents`, `judges` or `reviews`."""

    return any(spec.sub_agents or spec.judges or spec.reviews for spec in blueprint.agents)


def role_roots(blueprint: Blueprint) -> list[str]:
    """The agents that have sub-agents and are not listed as another agent's sub-agent."""

    children = {name for spec in blueprint.agents for name in spec.sub_agents}
    return [spec.name for spec in blueprint.agents if spec.sub_agents and spec.name not in children]


def role_root(blueprint: Blueprint) -> str | None:
    """The root of the role tree the entrypoint runs, or `None`.

    Only a blueprint without `team`, `workflow`, `network` or `process` has one; `entrypoint`
    names it, else the single candidate is the root.
    """

    if blueprint.team or blueprint.workflow or blueprint.network or blueprint.process:
        return None
    roots = role_roots(blueprint)
    if blueprint.entrypoint is not None:
        return blueprint.entrypoint if blueprint.entrypoint in roots else None
    return roots[0] if len(roots) == 1 else None


def bound_judge(blueprint: Blueprint, worker: str) -> AgentBlueprint | None:
    """The judge that gates `worker` (the first, when several claim it)."""

    return next((spec for spec in blueprint.agents if spec.judges == worker), None)


def bound_reviewers(blueprint: Blueprint, worker: str) -> list[AgentBlueprint]:
    """The reviewers that read `worker`'s finished output, in blueprint order."""

    return [spec for spec in blueprint.agents if spec.reviews == worker]


def role_members(blueprint: Blueprint) -> list[str]:
    """The agents of the role tree the entrypoint runs, in blueprint order (empty without one)."""

    root = role_root(blueprint)
    if root is None or blueprint.resolved_entrypoint() != root:
        return []
    known = {spec.name: spec for spec in blueprint.agents}
    seen: set[str] = set()
    pending = [root]
    while pending:
        name = pending.pop()
        spec = known.get(name)
        if name in seen or spec is None:
            continue
        seen.add(name)
        pending.extend(spec.sub_agents)
        pending.extend(judge.name for judge in [bound_judge(blueprint, name)] if judge)
        pending.extend(reviewer.name for reviewer in bound_reviewers(blueprint, name))
    return [spec.name for spec in blueprint.agents if spec.name in seen]


def judge_rounds(blueprint: Blueprint, worker: str) -> int:
    """Times the judge of `worker` may send its output back: the parent's `max_iterations`, else the default."""

    parent = parent_of(blueprint).get(worker)
    rounds = blueprint.agent(parent).max_iterations if parent is not None else None
    return rounds or DEFAULT_MAX_ITERATIONS


def tree_limits(blueprint: Blueprint, root: str) -> tuple[int, int]:
    """The depth and parallelism limits of the tree: `(max_depth, max_parallel)`."""

    delegation = blueprint.agent(root).delegation
    depth = delegation.max_depth if delegation and delegation.max_depth else DEFAULT_MAX_DEPTH
    parallel = (
        delegation.max_parallel if delegation and delegation.max_parallel else DEFAULT_MAX_PARALLEL
    )
    return depth, parallel


def parent_of(blueprint: Blueprint) -> dict[str, str]:
    """For every sub-agent the name of the agent that lists it (the first, when several do)."""

    parents: dict[str, str] = {}
    for spec in blueprint.agents:
        for child in spec.sub_agents:
            parents.setdefault(child, spec.name)
    return parents
