"""Cluster overview (from the account menu): what the student may use, what is in
use now, where their data is and how full it is, and how busy the cluster is."""

from __future__ import annotations

from ..overview import Overview
from .html import esc, listing, pill, table
from .views_activity import reason



def _fmt(value: float, unit: str) -> str:
    """Readable amounts: 2072 GB -> 2.0 TB, 2445710 files -> 2.4M."""
    if unit == " GB" and value >= 1024:
        return f"{value / 1024:.1f} TB"
    if not unit and value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if not unit and value >= 10_000:
        return f"{value / 1000:.0f}K"
    return f"{value:g}{unit}"


def _bar(used: float, limit: float | None, unit: str = "") -> str:
    if not limit:
        return f'<span class="muted small">{_fmt(used, unit)} used · no per-user limit</span>'
    share = min(100, round(100 * used / limit)) if limit else 0
    level = "bad" if share >= 90 else ("warn" if share >= 70 else "")
    return (f'<div class="bar {level}"><span style="width:{share}%"></span></div>'
            f'<span class="small">{_fmt(used, unit)} of {_fmt(limit, unit)} ({share}%)</span>')


def _limits(ov: Overview) -> str:
    if not ov.limits:
        return '<p class="empty">Per-user limits could not be read.</p>'
    groups = []
    for group in ov.limits:
        cards = "".join(f'<div class="metric static"><span>{esc(limit.name)}</span>{_bar(limit.used, limit.limit)}</div>'
                        for limit in group.limits)
        groups.append(f'<h3>{esc(", ".join(group.partitions))} <span class="muted small">(QOS {esc(group.qos)})</span></h3>'
                      f'<div class="metrics">{cards}</div>')
    return "".join(groups)


def _storage(ov: Overview) -> str:
    rows = [
        f"<tr><td><b>{esc(s.name)}</b><div class='muted small'>{esc(s.path)} · {esc(s.note)}</div></td>"
        f"<td>{_bar(round(s.used_gb or 0), round(s.limit_gb) if s.limit_gb else None, ' GB')}</td>"
        f"<td>{_bar(s.files, s.files_limit) if s.files is not None else ''}</td></tr>"
        for s in ov.storage
    ]
    footprint = "".join(f"<tr><td><code>{esc(name)}</code></td><td class=num>{size:g} GB</td></tr>" for name, size in ov.footprint.items())
    return (table(("Filesystem", "Space", "Files"), rows) if rows else '<p class="empty">Storage could not be read.</p>') + (
        f"<details><summary>sc-hub folders (updated hourly)</summary>{table(('Folder', 'Size'), [footprint])}</details>"
        if footprint else "")


def _jobs(ov: Overview) -> str:
    if not ov.jobs:
        return '<p class="empty">No jobs of yours in the queue.</p>'
    rows = [
        (f"{j.job_id} {j.name} {j.state} {j.partition} {j.node}",
         f"<td><code>{esc(j.job_id)}</code></td><td>{esc(j.name)}</td><td>{pill(j.state)}</td><td>{esc(j.partition)}</td>"
         f"<td class=num>{j.cpus}</td><td class=num>{j.mem_gb:g} GB</td><td class=num>{j.gpus or '–'}</td>"
         f"<td>{esc(j.elapsed)} / {esc(j.time_limit)}</td><td>{esc(j.node or reason(j.reason))}</td>")
        for j in ov.jobs
    ]
    headers = ("Job", "Name", "State", "Partition", "CPUs", "Memory", "GPUs", "Time", "Node or why it waits")
    return listing(headers, rows, "Filter your jobs", limit=15, key="cluster-jobs")


def _partitions(ov: Overview) -> str:
    rows = [
        f"<tr><td><b>{esc(p.name)}</b><div class='muted small'>{p.nodes} nodes · {p.mem_gb_per_node:g} GB/node · max {esc(p.max_time)}</div></td>"
        f"<td>{_bar(p.cpus_alloc, p.cpus_total)}</td><td>{_bar(p.gpus_used, p.gpus_total) if p.gpus_total else '–'}</td></tr>"
        for p in ov.partitions
    ]
    return table(("Partition", "CPUs in use", "GPUs in use"), rows) if rows else '<p class="empty">Partition load could not be read.</p>'


def render_cluster(ov: Overview | None) -> str:
    if ov is None:
        return '<p class="empty">No cluster information yet.</p>'
    logins = "".join(f"<li><code>{esc(line)}</code></li>" for line in ov.logins) or "<li class=muted>none</li>"
    problems = "".join(f'<p class="note warn small">{esc(p)}</p>' for p in ov.problems)
    return (
        f'<div class="run-title"><h2>Cluster overview · {esc(ov.user)}</h2><span class="muted small">'
        f"login node {esc(ov.login_node)} · updated {esc(ov.generated_at)}</span></div>{problems}"
        "<h2>Your limits and what is in use</h2>" + _limits(ov)
        + "<h2>Your jobs</h2>" + _jobs(ov)
        + "<h2>Storage</h2>" + _storage(ov)
        + "<h2>Cluster load</h2>" + _partitions(ov)
        + f"<h2>Your login sessions</h2><ul class='plain'>{logins}</ul>"
    )
