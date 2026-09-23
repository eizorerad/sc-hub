"""The step panel of a pipeline graph: what a step is, did and must do, one
<template> per step (rendered once per build, shared by every graph showing it)."""

from __future__ import annotations

from typing import Any

from ..brick_code import code_changed
from ..bricks import REGISTRY
from .collect import NodeView, Snapshot
from .html import ask_block, esc, kv, pill, warnings
from .lineage import SHORT
from .notebooks import code_section
from .views_runs import ImageUrl, cell_map, figures, log_block, result_tables, step_facts


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


def node_template(node: NodeView, snap: Snapshot, run_of: dict[str, str], image_url: ImageUrl,
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
