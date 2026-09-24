"""What is happening now: the status in the header, sc-hub's jobs in the Slurm queue and
the interactive sessions (both on the Runs page)."""

from __future__ import annotations

from pathlib import Path

from ..slurm import QueueJob
from .collect import Snapshot
from .html import esc, hint, listing, pill
from .steps import slurm_seconds

LIVE_SESSIONS = frozenset({"RUNNING", "PENDING", "CONFIGURING"})
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


def _ours(snap: Snapshot) -> list[QueueJob]:
    return [j for j in snap.jobs if j.name.startswith("schub-")]


def live_counts(snap: Snapshot) -> tuple[int, int]:
    """sc-hub steps running and waiting in the queue."""
    ours = _ours(snap)
    return sum(j.state == "RUNNING" for j in ours), sum(j.state == "PENDING" for j in ours)


def status_chip(snap: Snapshot) -> str:
    """The header's one-line answer to "is anything happening?"."""
    running, queued = live_counts(snap)
    sessions = sum(s.state in LIVE_SESSIONS for s in snap.sessions)
    parts = [f"{running} running" if running else "", f"{queued} queued" if queued else "",
             f"{sessions} session{'s' if sessions > 1 else ''}" if sessions else ""]
    text = " · ".join(p for p in parts if p)
    if snap.jobs_error:
        return '<a class="status bad" href="#runs/queue">queue unreadable</a>'
    if not text:
        return '<a class="status muted" href="#runs/queue">nothing running</a>'
    state = "RUNNING" if running or sessions else "PENDING"
    target = "#runs/queue" if running or queued else "#runs/sessions"
    return f'<a class="status" href="{target}"><span class="dot {state}"></span>{esc(text)}</a>'


def render_queue(snap: Snapshot) -> str:
    if snap.jobs_error:
        return f'<p class="note bad">The queue could not be read: {esc(snap.jobs_error)}</p>'
    ours = _ours(snap)
    others = [j for j in snap.jobs if not j.name.startswith("schub-")]
    headers = ("Job", "Name", "State", "Partition", "Progress or why it waits")
    html = (listing(headers, _job_rows(ours), "Filter jobs", key="jobs-schub") if ours
            else '<p class="empty">No sc-hub jobs in the queue.</p>')
    if others:
        html += f"<details><summary>{len(others)} other jobs of yours</summary>{listing(headers, _job_rows(others), 'Filter jobs', key='jobs-other')}</details>"
    rule = ("Cluster rule: on ws-ia each person runs at most 2 jobs at once (24 CPUs, about 107 GB). Extra steps wait "
            "in the queue and start by themselves. Your limits, storage and every job: Cluster overview in the menu "
            "under the sc-hub square.")
    return html + f'<p class="muted small why-wait">Why do steps wait? {hint(rule)} <a href="#cluster">Cluster overview</a></p>'


SESSION_NAMES = {"jupyter": "JupyterLab", "cellxgene": "cellxgene"}


def render_sessions(snap: Snapshot) -> str:
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
    how = hint("Ask your assistant to start JupyterLab (optionally with a GPU, or with a run's notebook) or "
               "cellxgene for a result; it runs as a job on a compute node.")
    return (f'<div class="cards">{"".join(cards)}</div>' if cards
            else f'<p class="empty">No interactive sessions. {how}</p>')
