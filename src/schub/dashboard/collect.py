"""Gather everything the dashboard shows, using one squeue call and file markers.

No sacct (unreachable from the login node) and no per-run Slurm queries: a
step's state comes from its own markers, plus the queue for live jobs.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..datasets import DatasetEntry
from ..h5ad_profile import UnsupportedFile
from ..headlines import headline
from ..library import AssetLocation, celltypist_dirs, library_mode
from ..projects import ProjectError, ProjectSummary
from ..provenance import KEY_PACKAGES, _version, env_id
from ..runs import RunManifest
from ..slurm import QueueJob, SlurmError
from ..state import Frozen
from ..stepfile import ERROR_FILE, STEP_FILE, SUCCESS, SUMMARY_FILE
from .steps import StepExtras, step_extras, trained_model_size_mb

FAILED = frozenset({"FAILED", "STOPPED", "MISSING"})
RUN_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}-[0-9a-f]{4}")
STATE = re.compile(r"[A-Z_]{1,24}")
RUNS_WITH_LOGS = 10
MAX_RUNS = 100


class StepView(Frozen):
    index: int
    brick: str
    key: str
    state: str
    job_id: str | None = None
    headline: str = ""
    message: str = ""
    params: dict[str, Any] = {}
    summary: dict[str, Any] | None = None
    step_dir: str = ""
    extras: StepExtras = StepExtras()


class RunView(Frozen):
    run_id: str
    project: str | None
    branch: str | None
    created_at: str
    dataset: str
    state: str
    steps: tuple[StepView, ...]

    @property
    def label(self) -> str:
        if self.project and self.branch:
            return f"{self.project} / {self.branch}"
        return f"{self.project} (history)" if self.project else "ad-hoc run"


class NodeView(Frozen):
    key: str
    parent: str
    dataset: str
    brick: str
    state: str
    headline: str = ""
    params: dict[str, Any] = {}
    labels: tuple[str, ...] = ()


class TrainedModel(Frozen):
    name: str
    kind: str
    run_id: str
    origin: str
    path: str
    size_mb: float


class Snapshot(Frozen):
    generated_at: str
    user: str
    library_mode: str
    env_id: str
    versions: dict[str, str]
    jobs: tuple[QueueJob, ...]
    jobs_error: str = ""
    runs: tuple[RunView, ...]
    projects: tuple[ProjectSummary, ...]
    datasets: tuple[DatasetEntry, ...]
    models: tuple[AssetLocation, ...]
    trained_models: tuple[TrainedModel, ...] = ()
    nodes: tuple[NodeView, ...]
    steps_by_key: dict[str, StepView] = {}


def dataset_label(path: str) -> str:
    p = Path(path)
    return p.parent.name if p.name == "data.h5ad" else p.stem


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def step_state(
    step_dir: Path, job_id: str | None, queue: dict[str, str], queue_ok: bool = True
) -> tuple[str, str]:
    if (step_dir / SUCCESS).exists():
        return "COMPLETED", ""
    if job_id and job_id in queue:
        state = queue[job_id]
        return (state if STATE.fullmatch(state) else "UNKNOWN"), ""
    error = _read_json(step_dir / ERROR_FILE)
    if error:
        return "FAILED", str(error.get("message", ""))[:300]
    if job_id and not queue_ok:
        return "UNKNOWN", "the queue could not be read"
    if job_id:
        return "STOPPED", "job ended without a success marker (cancelled, killed or lost)"
    return "MISSING", "cached output was removed"


def run_state(states: list[str]) -> str:
    if any(s in FAILED for s in states):
        return "FAILED"
    if states and all(s == "COMPLETED" for s in states):
        return "COMPLETED"
    if "UNKNOWN" in states:
        return "UNKNOWN"
    return "RUNNING" if "RUNNING" in states else "PENDING"


def _run_view(manifest: RunManifest, queue: dict[str, str], queue_ok: bool, with_logs: bool) -> RunView:
    steps = []
    for record in manifest.steps:
        step_dir = Path(record.step_dir)
        state, message = step_state(step_dir, record.job_id, queue, queue_ok)
        summary = _read_json(step_dir / "results" / SUMMARY_FILE)
        step_file = _read_json(step_dir / STEP_FILE) or {}
        steps.append(
            StepView(
                index=record.index, brick=record.brick, key=record.step_key, state=state,
                job_id=record.job_id, headline=headline(record.brick, summary), message=message,
                params=step_file.get("params", {}), summary=summary, step_dir=str(step_dir),
                extras=step_extras(step_dir, with_log=with_logs and state != "PENDING"),
            )
        )
    return RunView(
        run_id=manifest.run_id, project=manifest.project, branch=manifest.branch,
        created_at=manifest.created_at, dataset=dataset_label(manifest.dataset),
        state=run_state([s.state for s in steps]), steps=tuple(steps),
    )


def run_label(run: RunView) -> str:
    return f"{run.project}/{run.branch}" if run.project and run.branch else f"run {run.run_id[-4:]}"


def _nodes(hub: Any, runs: list[RunView], projects: list[ProjectSummary]) -> list[NodeView]:
    nodes: dict[str, NodeView] = {}

    def add(key: str, parent: str, dataset: str, brick: str, state: str, head: str,
            params: dict[str, Any], label: str) -> None:
        current = nodes.get(key)
        labels = tuple(sorted({*(current.labels if current else ()), label}))
        keep = current if current and current.state != "PLANNED" else None
        nodes[key] = NodeView(
            key=key, parent=parent, dataset=dataset, brick=brick,
            state=keep.state if keep else state, headline=keep.headline if keep else head,
            params=params, labels=labels,
        )

    for run in runs:
        parent = f"ds:{run.dataset}"
        for step in run.steps:
            add(step.key, parent, run.dataset, step.brick, step.state, step.headline, step.params, run_label(run))
            parent = step.key
    for project in projects:
        for branch in project.branches:
            try:
                plan = hub.preview_branch(project.path, branch)
            except (ProjectError, UnsupportedFile, ValueError, OSError, KeyError):
                continue
            dataset = dataset_label(plan.dataset)
            parent = f"ds:{dataset}"
            for step in plan.steps:
                # add() keeps the state of a node a run already produced.
                add(step.step_key, parent, dataset, step.brick, "PLANNED", "", step.params, f"{project.path}/{branch}")
                parent = step.step_key
    return list(nodes.values())


def _trained_models(runs: list[RunView]) -> list[TrainedModel]:
    found: dict[str, TrainedModel] = {}
    for run in runs:
        for step in run.steps:
            folder = Path(step.step_dir) / "results" / "scvi_model"
            if step.key in found or not folder.is_dir():
                continue
            latent = step.params.get("n_latent", "?")
            found[step.key] = TrainedModel(
                name=f"scVI latent {latent} · {run.dataset}", kind="scVI", run_id=run.run_id,
                origin=run.label, path=str(folder), size_mb=trained_model_size_mb(folder),
            )
    return list(found.values())


def _queue(hub: Any) -> tuple[list[QueueJob], str]:
    try:
        return hub.slurm.my_jobs(), ""
    except SlurmError as exc:
        return [], str(exc)[:200]


def _models(hub: Any) -> tuple[AssetLocation, ...]:
    shared = hub.settings.shared_library
    return tuple(
        AssetLocation(
            name=p.stem, path=str(p),
            source="library" if shared and folder.is_relative_to(shared) else "local",
        )
        for folder in celltypist_dirs(hub.settings) if folder.is_dir()
        for p in sorted(folder.glob("*.pkl"))
    )


def collect(hub: Any) -> Snapshot:
    jobs, jobs_error = _queue(hub)
    queue = {j.job_id: j.state for j in jobs}
    manifests = [m for m in hub.store.list_runs(MAX_RUNS) if RUN_ID.fullmatch(m.run_id)]
    runs = [_run_view(m, queue, not jobs_error, i < RUNS_WITH_LOGS) for i, m in enumerate(manifests)]
    projects = hub.list_projects()
    steps_by_key: dict[str, StepView] = {}
    for run in reversed(runs):  # the newest run wins for a shared step
        steps_by_key.update({s.key: s for s in run.steps})
    return Snapshot(
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        user=os.environ.get("USER", ""),
        library_mode=library_mode(hub.settings),
        env_id=env_id(),
        versions={p: _version(p) for p in KEY_PACKAGES},
        jobs=tuple(jobs),
        jobs_error=jobs_error,
        runs=tuple(runs),
        projects=tuple(projects),
        datasets=tuple(hub.datasets()),
        models=_models(hub),
        trained_models=tuple(_trained_models(runs)),
        nodes=tuple(_nodes(hub, runs, projects)),
        steps_by_key=steps_by_key,
    )
