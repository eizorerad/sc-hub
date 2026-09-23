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


def reap(settings: Settings, slurm: Slurm) -> list[str]:
    """Close jobs that reported; record the ones Slurm ended without a result. Returns those."""
    records = open_records(settings)
    waiting = [r for r in records if not (Path(r.job_dir) / "result.json").exists()]
    for record in records:
        if record not in waiting:
            close(settings, record.job_id)
    if not waiting:
        return []
    try:
        states = slurm.states([r.job_id for r in waiting])
    except SlurmError:
        return []  # unsure: look again next time
    ended = []
    for record in waiting:
        state = states.get(record.job_id, "ENDED")
        if state in ACTIVE_STATES:
            continue
        _record_end(settings, record, state)
        close(settings, record.job_id)
        ended.append(record.job_id)
    return ended


def _record_end(settings: Settings, record: JobRecord, state: str) -> None:
    cid = record.ref.partition("#")[2]
    journal = Journal(settings.projects_dir / record.project, record.project)
    try:
        journal.add_addendum(cid, f"job-{record.job_id}", {"kind": "job", "job": {
            "job_id": record.job_id, "state": state if state != "COMPLETED" else "ENDED", "finished": stamp(),
            "log": str(Path(record.job_dir) / f"slurm-{record.job_id}.log")}})
    except (JournalError, OSError):
        pass
