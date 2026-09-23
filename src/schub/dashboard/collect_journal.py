"""What the Journal tab shows: each project's hand-over, checkpoint and newest
entries, the workbench's state, and a few alerts worth a glance.

The student sees everything, including notes meant only for them (the assistant
does not). Alerts replace pull-only logs, VCC2026's weakest point: a stopped
workbench, full job slots, failed checks, lost cells, a quota nearly full.
"""

from __future__ import annotations

from typing import Any

from ..bench.checkpoint import CheckpointStore
from ..bench.fsio import read_json
from ..bench.inbox import Inbox, slug
from ..bench.journal import Journal
from ..bench.models import CellEntry, NoteEntry
from ..config import Settings
from ..overview import Overview
from ..projects import ProjectError, ProjectStore
from ..slurm import QueueJob
from ..state import Frozen

MAX_ENTRIES = 150
CODE_CHARS = 6000
OUTPUT_CHARS = 3000
QUOTA_ALERT = 0.95


class JournalCard(Frozen):
    project: str
    question: str = ""
    disposition: str = "active"
    next_action: str = ""
    handoff: str = ""
    entries: tuple[dict[str, Any], ...] = ()
    cells: int = 0
    failed_checks: int = 0
    notebook: str = ""  # the rendered notebook inside the view folder, without extension (.js, .ipynb)
    figures: tuple[tuple[str, str], ...] = ()  # (source file, path inside the view)


class BenchPanel(Frozen):
    workbench: str = ""
    alerts: tuple[str, ...] = ()


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit * 2 // 3] + "\n[...]\n" + text[-limit // 3:]


def _figure_path(prefix: str, image: str) -> str:
    return f"{prefix}/{image.removeprefix('cells/')}" if image else ""


def cell_data(entry: CellEntry, figure_prefix: str = "jfig") -> dict[str, Any]:
    return {
        "ref": entry.ref, "kind": "cell", "cid": entry.cid, "created": entry.created, "status": entry.status,
        "why": entry.why, "expect": entry.expect, "code": _cut(entry.code, CODE_CHARS), "setup": entry.setup,
        "data_scope": entry.data_scope, "duration_s": entry.duration_s, "message": entry.message,
        "outputs": [{"kind": o.kind, "text": _cut(o.text, OUTPUT_CHARS), "image": _figure_path(figure_prefix, o.image),
                     "ename": o.ename, "evalue": o.evalue} for o in entry.outputs],
        "files": [{"path": f.path, "change": f.change, "size": f.size} for f in entry.files],
        "downloads": [{"url": d.url, "sha256": d.sha256, "size": d.size, "status": d.status, "commit": d.commit}
                      for d in entry.downloads],
        "jobs": [{"job_id": j.job_id, "state": j.state, "exit_code": j.exit_code} for j in entry.jobs],
        "checks": [{"name": c.name, "status": c.status, "message": c.message} for c in entry.check_results],
        "bricks": list(entry.bricks), "by": entry.actor.client or entry.actor.kind, "engine": entry.actor.engine,
    }


def note_data(entry: NoteEntry) -> dict[str, Any]:
    return {
        "ref": entry.ref, "kind": entry.kind, "created": entry.created, "text": entry.text,
        "because": list(entry.because), "reverses_if": entry.reverses_if, "verdict": entry.verdict,
        "audience": entry.audience, "unresolved_numbers": list(entry.unresolved_numbers),
        "by": entry.actor.client or entry.actor.kind, "engine": entry.actor.engine,
    }


def journal_cards(settings: Settings) -> tuple[JournalCard, ...]:
    store = ProjectStore(settings)
    cards = []
    for name in store.names():
        try:
            meta, project_dir = store.meta(name), store.require(name)
        except ProjectError:
            continue
        journal = Journal(project_dir, name)
        entries = journal.entries(limit=MAX_ENTRIES)
        if not entries and not (project_dir / "journal" / "handoff.md").exists():
            continue  # a brick-era project without bench work
        checkpoint = CheckpointStore(project_dir)
        prefix = f"jfig/{slug(name)}"
        data = tuple(cell_data(e, prefix) if isinstance(e, CellEntry) else note_data(e) for e in entries)
        figures = tuple((str(journal.folder / o.image), _figure_path(prefix, o.image))
                        for e in entries if isinstance(e, CellEntry) for o in e.outputs if o.image)
        failed = sum(1 for e in data if any(c["status"] in ("fail", "error") for c in e.get("checks", [])))
        ids = journal.folder / "ids"
        cards.append(JournalCard(
            project=name, question=meta.question, disposition=checkpoint.read().disposition,
            next_action=checkpoint.read().next_action, handoff=checkpoint.read_handoff(), entries=data,
            cells=sum(1 for p in ids.iterdir() if p.name.startswith("c")) if ids.is_dir() else 0,
            failed_checks=failed, notebook=f"jnb/{slug(name)}", figures=figures,
        ))
    return tuple(cards)


def bench_panel(settings: Settings, jobs: tuple[QueueJob, ...], overview: Overview | None,
                cards: tuple[JournalCard, ...]) -> BenchPanel:
    from ..bench.workbench import WORKBENCH

    record = read_json(settings.bench_dir / "workbench.json") or {}
    workbench_job = next((j for j in jobs if j.name == f"{settings.job_prefix}-{WORKBENCH}"), None)
    alerts = list(_bench_alerts(settings, record, workbench_job))
    alerts += _slot_alerts(jobs)
    alerts += [f"{c.project}: {c.failed_checks} cell(s) with failed checks" for c in cards if c.failed_checks]
    alerts += _quota_alerts(overview)
    if workbench_job is not None:
        where = f" on {workbench_job.node}" if getattr(workbench_job, "node", "") else ""
        state = f"The workbench is {workbench_job.state.lower()}{where} (job {workbench_job.job_id})."
    else:
        state = f"No workbench is running ({record.get('state', 'never started')})."
    return BenchPanel(workbench=state, alerts=tuple(alerts))


def _bench_alerts(settings: Settings, record: dict, workbench_job: QueueJob | None) -> list[str]:
    alerts = []
    if (settings.bench_dir / "STOP").exists():
        alerts.append("The bench is stopped (bench/STOP): nothing runs until it is removed.")
    if record.get("state") == "crashed":
        alerts.append(f"The workbench failed: {record.get('reason', '')}")
    waiting = Inbox(settings.bench_dir).pending() if settings.bench_dir.is_dir() else []
    if waiting and workbench_job is None:
        alerts.append(f"{len(waiting)} cell(s) wait and no workbench job is queued; the watchdog starts one.")
    return alerts


def _slot_alerts(jobs: tuple[QueueJob, ...]) -> list[str]:
    """One alert per partition where jobs wait for a running-job slot (QOSMaxJobsPerUserLimit)."""
    alerts = []
    for partition in sorted({j.partition for j in jobs if "QOSMaxJobsPerUserLimit" in j.reason}):
        running = [j.name for j in jobs if j.partition == partition and j.state == "RUNNING"]
        blocked = sum(1 for j in jobs if j.partition == partition and "QOSMaxJobsPerUserLimit" in j.reason)
        taken = "Both job slots" if len(running) == 2 else "The job slots"
        alerts.append(f"{taken} on {partition} are taken ({', '.join(running)}); {blocked} job(s) wait.")
    return alerts


def _quota_alerts(overview: Overview | None) -> list[str]:
    alerts = []
    for storage in overview.storage if overview else ():
        if storage.limit_gb and storage.used_gb and storage.used_gb / storage.limit_gb >= QUOTA_ALERT:
            alerts.append(f"{storage.name} is {storage.used_gb / storage.limit_gb:.0%} full.")
    return alerts

