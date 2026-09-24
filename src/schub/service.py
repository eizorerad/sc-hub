"""One facade used by both the CLI and the MCP server, so the rules are the same
whether a student types a command or an agent calls a tool."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Sequence

from .bricks import REGISTRY, PlanContext, Resources, get_brick
from .config import Settings
from .datasets import DatasetEntry, dataset_fingerprint, list_datasets
from .fastq import MANIFEST_FILE, FastqError, detect_samples, fastq_profile, write_manifest
from .h5ad_profile import DatasetProfile, profile_h5ad
from .headlines import headline
from .library import celltypist_dirs, find_dataset
from .notebook import load_run, write_notebook
from .planner import DatasetOverrides, Plan, StepRequest, build_plan
from .project_env import EnvError, EnvJob, built, check_packages, remove_env, slug, submit_env_build
from .projects import BranchSpec, Idea, ProjectError, ProjectMeta, ProjectStore, ProjectSummary
from .provenance import code_id, env_id
from .runs import LimitExceeded, PlanRejected, RunManifest, RunResults, RunStatus, RunStore
from .sessions import SessionError, SessionInfo, SessionStore
from .seurat import ImportJob, SeuratImportError, submit_import
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
        self.sessions = SessionStore(settings, self.slurm)
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
        if CATALOG_NAME.fullmatch(ref) and not ref.endswith((".h5ad", ".yaml")):
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
        if not resolved.is_file() or not (resolved.suffix == ".h5ad" or resolved.name == MANIFEST_FILE):
            raise HubError(f"'{ref}' is not an .h5ad file or a FASTQ manifest ({MANIFEST_FILE})")
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
            try:
                self._profiles[key] = fastq_profile(path) if path.name == MANIFEST_FILE else profile_h5ad(path)
            except FastqError as exc:
                raise HubError(str(exc)) from exc
        return self._profiles[key]

    def fingerprint(self, path: Path) -> str:
        return dataset_fingerprint(path, self.settings.library_roots)

    def register_fastq(self, folder: str, technology: str, organism: str, title: str = "",
                       expected_cells: int | None = None) -> DatasetEntry:
        """Describe a folder of 10x-named FASTQ files in data/ so it can be planned."""
        target = (self.settings.data_dir / folder).resolve()
        if not target.is_relative_to(self.settings.data_dir.resolve()) or not target.is_dir():
            raise HubError(f"'{folder}' must be a folder inside {self.settings.data_dir}")
        try:
            samples = detect_samples(target)
            meta = {"title": title or target.name, "organism": organism, "technology": technology,
                    "expected_cells": expected_cells, "samples": [s.model_dump() for s in samples]}
            path = write_manifest(target, {k: v for k, v in meta.items() if v is not None})
        except (FastqError, ValueError) as exc:
            raise HubError(str(exc)) from exc
        entry = next((d for d in self.datasets() if d.path == str(path)), None)
        return entry or DatasetEntry(name=target.name, path=str(path), source="private", kind="fastq")

    # ---- planning & execution ------------------------------------------

    def plan_context(self) -> PlanContext:
        """What brick checks need to know (models, limits, code identities, libraries)."""
        return self._context()

    def _context(self) -> PlanContext:
        return PlanContext(
            celltypist_dirs=celltypist_dirs(self.settings),
            limits=self.settings.limits,
            env_id=env_id(),
            code_ids={name: code_id(spec) for name, spec in REGISTRY.items()},
            library_roots=self.settings.library_roots,
            dataset_lookup=self._lookup,
        )

    def _lookup(self, ref: str) -> tuple[DatasetProfile, str]:
        """Profile and fingerprint of another dataset (for bricks that read several)."""
        try:
            path = self.resolve_dataset(ref)
        except HubError as exc:
            raise KeyError(str(exc)) from exc
        return self._profile(path), self.fingerprint(path)

    def plan(
        self,
        dataset: str,
        steps: Sequence[StepRequest | dict[str, Any]],
        overrides: DatasetOverrides | None = None,
        project: str | None = None,
        branch: str | None = None,
        revision: int | None = None,
    ) -> Plan:
        plan = self.build(dataset, steps, overrides, project, branch).model_copy(update={"revision": revision})
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
        plan = self.build(resolved.dataset, resolved.steps, overrides, project=project, branch=branch)
        return plan.model_copy(update={"revision": self.projects.load_branch(project, branch).revision})

    def load_plan(self, plan_id: str) -> Plan:
        path = self.settings.plans_dir / f"{_check_id(plan_id, PLAN_ID, 'plan_id')}.json"
        if not path.exists():
            raise HubError(f"plan '{plan_id}' not found; call plan_pipeline first")
        return Plan.model_validate_json(path.read_text())

    def submit(self, plan_id: str, force_new: bool = False) -> RunManifest:
        """Submit now. At the cap of active pipelines nothing is queued: the answer says to wait."""
        try:
            return self._submit_now(_check_id(plan_id, PLAN_ID, "plan_id"), force_new)
        except LimitExceeded as exc:
            raise HubError(str(exc)) from exc

    def _current(self, plan: Plan) -> Plan:
        """What to submit for `plan`. A branch goes as it is now: planned again if it changed
        since (a new version of the branch, new sc-hub code or package versions, new data). A
        one-off plan made with other brick code is refused (its jobs would stop at once with
        "code changed")."""
        if plan.project and plan.branch:
            try:
                exists = self.projects.branch_exists(plan.project, plan.branch)
            except ProjectError:
                exists = False
            if not exists:
                raise PlanRejected(f"branch {plan.project}/{plan.branch} no longer exists")
            if self.preview_branch(plan.project, plan.branch).plan_id != plan.plan_id:
                return self.plan_branch(plan.project, plan.branch)
            return plan
        stale = sorted({s.brick for s in plan.steps
                        if s.brick in REGISTRY and s.code_id and s.code_id != code_id(REGISTRY[s.brick])})
        if stale:
            raise PlanRejected(f"sc-hub changed the code of {', '.join(stale)} after this plan was made; plan it again")
        return plan

    def _submit_now(self, plan_id: str, force_new: bool = False) -> RunManifest:
        plan = self._current(self.load_plan(plan_id))
        dataset = Path(plan.dataset)
        if not dataset.exists() or self.fingerprint(dataset) != plan.dataset_fingerprint:
            raise HubError("the dataset changed after planning; plan again")
        for step in plan.steps:
            for name, pin in step.pins.items():
                path, _, fingerprint = pin.partition("\t")
                if not Path(path).exists() or self.fingerprint(Path(path)) != fingerprint:
                    raise HubError(f"dataset '{name}' (step {step.index}) changed after planning; plan again")
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
        """The run as a notebook: every step with the exact code and parameters it ran with."""
        run_id = _check_id(run_id, RUN_ID, "run_id")
        manifest = self.store.load(run_id)
        question = ""
        if manifest.project:
            try:
                question = self.projects.meta(manifest.project).question
            except ProjectError:
                question = ""
        path = write_notebook(self.settings.root / "notebooks", load_run(manifest, question))
        how = (
            f"start_session(kind='jupyter', target='{path.relative_to(self.settings.root)}') starts JupyterLab "
            "on a compute node (gpu=True for scVI steps); then ./schub-lab jupyter on the laptop opens it. "
            "Each step can be re-run there with changed code or parameters; its results go to notebooks/work/."
        )
        return NotebookInfo(path=str(path), how_to_open=how)

    # ---- sessions, imports -----------------------------------------------------

    def start_session(self, kind: str, hours: int = 4, gpu: bool = False, target: str = "") -> SessionInfo:
        try:
            return self.sessions.start(kind, hours, gpu, target)
        except SessionError as exc:
            raise HubError(str(exc)) from exc

    def list_sessions(self) -> list[SessionInfo]:
        return self.sessions.list()

    def stop_session(self, session_id: str) -> SessionInfo:
        try:
            return self.sessions.stop(session_id)
        except SessionError as exc:
            raise HubError(str(exc)) from exc

    def session_line(self, kind: str) -> str:
        """'<node> <port> <path>' of the running session, for the laptop's schub-lab."""
        line = self.sessions.connection_line(kind)
        if line is None:
            raise HubError(f"no running {kind} session")
        return line

    def add_project_packages(self, project: str, pip: Sequence[str] = (), conda: Sequence[str] = (),
                             remove: bool = False) -> EnvJob:
        """Add (or with remove=True drop) extra packages of a project's Jupyter kernel and
        rebuild it in a job. The request builds on what is installed now, never on a
        previous request that failed."""
        self.projects.require(project)
        building = f"{self.settings.job_prefix}-env-{slug(project)}"
        queued = [j for j in self.slurm.active(self.settings.job_prefix) if j.name == building]
        if queued:
            raise HubError(f"the kernel of '{project}' is being built (job {queued[0].job_id}); "
                           "ask again when it has finished, so no request is lost")
        current = built(self.settings, project)
        have_pip, have_conda = (current.pip, current.conda) if current else ((), ())
        if remove:
            new_pip = tuple(x for x in have_pip if x not in set(pip))
            new_conda = tuple(x for x in have_conda if x not in set(conda))
        else:
            new_pip, new_conda = tuple(dict.fromkeys((*have_pip, *pip))), tuple(dict.fromkeys((*have_conda, *conda)))
        try:
            check_packages(new_pip, new_conda)
            if not new_pip and not new_conda:
                remove_env(self.settings, project)
                return EnvJob(project=project, job_id="", log="", env="", kernel="",
                              note="No extra packages left: the project uses the shared environment only.")
            return submit_env_build(self.settings, self.slurm, project, new_pip, new_conda)
        except EnvError as exc:
            raise HubError(str(exc)) from exc

    def import_seurat(self, rds: str, name: str) -> ImportJob:
        try:
            return submit_import(self.settings, self.slurm, rds, name)
        except SeuratImportError as exc:
            raise HubError(str(exc)) from exc

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
        """Every project; one unreadable project.yaml is reported, never hides the others."""
        found = []
        for name in self.projects.names():
            try:
                found.append(self.projects.summary(name))
            except ProjectError as exc:
                found.append(ProjectSummary(path=name, meta=ProjectMeta(name=name), branches=(), ideas=(), runs=(),
                                            logbook_tail=(), problems=(str(exc)[:300],)))
        return found

    def create_project(self, project: str, question: str = "", datasets: Sequence[str] = ()) -> ProjectMeta:
        return self.projects.create(project, question, tuple(datasets))

    def save_branch(self, project: str, name: str, spec: BranchSpec, overwrite: bool = False, reason: str = "") -> Plan:
        """Validate a branch in memory; save it only if its dry-run plan is clean.
        Replacing an existing branch (overwrite) saves a new revision of it."""
        if self.projects.branch_exists(project, name) and not overwrite:
            raise HubError(f"branch '{name}' already exists in '{project}'; pass overwrite=True to replace it "
                           "(the previous version is kept in the branch's history), or save an alternative under "
                           "another name")
        return self._save_checked(project, name, spec, reason)

    def dry_run_errors(self, project: str, name: str, spec: BranchSpec) -> str:
        """The plan errors `spec` would have if saved as `name` ('' if none)."""
        resolved = self.projects.resolve(project, name, spec)
        overrides = DatasetOverrides(species=resolved.species, gene_ids=resolved.gene_ids)
        preview = self.build(resolved.dataset, resolved.steps, overrides, project=project, branch=name)
        return "; ".join(f"step {i.step}: {i.message}" for i in preview.issues if i.level == "error")

    def _save_checked(self, project: str, name: str, spec: BranchSpec, reason: str) -> Plan:
        errors = self.dry_run_errors(project, name, spec)
        if errors:
            raise HubError(f"branch '{name}' not saved, its plan has errors: {errors}")
        self.projects.save_branch(project, name, spec, reason)
        return self.plan_branch(project, name)

    def save_checked_branch(self, project: str, name: str, spec: BranchSpec, reason: str) -> Plan:
        return self._save_checked(project, name, spec, reason)

    def plan_branch(self, project: str, branch: str) -> Plan:
        resolved = self.projects.resolve(project, branch)
        overrides = DatasetOverrides(species=resolved.species, gene_ids=resolved.gene_ids)
        revision = self.projects.load_branch(project, branch).revision
        return self.plan(resolved.dataset, resolved.steps, overrides, project=project, branch=branch, revision=revision)

    def add_idea(self, project: str, slug: str, title: str, hypothesis: str = "", reverses_if: str = "") -> Idea:
        return self.projects.add_idea(project, slug, title, hypothesis, reverses_if)

    def update_idea(self, project: str, slug: str, **changes: Any) -> Idea:
        return self.projects.update_idea(project, slug, **changes)

    def add_log(self, project: str, text: str) -> list[str]:
        self.projects.log(project, text)
        return self.projects.logbook_tail(project, 3)
