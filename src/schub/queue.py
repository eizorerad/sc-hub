"""sc-hub's own submission queue, for students who run many experiments.

sc-hub caps the pipelines a student has active at once (Limits.max_active_runs).
Above the cap a plan waits in `queue/` and is submitted, first in first out, when
a pipeline ends. There is no daemon: while plans wait, one tiny Slurm job
("<prefix>-pump", pending) waits on the last step of every active pipeline (any of
them ending wakes it), submits what fits and, if plans remain, schedules the next
pump before it exits. Submitting, checking a run's status and every dashboard
build also pump (at most every PUMP_EVERY_S seconds).

Everything that changes the queue, including scheduling the pump, happens under
one lock, so a pump that finds the queue empty and an enqueue cannot miss each
other. A plan that can no longer run goes to queue/failed/ with the reason; a
passing problem (Slurm busy, a Lustre hiccup) leaves it waiting for the next pump.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from .bricks import Resources
from .config import Settings
from .locking import LockTimeout, exclusive
from .planner import Plan
from .runs import LimitExceeded, RunError, RunManifest
from .slurm import JobSpec, Slurm, SlurmError, render_script
from .state import Frozen

PUMP_RESOURCES = Resources(cpus=1, mem_gb=1, time_min=5)
PUMP_EVERY_S = 30  # opportunistic pumps (status checks, dashboard builds) at most this often
LOCK_STALE_S = 900  # a pump submits several plans under the lock; never break it early
# Passing failures (Slurm down for maintenance, Lustre) are retried by every pump; a
# plan is set aside only after failing this long without a break.
GIVE_UP_AFTER_S = 6 * 3600
KEEP_PUMP_LOGS = 20


class QueuedSubmission(Frozen):
    status: Literal["queued"] = "queued"
    plan_id: str
    project: str | None = None
    branch: str | None = None
    queued_at: str
    force_new: bool = False
    attempts: int = 0
    failing_since: float | None = None
    last_error: str = ""
    position: int = 0
    message: str = ""


class QueueFailure(Frozen):
    plan_id: str
    project: str | None = None
    branch: str | None = None
    queued_at: str
    failed_at: str
    message: str


class SubmitResult(Frozen):
    """What an agent gets back from a submission: running now, or waiting in the queue."""

    status: Literal["submitted", "queued"]
    plan_id: str
    run_id: str | None = None
    project: str | None = None
    branch: str | None = None
    position: int | None = None
    message: str = ""
    run: RunManifest | None = None


def submit_result(outcome: RunManifest | QueuedSubmission) -> SubmitResult:
    if isinstance(outcome, QueuedSubmission):
        return SubmitResult(status="queued", plan_id=outcome.plan_id, project=outcome.project, branch=outcome.branch,
                            position=outcome.position, message=outcome.message)
    return SubmitResult(status="submitted", plan_id=outcome.plan_id, run_id=outcome.run_id, project=outcome.project,
                        branch=outcome.branch, message="submitted as a Slurm dependency chain; follow it with run_status",
                        run=outcome)


class QueueView(Frozen):
    waiting: tuple[QueuedSubmission, ...] = ()
    failed: tuple[QueueFailure, ...] = ()


class PumpResult(Frozen):
    submitted: tuple[RunManifest, ...] = ()
    failed: tuple[QueueFailure, ...] = ()
    waiting: int = 0
    pump_job: str | None = None
    note: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _write_atomic(path: Path, text: str) -> None:
    """A reader never sees half an entry; a failed write (quota) leaves no stray file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, partial = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(partial, path)
    except BaseException:
        Path(partial).unlink(missing_ok=True)
        raise


class SubmitQueue:
    def __init__(self, settings: Settings, slurm: Slurm, submit: Callable[[str, bool], RunManifest],
                 permanent: tuple[type[Exception], ...] = ()) -> None:
        self.settings = settings
        self.slurm = slurm
        self._submit = submit
        self._permanent = permanent  # errors after which retrying cannot help
        self.dir = settings.root / "queue"
        self._step_job = re.compile(rf"^{re.escape(settings.job_prefix)}-([0-9a-f]{{6}})-(\d{{2}})-")

    def _lock(self, wait_s: float = 30.0) -> AbstractContextManager[None]:
        return exclusive(self.dir / ".lock", wait_s=wait_s, stale_after_s=LOCK_STALE_S)

    # ---- reading -------------------------------------------------------------

    def _files(self) -> list[Path]:
        return sorted(self.dir.glob("*.json")) if self.dir.is_dir() else []

    def _message(self, position: int) -> str:
        return (f"{self.settings.limits.max_active_runs} pipelines are active, the most at once; sc-hub submits "
                f"this plan when one ends (position {position} in the queue). There is no need to submit it again.")

    def entries(self) -> list[QueuedSubmission]:
        found = []
        for path in self._files():
            try:
                found.append(QueuedSubmission(**json.loads(path.read_text())))
            except (OSError, ValueError, TypeError):
                continue  # set aside by the next pump, with the reason
        return [e.model_copy(update={"position": i, "message": self._message(i)}) for i, e in enumerate(found, start=1)]

    def find(self, plan_id: str) -> QueuedSubmission | None:
        return next((e for e in self.entries() if e.plan_id == plan_id), None)

    def find_plan(self, plan: Plan) -> QueuedSubmission | None:
        """The entry of this plan, or of its branch (a branch waits once, whatever its plan)."""
        def same(e: QueuedSubmission) -> bool:
            return e.plan_id == plan.plan_id or (bool(plan.branch) and (e.project, e.branch) == (plan.project, plan.branch))
        return next((e for e in self.entries() if same(e)), None)

    def failed(self) -> list[QueueFailure]:
        folder = self.dir / "failed"
        found = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            try:
                found.append(QueueFailure.model_validate_json(path.read_text()))
            except (OSError, ValueError):
                continue
        return found

    # ---- changing --------------------------------------------------------------

    def enqueue(self, plan: Plan, force_new: bool = False) -> QueuedSubmission:
        with self._lock():
            if self.find_plan(plan) is None:
                data = {"plan_id": plan.plan_id, "project": plan.project, "branch": plan.branch,
                        "queued_at": _now(), "force_new": force_new}
                _write_atomic(self.dir / f"{time.time_ns()}-{plan.plan_id}.json", json.dumps(data))
            self._schedule_pump_quietly()
            queued = self.find_plan(plan)
        if queued is None:
            raise RunError(f"could not queue plan {plan.plan_id} in {self.dir}")
        return queued

    def cancel(self, plan_id: str) -> bool:
        with self._lock():
            paths = [p for p in self._files() if p.name.endswith(f"-{plan_id}.json")]
            for path in paths:
                path.unlink(missing_ok=True)
        return bool(paths)

    def due(self) -> bool:
        """Plans wait and no pump ran in the last PUMP_EVERY_S seconds."""
        if not self._files():
            return False
        try:
            return time.time() - (self.dir / ".last-pump").stat().st_mtime >= PUMP_EVERY_S
        except OSError:
            return True

    def pump(self) -> PumpResult:
        """Submit waiting plans in order while there is room; schedule the next pump."""
        if not self._files():
            return PumpResult()
        submitted: list[RunManifest] = []
        failed: list[QueueFailure] = []
        try:
            with self._lock(wait_s=5):
                (self.dir / ".last-pump").touch()
                note = self._drain(submitted, failed)
                waiting = len(self._files())
                job = self._schedule_pump_quietly() if waiting else None
                self._prune_logs()
        except LockTimeout:
            return PumpResult(waiting=len(self._files()), note="another pump is at work")
        return PumpResult(submitted=tuple(submitted), failed=tuple(failed), waiting=waiting, pump_job=job, note=note)

    def _drain(self, submitted: list[RunManifest], failed: list[QueueFailure]) -> str:
        for path in self._files():
            outcome = self._pump_one(path)
            if isinstance(outcome, str):
                return outcome  # at the cap, or a passing problem: the next pump goes on
            (submitted if isinstance(outcome, RunManifest) else failed).append(outcome)
        return ""

    def _set_aside(self, path: Path, data: dict[str, Any], message: str) -> QueueFailure:
        failure = QueueFailure(plan_id=str(data.get("plan_id", path.stem)), project=data.get("project"),
                               branch=data.get("branch"), queued_at=str(data.get("queued_at", "")), failed_at=_now(),
                               message=message[:500])
        _write_atomic(self.dir / "failed" / path.name, failure.model_dump_json())
        path.unlink(missing_ok=True)
        return failure

    def _pump_one(self, path: Path) -> RunManifest | QueueFailure | str:
        try:
            data = json.loads(path.read_text())
            plan_id = str(data["plan_id"])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return self._set_aside(path, {}, f"unreadable queue entry ({type(exc).__name__})")
        try:
            manifest = self._submit(plan_id, bool(data.get("force_new")))
        except LimitExceeded:
            return "at the cap of active pipelines"
        except self._permanent as exc:
            return self._set_aside(path, data, str(exc))
        except (RunError, SlurmError, OSError, ValueError) as exc:  # Slurm busy, a lock, Lustre: try again later
            attempts = int(data.get("attempts", 0)) + 1
            since = float(data.get("failing_since") or time.time())
            if time.time() - since >= GIVE_UP_AFTER_S:
                return self._set_aside(path, data, f"gave up after {attempts} attempts in {GIVE_UP_AFTER_S // 3600} h: {exc}")
            _write_atomic(path, json.dumps({**data, "attempts": attempts, "failing_since": since,
                                            "last_error": str(exc)[:300]}))
            return f"passing problem, will retry: {exc}"
        path.unlink(missing_ok=True)
        return manifest

    # ---- the pump job ------------------------------------------------------------

    def _schedule_pump_quietly(self) -> str | None:
        """Called under the lock. A Slurm hiccup must not lose the entry just written:
        the next status check or dashboard build schedules the pump again."""
        try:
            return self.ensure_pump()
        except (SlurmError, OSError):
            return None

    def ensure_pump(self) -> str | None:
        """One pending pump, waiting for any active pipeline's last step to end. A running
        pump does not count: it may have found the queue empty and be about to exit."""
        me = os.environ.get("SLURM_JOB_ID", "")
        jobs = [j for j in self.slurm.active(self.settings.job_prefix) if j.job_id != me]
        pump_name = f"{self.settings.job_prefix}-pump"
        if (pending := next((j.job_id for j in jobs if j.name == pump_name and j.state == "PENDING"), None)) is not None:
            return pending
        last: dict[str, tuple[int, str]] = {}
        for job in jobs:
            if (m := self._step_job.match(job.name)) is not None:
                step = int(m.group(2))
                if m.group(1) not in last or step > last[m.group(1)][0]:
                    last[m.group(1)] = (step, job.job_id)
        if not last:
            return None  # nothing active: the next sc-hub call submits directly
        return self._submit_pump(tuple(sorted(job for _, job in last.values())))

    def _submit_pump(self, after: tuple[str, ...]) -> str:
        s = self.settings
        env = (("SCHUB_ROOT", str(s.root)), ("SCHUB_LIBRARY", str(s.library or "")),
               ("SCHUB_PARTITION", s.partition), ("SCHUB_PYTHON", str(s.python)),
               ("SCHUB_MAX_ACTIVE_RUNS", str(s.limits.max_active_runs)), ("PYTHONUNBUFFERED", "1"))
        logs = self.dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        spec = JobSpec(name=f"{s.job_prefix}-pump", partition=s.partition, resources=PUMP_RESOURCES,
                       log_path=logs / "pump-%j.log", workdir=self.dir,
                       command=(str(s.python), "-m", "schub.cli", "pump"), env=env, after_any=after)
        script = logs / f"pump-{time.time_ns()}.sbatch"  # never shared by two submissions
        script.write_text(render_script(spec))
        return self.slurm.submit(script)

    def _prune_logs(self) -> None:
        folder = self.dir / "logs"
        files = sorted(folder.glob("pump-*"), key=lambda p: p.stat().st_mtime) if folder.is_dir() else []
        for path in files[: max(0, len(files) - 2 * KEEP_PUMP_LOGS)]:
            path.unlink(missing_ok=True)
