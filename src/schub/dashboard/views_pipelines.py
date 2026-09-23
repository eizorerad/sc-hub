"""Pipelines: pick a branch (or everything), click a step to inspect it."""

from __future__ import annotations

from itertools import groupby

from .collect import NodeView, Snapshot
from .html import esc, kv, pill
from .lineage import SHORT, pipeline_views, render_graph
from .views_runs import ImageUrl, figures, log_block, step_facts


def _view_state(nodes: dict[str, NodeView], keys: tuple[str, ...]) -> str:
    states = [nodes[k].state for k in keys if k in nodes]
    if any(s in {"FAILED", "STOPPED", "MISSING"} for s in states):
        return "FAILED"
    if states and all(s == "COMPLETED" for s in states):
        return "COMPLETED"
    if "RUNNING" in states:
        return "RUNNING"
    return "PLANNED" if states and all(s == "PLANNED" for s in states) else "PENDING"


def _selector(snap: Snapshot) -> str:
    nodes = {n.key: n for n in snap.nodes}
    parts = []
    for group, views in groupby(pipeline_views(snap.nodes), key=lambda v: v.group):
        buttons = "".join(
            f'<button class="pipe" data-pipe="{esc(v.view_id)}"><span class="dot {esc(_view_state(nodes, v.keys))}"></span>'
            f'{esc(v.label)}<span class="muted small">{len(v.keys)}</span></button>'
            for v in views
        )
        parts.append(f'<div class="pipe-group"><h4>{esc(group)}</h4>{buttons}</div>')
    return "".join(parts)


def _node_template(node: NodeView, snap: Snapshot, run_of: dict[str, str], image_url: ImageUrl) -> str:
    step = snap.steps_by_key.get(node.key)
    branches = "".join(f"<li>{esc(label)}</li>" for label in node.labels)
    body = [f'<div class="panel-head"><h3>{esc(SHORT.get(node.brick, node.brick))}</h3>{pill(node.state)}</div>']
    if node.headline:
        body.append(f'<p class="headline">{esc(node.headline)}</p>')
    if step is not None:
        body.append(step_facts(step))
    body.append(f"<h4>Used by</h4><ul class=plain>{branches}</ul>")
    body.append("<h4>Parameters</h4>" + (kv(node.params) or '<p class="muted">defaults</p>'))
    if step is not None:
        body.append(figures(step, image_url))
        if step.summary:
            body.append("<h4>Result</h4>" + kv(step.summary))
        body.append(log_block(step))
        if step.message:
            body.append(f'<p class="note bad">{esc(step.message)}</p>')
    if node.key in run_of:
        body.append(f'<a class="button" href="#runs/{esc(run_of[node.key])}">Open the run →</a>')
    if node.state == "PLANNED":
        body.append('<p class="muted">Saved in a branch, not run yet. Ask your assistant to submit it.</p>')
    body.append(f'<p class="muted small">Step key {esc(node.key)}</p>')
    return f'<template data-node="{esc(node.key)}">{"".join(body)}</template>'


def render_pipelines(snap: Snapshot, image_url: ImageUrl) -> str:
    if not snap.nodes:
        return '<p class="empty">No pipelines yet. Ask your assistant to save a branch in a project.</p>'
    run_of: dict[str, str] = {}
    for run in reversed(snap.runs):
        run_of.update({s.key: run.run_id for s in run.steps})
    graphs = "".join(
        f'<div class="graph" data-pipe-view="{esc(v.view_id)}" hidden><h3>{esc(v.group)} · {esc(v.label)}</h3>'
        f"{render_graph(snap.nodes, v.keys)}</div>"
        for v in pipeline_views(snap.nodes)
    )
    legend = "".join(pill(s, label) for s, label in (
        ("COMPLETED", "done"), ("RUNNING", "running"), ("PENDING", "queued"), ("FAILED", "failed"), ("PLANNED", "planned")))
    templates = "".join(_node_template(n, snap, run_of, image_url) for n in snap.nodes)
    return (
        f'<div class="pipes"><aside class="pipe-list">{_selector(snap)}</aside>'
        f'<div class="pipe-main"><div class="legend">{legend}<span class="muted small">Click a step for params, job, log and outputs</span></div>{graphs}</div>'
        f'<aside class="panel" id="node-panel"><p class="muted">Select a step in the graph.</p></aside></div>'
        f"{templates}"
    )
