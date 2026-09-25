"""Import a Seurat object (.rds) as an sc-hub dataset in data/<name>/.

Seurat is the R ecosystem's standard; sc-hub works in AnnData. R (the library's
r-seurat tool) writes counts, metadata and embeddings as plain files; Python
assembles them into data.h5ad with raw counts in X when the object has them.
Runs as a Slurm job:  python -m schub.seurat --rds <file> --name <name>
or from a bench cell: bench.import_seurat(rds, name). Without R + Seurat in the shared
library, the first import builds them once into the student's own library
(scripts/build_tools.sh, conda-forge; about 2 GB and 10-20 minutes).
"""

from __future__ import annotations

import argparse
import re
import secrets
import shutil
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from .bricks import Resources
from .config import Settings
from .library import find_tool
from .locking import LockTimeout, exclusive
from .slurm import JobSpec, Slurm, render_script
from .state import Frozen
from .streaming import run_streamed

R_SCRIPT = Path(__file__).with_name("seurat_export.R")
BUILD_TOOLS = next((p for p in (Path(__file__).resolve().parents[2] / "scripts" / "build_tools.sh",  # a checkout
                                 Path(__file__).with_name("build_tools.sh"))  # a wheel (force-include)
                    if p.is_file()), Path(__file__).with_name("build_tools.sh"))
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class SeuratImportError(ValueError):
    """Bad input for an import (kept distinct from the builtin ImportError)."""


class ImportJob(Frozen):
    name: str
    job_id: str
    log: str
    target: str
    note: str


def _rscript(settings: Settings) -> Path:
    found = find_tool(settings.library_roots, "r-seurat", "bin/Rscript")
    if found is None:
        raise SeuratImportError("R + Seurat is not installed in the library (tools/r-seurat); ask the library owner")
    return found


def ensure_r(settings: Settings, out=None) -> Path:
    """Rscript with Seurat: the library's, else built once into the student's own library (streamed to `out`)."""
    found = find_tool(settings.library_roots, "r-seurat", "bin/Rscript")
    if found is not None:
        return found
    if not BUILD_TOOLS.is_file():
        raise SeuratImportError("R + Seurat is not installed in the library (tools/r-seurat), and this sc-hub has no "
                                "scripts/build_tools.sh to build it; ask the library owner")
    settings.local_library.mkdir(parents=True, exist_ok=True)
    code, tail = 0, []
    try:  # one build at a time (two projects importing at once would remove each other's half-built folder)
        with exclusive(settings.local_library / ".r-seurat.lock", wait_s=3600, stale_after_s=300, heartbeat_s=30):
            if find_tool(settings.library_roots, "r-seurat", "bin/Rscript") is None:  # not built while we waited
                code, tail = _build_r(settings, out or sys.stdout)
    except LockTimeout as exc:
        raise SeuratImportError(f"another import has been building R + Seurat for an hour: {exc}") from None
    found = find_tool(settings.library_roots, "r-seurat", "bin/Rscript")
    if code != 0 or found is None:
        raise SeuratImportError(f"building R + Seurat failed (exit {code}). Last lines:\n" + "".join(tail))
    return found


def _build_r(settings: Settings, out) -> tuple[int, list[str]]:
    out.write("[sc-hub] R + Seurat are not installed yet: building them once into your own library "
              "(conda-forge, about 2 GB, 10-20 minutes)\n")
    return run_streamed(["bash", str(BUILD_TOOLS), str(settings.local_library)], out,
                        env={**os.environ, "SCHUB_TOOLS": "r-seurat"}, keep=30)


def check_request(settings: Settings, rds: str, name: str) -> tuple[Path, Path]:
    if not NAME.fullmatch(name):
        raise SeuratImportError(f"invalid dataset name '{name}' (letters, digits, _ . -)")
    path = Path(rds).expanduser()
    path = (path if path.is_absolute() else settings.root / path).resolve()
    roots = [r.resolve() for r in settings.allowed_roots if r.exists()]
    if path.suffix.lower() != ".rds" or not path.is_file() or not any(path.is_relative_to(r) for r in roots):
        raise SeuratImportError(f"'{rds}' is not an .rds file inside your sc-hub folder (or a library); copy it into "
                                f"{settings.data_dir} or the project's data/")
    target = settings.data_dir / name
    if target.exists():
        raise SeuratImportError(f"{target} already exists; choose another name")
    return path, target


def submit_import(settings: Settings, slurm: Slurm, rds: str, name: str) -> ImportJob:
    path, target = check_request(settings, rds, name)
    _rscript(settings)
    logs = settings.logs_dir / "import"
    logs.mkdir(parents=True, exist_ok=True)
    spec = JobSpec(
        name=f"{settings.job_prefix}-import-{name}"[:120],
        partition=settings.partition,
        resources=Resources(cpus=4, mem_gb=32, time_min=60),
        log_path=logs / f"seurat-{name}-%j.log",
        workdir=settings.root,
        command=(str(settings.python), "-m", "schub.seurat", "--rds", str(path), "--name", name),
        env=(("SCHUB_ROOT", str(settings.root)), ("SCHUB_LIBRARY", str(settings.library or "")),
             ("SCHUB_PYTHON", str(settings.python))),
    )
    script = logs / f"seurat-{name}.sbatch"
    script.write_text(render_script(spec))
    job_id = slurm.submit(script)
    return ImportJob(
        name=name, job_id=job_id, log=str(logs / f"seurat-{name}-{job_id}.log"), target=str(target / "data.h5ad"),
        note="When the job finishes, the dataset appears in list_datasets under this name.",
    )


def _obsm(folder: Path, cells: list[str]) -> dict[str, Any]:
    import numpy as np
    import pandas as pd

    embeddings = {}
    for csv in sorted(folder.glob("emb_*.csv")):
        frame = pd.read_csv(csv, index_col=0).reindex(cells)
        key = "X_" + re.sub(r"[^a-z0-9_]", "_", csv.stem.removeprefix("emb_").lower())
        embeddings[key] = frame.to_numpy(dtype="float32", na_value=np.nan)
    return embeddings


def assemble(folder: Path) -> Any:
    """AnnData from the files seurat_export.R wrote."""
    import anndata as ad
    import pandas as pd
    import scipy.io
    import scipy.sparse as sp

    genes = (folder / "genes.txt").read_text().splitlines()
    cells = (folder / "cells.txt").read_text().splitlines()
    matrix = sp.csr_matrix(scipy.io.mmread(folder / "matrix.mtx").T, dtype="float32")
    types_file = folder / "meta_types.txt"
    types = dict(line.split("\t", 1) for line in types_file.read_text().splitlines() if "\t" in line) \
        if types_file.is_file() else {}
    labels = {name for name, kind in types.items() if kind in ("factor", "character", "logical")}
    obs = pd.read_csv(folder / "meta.csv", index_col=0, dtype={name: str for name in labels}).reindex(cells)
    for column in obs.columns:
        if column in labels or obs[column].dtype == object:
            obs[column] = obs[column].astype(str).astype("category")
    info = dict(line.split(" ", 1) for line in (folder / "info.txt").read_text().splitlines() if " " in line)
    adata = ad.AnnData(matrix, obs=obs, var=pd.DataFrame(index=genes), obsm=_obsm(folder, cells))
    adata.var_names_make_unique()
    adata.uns["schub_import"] = {"from": "Seurat .rds", **info}
    return adata


def run_import(settings: Settings, rds: str, name: str) -> Path:
    from .fetch import _publish_dataset

    path, target = check_request(settings, rds, name)
    work = settings.cache_dir / f"seurat-{name}-{secrets.token_hex(3)}"
    try:
        done = subprocess.run([str(_rscript(settings)), str(R_SCRIPT), str(path), str(work)], capture_output=True,
                              text=True)
        if done.returncode != 0:
            raise SeuratImportError(f"R could not read {path.name} as a Seurat object (exit {done.returncode}): "
                                    + (done.stderr or done.stdout).strip()[-800:])
        adata = assemble(work)
        kind = adata.uns["schub_import"].get("kind", "counts")
        return _publish_dataset(adata, target, {
            "title": f"{name} (imported from Seurat)",
            "organism": "unknown",
            "license": "as the source object",
            "description": f"Imported from {path.name}: X holds the Seurat '{kind}' layer"
            + ("" if kind == "counts" else " (normalized; no raw counts in the object)"),
            "source_file": str(path),
        })
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: Sequence[str] | None = None) -> int:
    from .config import load_settings

    parser = argparse.ArgumentParser(prog="schub.seurat")
    parser.add_argument("--rds", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    print(f"[sc-hub] imported {run_import(load_settings(), args.rds, args.name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
