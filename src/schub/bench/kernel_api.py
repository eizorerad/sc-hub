"""What a cell can use as `bench` (imported into every bench kernel):

    bench.fetch(url, dest=None, sha256=None, md5=None)   a resumable, recorded download
    bench.twin(path, stratify=None, keep=(), fraction=0.05)   a small stratified copy to try code on
    bench.clone(url, ref=None)                   a paper's repository at a recorded commit (work/repos/<name>)
    bench.repo_env(repo, python, torch, cuda, requirements)   its own uv environment; returns its python
    bench.packages(pip=[...], conda=[...])       add what the shared environment lacks to this project's kernel
    bench.import_seurat(rds, name)               a Seurat .rds as data/<name>/data.h5ad (R built once if needed)
    bench.compare(ours, paper, source="Table 2 ...")            our numbers next to the paper's
    from schub_ckpt import Run                   checkpoints that survive time limits (skills('paper_reproduction'))
    bench.run_brick(name, input, output, params)   a checked sc-hub brick
    bench.project_dir(), bench.work_dir(), bench.data_dir()
    %%slurm --gpus 1 --time 6h ...              the cell as a Slurm job (see skills('slurm_jobs'))
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .fetch import FetchError, fetch

_cell: dict[str, Any] = {"ref": "", "checks": []}
PORTABLE = str(Path(__file__).with_name("portable"))
if PORTABLE not in sys.path:
    sys.path.append(PORTABLE)  # `from schub_ckpt import Run` works in the kernel as in jobs


def alias() -> None:
    """`import bench` works too (agents write it), unless the project has its own module of that name."""
    import importlib.util

    if "bench" in sys.modules:
        return
    try:
        own = importlib.util.find_spec("bench") is not None
    except (ImportError, ValueError):
        own = True  # something named bench exists but cannot be looked at: leave it alone
    if not own:
        sys.modules["bench"] = sys.modules[__name__]


def set_cell(ref: str, checks_json: str = "[]") -> None:
    """Called by the runner before each cell (so %%slurm knows which cell it belongs to)."""
    _cell["ref"] = ref
    try:
        checks = json.loads(checks_json)
    except ValueError:
        checks = []
    _cell["checks"] = checks if isinstance(checks, list) else []


def current_cell() -> str:
    return str(_cell["ref"])


def current_checks() -> list[dict]:
    return list(_cell["checks"])


def project_dir() -> Path:
    return Path(os.environ.get("SCHUB_PROJECT_DIR", Path.cwd().parent))


def work_dir() -> Path:
    return project_dir() / "work"


def data_dir() -> Path:
    return project_dir() / "data"


def twin(source: str | os.PathLike, stratify: str | None = None, keep: tuple[str, ...] | list[str] = (),
         fraction: float = 0.05, min_per_group: int = 20, max_keep: int = 2000, max_cells: int = 50_000,
         seed: int = 0) -> Path:
    from .twins import twin as _twin

    return _twin(source, stratify, keep, fraction, min_per_group, max_keep, max_cells, seed)


def clone(url: str, ref: str | None = None, dest: str | os.PathLike | None = None) -> Path:
    from .repos import clone as _clone

    return _clone(url, ref, dest)


def repo_env(repo: str | os.PathLike, python: str = "3.11", torch: str | None = None, cuda: str = "cu128",
             requirements: str | None = None, install_repo: bool = False, extra: tuple[str, ...] | list[str] = ()) -> Path:
    from .repos import environment

    return environment(repo, python, torch, cuda, requirements, install_repo, extra)


def compare(ours: dict, paper: dict, source: str, tolerance: float | dict = 0.10,
            name: str = "comparison", bounds: dict | None = None) -> list[dict]:
    from .compare import compare as _compare

    return _compare(ours, paper, source, tolerance, name, bounds)


def packages(pip: tuple[str, ...] | list[str] = (), conda: tuple[str, ...] | list[str] = (),
             remove: bool = False) -> dict:
    """Add packages the shared environment lacks (pip: PyPI names like "decoupler>=2"; conda: conda-forge or
    bioconda tools) to this project's own environment, built now in this cell (a few minutes; only what is
    missing is installed, at versions that fit the shared ones). The project's NEXT cell starts a fresh
    kernel with them: variables are lost, so re-run the setup cells. remove=True drops the listed ones.
    Without arguments: what the project has. A failed build keeps the previous kernel."""
    from ..config import load_settings
    from ..project_env import EnvError, build_here, built, merged, slug
    from ..slurm import Slurm, SlurmError

    pip = [pip] if isinstance(pip, str) else list(pip)  # packages("scvelo") is one package, not six letters
    conda = [conda] if isinstance(conda, str) else list(conda)
    settings = load_settings()
    project = os.environ.get("SCHUB_PROJECT", "")
    if not project:
        raise RuntimeError("bench.packages works in a bench cell (SCHUB_PROJECT is not set)")
    if pip or conda:  # a build the older tool queued as a job would race this one
        try:
            busy = [j.job_id for j in Slurm().my_jobs() if j.name == f"{settings.job_prefix}-env-{slug(project)}"]
        except SlurmError:
            busy = []
        if busy:
            raise RuntimeError(f"job {busy[0]} is building this project's environment; ask again when it has ended")
    if not pip and not conda:
        current = built(settings, project)
        return {"pip": list(current.pip) if current else [], "conda": list(current.conda) if current else [],
                "built": current.built if current else ""}
    try:
        new_pip, new_conda = merged(settings, project, pip, conda, remove)
        result = build_here(settings, project, new_pip, new_conda)
    except EnvError as exc:
        raise RuntimeError(str(exc)) from None
    return {"pip": list(new_pip), "conda": list(new_conda), "built": result.built if result else "",
            "next": "the next cell of this project starts a fresh kernel with these packages; re-run setup cells"}


def import_seurat(rds: str | os.PathLike, name: str) -> Path:
    """A Seurat object (.rds; a relative path is the project's, e.g. "data/x.rds") as an AnnData dataset:
    data/<name>/data.h5ad in the sc-hub folder, listed by datasets()
    (counts in X when the object has them, metadata in obs, embeddings in obsm). R + Seurat come from the
    shared library, or are built once into your own library (10-20 minutes). Large objects need memory:
    run it in a %%slurm cell with --mem 32G. Returns the path of its data.h5ad."""
    from ..config import load_settings
    from ..seurat import SeuratImportError, ensure_r, run_import

    settings = load_settings()
    path = Path(rds)
    path = path if path.is_absolute() else project_dir() / path  # "data/x.rds": the project's, as in files()
    try:
        ensure_r(settings)
        return run_import(settings, str(path), name)
    except SeuratImportError as exc:
        raise RuntimeError(str(exc)) from None


def run_brick(name: str, input: str | os.PathLike, output: str | os.PathLike | None = None,
              params: dict | None = None, results_dir: str | os.PathLike | None = None) -> dict:
    from .bricks_lib import run_brick as _run

    return _run(name, input, output, params or {}, results_dir)


__all__ = ["FetchError", "alias", "clone", "compare", "current_cell", "current_checks", "data_dir", "fetch", "import_seurat",
           "packages", "project_dir", "repo_env", "run_brick", "set_cell", "twin", "work_dir"]
