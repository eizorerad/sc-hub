"""Thin, testable wrapper over sbatch/sacct/squeue/scancel/sinfo.

All commands go through an injectable runner, so tests never need a cluster.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .bricks import Resources
from .state import Frozen

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]

JOB_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
SCONTROL_STATE = re.compile(r"\bJobState=([A-Z_]+)")
ACTIVE_STATES = frozenset({"PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "REQUEUED", "SUSPENDED"})
FAILED_STATES = frozenset(
    {"FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "PREEMPTED"}
)


class SlurmError(RuntimeError):
    pass


def run_command(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    try:
        return subprocess.run(list(args), capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SlurmError(f"could not run {args[0]}: {exc}") from exc


@dataclass(frozen=True)
class JobSpec:
    name: str
    partition: str
    resources: Resources
    log_path: Path
    workdir: Path
    command: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    dependency: tuple[str, ...] = ()


class PartitionInfo(Frozen):
    name: str
    available: str
    time_limit: str
    allocated: int
    idle: int
    other: int
    total: int
    gres: str


class ActiveJob(Frozen):
    job_id: str
    name: str
    state: str


class QueueJob(Frozen):
    job_id: str
    name: str
    state: str
    elapsed: str
    reason: str
    partition: str
    time_limit: str = ""


def format_time(minutes: int) -> str:
    hours, mins = divmod(max(1, int(minutes)), 60)
    return f"{hours:02d}:{mins:02d}:00"


def render_script(spec: JobSpec) -> str:
    if not JOB_NAME.match(spec.name):
        raise ValueError(f"invalid job name {spec.name!r}")
    r = spec.resources
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={spec.name}",
        f"#SBATCH --partition={spec.partition}",
        f"#SBATCH --cpus-per-task={r.cpus}",
        f"#SBATCH --mem={r.mem_gb}G",
        f"#SBATCH --time={format_time(r.time_min)}",
        f"#SBATCH --output={spec.log_path}",
    ]
    if r.gpus:
        lines.append(f"#SBATCH --gres=gpu:{r.gpus}")
    if spec.dependency:
        lines.append(f"#SBATCH --dependency=afterok:{':'.join(spec.dependency)}")
        lines.append("#SBATCH --kill-on-invalid-dep=yes")
    lines.append("set -euo pipefail")
    lines += [f"export {key}={shlex.quote(value)}" for key, value in spec.env]
    lines.append(f"cd {shlex.quote(str(spec.workdir))}")
    lines.append("exec " + " ".join(shlex.quote(part) for part in spec.command))
    return "\n".join(lines) + "\n"


def parse_job_id(stdout: str) -> str:
    job_id = stdout.strip().split(";")[0]
    if not job_id.isdigit():
        raise SlurmError(f"unexpected sbatch output: {stdout!r}")
    return job_id


def _rows(stdout: str) -> list[list[str]]:
    return [line.split("|") for line in stdout.splitlines() if line.strip()]


class Slurm:
    def __init__(self, runner: Runner = run_command) -> None:
        self._run = runner

    def _checked(self, args: Sequence[str]) -> str:
        proc = self._run(args)
        if proc.returncode != 0:
            raise SlurmError(f"{args[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    def submit(self, script: Path) -> str:
        return parse_job_id(self._checked(["sbatch", "--parsable", str(script)]))

    def states(self, job_ids: Sequence[str]) -> dict[str, str]:
        """Best-effort job states: sacct, then squeue, then scontrol.

        sacct needs the accounting database, which some login nodes cannot reach;
        squeue knows queued/running jobs and scontrol recently finished ones.
        Ids unknown to all three are omitted; callers decide from step markers.
        """
        if not job_ids:
            return {}
        states = self._sacct_states(job_ids)
        missing = [j for j in job_ids if j not in states]
        if missing:
            # squeue exits non-zero for ids it no longer knows; keep whatever it printed.
            queued = self._run(["squeue", "-h", "-j", ",".join(missing), "-o", "%i|%T"]).stdout
            states = {**states, **{row[0]: row[1] for row in _rows(queued) if len(row) >= 2}}
        for job_id in [j for j in job_ids if j not in states]:
            found = SCONTROL_STATE.search(self._run(["scontrol", "show", "job", "-o", job_id]).stdout)
            if found:
                states = {**states, job_id: found.group(1)}
        return states

    def _sacct_states(self, job_ids: Sequence[str]) -> dict[str, str]:
        try:
            out = self._checked(["sacct", "-n", "-P", "-X", "-j", ",".join(job_ids), "--format=JobID,State"])
        except SlurmError:
            return {}  # accounting database unreachable from this node
        return {row[0]: row[1].split()[0] for row in _rows(out) if len(row) >= 2 and row[1]}

    def cancel(self, job_ids: Sequence[str]) -> None:
        if job_ids:
            self._checked(["scancel", *job_ids])

    def active(self, prefix: str) -> list[ActiveJob]:
        out = self._checked(["squeue", "--me", "-h", "-o", "%i|%j|%T"])
        return [
            ActiveJob(job_id=r[0], name=r[1], state=r[2])
            for r in _rows(out)
            if len(r) >= 3 and r[1].startswith(f"{prefix}-")
        ]

    def my_jobs(self) -> list[QueueJob]:
        out = self._checked(["squeue", "--me", "-h", "-o", "%i|%j|%T|%M|%R|%P|%l"])
        return [
            QueueJob(job_id=r[0], name=r[1], state=r[2], elapsed=r[3], reason=r[4], partition=r[5],
                     time_limit=r[6] if len(r) > 6 else "")
            for r in _rows(out)
            if len(r) >= 6
        ]

    def partitions(self) -> list[PartitionInfo]:
        out = self._checked(["sinfo", "-h", "-o", "%P|%a|%l|%F|%G"])
        infos = []
        for row in _rows(out):
            if len(row) < 5:
                continue
            counts = [int(x) for x in row[3].split("/")] if row[3].count("/") == 3 else [0, 0, 0, 0]
            infos.append(
                PartitionInfo(
                    name=row[0].rstrip("*"),
                    available=row[1],
                    time_limit=row[2],
                    allocated=counts[0],
                    idle=counts[1],
                    other=counts[2],
                    total=counts[3],
                    gres=row[4],
                )
            )
        return infos
