from __future__ import annotations

from typing import Any

from ..base import StepIO
from ..normalize_embed import NormalizeParams
from .common import counts_source, read, save_embedding, setup_scanpy, write


def run(io: StepIO, p: NormalizeParams) -> dict[str, Any]:
    sc = setup_scanpy(io)
    adata = read(io)
    counts, _ = counts_source(adata, io)
    adata.layers["counts"] = counts.copy()
    adata.X = counts.copy()
    sc.pp.normalize_total(adata, target_sum=p.target_sum)
    sc.pp.log1p(adata)
    n_top = min(p.n_top_genes, adata.n_vars)
    sc.pp.highly_variable_genes(
        adata, n_top_genes=n_top, flavor="seurat_v3", layer="counts", batch_key=p.batch_key
    )
    n_comps = max(2, min(p.n_pcs, adata.n_obs - 1, n_top - 1))
    sc.tl.pca(adata, n_comps=n_comps, mask_var="highly_variable")
    sc.pp.neighbors(adata, n_neighbors=p.n_neighbors, n_pcs=n_comps)
    sc.tl.umap(adata)
    sc.tl.leiden(
        adata, resolution=p.leiden_resolution, flavor="igraph", n_iterations=2, directed=False, key_added="leiden"
    )
    figure = save_embedding(sc, adata, ["leiden", p.batch_key], io.results_dir / "umap_leiden.png")
    write(adata, io.output)
    return {
        "cells": int(adata.n_obs),
        "n_hvg": int(adata.var["highly_variable"].sum()),
        "n_pcs": n_comps,
        "n_clusters": int(adata.obs["leiden"].nunique()),
        "figure": figure,
    }
