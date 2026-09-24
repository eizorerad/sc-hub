from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Sequence

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from scipy import sparse

from schub.bricks import PlanContext
from schub.config import Settings
from schub.library import celltypist_dirs

HUMAN_GENES = ["CD3E", "CD4", "MS4A1", "LYZ", "NKG7", "GAPDH", "ACTB", "C1orf112"] + [
    f"GENE{i}" for i in range(24)
] + ["MT-CO1", "MT-ND1"]


def make_adata(
    n_obs: int = 60,
    genes: Sequence[str] | None = None,
    x: str = "counts",
    counts_layer: bool = False,
    seed: int = 0,
) -> ad.AnnData:
    rng = np.random.default_rng(seed)
    genes = list(genes or HUMAN_GENES)
    counts = rng.poisson(2.0, size=(n_obs, len(genes))).astype(np.float32) + 1
    obs = pd.DataFrame(
        {
            "donor": pd.Categorical([f"d{i % 4}" for i in range(n_obs)]),
            "label": pd.Categorical(["ctrl" if i % 2 == 0 else "stim" for i in range(n_obs)]),
            "sample_name": [f"s{i % 3}" for i in range(n_obs)],
            "score": rng.random(n_obs),
        },
        index=[f"cell{i}" for i in range(n_obs)],
    )
    if x == "counts":
        matrix = sparse.csr_matrix(counts)
    elif x == "lognorm":
        matrix = sparse.csr_matrix(np.log1p(counts / counts.sum(axis=1, keepdims=True) * 1e4))
    elif x == "dense_lognorm":
        matrix = np.log1p(counts / counts.sum(axis=1, keepdims=True) * 1e4)
    elif x == "scaled":
        matrix = counts - counts.mean(axis=0)
    else:
        raise ValueError(x)
    adata = ad.AnnData(X=matrix, obs=obs, var=pd.DataFrame(index=genes))
    if counts_layer:
        adata.layers["counts"] = sparse.csr_matrix(counts)
    return adata


@pytest.fixture
def write_h5ad(tmp_path: Path) -> Callable[..., Path]:
    def _write(adata: ad.AnnData, name: str = "data.h5ad", directory: Path | None = None) -> Path:
        target = (directory or tmp_path) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        adata.write_h5ad(target)
        return target

    return _write


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A student root plus a readable shared library next to it."""
    root = tmp_path / "root"
    library = tmp_path / "library"
    for directory in (root / "data", library / "datasets", library / "models"):
        directory.mkdir(parents=True)
    return Settings(root=root, python=root / "env" / "bin" / "python", library=library)


def library_datasets(settings: Settings) -> Path:
    assert settings.library is not None
    return settings.library / "datasets"


@pytest.fixture
def ctx(settings: Settings) -> PlanContext:
    folder = celltypist_dirs(settings)[0]
    folder.mkdir(parents=True)
    (folder / "Immune_All_Low.pkl").write_bytes(b"model")
    return PlanContext(celltypist_dirs=celltypist_dirs(settings), limits=settings.limits)


class FakeCluster:
    """Simulates sbatch/sacct/squeue/scancel/sinfo for tests."""

    ACTIVE = {"PENDING", "RUNNING", "COMPLETING"}

    def __init__(self) -> None:
        self.next_id = 1000
        self.jobs: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.scripts: dict[str, str] = {}
        self.fail_sbatch_at: int | None = None
        self.sacct_down = False
        self.recently_finished: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.comments: dict[str, str] = {}
        self.updates: list[dict[str, str]] = []

    def _ok(self, args: Sequence[str], out: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(args), 0, out, "")

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        handler = getattr(self, f"_{args[0]}")
        return handler(args)

    def _sbatch(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if self.fail_sbatch_at is not None and len(self.jobs) >= self.fail_sbatch_at:
            return subprocess.CompletedProcess(args, 1, "", "sbatch: error: QOSMaxSubmitJobPerUserLimit")
        job_id = str(self.next_id)
        self.next_id += 1
        script = Path(args[-1]).read_text()
        self.scripts[job_id] = script
        self.names[job_id] = next(
            line.split("=", 1)[1] for line in script.splitlines() if line.startswith("#SBATCH --job-name=")
        )
        self.jobs[job_id] = "PENDING"
        comment = next((line.split("=", 1)[1] for line in script.splitlines() if line.startswith("#SBATCH --comment=")), "")
        if comment:
            self.comments[job_id] = comment
        return self._ok(args, f"{job_id}\n")

    def _sacct(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if self.sacct_down:
            return subprocess.CompletedProcess(args, 1, "", "sacct: error: Connection refused")
        ids = args[args.index("-j") + 1].split(",")
        return self._ok(args, "".join(f"{i}|{self.jobs[i]}\n" for i in ids if i in self.jobs))

    def _squeue(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if "--me" in args and args[args.index("-o") + 1] == "%i|%k":
            rows = [f"{i}|{self.comments.get(i, '')}\n" for i, s in self.jobs.items() if s in self.ACTIVE]
            return self._ok(args, "".join(rows))
        if "--me" in args:
            wide = args[args.index("-o") + 1].count("|") >= 5
            extra = "|0:42|None|ws-ia|1:00:00" if wide else ""
            rows = [f"{i}|{self.names[i]}|{s}{extra}\n" for i, s in self.jobs.items() if s in self.ACTIVE]
            return self._ok(args, "".join(rows))
        ids = args[args.index("-j") + 1].split(",")
        rows = [f"{i}|{self.jobs[i]}\n" for i in ids if self.jobs.get(i) in self.ACTIVE]
        return subprocess.CompletedProcess(args, 0 if rows else 1, "".join(rows), "")

    def _scontrol(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[1] == "update":
            fields = dict(a.split("=", 1) for a in args[2:])
            self.updates.append(fields)
            return self._ok(args)
        job_id = args[-1]
        if job_id in self.recently_finished:
            return self._ok(args, f"JobId={job_id} JobName=x JobState={self.recently_finished[job_id]} Reason=None\n")
        return subprocess.CompletedProcess(args, 1, "", "slurm_load_jobs error: Invalid job id specified")

    def _scancel(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        for job_id in args[1:]:
            self.jobs[job_id] = "CANCELLED"
        return self._ok(args)

    def _sinfo(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self._ok(
            args,
            "ws-ia*|up|1-00:00:00|21/94/1/116|gpu:nvidia-rtx-5000-ada-generation:1\n"
            "gpu|up|infinite|3/0/1/4|gpu:nvidia-rtx-5000-ada-generation:8\n",
        )

    def dependencies(self, job_id: str) -> str | None:
        for line in self.scripts[job_id].splitlines():
            if line.startswith("#SBATCH --dependency="):
                return line.split("=", 1)[1]
        return None


@pytest.fixture
def cluster() -> FakeCluster:
    return FakeCluster()
