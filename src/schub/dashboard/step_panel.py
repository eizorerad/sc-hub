"""The step panel of a pipeline graph, one <template> per step (rendered once per
build, shared by every graph showing it). In reading order: what the step gave (its
headline, problems, figures, cells, numbers), then asking the assistant to fix it or
try an alternative; parameters, code, log and the technical details are folded."""

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


def _labels(labels: list[str]) -> str:
    return esc(", ".join(labels)) if len(labels) <= 3 else f"{len(labels)} branches (see Details)"


def _notes(node: NodeView, snap: Snapshot, nodes: dict[str, NodeView]) -> str:
    """What the planner says, and whether the step must run again (one line per reason,
    however many branches share it)."""
    notes = [f'<p class="note {"bad" if i.level == "error" else "warn"}">{esc(i.message)}</p>'
             for i in sorted(node.issues, key=lambda i: i.level != "error")]
    reruns: dict[tuple[str, str], tuple[str, list[str]]] = {}  # (why, earlier key) -> (what it gave, branches)
    for label, key in sorted(node.earlier.items()):
        earlier = nodes.get(key)
        if earlier is not None:
            found = reruns.setdefault((_why(node, earlier, snap), key), (earlier.headline or earlier.state.lower(), []))
            found[1].append(label)
    for (why, key), (before, labels) in reruns.items():
        where = f" in {_labels(labels)}" if len(node.labels) > 1 else ""
        notes.append(
            f"<p class=\"note\">Needs a re-run{where}: {esc(why)}. Before, it gave <b>{esc(before)}</b>. "
            f'<button type="button" class="link" data-select-node="{esc(key)}">Show that result</button></p>')
    if not node.current:
        notes.append('<p class="note">An older version: no branch uses this result now '
                     "(the branch was revised, or sc-hub was updated since).</p>")
    return "".join(notes)


def _ask(node: NodeView, snap: Snapshot) -> str:
    """One 'ask your assistant' box; with several branches using the step, a picker."""
    if not node.refs:
        return ""
    label = node.refs[0].rpartition("#")[0]
    stale = any((info := snap.branches.get(r.rpartition("#")[0])) and info.keys and node.key not in info.keys
                for r in node.refs)
    note = ("This result comes from an earlier version of the branch; the request refers to the "
            "branch as it is now.") if stale else ""
    return ask_block(node.refs[0], node.brick, label.rsplit("/", 1)[-1], note, refs=node.refs)


def _folded(title: str, body: str) -> str:
    return f'<details class="fold"><summary>{esc(title)}</summary>{body}</details>' if body else ""


def _details(node: NodeView, step, run_of: dict[str, str]) -> str:
    """Where and how it ran: job, resources, timing, which branches use it, the step key."""
    facts = step_facts(step) if step is not None else ""
    used = "".join(f"<li>{esc(label)}</li>" for label in node.labels)
    run = (f'<a href="#runs/{esc(run_of[node.key])}">Open the run →</a>' if node.key in run_of else "")
    return (f"{facts}<h4>Used by</h4><ul class=plain>{used or '<li class=muted>no branch</li>'}</ul>"
            f'<p class="small">{run}</p><p class="muted small">Step key {esc(node.key)}</p>')


def node_template(node: NodeView, snap: Snapshot, run_of: dict[str, str], image_url: ImageUrl,
                   nodes: dict[str, NodeView]) -> str:
    step = snap.steps_by_key.get(node.key)
    summary = (step.summary if step is not None else None) or {}
    state = pill("OUTDATED", "needs re-run") if node.earlier else pill(node.state)
    body = [f'<div class="panel-head"><h3>{esc(SHORT.get(node.brick, node.brick))}</h3>{state}</div>']
    if node.headline:
        body.append(f'<p class="headline">{esc(node.headline)}</p>')
    body.append(_notes(node, snap, nodes) + warnings(summary))
    if step is not None and step.message:
        body.append(f'<p class="note bad">{esc(step.message)}</p>')
    if node.state == "PLANNED":
        body.append('<p class="muted">Not run yet. Ask your assistant to submit the branch.</p>')
    if step is not None:
        body.append(figures(step, image_url))
        body.append(cell_map(step, image_url, folded=False))
        if summary:
            body.append('<div class="result">' + kv(summary) + result_tables(step, summary) + "</div>")
    body.append(_ask(node, snap))
    body.append('<div class="folds">'
                + _folded("Parameters", kv(node.params) or '<p class="muted small">defaults</p>')
                + code_section(node.brick, step.code_id if step is not None else "")
                + (log_block(step) if step is not None else "")
                + _folded("Details", _details(node, step, run_of)) + "</div>")
    return f'<template data-node="{esc(node.key)}">{"".join(body)}</template>'
