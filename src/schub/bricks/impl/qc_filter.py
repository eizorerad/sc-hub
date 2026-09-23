from __future__ import annotations

from typing import Any

from ..base import BrickError, StepIO
from ..qc_filter import QcParams
from .common import read, setup_scanpy, write

QC_COLUMNS = ("n_genes_by_counts", "total_counts", "pct_counts_mt")


def _plot_qc(obs: Any, p: QcParams, path: Any) -> None:
    import matplotlib.pyplot as plt

    thresholds = {"n_genes_by_counts": [p.min_genes, p.max_genes], "pct_counts_mt": [p.max_pct_mt]}
    fig, axes = plt.subplots(1, 3, figsize=(13, 3.2))
    for ax, column in zip(axes, QC_COLUMNS):
        ax.hist(obs[column], bins=80, color="#4c72b0")
        for value in thresholds.get(column, []):
            if value is not None:
                ax.axvline(value, color="#c44e52", linestyle="--")
        ax.set_title(column)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    fig.savefig(path.with_name(path.stem + "_thumb.png"), dpi=36)
    plt.close(fig)


def run(io: StepIO, p: QcParams) -> dict[str, Any]:
    sc = setup_scanpy(io)
    import numpy as np

    adata = read(io)
    if io.state_in.x_kind != "raw_counts" and "counts" in adata.layers:
        adata.X = adata.layers["counts"].copy()
    adata.var["mt"] = adata.var_names.str.upper().str.startswith("MT-")
    sc.pp.calculate_qc_metrics(adata, qc_vars=["mt"], percent_top=None, log1p=False, inplace=True)
    _plot_qc(adata.obs, p, io.results_dir / "qc_distributions.png")
    n_before = adata.n_obs
    keep = (adata.obs["n_genes_by_counts"] >= p.min_genes) & (adata.obs["pct_counts_mt"] <= p.max_pct_mt)
    if p.max_genes is not None:
        keep &= adata.obs["n_genes_by_counts"] <= p.max_genes
    adata = adata[keep.to_numpy()].copy()
    if adata.n_obs == 0:
        raise BrickError("QC thresholds removed every cell; relax min_genes / max_pct_mt.")
    sc.pp.filter_genes(adata, min_cells=p.min_cells)
    n_doublets = 0
    if p.detect_doublets:
        sc.pp.scrublet(adata, batch_key=p.batch_key)
        n_doublets = int(adata.obs["predicted_doublet"].sum())
        if p.remove_doublets:
            adata = adata[~adata.obs["predicted_doublet"].to_numpy()].copy()
    adata.layers["counts"] = adata.X.copy()
    write(adata, io.output)
    mt_genes = int(adata.var["mt"].sum())
    warnings = ["no MT- genes: the % mitochondrial filter had no effect"] if mt_genes == 0 else []
    return {
        "warnings": warnings,
        "cells_before": n_before,
        "cells_after_thresholds": int(keep.sum()),
        "doublets_detected": n_doublets,
        "cells_final": int(adata.n_obs),
        "genes_final": int(adata.n_vars),
        "mt_genes_found": mt_genes,
        "median_genes_per_cell": float(np.median(adata.obs["n_genes_by_counts"])),
        "median_pct_mt": float(np.median(adata.obs["pct_counts_mt"])),
    }
