"""What a cell can use as `bench` (imported into every bench kernel):

    bench.fetch(url, dest=None, sha256=None, md5=None)   a resumable, recorded download
    bench.twin(path, stratify=None, keep=(), fraction=0.05)   a small stratified copy to try code on
    bench.run_brick(name, input, output, params)   a checked sc-hub brick
    bench.project_dir(), bench.work_dir(), bench.data_dir()
    %%slurm --gpus 1 --time 6h ...              the cell as a Slurm job (see skills('slurm_jobs'))
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .fetch import FetchError, fetch

_cell: dict[str, Any] = {"ref": "", "checks": []}


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


def run_brick(name: str, input: str | os.PathLike, output: str | os.PathLike | None = None,
              params: dict | None = None, results_dir: str | os.PathLike | None = None) -> dict:
    from .bricks_lib import run_brick as _run

    return _run(name, input, output, params or {}, results_dir)


__all__ = ["FetchError", "current_cell", "current_checks", "data_dir", "fetch", "project_dir", "run_brick",
           "set_cell", "twin", "work_dir"]
