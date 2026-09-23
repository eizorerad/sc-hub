"""One facade used by both the CLI and the MCP server, so the rules are the same
whether a student types a command or an agent calls a tool."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Sequence

from .bricks import REGISTRY, PlanContext, Resources, get_brick
from .config import Settings
from .datasets import DatasetEntry, dataset_fingerprint, list_datasets
from .h5ad_profile import DatasetProfile, profile_h5ad
from .headlines import headline
from .library import celltypist_dirs, find_dataset
from .notebook import write_notebook
from .planner import DatasetOverrides, Plan, StepRequest, build_plan
from .projects import BranchSpec, Idea, ProjectMeta, ProjectStore, ProjectSummary
from .provenance import code_id, env_id
from .runs import RunManifest, RunResults, RunStatus, RunStore
from .slurm import ActiveJob, JobSpec, PartitionInfo, Slurm, render_script
from .state import Frozen

PLAN_ID = re.compile(r"[0-9a-f]{12}")
RUN_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}-[0-9a-f]{4}")
CATALOG_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")
LOGGED_MARKER = ".logged"


class HubError(ValueError):
    """A user-facing error: bad input, missing file, disallowed path."""


class ClusterStatus(Frozen):
    partitions: tuple[PartitionInfo, ...]
    my_pipeline_jobs: tuple[ActiveJob, ...]


class NotebookInfo(Frozen):
    path: str
    how_to_open: str


class FetchJob(Frozen):
    asset: str
    job_id: str
    log: str
    note: str


def _check_id(value: str, pattern: re.Pattern[str], kind: str) -> str:
    if not pattern.fullmatch(value):
        raise HubError(f"invalid {kind} '{value}'")
    return value


class Hub:
    def __init__(self, settings: Settings, slurm: Slurm | None = None) -> None:
        self.settings = settings
        self.slurm = slurm or Slurm()
        self.store = RunStore(settings, self.slurm)
        self.projects = ProjectStore(settings)
        self._profiles: dict[tuple[str, int, int], DatasetProfile] = {}

    # ---- discovery ------------------------------------------------------

    def bricks(self) -> list[dict[str, Any]]:
        return [
            {"name": s.name, "version": s.version, "summary": s.summary, "uses_gpu": s.uses_gpu, "terminal": s.terminal}
            for s in REGISTRY.values()
        ]

    def brick(self, name: str) -> dict[str, Any]:
        try:
            return get_brick(name).describe()
        except KeyError as exc:
            raise HubError(str(exc.args[0])) from exc

    def datasets(self) -> list[DatasetEntry]:
        return list_datasets(self.settings)

    def resolve_dataset(self, ref: str) -> Path:
        ref = ref.strip()
        if CATALOG_NAME.fullmatch(ref) and not ref.endswith(".h5ad"):
            found = find_dataset(self.settings, ref)
            if found is None:
                raise HubError(
                    f"dataset '{ref}' is not in the shared library, the local library or data/; "
                    "download it with fetch_asset or pass a path"
                )
            candidate = Path(found.path)
        else:
            candidate = Path(ref).expanduser()
            if not candidate.is_absolute():
                candidate = self.settings.root / candidate
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as exc:
            raise HubError(f"dataset '{ref}' not found (looked at {candidate})") from exc
        if resolved.suffix != ".h5ad" or not resolved.is_file():
            raise HubError(f"'{ref}' is not an .h5ad file")
        roots = [r.resolve() for r in self.settings.allowed_roots if r.exists()]
        if not any(resolved.is_relative_to(r) for r in roots):
            raise HubError(
                f"'{resolved}' is outside the sc-hub areas ({', '.join(map(str, roots))}); "
                "copy or link it into your data/ folder."
            )
        return resolved

    def inspect(self, ref: str) -> DatasetProfile:
        return self._profile(self.resolve_dataset(ref))

    def _profile(self, path: Path) -> DatasetProfile:
        """Profiles are cached per file version (the dashboard previews many branches)."""
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        if key not in self._profiles:
            self._profiles[key] = profile_h5ad(path)
        return self._profiles[key]

    def fingerprint(self, path: Path) -> str:
        return dataset_fingerprint(path, self.settings.library_roots)

    # ---- planning & execution ------------------------------------------

    def _context(self) -> PlanContext:
        return PlanContext(
            celltypist_dirs=celltypist_dirs(self.settings),
            limits=self.settings.limits,
            env_id=env_id(),
            code_ids={name: code_id(spec) for name, spec in REGISTRY.items()},
        )

    def plan(
        self,
        dataset: str,
        steps: Sequence[StepRequest | dict[str, Any]],
        overrides: DatasetOverrides | None = None,
        project: str | None = None,
        branch: str | None = None,
    ) -> Plan:
        plan = self.build(dataset, steps, overrides, project, branch)
        self.settings.plans_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.plans_dir / f"{plan.plan_id}.json").write_text(plan.model_dump_json(indent=2))
        return plan

    def build(
        self,
        dataset: str,
        steps: Sequence[StepRequest | dict[str, Any]],
        overrides: DatasetOverrides | None = None,
        project: str | None = None,
        branch: str | None = None,
    ) -> Plan:
        """Validate a pipeline without persisting anything."""
        path = self.resolve_dataset(dataset)
        requests = [s if isinstance(s, StepRequest) else StepRequest.model_validate(s) for s in steps]
        if project is not None:
            self.projects.require(project)
        plan = build_plan(self._profile(path), self.fingerprint(path), requests, self._context(), overrides)
        return plan.model_copy(update={"project": project, "branch": branch})

    def preview_branch(self, project: str, branch: str) -> Plan:
        resolved = self.projects.resolve(project, branch)
        overrides = DatasetOverrides(species=resolved.species, gene_ids=resolved.gene_ids)
        return self.build(resolved.dataset, resolved.steps, overrides, project=project, branch=branch)

    def load_plan(self, plan_id: str) -> Plan:
        path = self.settings.plans_dir / f"{_check_id(plan_id, PLAN_ID, 'plan_id')}.json"
        if not path.exists():
            raise HubError(f"plan '{plan_id}' not found; call plan_pipeline first")
        return Plan.model_validate_json(path.read_text())

    def submit(self, plan_id: str, force_new: bool = False) -> RunManifest:
        plan = self.load_plan(plan_id)
        dataset = Path(plan.dataset)
        if not dataset.exists() or self.fingerprint(dataset) != plan.dataset_fingerprint:
            raise HubError("the dataset changed after planning; plan again")
        manifest = self.store.submit(plan, force_new=force_new)
        if manifest.project:
            try:
                self.projects.link_run(manifest.project, manifest.run_id)
            except (OSError, ValueError):
                pass  # the run exists; a missing project link must not hide that
        return manifest

    def status(self, run_id: str) -> RunStatus:
        return self.store.status(_check_id(run_id, RUN_ID, "run_id"))

    def runs(self, limit: int = 10) -> list[RunManifest]:
        return self.store.list_runs(max(1, min(limit, 100)))

    def logs(self, run_id: str, step: int, lines: int = 80) -> str:
        return self.store.logs(_check_id(run_id, RUN_ID, "run_id"), step, max(1, lines))

    def results(self, run_id: str) -> RunResults:
        results = self.store.results(_check_id(run_id, RUN_ID, "run_id"))
        self.log_completion(results)
        return results

    def log_completion(self, results: RunResults) -> None:
        """Write one logbook entry per completed project run (idempotent)."""
        manifest = self.store.load(results.run_id)
        if results.state != "COMPLETED" or not manifest.project:
            return
        try:
            with (self.settings.runs_dir / results.run_id / LOGGED_MARKER).open("x") as marker:
                marker.write("logged\n")
        except FileExistsError:
            return
        lines = [f"- {s.index}. {s.brick}: {headline(s.brick, s.summary) or 'done'}" for s in results.steps]
        branch = f" (branch {manifest.branch})" if manifest.branch else ""
        self.projects.log(manifest.project, "\n".join(lines), heading=f"run {results.run_id}{branch} completed")

    def cancel(self, run_id: str) -> RunStatus:
        return self.store.cancel(_check_id(run_id, RUN_ID, "run_id"))

    def notebook(self, run_id: str) -> NotebookInfo:
        run_id = _check_id(run_id, RUN_ID, "run_id")
        path = write_notebook(self.settings.root / "notebooks", self.store.load(run_id), self.store.results(run_id))
        how = (
            f"{self.settings.root}/bin/schub-notebook {path}  "
            "(asks Slurm for a small allocation, then prints the ssh -L tunnel command)"
        )
        return NotebookInfo(path=str(path), how_to_open=how)

    def cluster(self) -> ClusterStatus:
        return ClusterStatus(
            partitions=tuple(self.slurm.partitions()),
            my_pipeline_jobs=tuple(self.slurm.active(self.settings.job_prefix)),
        )

    # ---- assets ------------------------------------------------------------

    def fetch_asset(self, asset: str) -> FetchJob:
        """Queue a small Slurm job that downloads a missing asset into the local library."""
        from .fetch import FETCHERS, missing

        if asset not in FETCHERS:
            raise HubError(f"unknown asset '{asset}'; available: {', '.join(FETCHERS)}")
        if not missing(self.settings, [asset]):
            raise HubError(f"'{asset}' is already available; no download needed")
        s = self.settings
        queued = [j for j in self.slurm.active(s.job_prefix) if j.name == f"{s.job_prefix}-fetch-{asset}"]
        if queued:
            raise HubError(f"a download of '{asset}' is already queued as job {queued[0].job_id}")
        logs = s.logs_dir / "fetch"
        logs.mkdir(parents=True, exist_ok=True)
        spec = JobSpec(
            name=f"{s.job_prefix}-fetch-{asset}",
            partition=s.partition,
            resources=Resources(cpus=4, mem_gb=16, time_min=60),
            log_path=logs / "fetch-%j.log",
            workdir=s.root,
            command=(str(s.python), "-m", "schub.cli", "fetch", asset),
            env=(("SCHUB_ROOT", str(s.root)), ("SCHUB_LIBRARY", str(s.library or "")), ("SCHUB_PYTHON", str(s.python))),
        )
        script = logs / f"fetch-{asset}.sbatch"
        script.write_text(render_script(spec))
        job_id = self.slurm.submit(script)
        return FetchJob(
            asset=asset,
            job_id=job_id,
            log=str(logs / f"fetch-{job_id}.log"),
            note="Downloads into your local library; plan again when the job has finished.",
        )

    # ---- projects ----------------------------------------------------------

    def list_projects(self) -> list[ProjectSummary]:
        return [self.projects.summary(name) for name in self.projects.names()]

    def create_project(self, project: str, question: str = "", datasets: Sequence[str] = ()) -> ProjectMeta:
        return self.projects.create(project, question, tuple(datasets))

    def save_branch(self, project: str, name: str, spec: BranchSpec, overwrite: bool = False) -> Plan:
        """Validate a branch in memory; save it only if its dry-run plan is clean."""
        if self.projects.branch_exists(project, name) and not overwrite:
            raise HubError(f"branch '{name}' already exists in '{project}'; pass overwrite=true to replace it")
        resolved = self.projects.resolve(project, name, spec)
        overrides = DatasetOverrides(species=resolved.species, gene_ids=resolved.gene_ids)
        preview = self.build(resolved.dataset, resolved.steps, overrides, project=project, branch=name)
        if not preview.ok:
            errors = "; ".join(f"step {i.step}: {i.message}" for i in preview.issues if i.level == "error")
            raise HubError(f"branch '{name}' not saved, its plan has errors: {errors}")
        self.projects.save_branch(project, name, spec)
        return self.plan_branch(project, name)

    def plan_branch(self, project: str, branch: str) -> Plan:
        resolved = self.projects.resolve(project, branch)
        overrides = DatasetOverrides(species=resolved.species, gene_ids=resolved.gene_ids)
        return self.plan(resolved.dataset, resolved.steps, overrides, project=project, branch=branch)

    def add_idea(self, project: str, slug: str, title: str, hypothesis: str = "", reverses_if: str = "") -> Idea:
        return self.projects.add_idea(project, slug, title, hypothesis, reverses_if)

    def update_idea(self, project: str, slug: str, **changes: Any) -> Idea:
        return self.projects.update_idea(project, slug, **changes)

    def add_log(self, project: str, text: str) -> list[str]:
        self.projects.log(project, text)
        return self.projects.logbook_tail(project, 3)
