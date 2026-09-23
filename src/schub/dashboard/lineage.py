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
    label: str  # the entry: a branch, a run, or a project
    group: str
    keys: tuple[str, ...]
    kind: str = "branch"  # "branch", "run" (outside any branch) or "project" (every branch of it)
    full: str = ""  # "<project>/<branch>", the run label, or the project path


STUB = "__stub__"  # a pseudo-step: another branch that shares steps with the one shown


def view_id(label: str, prefix: str = "v") -> str:
    return f"{prefix}-" + re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def _unique(base: str, taken: set[str]) -> str:
    """Labels differing only in case or punctuation get -2, -3 (stable: callers sort)."""
    unique, suffix = base, 2
    while unique in taken:
        unique, suffix = f"{base}-{suffix}", suffix + 1
    taken.add(unique)
    return unique


def pipeline_views(nodes: tuple[NodeView, ...], projects: Iterable[str] = ()) -> list[PipelineView]:
    """A graph per project (all its branches), per branch and per run outside a branch."""
    taken: set[str] = set()
    labels = sorted({label for n in nodes for label in n.labels})
    views = []
    for project in sorted(set(projects)):
        keys = tuple(n.key for n in nodes if any(label.rsplit("/", 1)[0] == project for label in n.labels))
        views.append(PipelineView(view_id=_unique(view_id(project, "p"), taken), label=project, group="Projects",
                                  keys=keys, kind="project", full=project))
    for label in labels:
        # The project (or subproject, a/b) is the group; the branch is the entry.
        group = label.rsplit("/", 1)[0] if "/" in label else "Other runs"
        views.append(PipelineView(
            view_id=_unique(view_id(label), taken), label=label.rsplit("/", 1)[-1], group=group,
            keys=tuple(n.key for n in nodes if label in n.labels), kind="branch" if "/" in label else "run", full=label,
        ))
    return views


def _parent_in(node: NodeView, keys: set[str]) -> str:
    """The node's parent in this graph; a step whose parent is not shown hangs off its dataset."""
    return node.parent if node.parent in keys or node.parent.startswith("ds:") else f"ds:{node.dataset}"


def _layout(nodes: Iterable[NodeView]) -> tuple[dict[str, tuple[int, int]], dict[str, list[str]]]:
    nodes = list(nodes)
    keys = {n.key for n in nodes}
    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for node in nodes:
        parent = _parent_in(node, keys)
        children.setdefault(parent, []).append(node.key)
        if parent.startswith("ds:") and parent not in roots:
            roots.append(parent)
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
    """At a branch point, name the params that differ from sibling steps of the same brick."""
    same = [s for s in siblings if s.brick == node.brick]
    if len(same) < 2:
        return ""
    keys = sorted({k for s in same for k in s.params} | set(node.params))
    differing = [k for k in keys if len({repr(s.params.get(k)) for s in same}) > 1]
    return ", ".join(f"{k}={node.params.get(k)}" for k in differing[:2])


def earlier_key(node: NodeView, label: str | None) -> str:
    """The earlier result shown for a planned step: the branch's own, or any (all branches)."""
    return node.earlier.get(label, "") if label else next(iter(node.earlier.values()), "")


def _subtitle(node: NodeView, siblings: list[NodeView], everything: dict[str, NodeView], label: str | None) -> str:
    if node.brick == "merge_datasets" and node.inputs:
        return "+ " + ", ".join(i[3:] for i in node.inputs)  # instead of an edge across the graph
    earlier = everything.get(earlier_key(node, label))
    if node.state == "PLANNED" and earlier is not None:
        return f"↻ {earlier.headline}" if earlier.headline else "↻ re-run needed"  # what it gave before
    if len(siblings) > 1 and (diff := _divergence(node, siblings)):
        return diff
    if node.headline:
        return node.headline
    wanted = KEY_PARAMS.get(node.brick, ())
    shown = [f"{k}={node.params[k]}" for k in wanted if node.params.get(k) is not None]
    return ", ".join(shown) or node.state.lower()


def _path(key: str, by_key: dict[str, NodeView]) -> str:
    """The step, its ancestors, its dataset and any dataset a merge on the way added."""
    path, inputs = [], []
    while key in by_key:
        path.append(key)
        inputs += by_key[key].inputs
        key = by_key[key].parent
    root = [key] if key.startswith("ds:") else []
    return " ".join(path + root + [i for i in dict.fromkeys(inputs) if i not in root])


def _fit(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _box(x: int, y: int, css: str, title: str, subtitle: str, key: str, path: str, flag: str = "",
         href: str = "") -> str:
    said = f"needs re-run; before: {subtitle[2:]}" if subtitle.startswith("↻ ") else subtitle
    hover = f"{title} · {said}" + (f" · {flag}" if flag else "")
    mark = f'<text class="flag" x="{x + NODE_W - 14}" y="{y + 16}">!</text>' if flag else ""
    link = f' data-href="{escape(href, quote=True)}"' if href else ""
    return (
        f'<g class="node {escape(css, quote=True)}" data-key="{escape(key, quote=True)}"{link} '
        f'data-path="{escape(path, quote=True)}" tabindex="0" role="button">'
        f"<title>{escape(hover)}</title>"
        f'<rect x="{x}" y="{y}" width="{NODE_W}" height="{NODE_H}" rx="8"/>'
        f'<text class="t1" x="{x + 12}" y="{y + 18}">{escape(_fit(title, 19 if flag else 20))}</text>'
        f'<text class="t2" x="{x + 12}" y="{y + 34}">{escape(_fit(subtitle, 22))}</text>{mark}</g>'
    )


def _css(node: NodeView, label: str | None) -> str:
    flags = (("old", not node.current), ("outdated", bool(earlier_key(node, label))), ("flagged", bool(node.issues)))
    extra = [c for c, on in flags if on]
    return " ".join([f"st-{node.state}", *extra])


def _flag(node: NodeView) -> str:
    if not node.issues:
        return ""
    worst = "error" if any(i.level == "error" for i in node.issues) else "warning"
    return f"{len(node.issues)} planner {worst}{'s' if len(node.issues) > 1 else ''}"


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


def render_graph(nodes: tuple[NodeView, ...], keys: Iterable[str], vertical: bool = False, label: str | None = None,
                 index: dict[str, NodeView] | None = None) -> str:
    """The lineage of `keys`. Vertical suits one branch next to the step panel;
    horizontal suits the forest of every branch. `label`: the branch this graph shows.
    `index` (every node by key) saves rebuilding it for each of many graphs."""
    everything = index if index is not None else {n.key: n for n in nodes}
    extra = {n.key: n for n in nodes if n.key not in everything}  # stubs made for this graph
    wanted = set(keys)
    subset = tuple(found for k in dict.fromkeys(keys) if (found := everything.get(k) or extra.get(k)) is not None)
    if not subset:
        return '<p class="muted">Nothing to show yet.</p>'
    by_key = {n.key: n for n in subset}
    shown = set(by_key)
    positions, children = _layout(subset)
    origin = {key: _origin(depth, row, vertical) for key, (depth, row) in positions.items()}
    width = max(x for x, _ in origin.values()) + NODE_W + PAD
    height = max(y for _, y in origin.values()) + NODE_H + PAD
    edges, boxes = [], []
    for parent, kids in children.items():
        for kid in kids:
            kind = " stub" if by_key[kid].brick == STUB else (" dashed" if by_key[kid].state == "PLANNED" else "")
            edges.append(_edge(origin[parent], origin[kid], vertical, f"edge{kind}", kid))
    for key, (x, y) in origin.items():
        if key.startswith("ds:"):
            boxes.append(_box(x, y, "ds", key[3:], "dataset", key, key))
            continue
        node = by_key[key]
        if node.brick == STUB:  # another branch: a link to its own graph
            boxes.append(_box(x, y, f"stub st-{node.state}", f"→ {node.params.get('name', '')}", node.headline,
                              key, _path(key, by_key), href=str(node.params.get("href", ""))))
            continue
        siblings = [by_key[k] for k in children.get(_parent_in(node, shown), []) if by_key[k].brick != STUB]
        boxes.append(_box(x, y, _css(node, label), SHORT.get(node.brick, node.brick),
                          _subtitle(node, siblings, everything, label), key, _path(key, by_key), _flag(node)))
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
