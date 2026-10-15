"""The flow diagram of a blueprint as inline SVG in ranks, styled by the page's CSS theme."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from html import escape

from awa.blueprints import Blueprint
from awa.blueprints._diagram import DiagramEdge, blueprint_diagram

_CHAR_WIDTH = 7.4
_LINE_HEIGHT = 18
_GAP = 44
_MARGIN = 16


@dataclass
class _Node:
    id: str
    lines: list[str]
    shape: str
    rank: int = 0
    x: float = 0.0
    y: float = 0.0
    width: float = field(init=False)
    height: float = field(init=False)

    def __post_init__(self) -> None:
        self.width = max(96.0, max(len(line) for line in self.lines) * _CHAR_WIDTH + 28)
        self.height = len(self.lines) * _LINE_HEIGHT + 18


def blueprint_svg(blueprint: Blueprint) -> str:
    """Render the entrypoint of `blueprint` as an accessible inline SVG element."""

    diagram = blueprint_diagram(blueprint)
    nodes = {
        node.id: _Node(
            node.id, list(node.lines), "lead" if node.shape == "terminal" else node.shape
        )
        for node in diagram.nodes
    }
    edges = [e for e in diagram.edges if e.source in nodes and e.target in nodes]
    across = diagram.direction == "LR"
    _rank(nodes, edges)
    width, height = _place(nodes, across)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" class="flow" role="img" '
        f'viewBox="0 0 {width:.0f} {height:.0f}" width="{width:.0f}" height="{height:.0f}" '
        f'aria-label="{escape(f"Flow of {blueprint.name}")}">',
        f"<title>{escape(f'Flow of {blueprint.name}')}</title>",
        '<defs><marker id="awa-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" '
        'markerHeight="8" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
        'class="flow-head"/></marker></defs>',
    ]
    drawn: Counter[frozenset[str]] = Counter()
    for edge in edges:
        pair = frozenset((edge.source, edge.target))
        drawn[pair] += 1
        parts.append(_edge_svg(edge, nodes, curved=drawn[pair] > 1))
    parts += [_node_svg(node) for node in nodes.values()]
    parts.append("</svg>")
    return "".join(parts)


def _rank(nodes: dict[str, _Node], edges: list[DiagramEdge]) -> None:
    """Longest-path ranks over the solid arrows (back edges of cycles ignored)."""

    forward: dict[str, list[str]] = {name: [] for name in nodes}
    for edge in edges:
        if edge.style in ("solid", "thick"):
            forward[edge.source].append(edge.target)
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        seen.add(name)
        for target in forward[name]:
            if target not in seen:
                visit(target)
        order.append(name)

    for name in nodes:
        if name not in seen:
            visit(name)
    position = {name: index for index, name in enumerate(reversed(order))}
    for name in reversed(order):
        for target in forward[name]:
            if position[target] > position[name]:
                nodes[target].rank = max(nodes[target].rank, nodes[name].rank + 1)


def _place(nodes: dict[str, _Node], across: bool) -> tuple[float, float]:
    ranks: dict[int, list[_Node]] = {}
    for node in nodes.values():
        ranks.setdefault(node.rank, []).append(node)
    rows = [ranks[index] for index in sorted(ranks)]

    def along(node: _Node) -> float:
        return node.height if across else node.width

    def depth(node: _Node) -> float:
        return node.width if across else node.height

    spans = [sum(along(node) for node in row) + _GAP * (len(row) - 1) for row in rows]
    widest = max(spans, default=0.0)
    offset = float(_MARGIN)
    for row, span in zip(rows, spans, strict=True):
        cursor = _MARGIN + (widest - span) / 2
        thickness = max(depth(node) for node in row)
        for node in row:
            main, cross = offset + thickness / 2, cursor + along(node) / 2
            node.x, node.y = (main, cross) if across else (cross, main)
            cursor += along(node) + _GAP
        offset += thickness + _GAP * 1.4
    total = offset - _GAP * 1.4 + _MARGIN
    breadth = widest + 2 * _MARGIN
    return (total, breadth) if across else (breadth, total)


def _border(node: _Node, dx: float, dy: float) -> tuple[float, float]:
    """Where the ray from the node's center in direction (dx, dy) leaves its box."""

    scale = min(
        node.width / 2 / abs(dx) if dx else float("inf"),
        node.height / 2 / abs(dy) if dy else float("inf"),
    )
    return node.x + dx * scale, node.y + dy * scale


def _edge_svg(edge: DiagramEdge, nodes: dict[str, _Node], *, curved: bool) -> str:
    """A straight arrow; a curve for dotted edges and for a second edge between the same pair."""

    source, target = nodes[edge.source], nodes[edge.target]
    dx, dy = target.x - source.x, target.y - source.y
    if not dx and not dy:
        return ""
    x1, y1 = _border(source, dx, dy)
    x2, y2 = _border(target, -dx, -dy)
    if curved or edge.style in ("dotted", "both"):
        length = (dx * dx + dy * dy) ** 0.5
        bend = min(60.0, length / 3)
        cx, cy = (x1 + x2) / 2 - dy / length * bend, (y1 + y2) / 2 + dx / length * bend
        path = f"M{x1:.1f},{y1:.1f} Q{cx:.1f},{cy:.1f} {x2:.1f},{y2:.1f}"
        lx, ly = (x1 + 2 * cx + x2) / 4, (y1 + 2 * cy + y2) / 4
    else:
        path = f"M{x1:.1f},{y1:.1f} L{x2:.1f},{y2:.1f}"
        lx, ly = (x1 + x2) / 2, (y1 + y2) / 2
    start = ' marker-start="url(#awa-arrow)"' if edge.style == "both" else ""
    svg = (
        f'<path d="{path}" class="flow-edge flow-{edge.style}" fill="none"'
        f'{start} marker-end="url(#awa-arrow)"/>'
    )
    if edge.label:
        svg += (
            f'<text x="{lx:.1f}" y="{ly:.1f}" class="flow-label" text-anchor="middle" '
            f'dominant-baseline="middle">{escape(edge.label)}</text>'
        )
    return svg


def _node_svg(node: _Node) -> str:
    left, top = node.x - node.width / 2, node.y - node.height / 2
    radius = node.height / 2 if node.shape == "lead" else 8
    parts = [
        f'<g class="flow-node flow-{node.shape}"><rect x="{left:.1f}" y="{top:.1f}" '
        f'width="{node.width:.1f}" height="{node.height:.1f}" rx="{radius:.1f}"/>'
    ]
    if node.shape == "group":
        for inset in (8, node.width - 8):
            parts.append(
                f'<line x1="{left + inset:.1f}" y1="{top:.1f}" x2="{left + inset:.1f}" '
                f'y2="{top + node.height:.1f}"/>'
            )
    first = node.y - (len(node.lines) - 1) * _LINE_HEIGHT / 2
    for index, line in enumerate(node.lines):
        css = "flow-name" if index == 0 else "flow-note"
        parts.append(
            f'<text x="{node.x:.1f}" y="{first + index * _LINE_HEIGHT:.1f}" class="{css}" '
            f'text-anchor="middle" dominant-baseline="middle">{escape(line)}</text>'
        )
    parts.append("</g>")
    return "".join(parts)
