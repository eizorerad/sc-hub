"""What the Journal tab shows: each project's hand-over, checkpoint and newest
entries, where it stands (its group in the navigator), its outcome, the workbench's
state, and a few alerts worth a glance.

The student sees everything, including notes meant only for them (the assistant
does not). Alerts replace pull-only logs, VCC2026's weakest point: a stopped
workbench, full job slots, failed checks, lost cells, a quota nearly full.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..bench.checkpoint import CheckpointStore
from ..bench.fsio import read_json
from ..bench.inbox import Inbox, slug
from ..bench.journal import Journal
from ..bench.models import CellEntry, NoteEntry
from ..bench.report_store import ReportStore
from ..config import Settings
from ..overview import Overview
from ..projects import ProjectError, ProjectStore
from ..slurm import QueueJob
from ..state import Frozen

MAX_ENTRIES = 150
IDLE_DAYS = 7  # an unfinished project with no entry for this long is "quiet"
OUTCOME_CHARS = 700  # of a hand-over; a report's summary is short by design and shown whole (up to 2000)
SUMMARY_CHARS = 2000
BOLD = re.compile(r"\*\*(\S(?:.*?\S)?)\*\*")
ITALIC = re.compile(r"(?<![\w*])\*(\S(?:[^*]*?\S)?)\*(?![\w*])")
CODE = re.compile(r"`([^`\n]+)`")
HEADING = re.compile(r"^#{2,6} +", re.M)
GROUPS = ("blocked", "working", "done", "idle", "eval")  # most in need of the student first
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
    failed_checks: tuple[str, ...] = ()  # checks whose latest result fails (a later pass resolves one)
    notebook: str = ""  # the rendered notebook inside the view folder, without extension (.js, .ipynb)
    figures: tuple[tuple[str, str], ...] = ()  # (source file, path inside the view)
    reports: tuple[dict[str, Any], ...] = ()  # published reports, oldest first, with their paths in the view
    group: str = "working"  # working, blocked, done, idle (unfinished, quiet for a week) or eval
    updated: str = ""  # the newest entry or checkpoint change
    outcome: str = ""  # the latest report's summary, else the start of the hand-over
    outcome_from: str = ""  # "report" or "handoff"
    total_entries: int = 0  # entries in the journal (the card holds the newest MAX_ENTRIES)


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


def eval_projects(settings: Settings) -> set[str]:
    """Projects `schub eval-run` created (bench/evals/runs.jsonl)."""
    try:
        lines = (settings.bench_dir / "evals" / "runs.jsonl").read_text().splitlines()
    except OSError:
        return set()
    found = set()
    for line in lines:
        try:
            found.add(str(json.loads(line)["project"]))
        except (ValueError, KeyError, TypeError):
            continue
    return found


def _updated(entries: list, checkpoint_updated: str) -> str:
    times = [checkpoint_updated] + [t for e in entries for t in (e.created, getattr(e, "finished", None) or "")]
    return max(times, default="")


def _group(disposition: str, updated: str, evaluated: bool, now: datetime) -> str:
    if evaluated:
        return "eval"
    if disposition == "complete":
        return "done"
    if disposition == "blocked":
        return "blocked"
    try:
        when = datetime.fromisoformat(updated)
    except ValueError:
        return "working"  # no usable time: never hide a project for being quiet
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    quiet = now - when > timedelta(days=IDLE_DAYS)
    return "idle" if quiet else "working"


def _lead(text: str, limit: int = OUTCOME_CHARS) -> str:
    """Plain text (markdown emphasis and headings dropped), cut at a paragraph near `limit`."""
    lines = [line for line in text.strip().splitlines() if not line.startswith("# ")]
    body = HEADING.sub("", "\n".join(lines))
    for pattern in (BOLD, ITALIC, CODE):
        body = pattern.sub(r"\1", body)
    body = body.strip()
    if len(body) <= limit:
        return body
    cut = body.rfind("\n\n", 0, limit)
    return body[: cut if cut > limit // 3 else limit].rstrip() + " …"


def _outcome(project_dir: Any, reports: tuple[dict[str, Any], ...], handoff: str) -> tuple[str, str]:
    if reports:
        spec = read_json(project_dir / reports[-1]["folder"] / "spec.json") or {}
        if str(spec.get("summary", "")).strip():
            return _lead(str(spec["summary"]), SUMMARY_CHARS), "report"
    return (_lead(handoff), "handoff") if handoff.strip() else ("", "")


def journal_cards(settings: Settings) -> tuple[JournalCard, ...]:
    store = ProjectStore(settings)
    evaluated, now = eval_projects(settings), datetime.now(timezone.utc)
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
        failed = _failing_checks(data)
        ids = journal.folder / "ids"
        numbers = [int(p.name[1:]) for p in ids.iterdir() if p.name[:1] == "c" and p.name[1:].isdigit()] \
            if ids.is_dir() else []
        state, handoff = checkpoint.read(), checkpoint.read_handoff()
        reports = _reports(project_dir, name, numbers)
        updated = _updated(entries, state.updated)
        outcome, source = _outcome(project_dir, reports, handoff)
        notes = journal.notes_dir
        total = len(numbers) + (sum(1 for p in notes.glob("n*.json")) if notes.is_dir() else 0)
        cards.append(JournalCard(
            project=name, question=meta.question, disposition=state.disposition, next_action=state.next_action,
            handoff=handoff, entries=data, cells=len(numbers), failed_checks=failed, notebook=f"jnb/{slug(name)}",
            figures=figures, reports=reports, group=_group(state.disposition, updated, name in evaluated, now),
            updated=updated, outcome=outcome, outcome_from=source, total_entries=total,
        ))
    return tuple(cards)


def _reports(project_dir: Any, name: str, numbers: list[int]) -> tuple[dict[str, Any], ...]:
    """Each published report, its files and where the view keeps them, and how many cells came after it."""
    found = []
    for info in ReportStore(project_dir).published():
        covered = int(info.covers[1:]) if info.covers[1:].isdigit() else 0
        view = f"jrep/{slug(name)}/{info.folder.removeprefix('reports/')}"
        found.append({"title": info.title, "built": info.built, "warnings": info.warnings, "folder": info.folder,
                      "newer": sum(1 for n in numbers if n > covered), "view": view,
                      "notebook": str(project_dir / info.notebook),
                      "html": str(project_dir / info.html) if info.html else ""})
    return tuple(found)


def _failing_checks(data: tuple[dict, ...]) -> tuple[str, ...]:
    latest: dict[str, str] = {}
    for entry in data:  # oldest first
        for check in entry.get("checks", []):
            latest[check["name"]] = check["status"]
    return tuple(sorted(name for name, status in latest.items() if status in ("fail", "error")))


def bench_panel(settings: Settings, jobs: tuple[QueueJob, ...], overview: Overview | None,
                cards: tuple[JournalCard, ...]) -> BenchPanel:
    from ..bench.workbench import WORKBENCH

    record = read_json(settings.bench_dir / "workbench.json") or {}
    workbench_job = next((j for j in jobs if j.name == f"{settings.job_prefix}-{WORKBENCH}"), None)
    alerts = list(_bench_alerts(settings, record, workbench_job))
    alerts += _slot_alerts(jobs)
    alerts += [f"{c.project}: checks {', '.join(c.failed_checks)} are failing (their latest results)"
               if len(c.failed_checks) > 1 else f"{c.project}: check {c.failed_checks[0]} is failing (its latest result)"
               for c in cards if c.failed_checks and c.group in ("working", "blocked")]  # not finished or evaluation work
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

