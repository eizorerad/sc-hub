"""The bench's own Slurm jobs: who sent them, what became of them.

    bench/jobs/<job id>.json        a %%slurm job still expected to report
    bench/jobs/done/<job id>.json   its report (or its end) is in the journal

Only jobs listed here can be cancelled through sc-hub. A job killed by Slurm
(time limit, out of memory, scancel) never writes its result; the watchdog finds it
ended and records that in the cell's journal entry, so no job stays "PENDING" in
the journal forever. A job stays listed until the journal has taken all of it: a
write Lustre refused (EIO) is tried again on the watchdog's next pass.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from pydantic import ValidationError

from ..config import Settings
from ..slurm import ACTIVE_STATES, Slurm, SlurmError
from ..state import Frozen
from .clock import stamp
from .fsio import create_json_exclusive, read_json
from .jobrun import to_journal
from .journal import Journal, JournalError

FINAL_JOB_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "NOT_STARTED",
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

# How a job that ended without its result is explained to the agent and the student (nothing it would write exists)
BAD_ENDINGS = {
    "OUT_OF_MEMORY": "ran out of memory: send it again with a larger --mem",
    "TIMEOUT": "hit its time limit: send it again with a longer --time, or save checkpoints and resume",
    "NOT_STARTED": "never started (cancelled or not launched while queued): nothing ran; send it again if needed",
    "NODE_FAIL": "lost its node: send it again",
    "BOOT_FAIL": "lost its node: send it again",
    "DEADLINE": "passed its deadline: send it again",
    "FAILED": "failed: its log and this entry's outputs show the error; fix it and send the job again",
    "PREEMPTED": "was preempted by a higher-priority job: send it again",
    "ENDED": "ended without reporting its result: its log says why",
}


def bad_ending(state: str) -> str | None:
    """Why a job left nothing, for a state that means so (None for a running or completed one)."""
    if state.startswith("ENDED (never started"):  # the wording of records written before NOT_STARTED
        return BAD_ENDINGS["NOT_STARTED"]
    return BAD_ENDINGS.get(state.split(" (")[0])

def _reported(record: JobRecord) -> bool:
    return (Path(record.job_dir) / "result.json").exists()


GIVE_UP_S = 3 * 86400.0  # a report the journal has refused this long is not going in: the journal cannot be written


def _given_up(report: Path) -> bool:
    """A job whose report has waited this long for the journal stops keeping the watchdog armed (its result.json
    stays on disk)."""
    try:
        return time.time() - report.stat().st_mtime > GIVE_UP_S
    except OSError:
        return False


def _delivered(settings: Settings, record: JobRecord) -> bool:
    """Whether all the job reported is in its cell's journal entry, and numbered (see the journal's docstring).
    The job saves result.json first, then adds its parts (state, files, downloads, checks) one by one: a part it
    could not add is added here from result.json. Each part is added once, by its name, so this only fills gaps;
    the cell's change is numbered once more, since a part added without its number is 'already there' to the
    retry. False while the journal cannot be written, and after an error that is not a file's (a bug must not
    close the job): tried again until the report is given up."""
    job_dir, cid = Path(record.job_dir), record.ref.partition("#")[2]
    report = job_dir / "result.json"
    try:
        result = json.loads(report.read_text())
        if isinstance(result, dict):  # (anything else is not a report)
            to_journal(settings, {"project": record.project, "cid": cid}, job_dir, result)
            journal = Journal(settings.projects_dir / record.project, record.project)
            if journal.raw_cell(cid) is not None and not journal.note_change(cid):
                return _given_up(report)
    except (ValueError, KeyError, JournalError):
        pass  # not a report, or no journal entry takes it: no later pass could add it
    except Exception:  # noqa: BLE001 - OSError, and whatever else: the job stays open for another pass
        return _given_up(report)
    return True


def reap(settings: Settings, slurm: Slurm) -> list[str]:
    """Close jobs whose report is in the journal; record the ones Slurm ended without a result. Returns those.

    A job counts as ended only when the queue answered and does not list it: when Slurm
    cannot be asked, nothing is decided. sacct does not work here and scontrol forgets
    finished jobs after minutes, so the reason is read from the job's own log (Slurm
    writes "CANCELLED ... DUE TO TIME LIMIT" there). A job whose journal write failed
    stays open for the next pass."""
    records = open_records(settings)
    for record in [r for r in records if _reported(r)]:
        if _delivered(settings, record):
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
        if _record_end(settings, record, *_final_state(slurm, record)):
            close(settings, record.job_id)
            ended.append(record.job_id)
        elif _given_up(_end_marker(record)):
            close(settings, record.job_id)  # the journal has refused its end for days
    return ended


def _end_marker(record: JobRecord) -> Path:
    """What dates a job's end: its log, else (it never started) its folder."""
    log = Path(record.job_dir) / f"slurm-{record.job_id}.log"
    return log if log.exists() else Path(record.job_dir)


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
        return "NOT_STARTED", ""  # cancelled while queued, or it could not launch: nothing ran
    except OSError:
        return "ENDED", str(log)
    return next((state for marker, state in LOG_ENDINGS if marker in tail), "ENDED"), str(log)


def _record_end(settings: Settings, record: JobRecord, state: str, log: str) -> bool:
    """False while the journal cannot be written (the end is recorded on a later pass)."""
    cid = record.ref.partition("#")[2]
    journal = Journal(settings.projects_dir / record.project, record.project)
    try:
        journal.add_addendum(cid, f"jobend-{record.job_id}", {"kind": "job", "source": "watchdog", "job": {
            "job_id": record.job_id, "state": state if state != "COMPLETED" else "ENDED", "finished": stamp(),
            "log": log}})
        if journal.raw_cell(cid) is not None and not journal.note_change(cid):
            return False  # written, not numbered: the next pass numbers it
    except OSError:
        return False
    except JournalError:
        pass  # no journal entry takes it: no later pass could add it
    return True
