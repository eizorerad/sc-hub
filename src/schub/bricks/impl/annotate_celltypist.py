from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from ..annotate_celltypist import PREFIX, CelltypistParams, label_column
from ..base import BrickError, StepIO
from .common import read, save_embedding, setup_scanpy, write

MIN_GENE_OVERLAP = 100


def reference_agreement(labels: Any, reference: Any) -> dict[str, Any]:
    """How CellTypist labels line up with known labels (pandas Series, same cells).

    purity: share of cells whose label's most common known type is their own type
    (a label may split a known type, but should not mix types). by_reference: the
    label most cells of each known type got, and that share.
    """
    import pandas as pd

    # By position, not by cell name: names repeat in multi-sample files.
    known = reference.notna().to_numpy()
    table = pd.crosstab(labels.to_numpy()[known].astype(str), reference.to_numpy()[known].astype(str),
                        rownames=["label"], colnames=["reference"])
    total = int(table.to_numpy().sum())
    by_reference = {
        str(kind): {"label": str(column.idxmax()), "share": round(float(column.max() / column.sum()), 3),
                    "cells": int(column.sum())}
        for kind, column in table.items() if column.sum() > 0
    }
    purity = float(table.max(axis=1).sum() / total) if total else 0.0
    return {"purity": round(purity, 3), "by_reference": by_reference, "table": table}


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
    column = label_column(p)
    counts = labeled.obs[column].value_counts()
    figure = save_embedding(sc, labeled, [column, p.reference_key], io.results_dir / "umap_celltypist.png")
    write(labeled, io.output)
    summary = {
        "model": p.model,
        "model_genes_overlap": overlap,
        "label_column": column,
        "over_clustering": p.over_clustering or "CellTypist's own",
        "n_labels": int(counts.size),
        "top_labels": {str(k): int(v) for k, v in counts.head(15).items()},
        "median_conf_score": float(labeled.obs[f"{PREFIX}conf_score"].median()),
        "figure": figure,
    }
    if p.reference_key:
        agreement = reference_agreement(labeled.obs[column], labeled.obs[p.reference_key])
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", p.reference_key)[:60]
        agreement.pop("table").to_csv(io.results_dir / f"celltypist_vs_{safe}.csv")
        summary.update(reference_key=p.reference_key, reference_purity=agreement["purity"],
                       by_reference=agreement["by_reference"])
    return summary
