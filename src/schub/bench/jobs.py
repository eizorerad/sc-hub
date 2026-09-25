"""The bench's own Slurm jobs: who sent them, what became of them.

    bench/jobs/<job id>.json        a %%slurm job still expected to report
    bench/jobs/done/<job id>.json   reported, or found ended without a result

Only jobs listed here can be cancelled through sc-hub. A job killed by Slurm
(time limit, out of memory, scancel) never writes its result; the watchdog finds it
ended and records that in the cell's journal entry, so no job stays "PENDING" in
the journal forever.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError

from ..config import Settings
from ..slurm import ACTIVE_STATES, Slurm, SlurmError
from ..state import Frozen
from .clock import stamp
from .fsio import create_json_exclusive, read_json
from .journal import Journal, JournalError

FINAL_JOB_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL",
                              "PREEMPTED", "BOOT_FAIL", "DEADLINE", "ENDED"})


class JobRecord(Frozen):
    job_id: str
    project: str
    ref: str
    job_dir: str


def _folder(settings: Settings, *parts: str) -> Path:
    folder = settings.bench_dir.joinpath("jobs", *parts)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def register(settings: Settings, record: JobRecord) -> None:
    create_json_exclusive(_folder(settings) / f"{record.job_id}.json", record.model_dump())


def lookup(settings: Settings, job_id: str) -> JobRecord | None:
    if not job_id.isdigit():
        return None
    for path in (_folder(settings) / f"{job_id}.json", _folder(settings, "done") / f"{job_id}.json"):
        data = read_json(path)
        if data is not None:
            try:
                return JobRecord.model_validate(data)
            except ValidationError:
                return None
    return None


def open_records(settings: Settings) -> list[JobRecord]:
    found = []
    for path in sorted(_folder(settings).glob("*.json")):
        data = read_json(path)
        try:
            found.append(JobRecord.model_validate(data)) if data is not None else None
        except ValidationError:
            continue
    return found


def close(settings: Settings, job_id: str) -> None:
    try:
        os.replace(_folder(settings) / f"{job_id}.json", _folder(settings, "done") / f"{job_id}.json")
    except FileNotFoundError:
        pass


# matched in lower case: this cluster's Slurm writes "Detected 1 oom_kill event ... OOM Killed", others "oom-kill"
# (a GPU's "CUDA out of memory" is the cell's own error, not Slurm's memory limit)
LOG_ENDINGS = (("due to time limit", "TIMEOUT"), ("oom-kill", "OUT_OF_MEMORY"), ("oom_kill", "OUT_OF_MEMORY"),
               ("oom killed", "OUT_OF_MEMORY"), ("out of memory", "OUT_OF_MEMORY"), ("due to preemption", "PREEMPTED"),
               ("due to node failure", "NODE_FAIL"), ("cancelled at", "CANCELLED"))


def _reported(record: JobRecord) -> bool:
    return (Path(record.job_dir) / "result.json").exists()


def reap(settings: Settings, slurm: Slurm) -> list[str]:
    """Close jobs that reported; record the ones Slurm ended without a result. Returns those.

    A job counts as ended only when the queue answered and does not list it: when Slurm
    cannot be asked, nothing is decided. sacct does not work here and scontrol forgets
    finished jobs after minutes, so the reason is read from the job's own log (Slurm
    writes "CANCELLED ... DUE TO TIME LIMIT" there)."""
    records = open_records(settings)
    for record in [r for r in records if _reported(r)]:
        close(settings, record.job_id)
    waiting = [r for r in records if not _reported(r)]
    if not waiting:
        return []
    try:
        active = {j.job_id for j in slurm.my_jobs()}
    except SlurmError:
        return []
    ended = []
    for record in waiting:
        if record.job_id in active or _reported(record):  # still queued, or it reported just now
            continue
        _record_end(settings, record, *_final_state(slurm, record))
        close(settings, record.job_id)
        ended.append(record.job_id)
    return ended


def _final_state(slurm: Slurm, record: JobRecord) -> tuple[str, str]:
    """(state, log): Slurm's word if it still knows the job, else the log's last lines. A job without a log
    never started (cancelled while queued, or it could not launch)."""
    log = Path(record.job_dir) / f"slurm-{record.job_id}.log"
    try:
        known = slurm.states([record.job_id]).get(record.job_id)
    except SlurmError:
        known = None
    if known and known not in ACTIVE_STATES:
        return known, str(log) if log.exists() else ""
    try:
        with log.open("rb") as handle:
            handle.seek(max(0, log.stat().st_size - 8192))
            tail = handle.read().decode(errors="replace").lower()
            tail = tail.replace("cuda out of memory", "cuda oom").replace("cuda error: out of memory", "cuda oom")
    except FileNotFoundError:
        return "ENDED (never started: cancelled or not launched while queued)", ""
    except OSError:
        return "ENDED", str(log)
    return next((state for marker, state in LOG_ENDINGS if marker in tail), "ENDED"), str(log)


def _record_end(settings: Settings, record: JobRecord, state: str, log: str) -> None:
    cid = record.ref.partition("#")[2]
    journal = Journal(settings.projects_dir / record.project, record.project)
    try:
        journal.add_addendum(cid, f"jobend-{record.job_id}", {"kind": "job", "source": "watchdog", "job": {
            "job_id": record.job_id, "state": state if state != "COMPLETED" else "ENDED", "finished": stamp(),
            "log": log}})
    except (JournalError, OSError):
        pass
