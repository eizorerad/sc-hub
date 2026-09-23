"""Projects, subprojects, branches, ideas and logbooks as plain files.

    projects/<project>[/<subproject>...]/
      project.yaml          question, status, datasets
      pipelines/<b>.yaml    a branch: steps, or `from: <other>` + overrides
      ideas/<slug>.md       YAML front matter (status, hypothesis, ...) + notes
      logbook.md            dated entries, newest last
      runs/<run_id>         links to the runs submitted from this project

A branch is a variant of a pipeline. Because step outputs are cached by
content, branches that share a prefix recompute only where they differ.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ConfigDict, Field, ValidationError, model_validator

from .config import Settings
from .planner import StepRequest
from .state import Frozen, GeneIds, Species

SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
MAX_DEPTH = 3
MAX_INHERITANCE = 8
PROJECT_FILE = "project.yaml"
LOGBOOK = "logbook.md"
IdeaStatus = Literal["open", "planned", "running", "done", "dropped"]


class ProjectError(ValueError):
    pass


class ProjectMeta(Frozen):
    name: str
    question: str = ""
    status: Literal["active", "paused", "done"] = "active"
    created: str = ""
    datasets: tuple[str, ...] = ()


class BranchSpec(Frozen):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    dataset: str | None = None
    from_branch: str | None = Field(None, alias="from")
    steps: tuple[StepRequest, ...] = ()
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    append: tuple[StepRequest, ...] = ()
    species: Species | None = None
    gene_ids: GeneIds | None = None
    idea: str | None = None
    description: str = ""

    @model_validator(mode="after")
    def _steps_or_parent(self) -> "BranchSpec":
        if self.from_branch and self.steps:
            raise ValueError("give either `steps` or `from` (+ overrides/append), not both")
        return self


class ResolvedBranch(Frozen):
    project: str
    name: str
    dataset: str
    steps: tuple[StepRequest, ...]
    species: Species | None = None
    gene_ids: GeneIds | None = None
    lineage: tuple[str, ...]


class Idea(Frozen):
    slug: str
    title: str
    status: IdeaStatus = "open"
    hypothesis: str = ""
    reverses_if: str = ""
    branches: tuple[str, ...] = ()
    created: str = ""
    notes: str = ""


class ProjectSummary(Frozen):
    path: str
    meta: ProjectMeta
    branches: tuple[str, ...]
    ideas: tuple[Idea, ...]
    runs: tuple[str, ...]
    logbook_tail: tuple[str, ...]
    problems: tuple[str, ...] = ()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _check_name(name: str, kind: str) -> str:
    if not SEGMENT.fullmatch(name):
        raise ProjectError(f"invalid {kind} '{name}': use lowercase letters, digits, '-' or '_'")
    return name


def apply_overrides(steps: tuple[StepRequest, ...], overrides: dict[str, dict[str, Any]]) -> tuple[StepRequest, ...]:
    """Patch params by brick name (all steps of that brick) or by 1-based index."""
    bricks = {s.brick for s in steps}
    unknown = [k for k in overrides if k not in bricks and not (k.isdigit() and 1 <= int(k) <= len(steps))]
    if unknown:
        raise ProjectError(f"overrides target no step: {unknown}; steps are {[s.brick for s in steps]}")
    patched = []
    for index, step in enumerate(steps, start=1):
        patch = {**overrides.get(step.brick, {}), **overrides.get(str(index), {})}
        patched.append(step.model_copy(update={"params": {**step.params, **patch}}) if patch else step)
    return tuple(patched)


def _load_yaml(text: str, where: str) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProjectError(f"{where} is not valid YAML: {exc}") from exc
    return loaded if isinstance(loaded, dict) else {}


def _validated(model: type[Any], data: dict[str, Any], where: str) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ProjectError(f"{where} is invalid: {exc.errors()[0]['msg']}") from exc


def _read_front_matter(text: str, where: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    head, _, body = text[4:].partition("\n---\n")
    return _load_yaml(head, where), body


def _quote(text: str) -> str:
    """Logbook text as a quoted block, so it can never forge a new entry heading."""
    return "\n".join(f"> {line}" if line else ">" for line in text.strip().splitlines())


class ProjectStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # ---- projects ---------------------------------------------------------

    def path_of(self, project: str) -> Path:
        parts = project.strip("/").split("/")
        if not parts or len(parts) > MAX_DEPTH:
            raise ProjectError(f"project path '{project}' must have 1-{MAX_DEPTH} levels")
        for part in parts:
            _check_name(part, "project name")
        return self.settings.projects_dir.joinpath(*parts)

    def require(self, project: str) -> Path:
        directory = self.path_of(project)
        if not (directory / PROJECT_FILE).is_file():
            raise ProjectError(f"project '{project}' does not exist; create it first")
        return directory

    def create(self, project: str, question: str = "", datasets: tuple[str, ...] = ()) -> ProjectMeta:
        directory = self.path_of(project)
        parent = project.strip("/").rpartition("/")[0]
        if parent:
            self.require(parent)
        if (directory / PROJECT_FILE).exists():
            raise ProjectError(f"project '{project}' already exists")
        for sub in ("pipelines", "ideas", "runs", "notebooks", "exports"):
            (directory / sub).mkdir(parents=True, exist_ok=True)
        meta = ProjectMeta(name=project, question=question, created=_now(), datasets=datasets)
        (directory / PROJECT_FILE).write_text(yaml.safe_dump(meta.model_dump(mode="json"), sort_keys=False))
        (directory / LOGBOOK).write_text(f"# Logbook: {project}\n\n")
        return meta

    def meta(self, project: str) -> ProjectMeta:
        path = self.require(project) / PROJECT_FILE
        return _validated(ProjectMeta, _load_yaml(path.read_text(), str(path)), str(path))

    def names(self) -> list[str]:
        base = self.settings.projects_dir
        if not base.is_dir():
            return []
        found = [p.parent.relative_to(base) for p in base.rglob(PROJECT_FILE)]
        valid = [p for p in found if len(p.parts) <= MAX_DEPTH and all(SEGMENT.fullmatch(s) for s in p.parts)]
        return sorted(str(p) for p in valid)

    def summary(self, project: str) -> ProjectSummary:
        directory = self.require(project)
        runs_dir = directory / "runs"
        runs = tuple(sorted((p.name for p in runs_dir.iterdir()), reverse=True)) if runs_dir.is_dir() else ()
        ideas, problems = self._ideas_and_problems(project)
        return ProjectSummary(
            path=project,
            meta=self.meta(project),
            branches=tuple(self.branches(project)),
            ideas=tuple(ideas),
            runs=runs,
            logbook_tail=tuple(self.logbook_tail(project)),
            problems=tuple(problems),
        )

    # ---- branches ---------------------------------------------------------

    def branches(self, project: str) -> list[str]:
        folder = self.require(project) / "pipelines"
        return sorted(p.stem for p in folder.glob("*.yaml")) if folder.is_dir() else []

    def save_branch(self, project: str, name: str, spec: BranchSpec) -> Path:
        path = self.require(project) / "pipelines" / f"{_check_name(name, 'branch name')}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = spec.model_dump(mode="json", by_alias=True, exclude_defaults=True)
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        return path

    def load_branch(self, project: str, name: str) -> BranchSpec:
        path = self.require(project) / "pipelines" / f"{_check_name(name, 'branch name')}.yaml"
        if not path.is_file():
            raise ProjectError(f"branch '{name}' not found in '{project}'; have {self.branches(project)}")
        return _validated(BranchSpec, _load_yaml(path.read_text(), str(path)), str(path))

    def branch_exists(self, project: str, name: str) -> bool:
        return (self.require(project) / "pipelines" / f"{_check_name(name, 'branch name')}.yaml").is_file()

    def resolve(self, project: str, name: str, spec: BranchSpec | None = None) -> ResolvedBranch:
        """Resolve a saved branch, or an unsaved `spec` as if it were saved as `name`."""
        return self._resolve(project, name, (), spec)

    def _resolve(
        self, project: str, name: str, seen: tuple[str, ...], override: BranchSpec | None = None
    ) -> ResolvedBranch:
        if name in seen or len(seen) >= MAX_INHERITANCE:
            raise ProjectError(f"branch inheritance cycle or too deep: {' -> '.join((*seen, name))}")
        spec = override or self.load_branch(project, name)
        if spec.from_branch:
            parent = self._resolve(project, spec.from_branch, (*seen, name))
            base_steps, dataset = parent.steps, spec.dataset or parent.dataset
            species, gene_ids = spec.species or parent.species, spec.gene_ids or parent.gene_ids
            lineage = (*parent.lineage, name)
        else:
            base_steps, dataset, species, gene_ids = spec.steps, spec.dataset, spec.species, spec.gene_ids
            lineage = (name,)
        if not dataset:
            raise ProjectError(f"branch '{name}' has no dataset (set `dataset:` or `from:`)")
        steps = apply_overrides(base_steps, spec.overrides) + spec.append
        return ResolvedBranch(
            project=project, name=name, dataset=dataset, steps=steps,
            species=species, gene_ids=gene_ids, lineage=lineage,
        )

    # ---- ideas ------------------------------------------------------------

    def _idea_path(self, project: str, slug: str) -> Path:
        return self.require(project) / "ideas" / f"{_check_name(slug, 'idea slug')}.md"

    def add_idea(self, project: str, slug: str, title: str, hypothesis: str = "", reverses_if: str = "") -> Idea:
        path = self._idea_path(project, slug)
        if path.exists():
            raise ProjectError(f"idea '{slug}' already exists in '{project}'")
        idea = Idea(slug=slug, title=title, hypothesis=hypothesis, reverses_if=reverses_if, created=_now())
        self._write_idea(path, idea)
        return idea

    def update_idea(self, project: str, slug: str, **changes: Any) -> Idea:
        path = self._idea_path(project, slug)
        if not path.is_file():
            raise ProjectError(f"idea '{slug}' not found in '{project}'")
        current = self._read_idea(path)
        updated = Idea.model_validate({**current.model_dump(), **changes})
        self._write_idea(path, updated)
        return updated

    def ideas(self, project: str) -> list[Idea]:
        return self._ideas_and_problems(project)[0]

    def _ideas_and_problems(self, project: str) -> tuple[list[Idea], list[str]]:
        """Read every idea; a broken file is reported, never fatal for the others."""
        folder = self.require(project) / "ideas"
        ideas, problems = [], []
        for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
            try:
                ideas.append(self._read_idea(path))
            except ProjectError as exc:
                problems.append(str(exc)[:200])
        return ideas, problems

    def _read_idea(self, path: Path) -> Idea:
        if not SEGMENT.fullmatch(path.stem):
            raise ProjectError(f"{path.name}: invalid idea file name")
        front, body = _read_front_matter(path.read_text(), path.name)
        return _validated(Idea, {**front, "slug": path.stem, "notes": body.strip()}, path.name)

    def _write_idea(self, path: Path, idea: Idea) -> None:
        front = idea.model_dump(mode="json", exclude={"slug", "notes"})
        path.write_text(f"---\n{yaml.safe_dump(front, sort_keys=False)}---\n\n{idea.notes}\n")

    # ---- logbook and runs --------------------------------------------------

    def log(self, project: str, text: str, heading: str = "") -> None:
        title = " ".join(heading.split())[:120]
        entry = f"## {_now()}{' · ' + title if title else ''}\n\n{_quote(text)}\n\n"
        with (self.require(project) / LOGBOOK).open("a") as handle:
            handle.write(entry)

    def logbook_tail(self, project: str, entries: int = 5) -> list[str]:
        path = self.require(project) / LOGBOOK
        if not path.is_file():
            return []
        chunks = [c.strip() for c in path.read_text().split("\n## ")[1:]]
        return chunks[-entries:]

    def link_run(self, project: str, run_id: str) -> None:
        link = self.require(project) / "runs" / run_id
        if link.is_symlink() or link.exists():
            return
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(self.settings.runs_dir / run_id)
