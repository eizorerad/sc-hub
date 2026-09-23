"""Shared helpers for brick implementations (run inside jobs only)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Sequence

from ..base import BrickError, StepIO


def setup_scanpy(io: StepIO) -> Any:
    import matplotlib

    matplotlib.use("Agg")
    import scanpy as sc

    sc.settings.verbosity = 2
    sc.settings.n_jobs = int(os.environ.get("OMP_NUM_THREADS", "4"))
    sc.settings.figdir = io.results_dir
    return sc


def read(io: StepIO) -> Any:
    import anndata as ad
    import h5py

    from ...h5ad_profile import reject_external_storage

    with h5py.File(io.input, "r") as handle:
        reject_external_storage(handle)
    adata = ad.read_h5ad(io.input)
    adata.var_names_make_unique()
    return adata


def counts_source(adata: Any, io: StepIO) -> tuple[Any, str | None]:
    """Raw counts as the planner saw them: layers['counts'] if verified, else X."""
    if io.state_in.counts_layer:
        return adata.layers["counts"], "counts"
    if io.state_in.x_kind == "raw_counts":
        return adata.X, None
    raise BrickError("no raw counts in this dataset (planner state disagrees with the file)")


def write(adata: Any, path: Path | None) -> None:
    if path is None:
        raise ValueError("this brick must write an output .h5ad but no output path was given")
    partial = path.with_name(path.stem + ".partial.h5ad")
    adata.write_h5ad(partial)
    os.replace(partial, path)


def save_embedding(sc: Any, adata: Any, colors: Sequence[str], path: Path, basis: str = "umap") -> str | None:
    import matplotlib.pyplot as plt

    present = [c for c in colors if c and c in adata.obs.columns]
    if f"X_{basis}" not in adata.obsm or not present:
        return None
    fig = sc.pl.embedding(adata, basis=basis, color=present, show=False, return_fig=True, ncols=2)
    fig.savefig(path, dpi=120, bbox_inches="tight")
    # Small copy for the dashboard, which must stay light on weak laptops.
    fig.savefig(path.with_name(path.stem + "_thumb.png"), dpi=36, bbox_inches="tight")
    plt.close(fig)
    return str(path)


def library_roots(io: StepIO) -> tuple[Path, ...]:
    """Library roots the submitting side saw (shared first, then the local fallback)."""
    return tuple(Path(p) for p in io.context.get("library_roots", "").split(os.pathsep) if p)


def tail(path: Path, lines: int) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""
