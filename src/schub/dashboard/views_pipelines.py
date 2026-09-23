"""Pipelines: pick a branch (or everything), click a step to inspect it.

Graphs show each branch as it is now. Results no branch uses any more (after a
revision, or after an sc-hub update gave the steps new keys) are history: hidden
until the student asks, and a step that must run again says what it gave before.
"""

from __future__ import annotations

from itertools import groupby
from typing import Any

from ..brick_code import code_changed
from ..bricks import REGISTRY
from .collect import BRANCH_LABELS, BranchInfo, NodeView, RunView, Snapshot, run_label
from .html import ask_block, esc, kv, pill, warnings
from .lineage import SHORT, PipelineView, pipeline_views, render_graph
from .notebooks import code_section, notebook_button
from .views_runs import ImageUrl, cell_map, figures, log_block, result_tables, step_facts

LEGEND = (("COMPLETED", "done"), ("RUNNING", "running"), ("PENDING", "queued"), ("FAILED", "failed"),
          ("PLANNED", "planned"), ("OUTDATED", "needs re-run"))


# For "All pipelines": the most pressing state among the branches (and other runs).
URGENCY = ("RUNNING", "PENDING", "FAILED", "BLOCKED", "OUTDATED", "PLANNED", "COMPLETED")


def _view_state(nodes: dict[str, NodeView], keys: tuple[str, ...]) -> str:
    """A run outside any branch: its steps' states."""
    states = {nodes[k].state for k in keys if k in nodes}
    if states & {"FAILED", "STOPPED", "MISSING"}:
        return "FAILED"
    if states == {"COMPLETED"}:
        return "COMPLETED"
    if "RUNNING" in states:
        return "RUNNING"
    return "PENDING" if states - {"COMPLETED", "PLANNED"} else "PLANNED"


def _overall(states: list[str]) -> str:
    return next((s for s in URGENCY if s in states), "PLANNED")


def _branch(snap: Snapshot, view: PipelineView) -> BranchInfo | None:
    return snap.branches.get(f"{view.group}/{view.label}") if view.view_id != "v-all" else None


def current_keys(snap: Snapshot, view: PipelineView) -> tuple[str, ...]:
    """The steps of a view as things are now: a branch's own plan, or what is not history."""
    info = _branch(snap, view)
    if info is not None and info.keys:
        planned = set(info.keys)
        return tuple(k for k in view.keys if k in planned)
    nodes = {n.key: n for n in snap.nodes}
    return tuple(k for k in view.keys if nodes[k].current) if view.view_id == "v-all" else view.keys


def _selector(snap: Snapshot, views: list[PipelineView]) -> str:
    nodes = {n.key: n for n in snap.nodes}
    states = {v.view_id: _state(snap, v, nodes) for v in views if v.view_id != "v-all"}
    parts = []
    for group, grouped in groupby(views, key=lambda v: v.group):
        buttons = []
        for view in grouped:
            now = current_keys(snap, view)
            state = states.get(view.view_id) or _overall(list(states.values()))
            title = BRANCH_LABELS.get(state, state.lower())
            buttons.append(
                f'<button class="pipe" data-pipe="{esc(view.view_id)}" data-text="{esc((group + " " + view.label).lower())}">'
                f'<span class="dot {esc(state)}" title="{esc(title)}"></span>'
                f'{esc(view.label)}<span class="muted small">{len(now)}</span></button>'
            )
        parts.append(f'<div class="pipe-group"><h4>{esc(group)}</h4>{"".join(buttons)}</div>')
    search = '<input class="tree-filter" type="search" placeholder="Filter branches" aria-label="Filter branches">' \
        if len(views) > 12 else ""
    return search + "".join(parts)


def _state(snap: Snapshot, view: PipelineView, nodes: dict[str, NodeView]) -> str:
    info = _branch(snap, view)
    return info.state if info is not None else _view_state(nodes, current_keys(snap, view))


def _defaults(brick: str) -> dict[str, Any]:
    spec = REGISTRY.get(brick)
    if spec is None:
        return {}
    fields = spec.params_model.model_fields
    return {k: f.default for k, f in fields.items() if not f.is_required()}


def _why(node: NodeView, earlier: NodeView, snap: Snapshot) -> str:
    """Why a step that ran before has a new key now. Parameters the student did not set
    may differ because sc-hub changed a default or added a parameter."""
    changed = {k for k in node.params.keys() | earlier.params.keys() if node.params.get(k) != earlier.params.get(k)}
    defaults = _defaults(node.brick)
    if any(k not in defaults or node.params.get(k) != defaults[k] for k in changed):
        return "its parameters changed"
    if changed:
        return "sc-hub changed the defaults of this step"
    step = snap.steps_by_key.get(earlier.key)
    if step is not None and code_changed(node.brick, step.code_id):
        return "sc-hub updated the code of this step"
    return "sc-hub or a step before it changed"


def _notes(node: NodeView, snap: Snapshot, nodes: dict[str, NodeView]) -> str:
    """What the planner says, and how this step relates to each branch as it is now."""
    notes = [f'<p class="note {"bad" if i.level == "error" else "warn"}">{esc(i.message)}</p>'
             for i in sorted(node.issues, key=lambda i: i.level != "error")]
    for label, key in sorted(node.earlier.items()):
        earlier = nodes.get(key)
        if earlier is None:
            continue
        where = f"in {label} " if len(node.labels) > 1 else ""
        notes.append(
            f"<p class=\"note\">Not run {esc(where)}as the branch is now: {esc(_why(node, earlier, snap))}. "
            f"Before, it gave <b>{esc(earlier.headline or earlier.state.lower())}</b>. "
            f'<button type="button" class="quiet" data-select-node="{esc(earlier.key)}">Show that result</button></p>')
    if not node.current:
        notes.append('<p class="note">An older version: no branch uses this result as it is now '
                     "(the branch was revised, or sc-hub was updated since).</p>")
    return "".join(notes)


def _node_template(node: NodeView, snap: Snapshot, run_of: dict[str, str], image_url: ImageUrl,
                   nodes: dict[str, NodeView]) -> str:
    step = snap.steps_by_key.get(node.key)
    summary = (step.summary if step is not None else None) or {}
    branches = "".join(f"<li>{esc(label)}</li>" for label in node.labels)
    state = pill("OUTDATED", "needs re-run") if node.earlier else pill(node.state)
    body = [f'<div class="panel-head"><h3>{esc(SHORT.get(node.brick, node.brick))}</h3>{state}</div>']
    if node.headline:
        body.append(f'<p class="headline">{esc(node.headline)}</p>')
    body.append(_notes(node, snap, nodes) + warnings(summary))
    if step is not None:
        body.append(step_facts(step))
    body.append(f"<h4>Used by</h4><ul class=plain>{branches}</ul>")
    body.append("<h4>Parameters</h4>" + (kv(node.params) or '<p class="muted">defaults</p>'))
    body.append(code_section(node.brick, step.code_id if step is not None else ""))
    if step is not None:
        body.append(figures(step, image_url))
        body.append(cell_map(step, image_url, folded=False))
        if summary:
            body.append("<h4>Result</h4>" + kv(summary) + result_tables(step, summary))
        body.append(log_block(step))
        if step.message:
            body.append(f'<p class="note bad">{esc(step.message)}</p>')
    for ref in node.refs[:4]:  # one per branch that uses this step
        label, _, index = ref.rpartition("#")
        info = snap.branches.get(label)
        note = ("This result comes from an earlier version of the branch; the request refers to the "
                "branch as it is now.") if info and info.keys and node.key not in info.keys else ""
        body.append(ask_block(ref, node.brick, label.rsplit("/", 1)[-1], note))
    if node.key in run_of:
        body.append(f'<a class="button" href="#runs/{esc(run_of[node.key])}">Open the run →</a>')
    if node.state == "PLANNED":
        body.append('<p class="muted">Saved in a branch, not run yet. Ask your assistant to submit it.</p>')
    body.append(f'<p class="muted small">Step key {esc(node.key)}</p>')
    return f'<template data-node="{esc(node.key)}">{"".join(body)}</template>'


def _actions(view: PipelineView, snap: Snapshot, latest: dict[str, RunView]) -> str:
    """For one branch: its state as it is now, its latest run and that run as a notebook."""
    if view.view_id == "v-all":
        return ""
    info = _branch(snap, view)
    state = pill(info.state, BRANCH_LABELS.get(info.state)) if info is not None else ""
    run = latest.get(f"{view.group}/{view.label}") or latest.get(view.label)
    if run is None:
        return f'<span class="graph-actions">{state}</span>'
    return (f'<span class="graph-actions">{state}<a href="#runs/{esc(run.run_id)}">latest run</a>'
            f"{notebook_button(run, snap.notebooks, 'Notebook')}</span>")


def _graph(view: PipelineView, snap: Snapshot, latest: dict[str, RunView]) -> str:
    """The view as it is now, plus (hidden) the same view with older versions."""
    vertical = view.view_id != "v-all"
    label = f"{view.group}/{view.label}" if _branch(snap, view) is not None else None
    now = current_keys(snap, view)
    older = len(view.keys) - len(now)
    toggle = (f'<button type="button" class="quiet history-toggle" data-history-toggle '
              f'data-count="{older}">Show {older} older step{"s" if older != 1 else ""}</button>') if older else ""
    history = (f'<div data-history="1" hidden>{render_graph(snap.nodes, view.keys, vertical, label)}</div>'
               if older else "")
    return (
        f'<div class="graph" data-pipe-view="{esc(view.view_id)}" hidden><div class="graph-head">'
        f"<h3>{esc(view.group)} · {esc(view.label)}</h3>{_actions(view, snap, latest)}{toggle}</div>"
        f'<div data-history="0">{render_graph(snap.nodes, now, vertical, label)}</div>{history}</div>'
    )


def render_pipelines(snap: Snapshot, image_url: ImageUrl) -> str:
    if not snap.nodes:
        return '<p class="empty">No pipelines yet. Ask your assistant to save a branch in a project.</p>'
    run_of: dict[str, str] = {}
    for run in reversed(snap.runs):
        run_of.update({s.key: run.run_id for s in run.steps})
    latest: dict[str, RunView] = {}
    for run in snap.runs:  # newest first
        latest.setdefault(run_label(run), run)
    views = pipeline_views(snap.nodes)
    graphs = "".join(_graph(v, snap, latest) for v in views)
    legend = "".join(pill(s, label) for s, label in LEGEND)
    nodes = {n.key: n for n in snap.nodes}
    templates = "".join(_node_template(n, snap, run_of, image_url, nodes) for n in snap.nodes)
    return (
        f'<div class="pipes no-node"><aside class="pipe-list">{_selector(snap, views)}</aside>'
        f'<div class="pipe-main"><div class="legend">{legend}<span class="muted small">Click a step for params, job, log and outputs</span></div>{graphs}</div>'
        f'<aside class="panel" id="node-panel"><p class="muted">Select a step in the graph.</p></aside></div>'
        f"{templates}"
    )
