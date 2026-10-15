"""The diagram of a role tree: roles on the boxes, delegation arrows, judge and review edges."""

from __future__ import annotations

from awa.blueprints._diagram import Diagram, DiagramEdge, DiagramNode, node_label
from awa.blueprints._roles import bound_judge, bound_reviewers, judge_rounds, role_members
from awa.blueprints.schema import Blueprint


def role_diagram(blueprint: Blueprint, root: str) -> Diagram:
    """The tree led by `root`: solid arrows delegate, dotted arrows lead to a judge (labeled with its rounds) or reviewer."""

    names = role_members(blueprint)
    ids = {name: f"a{index}" for index, name in enumerate(names)}
    items: list[DiagramNode | DiagramEdge] = []
    for name in names:
        spec = blueprint.agent(name)
        lines = (*node_label(blueprint, name), f"role: {spec.role or 'worker'}")
        items.append(DiagramNode(ids[name], lines, "lead" if name == root else "agent"))
    for name in names:
        spec = blueprint.agent(name)
        items += [DiagramEdge(ids[name], ids[child]) for child in spec.sub_agents]
        judge = bound_judge(blueprint, name)
        if judge is not None:
            rounds = judge_rounds(blueprint, name)
            items.append(
                DiagramEdge(ids[name], ids[judge.name], "dotted", f"judge, max {rounds} rounds")
            )
        items += [
            DiagramEdge(ids[name], ids[reviewer.name], "dotted", "reviews once")
            for reviewer in bound_reviewers(blueprint, name)
        ]
    return Diagram("TD", tuple(items))


def role_tree_lines(blueprint: Blueprint, root: str) -> list[str]:
    """The tree led by `root` as indented text lines, with each worker's judge and reviewers."""

    lines: list[str] = []

    def add(name: str, depth: int) -> None:
        spec = blueprint.agent(name)
        line = f"{'  ' * depth}{name} ({spec.role or 'worker'})"
        judge = bound_judge(blueprint, name)
        if judge is not None:
            line += f"  judged by {judge.name}, at most {judge_rounds(blueprint, name)} rounds"
        reviewers = bound_reviewers(blueprint, name)
        if reviewers:
            line += f"  reviewed once by {', '.join(r.name for r in reviewers)}"
        lines.append(line)
        for child in spec.sub_agents:
            add(child, depth + 1)

    add(root, 0)
    return lines
