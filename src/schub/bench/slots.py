"""How many of the student's running-job slots are taken, in plain words.

QOS ia-std on ws-ia allows two running jobs per user; an interactive personal-ws
job and the workbench can take both. Every bench answer says so, so a pending
workbench or %%slurm job is never a mystery.
"""

from __future__ import annotations

import json

from ..config import Settings
from ..slurm import QueueJob, Slurm, SlurmError
from ..state import Frozen


class SlotUsage(Frozen):
    partition: str
    running: int
    limit: int | None
    jobs: tuple[str, ...] = ()
    unknown: bool = False  # squeue failed

    @property
    def full(self) -> bool:
        return self.limit is not None and self.running >= self.limit

    def summary(self) -> str:
        if self.unknown:
            return f"{self.partition}: job slots unknown (squeue failed)"
        limit = "?" if self.limit is None else str(self.limit)
        state = " FULL" if self.full else ""
        listed = f": {', '.join(self.jobs)}" if self.jobs else ""
        return f"{self.partition} {self.running}/{limit} running{state}{listed}"


def running_limit(settings: Settings, partition: str) -> int:
    """Per-user running jobs from the cached cluster overview, else the configured default."""
    try:
        data = json.loads((settings.cache_dir / "overview.json").read_text())
    except (OSError, ValueError):
        return settings.bench.max_running_jobs
    for qos in data.get("limits", []):
        if partition not in qos.get("partitions", []):
            continue
        for limit in qos.get("limits", []):
            if limit.get("name") == "running jobs" and limit.get("limit") is not None:
                return int(limit["limit"])
    return settings.bench.max_running_jobs


def _label(job: QueueJob) -> str:
    reason = f" ({job.reason.strip('()')})" if job.state == "PENDING" and job.reason not in ("", "None", "(None)") else ""
    return f"{job.name} {job.state}{reason}"


def slot_usage(settings: Settings, slurm: Slurm, partition: str | None = None) -> SlotUsage:
    partition = partition or settings.bench.partition
    try:
        jobs = [j for j in slurm.my_jobs() if j.partition == partition]
    except SlurmError:
        return SlotUsage(partition=partition, running=0, limit=None, unknown=True)
    running = sum(1 for j in jobs if j.state == "RUNNING")
    return SlotUsage(partition=partition, running=running, limit=running_limit(settings, partition),
                     jobs=tuple(_label(j) for j in jobs))
