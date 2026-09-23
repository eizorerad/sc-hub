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

from ..datasets import DatasetEntry, dataset_label
from ..h5ad_profile import UnsupportedFile
from ..headlines import headline
from ..library import AssetLocation, celltypist_dirs, find_tool, kallisto_ref, library_mode
from ..projects import ProjectError, ProjectSummary
from ..provenance import KEY_PACKAGES, _version, env_id
from ..runs import RunManifest
from ..bricks.merge_datasets import label_of
from ..overview import Overview, cached_overview
from ..project_env import BuiltEnv, built, slug
from ..revisions import Revision, history
from ..sessions import SessionInfo
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
    code_id: str = ""  # the brick code this step ran with (differs after an sc-hub update)
    extras: StepExtras = StepExtras()


class RunView(Frozen):
    run_id: str
    project: str | None
    branch: str | None
    created_at: str
    dataset: str
    state: str
    steps: tuple[StepView, ...]
    revision: int | None = None
    inputs: tuple[str, ...] = ()  # every dataset the run read (merge steps add more)
    schub_version: str = ""

    @property
    def label(self) -> str:
        if self.project and self.branch:
            return f"{self.project} / {self.branch}" + (f" · r{self.revision}" if self.revision else "")
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
    inputs: tuple[str, ...] = ()  # extra dataset roots ("ds:<name>") a merge step reads
    refs: tuple[str, ...] = ()  # "<project>/<branch>#<step>" of every branch using this step


class TrainedModel(Frozen):
    name: str
    kind: str
    run_id: str
    origin: str
    path: str
    size_mb: float


class Reference(Frozen):
    name: str
    kind: str
    status: str  # a version or folder name, or "not installed"


class BranchInfo(Frozen):
    project: str
    name: str
    revision: int = 1
    from_branch: str | None = None
    forked_from: str | None = None
    description: str = ""
    reason: str = ""
    datasets: tuple[str, ...] = ()
    steps: int = 0
    history: tuple[Revision, ...] = ()
    keys: tuple[str, ...] = ()  # step keys of the branch as it is now
    problem: str = ""


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
    references: tuple[Reference, ...] = ()
    sessions: tuple[SessionInfo, ...] = ()
    branches: dict[str, BranchInfo] = {}  # "<project>/<branch>"
    kernels: dict[str, BuiltEnv] = {}  # project -> what its own Jupyter kernel has installed
    env_builds: tuple[str, ...] = ()  # projects whose kernel is being built right now
    overview: Overview | None = None
    nodes: tuple[NodeView, ...]
    steps_by_key: dict[str, StepView] = {}
    notebooks: frozenset[str] = frozenset()  # runs with nb/<run_id>.js (set when the page is built)


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
                code_id=str(step_file.get("code_id", "")),
                extras=step_extras(step_dir, with_log=with_logs and state != "PENDING"),
            )
        )
    extra = tuple(i[3:] for s in steps for i in _inputs(s.brick, s.params))
    return RunView(
        run_id=manifest.run_id, project=manifest.project, branch=manifest.branch,
        created_at=manifest.created_at, dataset=dataset_label(manifest.dataset),
        state=run_state([s.state for s in steps]), steps=tuple(steps), revision=manifest.revision,
        inputs=(dataset_label(manifest.dataset), *extra), schub_version=manifest.schub_version,
    )


def run_label(run: RunView) -> str:
    return f"{run.project}/{run.branch}" if run.project and run.branch else f"run {run.run_id[-4:]}"


def input_label(ref: str) -> str:
    """Dataset name for a catalog name or a path (the same label merge_datasets writes)."""
    return label_of(ref)


def _inputs(brick: str, params: dict[str, Any]) -> tuple[str, ...]:
    others = params.get("others") if brick == "merge_datasets" else None
    return tuple(f"ds:{input_label(str(o))}" for o in others) if isinstance(others, list) else ()


def _previews(hub: Any, projects: list[ProjectSummary]) -> dict[str, Any]:
    """Every branch planned once per build: a Plan, or the error that stopped it."""
    found: dict[str, Any] = {}
    for project in projects:
        for branch in project.branches:
            try:
                found[f"{project.path}/{branch}"] = hub.preview_branch(project.path, branch)
            except (ProjectError, UnsupportedFile, ValueError, OSError, KeyError) as exc:
                found[f"{project.path}/{branch}"] = exc
    return found


def _nodes(hub: Any, runs: list[RunView], projects: list[ProjectSummary], previews: dict[str, Any]) -> list[NodeView]:
    nodes: dict[str, NodeView] = {}

    def add(key: str, parent: str, dataset: str, brick: str, state: str, head: str,
            params: dict[str, Any], label: str, ref: str = "") -> None:
        current = nodes.get(key)
        labels = tuple(sorted({*(current.labels if current else ()), label}))
        refs = tuple(sorted({*(current.refs if current else ()), *((ref,) if ref else ())}))
        keep = current if current and current.state != "PLANNED" else None
        nodes[key] = NodeView(
            key=key, parent=parent, dataset=dataset, brick=brick,
            state=keep.state if keep else state, headline=keep.headline if keep else head,
            params=params, labels=labels, inputs=_inputs(brick, params), refs=refs,
        )

    for run in runs:
        parent = f"ds:{run.dataset}"
        for step in run.steps:
            ref = f"{run.project}/{run.branch}#{step.index}" if run.project and run.branch else ""
            add(step.key, parent, run.dataset, step.brick, step.state, step.headline, step.params, run_label(run), ref)
            parent = step.key
    for project in projects:
        for branch in project.branches:
            plan = previews.get(f"{project.path}/{branch}")
            if plan is None or isinstance(plan, Exception):
                continue
            dataset = dataset_label(plan.dataset)
            parent = f"ds:{dataset}"
            for step in plan.steps:
                # add() keeps the state of a node a run already produced.
                label = f"{project.path}/{branch}"
                add(step.step_key, parent, dataset, step.brick, "PLANNED", "", step.params, label, f"{label}#{step.index}")
                parent = step.step_key
    return list(nodes.values())


def _trained_models(runs: list[RunView]) -> list[TrainedModel]:
    found: dict[str, TrainedModel] = {}
    for run in runs:
        for step in run.steps:
            for kind, name in (("scVI", "scvi_model"), ("scANVI", "scanvi_model")):
                folder = Path(step.step_dir) / "results" / name
                if step.key in found or not folder.is_dir():
                    continue
                latent = step.params.get("n_latent", "?")
                found[step.key] = TrainedModel(
                    name=f"{kind} latent {latent} · {run.dataset}", kind=kind, run_id=run.run_id,
                    origin=run.label, path=str(folder), size_mb=trained_model_size_mb(folder),
                )
    return list(found.values())


def _references(hub: Any) -> tuple[Reference, ...]:
    roots = hub.settings.library_roots
    found = [
        Reference(name=f"kallisto index ({organism})", kind="kb_count",
                  status="ready" if kallisto_ref(roots, organism) else "not installed (fetch_asset)")
        for organism in ("human", "mouse")
    ]
    for tool, executable, kind in (("cellranger", "cellranger", "cellranger_count"),
                                   ("cellxgene", "bin/python", "cellxgene sessions"),
                                   ("r-seurat", "bin/Rscript", "import_seurat")):
        path = find_tool(roots, tool, executable)
        # tools/<tool>/<version>/<executable>: the folder above the executable's own path
        version = path.parents[len(Path(executable).parts) - 1].name if path else "not installed"
        found.append(Reference(name=tool, kind=kind, status=version))
    return tuple(found)


def _branches(hub: Any, projects: list[ProjectSummary], previews: dict[str, Any]) -> dict[str, BranchInfo]:
    found = {}
    for project in projects:
        for name in project.branches:
            key = f"{project.path}/{name}"
            try:
                spec = hub.projects.load_branch(project.path, name)
                resolved = hub.projects.resolve(project.path, name)
                datasets = (input_label(resolved.dataset),) + tuple(
                    i[3:] for s in resolved.steps for i in _inputs(s.brick, s.params)
                )
                found[key] = BranchInfo(
                    project=project.path, name=name, revision=spec.revision, from_branch=spec.from_branch,
                    forked_from=spec.forked_from, description=spec.description, reason=spec.reason,
                    datasets=datasets, steps=len(resolved.steps),
                    history=tuple(history(hub.projects, project.path, name)),
                    keys=_keys(previews.get(key)),
                    problem=str(previews[key])[:200] if isinstance(previews.get(key), Exception) else "",
                )
            except (ProjectError, UnsupportedFile, ValueError, OSError, KeyError) as exc:
                found[key] = BranchInfo(project=project.path, name=name, problem=str(exc)[:200])
    return found


def _keys(plan: Any) -> tuple[str, ...]:
    return tuple(s.step_key for s in plan.steps) if plan is not None and not isinstance(plan, Exception) else ()


def _overview(hub: Any) -> Overview | None:
    try:
        return cached_overview(hub.settings)
    except (OSError, ValueError) as exc:  # never let the cluster view break the page
        return Overview(user=os.environ.get("USER", ""), login_node="", generated_at="", problems=(str(exc)[:200],))


def _sessions(hub: Any, queue: dict[str, str], queue_ok: bool) -> tuple[SessionInfo, ...]:
    try:
        return tuple(hub.sessions.list(queue if queue_ok else None))
    except (OSError, ValueError, SlurmError):
        return ()


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
    previews = _previews(hub, projects)
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
        references=_references(hub),
        sessions=_sessions(hub, queue, not jobs_error),
        branches=_branches(hub, projects, previews),
        kernels={p.path: env for p in projects if (env := built(hub.settings, p.path)) is not None},
        env_builds=tuple(p.path for p in projects if f"{hub.settings.job_prefix}-env-{slug(p.path)}" in {j.name for j in jobs}),
        overview=_overview(hub),
        nodes=tuple(_nodes(hub, runs, projects, previews)),
        steps_by_key=steps_by_key,
    )
