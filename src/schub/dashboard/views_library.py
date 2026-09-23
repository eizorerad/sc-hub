"""Library: datasets, models, references/tools and the environment behind
sub-tabs, each a filterable list (100 datasets never push the models out of sight)."""

from __future__ import annotations

from .collect import Snapshot
from .html import esc, hint, listing, pill, subtabs


def render_library(snap: Snapshot) -> str:
    used: dict[str, int] = {}
    for run in snap.runs:
        for name in set(run.inputs or (run.dataset,)):
            used[name] = used.get(name, 0) + 1
    datasets = [
        (f"{d.name} {d.title} {d.organism} {d.source} {d.kind}",
         f"<td><b>{esc(d.name)}</b>{'<span class=tag>FASTQ</span>' if d.kind == 'fastq' else ''}"
         f"<div class='muted small'>{esc(d.title)}</div></td><td>{esc(d.source)}</td>"
         f"<td class=num>{d.size_mb:g} MB</td><td>{esc(d.organism)}</td><td class=num>{used.get(d.name, 0)}</td>"
         f"<td>{hint('License: ' + d.license, end=True) if d.license else ''}</td>")
        for d in snap.datasets
    ]
    models = [
        (f"{m.name} celltypist {m.source}", f"<td>{esc(m.name)}</td><td>CellTypist (reference)</td><td>{esc(m.source)}</td><td></td>")
        for m in snap.models
    ] + [
        (f"{m.name} {m.kind} {m.origin}",
         f'<td>{esc(m.name)}</td><td>{esc(m.kind)} (trained)</td><td><a href="#runs/{esc(m.run_id)}">{esc(m.origin)}</a></td>'
         f"<td class=num>{m.size_mb:g} MB</td>")
        for m in snap.trained_models
    ]
    refs = [
        (f"{r.name} {r.kind} {r.status}",
         f"<td>{esc(r.name)}</td><td class='muted'>{esc(r.kind)}</td>"
         f"<td>{pill('COMPLETED', r.status) if not r.status.startswith('not ') else esc(r.status)}</td>")
        for r in snap.references
    ]
    versions = " · ".join(f"{k} {v}" for k, v in snap.versions.items() if v != "absent")
    environment = (f'<p class="muted">{esc(snap.library_mode)}</p>'
                   f'<p class="small">Environment <code>{esc(snap.env_id)}</code>: {esc(versions)}</p>')
    return subtabs("library", [
        ("datasets", "Datasets", len(datasets),
         listing(("Dataset", "Where", "Size", "Organism", "Runs", ""), datasets, "Filter datasets", key="library-datasets")
         if datasets else '<p class="empty">No datasets yet.</p>'),
        ("models", "Models", len(models),
         listing(("Model", "Kind", "From", "Size"), models, "Filter models", key="library-models") if models else '<p class="empty">None yet.</p>'),
        ("refs", "References and tools", len(refs), listing(("Reference / tool", "Used by", "Status"), refs, "Filter", key="library-refs")),
        ("env", "Environment", None, environment),
    ])
