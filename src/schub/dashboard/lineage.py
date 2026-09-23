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
V_ROW_H = 68  # vertical layout: one step per row
SHORT = {
    "kb_count": "kallisto|bus",
    "cellranger_count": "Cell Ranger",
    "merge_datasets": "Merge",
    "qc_filter": "QC",
    "normalize_embed": "Normalize",
    "integrate_scvi": "scVI",
    "integrate_scanvi": "scANVI",
    "annotate_celltypist": "CellTypist",
    "pseudobulk_de": "Pseudobulk DE",
    "memento_de": "memento",
    "export_cellxgene": "cellxgene file",
}
KEY_PARAMS = {
    "kb_count": ("technology",),
    "merge_datasets": ("others", "join"),
    "integrate_scvi": ("n_latent", "batch_key"),
    "integrate_scanvi": ("labels_key", "batch_key"),
    "normalize_embed": ("n_top_genes", "leiden_resolution"),
    "qc_filter": ("min_genes", "max_pct_mt"),
    "annotate_celltypist": ("model",),
    "pseudobulk_de": ("group_key",),
    "memento_de": ("group_key", "capture_rate"),
    "export_cellxgene": ("max_cells",),
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
        # The project (or subproject, a/b) is the group; the branch is the entry.
        group = label.rsplit("/", 1)[0] if "/" in label else "Other runs"
        # Labels differing only in case or punctuation get -2, -3 (stable: labels are sorted).
        base = unique = view_id(label)
        suffix = 2
        while unique in taken:
            unique, suffix = f"{base}-{suffix}", suffix + 1
        taken.add(unique)
        views.append(
            PipelineView(
                view_id=unique,
                label=label.rsplit("/", 1)[1] if "/" in label else label,
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
        for root in (node.parent, *node.inputs):
            if root.startswith("ds:") and root not in roots:
                roots.append(root)
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


def _origin(depth: int, row: int, vertical: bool) -> tuple[int, int]:
    """Top-left corner of a node box: steps flow left-to-right, or top-to-bottom."""
    if vertical:
        return row * COL_W + PAD, depth * V_ROW_H + PAD
    return depth * COL_W + PAD, row * ROW_H + PAD


def _edge(start: tuple[int, int], end: tuple[int, int], vertical: bool, css: str, to: str, bend: int = 14) -> str:
    (sx, sy), (ex, ey) = start, end
    if vertical:
        x1, y1, x2, y2 = sx + NODE_W // 2, sy + NODE_H, ex + NODE_W // 2, ey
        curve = f"C{x1},{y1 + bend} {x2},{y2 - bend}"
    else:
        x1, y1, x2, y2 = sx + NODE_W, sy + NODE_H // 2, ex, ey + NODE_H // 2
        curve = f"C{x1 + bend},{y1} {x2 - bend},{y2}"
    return f'<path class="{css}" data-to="{escape(to, quote=True)}" d="M{x1},{y1} {curve} {x2},{y2}"/>'


def render_graph(nodes: tuple[NodeView, ...], keys: tuple[str, ...], vertical: bool = False) -> str:
    """The lineage of `keys`. Vertical suits one branch next to the step panel;
    horizontal suits the forest of every branch."""
    wanted = set(keys)
    subset = tuple(n for n in nodes if n.key in wanted)
    if not subset:
        return '<p class="muted">Nothing to show yet.</p>'
    by_key = {n.key: n for n in subset}
    positions, children = _layout(subset)
    origin = {key: _origin(depth, row, vertical) for key, (depth, row) in positions.items()}
    width = max(x for x, _ in origin.values()) + NODE_W + PAD
    height = max(y for _, y in origin.values()) + NODE_H + PAD
    edges, boxes = [], []
    for parent, kids in children.items():
        for kid in kids:
            dashed = " dashed" if by_key[kid].state == "PLANNED" else ""
            edges.append(_edge(origin[parent], origin[kid], vertical, f"edge{dashed}", kid))
    for node in subset:
        # Extra datasets a merge step reads: one more edge per dataset root.
        for root in node.inputs:
            if root in origin and node.key in origin:
                edges.append(_edge(origin[root], origin[node.key], vertical, "edge merge", node.key, bend=40))
    for key, (x, y) in origin.items():
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
