"""Import a Seurat object (.rds) as an sc-hub dataset in data/<name>/.

Seurat is the R ecosystem's standard; sc-hub works in AnnData. R (the library's
r-seurat tool) writes counts, metadata and embeddings as plain files; Python
assembles them into data.h5ad with raw counts in X when the object has them.
Runs as a Slurm job:  python -m schub.seurat --rds <file> --name <name>
"""

from __future__ import annotations

import argparse
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from .bricks import Resources
from .config import Settings
from .library import find_tool
from .slurm import JobSpec, Slurm, render_script
from .state import Frozen

R_SCRIPT = Path(__file__).with_name("seurat_export.R")
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


def check_request(settings: Settings, rds: str, name: str) -> tuple[Path, Path]:
    if not NAME.fullmatch(name):
        raise SeuratImportError(f"invalid dataset name '{name}' (letters, digits, _ . -)")
    path = Path(rds).expanduser()
    path = (path if path.is_absolute() else settings.root / path).resolve()
    roots = [r.resolve() for r in settings.allowed_roots if r.exists()]
    if path.suffix.lower() != ".rds" or not path.is_file() or not any(path.is_relative_to(r) for r in roots):
        raise SeuratImportError(f"'{rds}' is not an .rds file inside the sc-hub areas; copy it into {settings.data_dir}")
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
        subprocess.run([str(_rscript(settings)), str(R_SCRIPT), str(path), str(work)], check=True)
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
