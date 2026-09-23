"""Submitting plans as Slurm dependency chains and tracking their runs.

Guarantees that matter when an agent is the one pressing the button:
  * resubmitting the same plan returns the existing run instead of new jobs;
  * a step whose output already exists (same key) is reused, not recomputed;
  * a step already queued by another run is depended on, not duplicated;
  * a failed step cancels its dependents (--kill-on-invalid-dep);
  * the number of concurrently active pipelines per user is capped.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import Settings
from .library import celltypist_dirs
from .locking import LockTimeout, exclusive
from .planner import Plan, PlannedStep
from .slurm import ACTIVE_STATES, FAILED_STATES, JobSpec, Slurm, render_script
from .state import Frozen
from .stepfile import ERROR_FILE, JOB_ID_FILE, STEP_FILE, SUCCESS, SUMMARY_FILE, StepFile

MAX_LOG_LINES = 400
MAX_LOG_BYTES = 20_000
LOST_STATES = frozenset({"UNKNOWN", "LOST", "MISSING"})


class RunError(RuntimeError):
    pass


class RunNotFound(RunError):
    pass


class LimitExceeded(RunError):
    pass


class StepRecord(Frozen):
    index: int
    brick: str
    step_key: str
    step_dir: str
    job_id: str | None = None
    reused: bool = False


class RunManifest(Frozen):
    run_id: str
    plan_id: str
    created_at: str
    dataset: str
    steps: tuple[StepRecord, ...]
    schub_version: str
    project: str | None = None
    branch: str | None = None


class StepStatus(Frozen):
    index: int
    brick: str
    state: str
    job_id: str | None
    reused: bool
    message: str | None = None


class RunStatus(Frozen):
    run_id: str
    plan_id: str
    state: str
    steps: tuple[StepStatus, ...]


class StepResults(Frozen):
    index: int
    brick: str
    summary: dict[str, Any] | None
    files: tuple[str, ...]


class RunResults(Frozen):
    run_id: str
    state: str
    final_output: str | None
    steps: tuple[StepResults, ...]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def overall_state(steps: tuple[StepStatus, ...]) -> str:
    states = [s.state for s in steps]
    if any(s in FAILED_STATES or s in LOST_STATES for s in states):
        return "FAILED"
    if states and all(s == "COMPLETED" for s in states):
        return "COMPLETED"
    if "RUNNING" in states:
        return "RUNNING"
    return "PENDING"


class RunStore:
    def __init__(
        self, settings: Settings, slurm: Slurm, clock: Callable[[], datetime] = _utcnow
    ) -> None:
        self.settings = settings
        self.slurm = slurm
        self._clock = clock

    # ---- submission -------------------------------------------------------

    def submit(self, plan: Plan, force_new: bool = False) -> RunManifest:
        if not plan.ok:
            errors = "; ".join(i.message for i in plan.issues if i.level == "error")
            raise RunError(f"plan {plan.plan_id} has errors: {errors}")
        try:
            with exclusive(self.settings.root / ".submit.lock"):
                return self._submit_locked(plan, force_new)
        except LockTimeout as exc:
            raise RunError(str(exc)) from exc

    def _submit_locked(self, plan: Plan, force_new: bool) -> RunManifest:
        existing = None if force_new else self._reusable_run(plan.plan_id)
        if existing is not None:
            return existing
        self._check_active_limit()
        created = self._clock()
        run_id = f"{created:%Y%m%d-%H%M%S}-{plan.plan_id[:6]}-{secrets.token_hex(2)}"
        records: list[StepRecord] = []
        prev_job: str | None = None
        prev_output = Path(plan.dataset)
        fresh_upstream = False
        try:
            for step in plan.steps:
                record, prev_job, prev_output = self._submit_step(
                    plan, step, prev_job, prev_output, allow_live=not fresh_upstream
                )
                fresh_upstream = fresh_upstream or (record.job_id is not None and not record.reused)
                records.append(record)
            manifest = RunManifest(
                run_id=run_id,
                plan_id=plan.plan_id,
                created_at=created.isoformat(),
                dataset=plan.dataset,
                steps=tuple(records),
                schub_version=__version__,
                project=plan.project,
                branch=plan.branch,
            )
            self._write_manifest(manifest, plan)
        except Exception:
            self.slurm.cancel([r.job_id for r in records if r.job_id and not r.reused])
            raise
        return manifest

    def _submit_step(
        self, plan: Plan, step: PlannedStep, prev_job: str | None, prev_output: Path, allow_live: bool
    ) -> tuple[StepRecord, str | None, Path]:
        step_dir = self.settings.steps_dir / step.step_key
        output = step_dir / "output.h5ad"
        base = {"index": step.index, "brick": step.brick, "step_key": step.step_key, "step_dir": str(step_dir)}
        if (step_dir / SUCCESS).exists():
            return StepRecord(**base, reused=True), None, output
        # Below a freshly submitted step, a live job for this key waits on a doomed
        # upstream job (it will be killed by --kill-on-invalid-dep), so never reuse it.
        live = self._live_job(step_dir) if allow_live else None
        if live is not None:
            return StepRecord(**base, job_id=live, reused=True), live, output
        step_dir.mkdir(parents=True, exist_ok=True)
        for stale in (ERROR_FILE, JOB_ID_FILE):
            (step_dir / stale).unlink(missing_ok=True)
        step_file = StepFile(
            brick=step.brick,
            version=step.version,
            code_id=step.code_id,
            params=step.params,
            input=str(prev_output),
            output=None if step.terminal else str(output),
            results_dir=str(step_dir / "results"),
            state_in=step.state_in,
            context={
                "celltypist_dirs": os.pathsep.join(str(d) for d in celltypist_dirs(self.settings)),
                "library_roots": os.pathsep.join(str(r) for r in self.settings.library_roots),
            },
        )
        (step_dir / STEP_FILE).write_text(step_file.model_dump_json(indent=2))
        script = step_dir / "job.sbatch"
        script.write_text(render_script(self._job_spec(plan, step, step_dir, prev_job)))
        job_id = self.slurm.submit(script)
        (step_dir / JOB_ID_FILE).write_text(job_id)
        return StepRecord(**base, job_id=job_id), job_id, output

    def _job_spec(self, plan: Plan, step: PlannedStep, step_dir: Path, prev_job: str | None) -> JobSpec:
        s = self.settings
        env = (
            ("OMP_NUM_THREADS", str(step.resources.cpus)),
            ("MPLBACKEND", "Agg"),
            ("PYTHONUNBUFFERED", "1"),
            ("CELLTYPIST_FOLDER", str(s.celltypist_home)),
            ("NUMBA_CACHE_DIR", str(s.cache_dir / "numba")),
            ("XDG_CACHE_HOME", str(s.cache_dir)),
            ("SCHUB_ROOT", str(s.root)),
            ("SCHUB_LIBRARY", str(s.library or "")),
        )
        return JobSpec(
            name=f"{s.job_prefix}-{plan.plan_id[:6]}-{step.index:02d}-{step.brick}",
            partition=s.partition,
            resources=step.resources,
            log_path=step_dir / "slurm-%j.log",
            workdir=step_dir,
            command=(str(s.python), "-m", "schub.execute", "--step-dir", str(step_dir)),
            env=env,
            dependency=(prev_job,) if prev_job else (),
        )

    def _live_job(self, step_dir: Path) -> str | None:
        job_file = step_dir / JOB_ID_FILE
        if not job_file.exists():
            return None
        job_id = job_file.read_text().strip()
        return job_id if self.slurm.states([job_id]).get(job_id) in ACTIVE_STATES else None

    def _check_active_limit(self) -> None:
        jobs = self.slurm.active(self.settings.job_prefix)
        # Pipeline steps are named <prefix>-<plan id>-<NN>-<brick>; sessions, imports
        # and downloads (<prefix>-session-..., -import-, -fetch-) are not pipelines.
        step = re.compile(rf"^{re.escape(self.settings.job_prefix)}-([0-9a-f]{{6}})-\d{{2}}-")
        pipelines = {m.group(1) for job in jobs if (m := step.match(job.name))}
        limit = self.settings.limits.max_active_runs
        if len(pipelines) >= limit:
            raise LimitExceeded(
                f"{len(pipelines)} pipelines are already active (limit {limit}); "
                "wait for them or cancel one."
            )

    def _reusable_run(self, plan_id: str) -> RunManifest | None:
        index = self.settings.plans_dir / f"{plan_id}.runs"
        if not index.exists():
            return None
        run_ids = [line for line in index.read_text().splitlines() if line.strip()]
        if not run_ids:
            return None
        latest = self.load(run_ids[-1])
        return None if self.status(latest.run_id).state == "FAILED" else latest

    def _write_manifest(self, manifest: RunManifest, plan: Plan) -> None:
        run_dir = self.settings.runs_dir / manifest.run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2))
        (run_dir / "plan.json").write_text(plan.model_dump_json(indent=2))
        for record in manifest.steps:
            (run_dir / f"{record.index:02d}_{record.brick}").symlink_to(record.step_dir)
        self.settings.plans_dir.mkdir(parents=True, exist_ok=True)
        with (self.settings.plans_dir / f"{plan.plan_id}.runs").open("a") as handle:
            handle.write(manifest.run_id + "\n")

    # ---- inspection -------------------------------------------------------

    def load(self, run_id: str) -> RunManifest:
        path = self.settings.runs_dir / run_id / "manifest.json"
        if not path.exists():
            raise RunNotFound(f"run '{run_id}' not found")
        return RunManifest.model_validate_json(path.read_text())

    def list_runs(self, limit: int = 10) -> list[RunManifest]:
        if not self.settings.runs_dir.is_dir():
            return []
        manifests = [
            RunManifest.model_validate_json(p.read_text())
            for p in self.settings.runs_dir.glob("*/manifest.json")
        ]
        return sorted(manifests, key=lambda m: m.created_at, reverse=True)[:limit]

    def status(self, run_id: str) -> RunStatus:
        manifest = self.load(run_id)
        pending = [
            s.job_id for s in manifest.steps if s.job_id and not (Path(s.step_dir) / SUCCESS).exists()
        ]
        states = self.slurm.states(pending)
        steps = tuple(self._step_status(s, states) for s in manifest.steps)
        return RunStatus(run_id=run_id, plan_id=manifest.plan_id, state=overall_state(steps), steps=steps)

    def _step_status(self, record: StepRecord, states: dict[str, str]) -> StepStatus:
        step_dir = Path(record.step_dir)
        base = {"index": record.index, "brick": record.brick, "job_id": record.job_id, "reused": record.reused}
        if (step_dir / SUCCESS).exists():
            return StepStatus(**base, state="COMPLETED")
        failure = _read_json(step_dir / ERROR_FILE)
        message = failure.get("message") if failure else None
        if record.job_id is None:
            return StepStatus(**base, state="MISSING", message="cached output was removed; resubmit")
        state = states.get(record.job_id, "UNKNOWN")
        if failure and state not in ACTIVE_STATES:
            return StepStatus(**base, state="FAILED", message=message)
        if state == "COMPLETED":
            return StepStatus(**base, state="LOST", message="job ended without a success marker")
        if state == "UNKNOWN":
            return StepStatus(**base, state="UNKNOWN", message="Slurm has no record of this job")
        return StepStatus(**base, state=state, message=message)

    def step_record(self, run_id: str, index: int) -> StepRecord:
        manifest = self.load(run_id)
        for record in manifest.steps:
            if record.index == index:
                return record
        raise RunError(f"run '{run_id}' has no step {index}")

    def logs(self, run_id: str, index: int, lines: int = 80) -> str:
        step_dir = Path(self.step_record(run_id, index).step_dir)
        logs = sorted(step_dir.glob("slurm-*.log"), key=lambda p: p.stat().st_mtime)
        if not logs:
            return "(no log yet: the job has not started)"
        with logs[-1].open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 4 * MAX_LOG_BYTES))
            text = handle.read().decode(errors="replace")
        tail = text.splitlines()[-min(lines, MAX_LOG_LINES) :]
        return "\n".join(tail)[-MAX_LOG_BYTES:]

    def results(self, run_id: str) -> RunResults:
        manifest = self.load(run_id)
        steps = []
        final_output = None
        for record in manifest.steps:
            step_dir = Path(record.step_dir)
            results_dir = step_dir / "results"
            files = tuple(sorted(str(p) for p in results_dir.glob("*") if p.is_file())) if results_dir.is_dir() else ()
            steps.append(
                StepResults(
                    index=record.index,
                    brick=record.brick,
                    summary=_read_json(results_dir / SUMMARY_FILE),
                    files=files,
                )
            )
            if (step_dir / "output.h5ad").exists():
                final_output = str(step_dir / "output.h5ad")
        return RunResults(
            run_id=run_id,
            state=self.status(run_id).state,
            final_output=final_output,
            steps=tuple(steps),
        )

    def cancel(self, run_id: str) -> RunStatus:
        status = self.status(run_id)
        shared = self._jobs_referenced_elsewhere(run_id)
        # Jobs this run reused, or that another run depends on, are left alone.
        active = [
            s.job_id
            for s in status.steps
            if s.job_id and not s.reused and s.job_id not in shared and s.state in ACTIVE_STATES
        ]
        self.slurm.cancel(active)
        return self.status(run_id)

    def _jobs_referenced_elsewhere(self, run_id: str) -> set[str]:
        if not self.settings.runs_dir.is_dir():
            return set()
        referenced: set[str] = set()
        for path in self.settings.runs_dir.glob("*/manifest.json"):
            other = RunManifest.model_validate_json(path.read_text())
            if other.run_id != run_id:
                referenced |= {s.job_id for s in other.steps if s.job_id}
        return referenced
