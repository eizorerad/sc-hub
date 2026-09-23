"""Overview, jobs, library and projects views."""

from __future__ import annotations

from ..projects import ProjectSummary
from ..slurm import QueueJob
from .collect import RunView, Snapshot
from .html import dot, esc, pill, table
from .lineage import view_id
from .steps import slurm_seconds

IDEA_COLUMNS = ("open", "planned", "running", "done", "dropped")
IDEA_STATE = {"open": "PENDING", "planned": "PLANNED", "running": "RUNNING", "done": "COMPLETED", "dropped": "FAILED"}
PROJECT_STATE = {"active": "RUNNING", "paused": "PENDING", "done": "COMPLETED"}
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


def _job_rows(jobs: list[QueueJob]) -> list[str]:
    return [
        f"<tr><td><code>{esc(j.job_id)}</code></td><td>{esc(j.name)}</td><td>{pill(j.state)}</td>"
        f"<td>{esc(j.partition)}</td><td>{_progress(j)}</td></tr>"
        for j in jobs
    ]


def render_jobs(snap: Snapshot) -> str:
    if snap.jobs_error:
        return f'<p class="note bad">The queue could not be read: {esc(snap.jobs_error)}</p>'
    ours = [j for j in snap.jobs if j.name.startswith("schub-")]
    others = [j for j in snap.jobs if not j.name.startswith("schub-")]
    headers = ("Job", "Name", "State", "Partition", "Progress or why it waits")
    html = table(headers, _job_rows(ours)) if ours else '<p class="empty">No sc-hub jobs in the queue.</p>'
    if others:
        html += f"<details><summary>{len(others)} other jobs of yours</summary>{table(headers, _job_rows(others))}</details>"
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


def render_overview(snap: Snapshot) -> str:
    running = sum(j.state == "RUNNING" for j in snap.jobs if j.name.startswith("schub-"))
    queued = sum(j.state == "PENDING" for j in snap.jobs if j.name.startswith("schub-"))
    done = sum(r.state == "COMPLETED" for r in snap.runs)
    failed = sum(r.state == "FAILED" for r in snap.runs)
    active = [r for r in snap.runs if r.state in {"RUNNING", "PENDING", "UNKNOWN"}]
    latest = [r for r in snap.runs if r not in active][:6]
    projects = "".join(
        f'<a class="card" href="#projects"><b>{esc(p.path)}</b><div class="muted small">{esc(p.meta.question or "No question yet")}</div>'
        f'<div class="small">{len(p.branches)} branches · {len(p.ideas)} ideas · {len(p.runs)} runs</div></a>'
        for p in snap.projects
    )
    return (
        '<div class="metrics">'
        + _metric("Running steps", running, "#jobs")
        + _metric("Queued steps", queued, "#jobs")
        + _metric("Completed runs", done, "#runs", "COMPLETED")
        + _metric("Failed runs", failed, "#runs", "FAILED")
        + "</div>"
        + (f'<h2>In progress</h2><div class="cards">{"".join(_run_card(r) for r in active)}</div>' if active else "")
        + (f'<h2>Latest runs</h2><div class="cards">{"".join(_run_card(r) for r in latest)}</div>' if latest else
           '<p class="empty">No runs yet.</p>')
        + (f'<h2>Projects</h2><div class="cards">{projects}</div>' if projects else "")
    )


def render_library(snap: Snapshot) -> str:
    used: dict[str, int] = {}
    for run in snap.runs:
        used[run.dataset] = used.get(run.dataset, 0) + 1
    datasets = [
        f"<tr><td><b>{esc(d.name)}</b><div class='muted small'>{esc(d.title)}</div></td><td>{esc(d.source)}</td>"
        f"<td class=num>{d.size_mb:g} MB</td><td>{esc(d.organism)}</td><td class=num>{used.get(d.name, 0)}</td>"
        f"<td class='muted small'>{esc(d.license)}</td></tr>"
        for d in snap.datasets
    ]
    reference = [f"<tr><td>{esc(m.name)}</td><td>CellTypist</td><td>{esc(m.source)}</td></tr>" for m in snap.models]
    trained = [
        f'<tr><td>{esc(m.name)}</td><td><a href="#runs/{esc(m.run_id)}">{esc(m.origin)}</a></td><td class=num>{m.size_mb:g} MB</td></tr>'
        for m in snap.trained_models
    ]
    versions = " · ".join(f"{k} {v}" for k, v in snap.versions.items() if v != "absent")
    return (
        "<h2>Datasets</h2>" + table(("Dataset", "Where", "Size", "Organism", "Runs", "License"), datasets)
        + "<h2>Reference models</h2>" + (table(("Model", "Kind", "Where"), reference) if reference else '<p class="empty">None staged.</p>')
        + "<h2>Models trained in your runs</h2>" + (table(("Model", "From run", "Size"), trained) if trained else '<p class="empty">None yet.</p>')
        + f'<h2>Environment</h2><p class="muted">{esc(snap.library_mode)}</p><p class="small">Environment <code>{esc(snap.env_id)}</code>: {esc(versions)}</p>'
    )


def _idea_card(idea) -> str:
    hypothesis = f'<div class="muted small">{esc(idea.hypothesis)}</div>' if idea.hypothesis else ""
    branches = f'<div class="small">branches: {esc(", ".join(idea.branches))}</div>' if idea.branches else ""
    return f'<div class="idea" title="{esc(idea.hypothesis)}"><b>{esc(idea.title)}</b>{hypothesis}{branches}</div>'


def _ideas(project: ProjectSummary) -> str:
    columns = []
    for status in IDEA_COLUMNS:
        items = [i for i in project.ideas if i.status == status]
        if not items and status == "dropped":
            continue
        cards = "".join(_idea_card(i) for i in items)
        columns.append(f"<div class=column>{pill(IDEA_STATE[status], status)}{cards or '<p class=empty>none</p>'}</div>")
    return f'<div class="board">{"".join(columns)}</div>'


def _project(project: ProjectSummary, runs: tuple[RunView, ...]) -> str:
    latest: dict[str, RunView] = {}
    for run in runs:
        if run.project == project.path and run.branch and run.branch not in latest:
            latest[run.branch] = run
    rows = [
        f"<tr><td><code>{esc(b)}</code></td><td>{pill(latest[b].state) if b in latest else pill('PLANNED', 'not run yet')}</td>"
        f"<td>{'<a href=' + chr(34) + '#runs/' + esc(latest[b].run_id) + chr(34) + '>open run</a>' if b in latest else ''}</td>"
        f'<td><a href="#pipelines/{esc(view_id(project.path + "/" + b))}">graph</a></td></tr>'
        for b in project.branches
    ]
    log = "".join(f"<pre class=entry>{esc(e)}</pre>" for e in reversed(project.logbook_tail)) or '<p class="empty">Empty.</p>'
    problems = "".join(f'<p class="note bad">{esc(p)}</p>' for p in project.problems)
    return (
        f'<div class="card project"><div class="run-title"><h2>{esc(project.path)}</h2>'
        f"{pill(PROJECT_STATE[project.meta.status], project.meta.status)}</div>"
        f'<p class="question">{esc(project.meta.question or "No question written yet.")}</p>'
        + (table(("Branch", "Latest", "Run", "Pipeline"), rows) if rows else '<p class="empty">No branches yet.</p>')
        + f"<h3>Ideas</h3>{problems}{_ideas(project)}"
        + f"<details><summary>Logbook, latest first</summary>{log}</details></div>"
    )


def render_projects(snap: Snapshot) -> str:
    if not snap.projects:
        return '<p class="empty">No projects yet. Ask your assistant to create one with your research question.</p>'
    return "".join(_project(p, snap.runs) for p in snap.projects)
