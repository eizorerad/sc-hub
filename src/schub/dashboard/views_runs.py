"""Runs: what is happening (numbers, queue, sessions), the history of runs, and one
detail view per run (step timeline, its notebook)."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Callable

from ..bricks import REGISTRY
from .collect import BranchInfo, RunView, Snapshot, StepView
from .html import ask_block, dot, esc, kv, pill, subtabs, table, warnings
from .notebooks import code_section, notebook_button
from .steps import duration
from .views_activity import metrics, render_queue, render_sessions

DE_TOP = 12
ImageUrl = Callable[[StepView, str, bool], str | None]


def _de_table(step_dir: Path) -> str:
    path = step_dir / "results" / "de_all.csv"
    if not path.is_file():
        return ""
    try:
        with path.open(newline="") as handle:
            rows = [r for r in csv.DictReader(handle) if r.get("padj") not in (None, "", "nan")]
        rows.sort(key=lambda r: float(r["padj"]))
        first = next(iter(rows[0])) if rows else ""
        body = [
            f"<tr><td><b>{esc(r.get(first, ''))}</b></td><td>{esc(r.get('group', ''))}</td>"
            f"<td class=num>{float(r['log2FoldChange']):+.2f}</td><td class=num>{float(r['padj']):.1e}</td></tr>"
            for r in rows[:DE_TOP]
        ]
    except (OSError, csv.Error, KeyError, TypeError, ValueError) as error:
        return f'<p class="note">Could not read de_all.csv ({esc(type(error).__name__)}); the file is in the step folder.</p>'
    return f"<h4>Top {DE_TOP} genes</h4>" + table(("Gene", "Group", "log2 FC", "padj"), body, "compact")


def _memento_table(step_dir: Path) -> str:
    path = step_dir / "results" / "memento_all.csv"
    if not path.is_file():
        return ""
    try:
        with path.open(newline="") as handle:
            rows = [r for r in csv.DictReader(handle) if r.get("de_padj") not in (None, "", "nan")]
        rows.sort(key=lambda r: float(r["de_padj"]))
        first = next(iter(rows[0])) if rows else ""
        body = [
            f"<tr><td><b>{esc(r.get(first, ''))}</b></td><td>{esc(r.get('group', ''))}</td>"
            f"<td class=num>{float(r['de_coef']):+.2f}</td><td class=num>{float(r['de_padj']):.1e}</td>"
            f"<td class=num>{float(r['dv_padj']):.1e}</td></tr>"
            for r in rows[:DE_TOP]
        ]
    except (OSError, csv.Error, KeyError, TypeError, ValueError) as error:
        return f'<p class="note">Could not read memento_all.csv ({esc(type(error).__name__)}).</p>'
    return f"<h4>Top {DE_TOP} genes (mean)</h4>" + table(("Gene", "Group", "Mean effect", "padj", "Variability padj"), body, "compact")


def _samples(summary: dict) -> str:
    samples = summary.get("samples")
    if not isinstance(samples, dict) or not samples:
        return ""
    columns = sorted({k for s in samples.values() if isinstance(s, dict) for k in s})
    rows = [
        f"<tr><td>{esc(name)}</td>" + "".join(f"<td class=num>{esc(s.get(c, '–'))}</td>" for c in columns) + "</tr>"
        for name, s in samples.items() if isinstance(s, dict)
    ]
    return "<h4>Per sample</h4>" + table(("Sample", *(c.replace("_", " ") for c in columns)), rows, "compact")


def _extra(step: StepView, summary: dict) -> str:
    if step.brick == "pseudobulk_de":
        return _groups(summary) + _de_table(Path(step.step_dir))
    if step.brick == "memento_de":
        return _groups(summary) + _memento_table(Path(step.step_dir))
    if step.brick in {"kb_count", "cellranger_count"}:
        return _samples(summary)
    return ""


def _groups(summary: dict) -> str:
    groups = summary.get("groups")
    if not isinstance(groups, dict):
        return ""
    rows = [
        f"<tr><td>{esc(name)}</td><td class=num>{esc(g.get('significant', '–'))}</td>"
        f"<td>{esc(', '.join(g.get('top_up', [])) or g.get('skipped', ''))}</td></tr>"
        for name, g in groups.items() if isinstance(g, dict)
    ]
    return "<h4>Per group</h4>" + table(("Group", "DE genes", "Top up / note"), rows, "compact")


def cell_map(step: StepView, image_url: ImageUrl, folded: bool) -> str:
    """A canvas the page script fills from pts/<key>.js (loaded only when shown)."""
    points = getattr(image_url, "points", None)
    src = points(step) if points else None
    if not src:
        return ""
    box = (f'<div class="cellmap" data-pts="{esc(src)}" data-key="{esc(step.key)}"><div class="cm-bar">'
           f'<select aria-label="Color cells by"></select><span class="muted small cm-n"></span></div>'
           f'<canvas width="300" height="300" aria-label="UMAP of the cells"></canvas><div class="cm-legend"></div></div>')
    return f'<details class="cells"><summary>Cell map</summary>{box}</details>' if folded else f"<h4>Cells</h4>{box}"


def figures(step: StepView, image_url: ImageUrl) -> str:
    tags = []
    for name in step.extras.figures:
        thumb = image_url(step, name, False)
        full = image_url(step, name, True) or thumb
        if thumb:
            tags.append(f'<a href="{esc(full)}" target="_blank"><img class="thumb" src="{esc(thumb)}" alt="{esc(name)}" loading="lazy"></a>')
    return f'<div class="thumbs">{"".join(tags)}</div>' if tags else ""


def step_facts(step: StepView) -> str:
    x = step.extras
    facts = [("Job", step.job_id or "reused from cache")]
    if x.resources:
        facts.append(("Resources", " · ".join(f"{k} {v}" for k, v in x.resources.items())))
    if x.finished:
        facts.append(("Finished", x.finished + (f" · took {duration(x.seconds)}" if x.seconds else "")))
    return '<dl class="facts">' + "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in facts) + "</dl>"


def log_block(step: StepView) -> str:
    if not step.extras.log_tail:
        return ""
    return f"<details><summary>Log, last {len(step.extras.log_tail)} lines</summary><pre class=log>{esc(chr(10).join(step.extras.log_tail))}</pre></details>"


def _ask(run: RunView, step: StepView, info: BranchInfo | None) -> str:
    if not (run.project and run.branch):
        return ""
    note = ""
    # Changed if the branch as it is now (also through a revised parent) has other steps.
    if info and info.keys and run.steps and run.steps[-1].key not in info.keys:
        now = f" (now r{info.revision})" if run.revision and info.revision != run.revision else ""
        note = (f"This run used an earlier version of the branch{now}. The request refers to the branch "
                "as it is now; check the step with inspect_step first.")
    return ask_block(f"{run.project}/{run.branch}#{step.index}", step.brick, run.branch, note)


def _step_card(step: StepView, image_url: ImageUrl, ask: str = "") -> str:
    summary = step.summary or {}
    message = f'<p class="note bad">{esc(step.message)}</p>' if step.message else ""
    extra = _extra(step, summary)
    return (
        f'<li class="step {esc(step.state)}"><div class="step-head">{dot(step.state)}'
        f'<b>{step.index}. {esc(step.brick)}</b> {pill(step.state)}'
        f'<span class="muted right">{esc(duration(step.extras.seconds))}</span></div>'
        f'<p class="headline">{esc(step.headline)}</p>{message}{warnings(summary)}'
        f"{figures(step, image_url)}{cell_map(step, image_url, folded=True)}{ask}"
        f"<details><summary>Details</summary>{step_facts(step)}{kv(step.params)}{kv(summary)}{extra}"
        f'<p class="muted small">Step key {esc(step.key)}</p></details>{code_section(step.brick, step.code_id)}'
        f"{log_block(step)}</li>"
    )


def _safe_card(step: StepView, image_url: ImageUrl, ask: str = "") -> str:
    """One unreadable step folder must not take the whole page down."""
    try:
        return _step_card(step, image_url, ask)
    except Exception as error:  # noqa: BLE001 - shown on the page instead
        return (
            f'<li class="step {esc(step.state)}"><div class="step-head">{dot(step.state)}'
            f'<b>{step.index}. {esc(step.brick)}</b> {pill(step.state)}</div>'
            f'<p class="note bad">Could not show this step ({esc(type(error).__name__)}); see {esc(step.step_dir)}</p></li>'
        )


def _notebook(run: RunView, notebooks: frozenset[str]) -> str:
    """Download the run as a notebook, or have the assistant open it in JupyterLab on the cluster."""
    button = notebook_button(run, notebooks, "Download notebook (.ipynb)")
    gpu = any(s.brick in REGISTRY and REGISTRY[s.brick].uses_gpu for s in run.steps)
    return (
        f'<div class="nb-box" data-nb-run="{esc(run.run_id)}" data-nb-gpu="{"1" if gpu else ""}">'
        '<div><b>Notebook</b> <span class="muted small">every step with the exact code and parameters it ran with; '
        "change a step and run it again from there</span></div>"
        f'<div class="ask-buttons">{button}<button type="button" data-ask="jupyter">Open in JupyterLab on the cluster</button></div>'
        '<p class="muted small copied" hidden>Copied: paste it into Codex or Claude.</p>'
        '<textarea class="manual" readonly hidden rows="3" aria-label="Request to copy"></textarea></div>'
    )


def run_detail(run: RunView, image_url: ImageUrl, info: BranchInfo | None = None,
               notebooks: frozenset[str] = frozenset()) -> str:
    return (
        f'<div class="run-detail" data-run="{esc(run.run_id)}" hidden>'
        f'<a class="back" href="#runs/history">← All runs</a>'
        f'<div class="run-title"><h2>{esc(run.label)}</h2>{pill(run.state)}</div>'
        f'<p class="muted">Run <code>{esc(run.run_id)}</code> · dataset {esc(run.dataset)} · created {esc(run.created_at[:16].replace("T", " "))}</p>'
        f"{_notebook(run, notebooks)}"
        f'<ol class="timeline">{"".join(_safe_card(s, image_url, _ask(run, s, info)) for s in run.steps)}</ol></div>'
    )


def _row(run: RunView) -> str:
    dots = "".join(dot(s.state, f"{s.brick}: {s.state.lower()}") for s in run.steps)
    last = next((s.headline for s in reversed(run.steps) if s.headline), "")
    text = f"{run.run_id} {run.label} {run.dataset} {last}".lower()
    return (
        f'<tr data-href="runs/{esc(run.run_id)}" data-state="{esc(run.state)}" data-text="{esc(text)}">'
        f'<td><a href="#runs/{esc(run.run_id)}">{esc(run.label)}</a><div class="muted small">{esc(run.run_id)}</div></td>'
        f"<td>{esc(' + '.join(run.inputs) or run.dataset)}</td><td>{pill(run.state)}</td><td class=dots>{dots}</td>"
        f'<td class="muted">{esc(last)}</td></tr>'
    )


def _history(snap: Snapshot) -> str:
    states = sorted({r.state for r in snap.runs} | {"COMPLETED", "FAILED"})  # the metrics filter by these
    options = '<option value="">All states</option>' + "".join(f'<option value="{esc(s)}">{esc(s.lower())}</option>' for s in states)
    rows = table(("Run", "Dataset", "State", "Steps", "Latest result"), [_row(r) for r in snap.runs], "runs clickable") \
        if snap.runs else '<p class="empty">No runs yet. Ask your assistant to plan and submit a branch.</p>'
    more = f'<button class="more" id="runs-more" type="button" hidden>Show all {len(snap.runs)} runs</button>'
    return (f'<div class="toolbar" id="run-filters">'
            f'<input id="run-search" type="search" placeholder="Filter runs, projects, results" aria-label="Filter runs">'
            f'<select id="run-state" aria-label="State">{options}</select></div>{rows}{more}')


def render_runs(snap: Snapshot, image_url: ImageUrl) -> str:
    queued = sum(j.name.startswith("schub-") for j in snap.jobs)
    sections = subtabs("runs", [
        ("history", "History", len(snap.runs), _history(snap)),
        ("queue", "Queue", queued, render_queue(snap)),
        ("sessions", "Sessions", len(snap.sessions), render_sessions(snap)),
    ])
    return (
        f'<div id="run-list">{metrics(snap)}{sections}</div>'
        + "".join(run_detail(r, image_url, snap.branches.get(f"{r.project}/{r.branch}"), snap.notebooks) for r in snap.runs)
    )
