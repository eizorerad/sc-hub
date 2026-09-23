"""Lineage of pipeline steps as precomputed SVGs (the browser only toggles them).

Step keys chain from the dataset, so the graph is a forest rooted at datasets:
branches that share a prefix share nodes and diverge where params differ.
One SVG is rendered per selectable view: everything, and each branch or run.
"""

from __future__ import annotations

import re
from html import escape
from typing import Iterable

from ..state import Frozen
from .collect import NodeView

COL_W, NODE_W, NODE_H, ROW_H, PAD = 164, 144, 44, 56, 10
SHORT = {
    "qc_filter": "QC",
    "normalize_embed": "Normalize",
    "integrate_scvi": "scVI",
    "annotate_celltypist": "CellTypist",
    "pseudobulk_de": "Pseudobulk DE",
}
KEY_PARAMS = {
    "integrate_scvi": ("n_latent", "batch_key"),
    "normalize_embed": ("n_top_genes", "leiden_resolution"),
    "qc_filter": ("min_genes", "max_pct_mt"),
    "annotate_celltypist": ("model",),
    "pseudobulk_de": ("group_key",),
}


class PipelineView(Frozen):
    view_id: str
    label: str
    group: str
    keys: tuple[str, ...]


def view_id(label: str) -> str:
    return "v-" + re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def pipeline_views(nodes: tuple[NodeView, ...]) -> list[PipelineView]:
    views = [PipelineView(view_id="v-all", label="All pipelines", group="Overview", keys=tuple(n.key for n in nodes))]
    taken = {"v-all"}
    for label in sorted({label for n in nodes for label in n.labels}):
        group = label.split("/")[0] if "/" in label else "Other runs"
        # Labels differing only in case or punctuation get -2, -3 (stable: labels are sorted).
        base = unique = view_id(label)
        suffix = 2
        while unique in taken:
            unique, suffix = f"{base}-{suffix}", suffix + 1
        taken.add(unique)
        views.append(
            PipelineView(
                view_id=unique,
                label=label.split("/", 1)[1] if "/" in label else label,
                group=group,
                keys=tuple(n.key for n in nodes if label in n.labels),
            )
        )
    return views


def _layout(nodes: Iterable[NodeView]) -> tuple[dict[str, tuple[int, int]], dict[str, list[str]]]:
    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for node in nodes:
        children.setdefault(node.parent, []).append(node.key)
        if node.parent.startswith("ds:") and node.parent not in roots:
            roots.append(node.parent)
    positions: dict[str, tuple[int, int]] = {}
    row = 0

    def place(key: str, depth: int) -> int:
        nonlocal row
        kids = children.get(key, [])
        if not kids:
            positions[key] = (depth, row)
            row += 1
            return positions[key][1]
        first = [place(kid, depth + 1) for kid in kids][0]
        positions[key] = (depth, first)
        return first

    for root in sorted(roots):
        place(root, 0)
    return positions, children


def _divergence(node: NodeView, siblings: list[NodeView]) -> str:
    """At a branch point, name the params that differ from the sibling nodes."""
    keys = sorted({k for s in siblings for k in s.params} | set(node.params))
    differing = [k for k in keys if len({repr(s.params.get(k)) for s in siblings}) > 1]
    return ", ".join(f"{k}={node.params.get(k)}" for k in differing[:2])


def _subtitle(node: NodeView, siblings: list[NodeView]) -> str:
    if len(siblings) > 1 and (diff := _divergence(node, siblings)):
        return diff
    if node.headline:
        return node.headline
    wanted = KEY_PARAMS.get(node.brick, ())
    shown = [f"{k}={node.params[k]}" for k in wanted if node.params.get(k) is not None]
    return ", ".join(shown) or node.state.lower()


def _ancestors(key: str, by_key: dict[str, NodeView]) -> list[str]:
    path = []
    while key in by_key:
        path.append(key)
        key = by_key[key].parent
    return path


def _box(x: int, y: int, css: str, title: str, subtitle: str, key: str, path: str) -> str:
    return (
        f'<g class="node {escape(css, quote=True)}" data-key="{escape(key, quote=True)}" '
        f'data-path="{escape(path, quote=True)}" tabindex="0" role="button">'
        f'<rect x="{x}" y="{y}" width="{NODE_W}" height="{NODE_H}" rx="8"/>'
        f'<text class="t1" x="{x + 12}" y="{y + 18}">{escape(title[:20])}</text>'
        f'<text class="t2" x="{x + 12}" y="{y + 34}">{escape(subtitle[:22])}</text></g>'
    )


def render_graph(nodes: tuple[NodeView, ...], keys: tuple[str, ...]) -> str:
    wanted = set(keys)
    subset = tuple(n for n in nodes if n.key in wanted)
    if not subset:
        return '<p class="muted">Nothing to show yet.</p>'
    by_key = {n.key: n for n in subset}
    positions, children = _layout(subset)
    width = (max(d for d, _ in positions.values()) + 1) * COL_W + PAD
    height = (max(r for _, r in positions.values()) + 1) * ROW_H + PAD
    edges, boxes = [], []
    for parent, kids in children.items():
        px, py = positions[parent]
        for kid in kids:
            kx, ky = positions[kid]
            x1, y1 = px * COL_W + NODE_W + PAD, py * ROW_H + PAD + NODE_H // 2
            x2, y2 = kx * COL_W + PAD, ky * ROW_H + PAD + NODE_H // 2
            dashed = " dashed" if by_key[kid].state == "PLANNED" else ""
            edges.append(
                f'<path class="edge{dashed}" data-to="{escape(kid, quote=True)}" '
                f'd="M{x1},{y1} C{x1 + 14},{y1} {x2 - 14},{y2} {x2},{y2}"/>'
            )
    for key, (depth, row) in positions.items():
        x, y = depth * COL_W + PAD, row * ROW_H + PAD
        if key.startswith("ds:"):
            boxes.append(_box(x, y, "ds", key[3:], "dataset", key, key))
            continue
        node = by_key[key]
        siblings = [by_key[k] for k in children.get(node.parent, []) if k in by_key]
        path = " ".join(_ancestors(key, by_key))
        boxes.append(_box(x, y, f"st-{node.state}", SHORT.get(node.brick, node.brick),
                          _subtitle(node, siblings), key, path))
    return (
        f'<div class="scroll"><svg class="lineage" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="pipeline lineage">'
        + "".join(edges) + "".join(boxes) + "</svg></div>"
    )


def render_lineage(nodes: tuple[NodeView, ...]) -> str:
    """The full forest in one graph."""
    if not nodes:
        return '<p class="muted">No runs or branches yet.</p>'
    return render_graph(nodes, tuple(n.key for n in nodes))
