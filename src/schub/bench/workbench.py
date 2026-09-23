"""The workbench job (the runner) and its watchdog, as Slurm jobs.

- ensure(): start the workbench on demand; never a second one.
- arm_successor(): near the time limit, a successor waits on the current job
  (afterany), so it starts the moment the slot frees up. The count of existing
  workbench jobs leaves out the caller's own job: in VCC2026 a component that
  counted itself as its own successor let the chain die (incident A4).
- ensure_watchdog(): a 1-CPU job that reschedules itself (scrontab is disabled on
  the cluster and processes on the login node get killed).
- The off switch is bench/STOP: cancelling a job alone would just get it replaced.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from ..bricks import Resources
from ..config import Settings
from ..slurm import ACTIVE_STATES, JobSpec, QueueJob, Slurm, SlurmError, render_script
from ..state import Frozen
from .clock import Clock, seconds_between, stamp
from .fsio import read_json

WORKBENCH, WATCHDOG = "bench-workbench", "bench-watchdog"
PASS_THROUGH = ("SCHUB_ROOT", "SCHUB_LIBRARY", "SCHUB_PYTHON", "SCHUB_PARTITION", "SCHUB_EXTRA_ROOTS",
                "SCHUB_LEGACY_TOOLS", "PYTHONPATH")


class BenchStopped(RuntimeError):
    pass


class WorkbenchState(Frozen):
    job_id: str | None = None
    slurm_state: str | None = None  # PENDING, RUNNING... None: no job
    reason: str = ""  # why a job is pending
    runner: str = ""  # the runner's own record: running, idle-stopped, retired, stopped
    node: str = ""
    heartbeat: str = ""
    stopped: bool = False  # bench/STOP exists
    started_now: bool = False

    def summary(self) -> str:
        if self.stopped:
            return "The bench is stopped (bench/STOP exists); nothing runs until the student removes it."
        if self.slurm_state == "RUNNING":
            return f"The workbench runs on {self.node or 'a compute node'} (job {self.job_id})."
        if self.slurm_state == "PENDING":
            why = f" ({self.reason})" if self.reason else ""
            fresh = "was started and " if self.started_now else ""
            return f"The workbench {fresh}waits in the Slurm queue{why} (job {self.job_id})."
        return "No workbench is running; the next cell starts one."


def stop_path(settings: Settings) -> Path:
    return settings.bench_dir / "STOP"


class Workbench:
    def __init__(self, settings: Settings, slurm: Slurm, now: Clock = stamp) -> None:
        self.settings = settings
        self.slurm = slurm
        self.now = now

    def job_name(self, kind: str) -> str:
        return f"{self.settings.job_prefix}-{kind}"

    def jobs(self, kind: str) -> list[QueueJob]:
        return [j for j in self.slurm.my_jobs() if j.name == self.job_name(kind) and j.state in ACTIVE_STATES]

    def stopped(self) -> bool:
        return stop_path(self.settings).exists()

    def state(self, started_now: bool = False) -> WorkbenchState:
        record = read_json(self.settings.bench_dir / "workbench.json") or {}
        base = WorkbenchState(stopped=self.stopped(), started_now=started_now, runner=str(record.get("state", "")),
                              heartbeat=str(record.get("heartbeat", "")))
        active = sorted(self.jobs(WORKBENCH), key=lambda j: (j.state != "RUNNING", j.job_id))
        if not active:
            return base
        job = active[0]
        node = str(record.get("node", "")) if record.get("job_id") == job.job_id else ""
        return base.model_copy(update={"job_id": job.job_id, "slurm_state": job.state,
                                       "reason": job.reason.strip("()") if job.state == "PENDING" else "",
                                       "node": node})

    def ensure(self) -> WorkbenchState:
        if self.stopped():
            raise BenchStopped("the bench is stopped (bench/STOP exists); the student removes it to continue")
        if self.jobs(WORKBENCH):
            return self.state()
        self.submit_workbench()
        self.ensure_watchdog()
        return self.state(started_now=True)

    def submit_workbench(self, after: str | None = None) -> str:
        bench = self.settings.bench
        spec = self._spec(
            WORKBENCH, bench.partition, Resources(cpus=bench.cpus, mem_gb=bench.mem_gb, time_min=bench.hours * 60),
            ("-m", "schub.bench.runner"), signal=f"B:USR1@{bench.retire_before_end_s}",
            after_any=(after,) if after else (),
        )
        return self._submit(spec, "workbench")

    def arm_successor(self, own_job_id: str) -> str | None:
        """Called by a retiring runner: a successor unless one already waits."""
        if self.stopped():
            return None
        others = [j for j in self.jobs(WORKBENCH) if j.job_id != own_job_id]
        if others:
            return None
        return self.submit_workbench(after=own_job_id)

    def stop(self) -> tuple[str, ...]:
        """Free the slot now: the runner gets SIGTERM, marks a running cell retired and exits. The jobs cancelled."""
        cancelled = tuple(j.job_id for j in self.jobs(WORKBENCH))
        if cancelled:
            self.slurm.cancel(list(cancelled))
        return cancelled

    # ---- the watchdog ------------------------------------------------------------------

    def ensure_watchdog(self, own_job_id: str | None = None) -> str | None:
        if self.stopped():
            return None
        if any(j.job_id != own_job_id for j in self.jobs(WATCHDOG)):
            return None
        bench = self.settings.bench
        for partition in dict.fromkeys((bench.background_partition, bench.background_fallback)):
            spec = self._spec(WATCHDOG, partition, Resources(cpus=1, mem_gb=2, time_min=10),
                              ("-m", "schub.bench.watchdog"), begin=f"now+{bench.watchdog_every_min}minutes")
            try:
                return self._submit(spec, "watchdog")
            except SlurmError:
                continue  # the background partition may refuse; try the fallback
        return None

    def dormant(self) -> bool:
        """No workbench activity for dormant_after_h: the watchdog lets itself lapse."""
        record = read_json(self.settings.bench_dir / "workbench.json") or {}
        heartbeat = str(record.get("heartbeat", ""))
        if not heartbeat:
            return True
        try:
            return seconds_between(heartbeat, self.now()) > self.settings.bench.dormant_after_h * 3600
        except ValueError:
            return True

    # ---- plumbing ----------------------------------------------------------------------

    def _spec(self, kind: str, partition: str, resources: Resources, args: tuple[str, ...], **extra: object) -> JobSpec:
        s = self.settings
        env = [(k, os.environ[k]) for k in PASS_THROUGH if os.environ.get(k)]
        env += [(k, v) for k, v in sorted(os.environ.items()) if k.startswith("SCHUB_BENCH_")]
        env += [("SCHUB_ROOT", str(s.root)), ("SCHUB_PYTHON", str(s.python)), ("PYTHONUNBUFFERED", "1"),
                ("OMP_NUM_THREADS", str(resources.cpus))]
        if s.library:
            env.append(("SCHUB_LIBRARY", str(s.library)))
        logs = s.bench_dir / "logs"
        return JobSpec(name=self.job_name(kind), partition=partition, resources=resources,
                       log_path=logs / f"{kind.split('-')[-1]}-%j.log", workdir=s.root,
                       command=(str(s.python), *args), env=tuple(dict(env).items()), **extra)  # type: ignore[arg-type]

    def _submit(self, spec: JobSpec, label: str) -> str:
        scripts = self.settings.bench_dir / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (self.settings.bench_dir / "logs").mkdir(parents=True, exist_ok=True)
        script = scripts / f"{label}-{secrets.token_hex(4)}.sbatch"
        script.write_text(render_script(spec))
        return self.slurm.submit(script)
