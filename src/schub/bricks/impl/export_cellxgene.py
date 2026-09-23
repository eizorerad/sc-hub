from __future__ import annotations

import os
from typing import Any

from ..base import StepIO
from ..export_cellxgene import CellxgeneParams
from .common import counts_source, read, setup_scanpy

TARGET_SUM = 1e4


def run(io: StepIO, p: CellxgeneParams) -> dict[str, Any]:
    sc = setup_scanpy(io)
    import anndata as ad
    import numpy as np
    import scipy.sparse as sp

    adata = read(io)
    n_before = adata.n_obs
    if adata.n_obs > p.max_cells:
        keep = np.sort(np.random.default_rng(0).choice(adata.n_obs, p.max_cells, replace=False))
        adata = adata[keep].copy()
    if io.state_in.x_kind == "normalized_log":
        x = adata.X
    else:
        counts, _ = counts_source(adata, io)
        normalized = ad.AnnData(sp.csr_matrix(counts, dtype="float32"))
        sc.pp.normalize_total(normalized, target_sum=TARGET_SUM)
        sc.pp.log1p(normalized)
        x = normalized.X
    obs = adata.obs.copy()
    for column in obs.columns:
        if obs[column].dtype == object:
            obs[column] = obs[column].astype(str).astype("category")
    embeddings = {k: np.asarray(v, dtype="float32") for k, v in adata.obsm.items() if k.startswith("X_") and np.ndim(v) == 2}
    out = ad.AnnData(sp.csr_matrix(x, dtype="float32"), obs=obs, var=adata.var[[]].copy(), obsm=embeddings)
    target = io.results_dir / "cellxgene.h5ad"
    partial = target.with_name(".cellxgene.partial.h5ad")
    out.write_h5ad(partial, compression="gzip")
    os.replace(partial, target)
    return {
        "file": str(target),
        "size_mb": round(target.stat().st_size / 1e6, 1),
        "n_cells": int(out.n_obs),
        "n_cells_before": int(n_before),
        "n_genes": int(out.n_vars),
        "embeddings": sorted(embeddings),
        "how_to_view": "start_session(kind='cellxgene', target=<this file>) opens it on the cluster; "
        "or copy it to the laptop and run: cellxgene launch cellxgene.h5ad --open",
    }
