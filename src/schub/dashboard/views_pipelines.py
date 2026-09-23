"""Pipelines: one graph at a time, loaded on demand.

A branch's graph shows its own steps as they are now, with the branches around it
as link boxes where they part from it; a project's map shows all its branches;
runs outside any branch have their own graph. Each graph (with its step panels) is
a file br/<view>.js the page loads when it is opened, so the page stays small with
hundreds of branches. Results no branch uses any more are history: hidden until
the student asks, and a step that must run again says what it gave before.
"""

from __future__ import annotations

import json

from ..state import Frozen
from .collect import BRANCH_LABELS, BranchInfo, NodeView, RunView, Snapshot, run_label
from .html import esc, pill
from .lineage import PipelineView, pipeline_views, render_graph
from .notebooks import notebook_button
from .relatives import relative_stubs
from .step_panel import node_template
from .views_runs import ImageUrl

LEGEND = (("COMPLETED", "done"), ("RUNNING", "running"), ("PENDING", "queued"), ("FAILED", "failed"),
          ("PLANNED", "planned"), ("OUTDATED", "needs re-run"))
# The most pressing state first, for a project made of many branches.
URGENCY = ("RUNNING", "PENDING", "WAITING", "FAILED", "BLOCKED", "OUTDATED", "PLANNED", "COMPLETED")


class PipelinePage(Frozen):
    shell: str  # the Pipelines view in index.html
    files: dict[str, str]  # br/<view>.js -> content
    views: dict[str, str]  # "<project>/<branch>" or a run label -> view id
    project_views: dict[str, str]  # project path -> view id of its map


def overall(states: list[str]) -> str:
    return next((s for s in URGENCY if s in states), "PLANNED")


def _run_state(nodes: dict[str, NodeView], keys: tuple[str, ...]) -> str:
    states = {nodes[k].state for k in keys if k in nodes}
    if states & {"FAILED", "STOPPED", "MISSING"}:
        return "FAILED"
    if states == {"COMPLETED"}:
        return "COMPLETED"
    return "RUNNING" if "RUNNING" in states else ("PENDING" if states - {"COMPLETED", "PLANNED"} else "PLANNED")


def _project_branches(snap: Snapshot, project: str) -> list[BranchInfo]:
    return [b for b in snap.branches.values() if b.project == project]


def current_keys(snap: Snapshot, view: PipelineView) -> tuple[str, ...]:
    """The steps of a view as things are now: branches' own plans; history aside."""
    if view.kind == "branch" and (info := snap.branches.get(view.full)) is not None and info.keys:
        planned = set(info.keys)
        return tuple(k for k in view.keys if k in planned)
    if view.kind == "project":
        planned = {k for b in _project_branches(snap, view.full) for k in b.keys}
        return tuple(k for k in view.keys if k in planned) if planned else view.keys
    return view.keys


def _state(snap: Snapshot, view: PipelineView, nodes: dict[str, NodeView]) -> str:
    if view.kind == "project":
        return overall([b.state for b in _project_branches(snap, view.full)])
    info = snap.branches.get(view.full)
    return info.state if info is not None else _run_state(nodes, current_keys(snap, view))


def _selector(snap: Snapshot, views: list[PipelineView], nodes: dict[str, NodeView]) -> str:
    groups = [("Projects", [v for v in views if v.kind == "project"]), ("Other runs", [v for v in views if v.kind == "run"])]
    parts = []
    for title, members in groups:
        if not members:
            continue
        buttons = "".join(
            f'<button class="pipe" data-pipe="{esc(v.view_id)}" data-text="{esc(v.full.lower())}">'
            f'<span class="dot {esc(s)}" title="{esc(BRANCH_LABELS.get(s, s.lower()))}"></span>{esc(v.label)}'
            f'<span class="muted small">{len(_project_branches(snap, v.full)) if v.kind == "project" else len(v.keys)}</span></button>'
            for v in members for s in (_state(snap, v, nodes),)
        )
        parts.append(f'<div class="pipe-group"><h4>{esc(title)}</h4>{buttons}</div>')
    hint = '<p class="muted small pipe-hint">Open a branch from <a href="#experiments">Experiments</a> or a project map.</p>'
    search = ('<input class="tree-filter" type="search" placeholder="Filter" aria-label="Filter graphs">'
              if len(views) > 12 else "")
    return search + "".join(parts) + hint


def _requests(label: str) -> str:
    """Bookkeeping the page cannot do itself (it is read-only): copy a request for the assistant."""
    branch = label.rsplit("/", 1)[-1]
    return (f'<div class="ask inline" data-ref="{esc(label)}" data-brick="" data-branch="{esc(branch)}">'
            '<div class="ask-buttons"><button type="button" data-ask="sweep">Sweep a parameter</button>'
            '<button type="button" data-ask="pin" class="quiet">Pin</button>'
            '<button type="button" data-ask="archive" class="quiet">Archive</button></div>'
            '<p class="muted small copied" hidden>Copied: paste it into Codex or Claude.</p>'
            '<textarea class="manual" readonly hidden rows="3" aria-label="Request to copy"></textarea></div>')


def _head(view: PipelineView, snap: Snapshot, nodes: dict[str, NodeView], latest: dict[str, RunView],
          project_views: dict[str, str], toggle: str) -> str:
    state = _state(snap, view, nodes)
    title = f"{view.group} · {view.label}" if view.kind == "branch" else view.label
    parts = [f"<h3>{esc(title)}</h3>", pill(state, BRANCH_LABELS.get(state))]
    info = snap.branches.get(view.full)
    if info is not None:
        parts.append(f'<span class="tag">r{info.revision}</span>')
    if (run := latest.get(view.full)) is not None:
        parts.append(f'<a href="#runs/{esc(run.run_id)}">latest run</a>{notebook_button(run, snap.notebooks, "Notebook")}')
    project = view.full if view.kind == "project" else view.group
    if view.kind != "run":
        parts.append(f'<a href="#experiments/project={esc(project)}">in Experiments</a>')
    if view.kind == "branch" and project in project_views:
        parts.append(f'<a href="#pipelines/{esc(project_views[project])}">project map</a>')
    head = f'<div class="graph-head">{"".join(parts)}{toggle}</div>'
    return head + (_requests(view.full) if view.kind == "branch" else "")


def _graph(view: PipelineView, snap: Snapshot, nodes: dict[str, NodeView], all_views: dict[str, str],
           latest: dict[str, RunView], project_views: dict[str, str], peers: dict[str, list[BranchInfo]]) -> str:
    vertical = view.kind != "project"
    label = view.full if view.kind == "branch" else None
    now = current_keys(snap, view)
    info = snap.branches.get(view.full) if view.kind == "branch" else None
    stubs = relative_stubs(info, peers.get(info.project, []), nodes, all_views) if info is not None else []
    older = len(view.keys) - len(now)
    toggle = (f'<button type="button" class="quiet history-toggle" data-history-toggle '
              f'data-count="{older}">Show {older} older step{"s" if older != 1 else ""}</button>') if older else ""
    history = (f'<div data-history="1" hidden>{render_graph((), view.keys, vertical, label, nodes)}</div>'
               if older else "")
    return (f'<div class="graph" data-pipe-view="{esc(view.view_id)}">'
            + _head(view, snap, nodes, latest, project_views, toggle)
            + f'<div data-history="0">{render_graph(tuple(stubs), (*now, *(s.key for s in stubs)), vertical, label, nodes)}</div>'
            + f"{history}</div>")


def view_file(view_id: str, graph: str, templates: str) -> str:
    """br/<view>.js: a script (file:// pages cannot fetch) that registers the graph."""
    payload = json.dumps({"graph": graph, "templates": templates}).replace("</", "<\\/")
    return f"(window.SCHUB_BR = window.SCHUB_BR || {{}})[{json.dumps(view_id)}] = {payload};\n"


def render_pipelines(snap: Snapshot, image_url: ImageUrl) -> PipelinePage:
    nodes = {n.key: n for n in snap.nodes}
    projects = {b.project for b in snap.branches.values()}
    views = pipeline_views(snap.nodes, projects)
    all_views = {v.full: v.view_id for v in views if v.kind != "project"}
    project_views = {v.full: v.view_id for v in views if v.kind == "project"}
    run_of: dict[str, str] = {}
    for run in reversed(snap.runs):
        run_of.update({s.key: run.run_id for s in run.steps})
    latest: dict[str, RunView] = {}
    for run in snap.runs:  # newest first
        latest.setdefault(run_label(run), run)
    rendered: dict[str, str] = {}
    peers: dict[str, list[BranchInfo]] = {}
    for info in snap.branches.values():
        peers.setdefault(info.project, []).append(info)

    def template(key: str) -> str:  # once per step per build, whatever number of graphs show it
        if key not in rendered:
            rendered[key] = node_template(nodes[key], snap, run_of, image_url, nodes)
        return rendered[key]

    files = {
        f"br/{v.view_id}.js": view_file(v.view_id, _graph(v, snap, nodes, all_views, latest, project_views, peers),
                                        "".join(template(k) for k in v.keys if k in nodes))
        for v in views
    }
    legend = "".join(pill(s, label) for s, label in LEGEND)
    empty = '<p class="empty">No pipelines yet. Ask your assistant to save a branch in a project.</p>'
    shell = (
        f'<div class="pipes no-node"><aside class="pipe-list">{_selector(snap, views, nodes)}</aside>'
        f'<div class="pipe-main"><div class="legend">{legend}<span class="muted small">Click a step for params, job, '
        f'log and outputs; a → box opens that branch</span></div><div id="pipe-slot">{"" if views else empty}</div></div>'
        '<aside class="panel" id="node-panel"><p class="muted">Select a step in the graph.</p></aside></div>'
        '<div id="node-templates" hidden></div>'
    )
    return PipelinePage(shell=shell, files=files, views=all_views, project_views=project_views)
