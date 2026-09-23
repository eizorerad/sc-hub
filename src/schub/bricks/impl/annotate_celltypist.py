from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..annotate_celltypist import PREFIX, CelltypistParams
from ..base import BrickError, StepIO
from .common import read, save_embedding, setup_scanpy, write

MIN_GENE_OVERLAP = 100


def run(io: StepIO, p: CelltypistParams) -> dict[str, Any]:
    sc = setup_scanpy(io)
    import celltypist
    from celltypist import models

    adata = read(io)
    folders = [Path(d) for d in io.context.get("celltypist_dirs", "").split(os.pathsep) if d]
    model_path = next((d / p.model for d in folders if (d / p.model).is_file()), None)
    if model_path is None:
        raise BrickError(f"CellTypist model {p.model} not found in {[str(d) for d in folders]}")
    model = models.Model.load(str(model_path))
    overlap = len(set(model.features) & set(adata.var_names))
    if overlap < MIN_GENE_OVERLAP:
        raise BrickError(
            f"Only {overlap} of the model's genes are present; check gene naming and species."
        )
    over = p.over_clustering if p.majority_voting else None
    predictions = celltypist.annotate(
        adata, model=model, majority_voting=p.majority_voting, over_clustering=over
    )
    labeled = predictions.to_adata(insert_labels=True, insert_conf=True, prefix=PREFIX)
    label_column = f"{PREFIX}majority_voting" if p.majority_voting else f"{PREFIX}predicted_labels"
    counts = labeled.obs[label_column].value_counts()
    figure = save_embedding(sc, labeled, [label_column], io.results_dir / "umap_celltypist.png")
    write(labeled, io.output)
    return {
        "model": p.model,
        "model_genes_overlap": overlap,
        "label_column": label_column,
        "n_labels": int(counts.size),
        "top_labels": {str(k): int(v) for k, v in counts.head(15).items()},
        "median_conf_score": float(labeled.obs[f"{PREFIX}conf_score"].median()),
        "figure": figure,
    }
