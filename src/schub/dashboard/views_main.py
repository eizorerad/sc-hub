"""Overview, jobs and library views (projects: views_projects, cluster: views_cluster)."""

from __future__ import annotations

from pathlib import Path

from ..slurm import QueueJob
from .collect import RunView, Snapshot
from .html import dot, esc, listing, pill, subtabs
from .steps import slurm_seconds

OVERVIEW_PROJECTS = 6
REASONS = {
    "QOSMaxJobsPerUserLimit": "waiting for a free slot (max 2 running jobs per user on ws-ia)",
    "QOSMaxCpuPerUserLimit": "waiting: your jobs already use the 24-CPU per-user limit",
    "QOSMaxMemoryPerUser": "waiting: your jobs already use the per-user memory limit",
    "Dependency": "waiting for the previous step",
    "DependencyNeverSatisfied": "blocked: a previous step failed; cancel this job",
    "Resources": "waiting for free nodes",
    "Priority": "queued behind higher-priority jobs",
    "BeginTime": "scheduled to start later",
    "None": "",
}


def reason(raw: str) -> str:
    key = raw.strip("()")
    return REASONS.get(key, key)


def _progress(job: QueueJob) -> str:
    used, limit = slurm_seconds(job.elapsed), slurm_seconds(job.time_limit)
    if job.state != "RUNNING" or not used or not limit:
        return f'<span class="muted">{esc(reason(job.reason))}</span>'
    share = min(100, round(100 * used / limit))
    return (
        f'<div class="bar" title="{esc(job.elapsed)} of {esc(job.time_limit)}"><span style="width:{share}%"></span></div>'
        f'<span class="muted small">{esc(job.elapsed)} of {esc(job.time_limit)}</span>'
    )


def _job_rows(jobs: list[QueueJob]) -> list[tuple[str, str]]:
    return [
        (f"{j.job_id} {j.name} {j.state} {j.partition}",
         f"<td><code>{esc(j.job_id)}</code></td><td>{esc(j.name)}</td><td>{pill(j.state)}</td>"
         f"<td>{esc(j.partition)}</td><td>{_progress(j)}</td>")
        for j in jobs
    ]


def render_jobs(snap: Snapshot) -> str:
    if snap.jobs_error:
        return f'<p class="note bad">The queue could not be read: {esc(snap.jobs_error)}</p>'
    ours = [j for j in snap.jobs if j.name.startswith("schub-")]
    others = [j for j in snap.jobs if not j.name.startswith("schub-")]
    headers = ("Job", "Name", "State", "Partition", "Progress or why it waits")
    html = listing(headers, _job_rows(ours), "Filter jobs", key="jobs-schub") if ours else '<p class="empty">No sc-hub jobs in the queue.</p>'
    if others:
        html += f"<details><summary>{len(others)} other jobs of yours</summary>{listing(headers, _job_rows(others), 'Filter jobs', key='jobs-other')}</details>"
    return html + (
        '<p class="note">Cluster rule: on ws-ia each person runs at most 2 jobs at once (24 CPUs, about 107 GB). '
        "Extra steps wait in the queue and start by themselves.</p>"
    )


def _metric(label: str, value: object, href: str, state: str = "") -> str:
    attr = f' data-filter-state="{esc(state)}"' if state else ""
    return f'<a class="metric" href="{esc(href)}"{attr}><span>{esc(label)}</span><b>{esc(value)}</b></a>'


def _run_card(run: RunView) -> str:
    dots = "".join(dot(s.state, f"{s.brick}: {s.state.lower()}") for s in run.steps)
    current = next((s for s in run.steps if s.state in {"RUNNING", "PENDING"}), None)
    note = f"now: {current.brick}" if current else next((s.headline for s in reversed(run.steps) if s.headline), "")
    return (
        f'<a class="card run-card" href="#runs/{esc(run.run_id)}"><div class="run-title"><b>{esc(run.label)}</b>{pill(run.state)}</div>'
        f'<div class="dots">{dots}</div><div class="muted small">{esc(run.dataset)} · {esc(note)}</div></a>'
    )


SESSION_NAMES = {"jupyter": "JupyterLab", "cellxgene": "cellxgene"}


def _sessions(snap: Snapshot) -> str:
    cards = []
    for s in snap.sessions:
        details = [s.node and f"on {s.node}", f"{s.hours} h", "GPU" if s.gpu else "", f"started {s.started}"]
        target = f'<span class="muted small">{esc(Path(s.target).name)}</span>' if s.target else ""
        how = (f"<span>Open on the laptop: <code>./schub-lab {esc(s.kind)}</code> "
               f"<span class='muted small'>(Windows: .\\schub-lab.cmd {esc(s.kind)})</span></span>") if s.node else ""
        cards.append(
            f'<div class="card session"><b>{esc(SESSION_NAMES.get(s.kind, s.kind))}</b>{pill(s.state)}'
            f'<span class="muted small">{esc(" · ".join(d for d in details if d))}</span>{target}{how}</div>'
        )
    return f'<h2>Interactive sessions</h2><div class="cards">{"".join(cards)}</div>' if cards else ""


def render_overview(snap: Snapshot) -> str:
    running = sum(j.state == "RUNNING" for j in snap.jobs if j.name.startswith("schub-"))
    queued = sum(j.state == "PENDING" for j in snap.jobs if j.name.startswith("schub-"))
    done = sum(r.state == "COMPLETED" for r in snap.runs)
    failed = sum(r.state == "FAILED" for r in snap.runs)
    active = [r for r in snap.runs if r.state in {"RUNNING", "PENDING", "UNKNOWN"}]
    latest = [r for r in snap.runs if r not in active][:6]
    recent = sorted(snap.projects, key=lambda p: p.runs[0] if p.runs else "", reverse=True)[:OVERVIEW_PROJECTS]
    projects = "".join(
        f'<a class="card" href="#projects/{esc(p.path)}"><b>{esc(p.path)}</b><div class="muted small">{esc(p.meta.question or "No question yet")}</div>'
        f'<div class="small">{len(p.branches)} branches · {len(p.ideas)} ideas · {len(p.runs)} runs</div></a>'
        for p in recent
    )
    more = len(snap.projects) - len(recent)
    all_projects = f'<p><a href="#projects">All {len(snap.projects)} projects →</a></p>' if more > 0 else ""
    return (
        '<div class="metrics">'
        + _metric("Running steps", running, "#jobs")
        + _metric("Queued steps", queued, "#jobs")
        + _metric("Completed runs", done, "#runs", "COMPLETED")
        + _metric("Failed runs", failed, "#runs", "FAILED")
        + "</div>"
        + _sessions(snap)
        + (f'<h2>In progress</h2><div class="cards">{"".join(_run_card(r) for r in active)}</div>' if active else "")
        + (f'<h2>Latest runs</h2><div class="cards">{"".join(_run_card(r) for r in latest)}</div>' if latest else
           '<p class="empty">No runs yet.</p>')
        + (f'<h2>Projects</h2><div class="cards">{projects}</div>{all_projects}' if projects else "")
    )


def render_library(snap: Snapshot) -> str:
    """Datasets, models, references/tools and the environment behind sub-tabs, each a
    filterable list: 100 datasets never push the models out of sight."""
    used: dict[str, int] = {}
    for run in snap.runs:
        for name in set(run.inputs or (run.dataset,)):
            used[name] = used.get(name, 0) + 1
    datasets = [
        (f"{d.name} {d.title} {d.organism} {d.source} {d.kind}",
         f"<td><b>{esc(d.name)}</b>{'<span class=tag>FASTQ</span>' if d.kind == 'fastq' else ''}"
         f"<div class='muted small'>{esc(d.title)}</div></td><td>{esc(d.source)}</td>"
         f"<td class=num>{d.size_mb:g} MB</td><td>{esc(d.organism)}</td><td class=num>{used.get(d.name, 0)}</td>"
         f"<td class='muted small'>{esc(d.license)}</td>")
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
         listing(("Dataset", "Where", "Size", "Organism", "Runs", "License"), datasets, "Filter datasets", key="library-datasets")
         if datasets else '<p class="empty">No datasets yet.</p>'),
        ("models", "Models", len(models),
         listing(("Model", "Kind", "From", "Size"), models, "Filter models", key="library-models") if models else '<p class="empty">None yet.</p>'),
        ("refs", "References and tools", len(refs), listing(("Reference / tool", "Used by", "Status"), refs, "Filter", key="library-refs")),
        ("env", "Environment", None, environment),
    ])
