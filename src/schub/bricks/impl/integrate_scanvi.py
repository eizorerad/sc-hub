from __future__ import annotations

from typing import Any

from ..base import BrickError, StepIO
from ..integrate_scanvi import ScanviParams
from .common import counts_source, read, save_embedding, setup_scanpy, write
from .integrate_scvi import guard_confounding


def holdout_mask(labels: Any, unlabeled: str, fraction: float, seed: int = 0) -> Any:
    """Cells whose labels are hidden during training: per label, `fraction` of its
    labeled cells (rounded down), chosen reproducibly. `labels` is a pandas Series."""
    import numpy as np

    rng = np.random.default_rng(seed)
    values = labels.astype(str).to_numpy()
    held = np.zeros(len(values), dtype=bool)
    for label in sorted(set(values) - {unlabeled}):
        where = np.flatnonzero(values == label)
        count = int(len(where) * fraction)
        if count:
            held[rng.choice(where, size=count, replace=False)] = True
    return held


def _device() -> tuple[Any, bool]:
    import os

    import torch

    cuda = torch.cuda.is_available()
    if not cuda and os.environ.get("CUDA_VISIBLE_DEVICES"):
        raise BrickError(
            f"The job has a GPU but torch {torch.__version__} (CUDA {torch.version.cuda}) cannot use it; "
            "rebuild the env with SCHUB_TORCH_BACKEND=cu128."
        )
    return torch, cuda


def _train(scvi: Any, bdata: Any, io: StepIO, p: ScanviParams, accelerator: str) -> Any:
    """scVI pretraining, then scANVI fine-tuning with the (training) labels in bdata."""
    _, layer = counts_source(bdata, io)
    scvi.model.SCVI.setup_anndata(bdata, layer=layer, batch_key=p.batch_key, labels_key=p.labels_key)
    vae = scvi.model.SCVI(bdata, n_latent=p.n_latent)
    vae.train(max_epochs=p.max_epochs, accelerator=accelerator, devices=1, early_stopping=True)
    model = scvi.model.SCANVI.from_scvi_model(vae, unlabeled_category=p.unlabeled_category, labels_key=p.labels_key)
    model.train(max_epochs=p.scanvi_epochs, accelerator=accelerator, devices=1)
    return model


def run(io: StepIO, p: ScanviParams) -> dict[str, Any]:
    sc = setup_scanpy(io)
    import numpy as np
    import pandas as pd
    import scvi

    adata = read(io)
    guard_confounding(adata.obs, p)
    raw_labels = adata.obs[p.labels_key]
    labels = raw_labels.astype(object).where(raw_labels.notna(), p.unlabeled_category).astype(str)
    known = (labels != p.unlabeled_category).to_numpy()
    labeled = int(known.sum())
    if labeled == 0:
        raise BrickError(f"No cell has a label in '{p.labels_key}' (all are '{p.unlabeled_category}').")
    held = holdout_mask(labels, p.unlabeled_category, p.holdout_fraction)
    adata.obs[p.labels_key] = labels.astype("category")
    scvi.settings.seed = 0
    torch, cuda = _device()
    use_hvg = "highly_variable" in adata.var.columns
    bdata = adata[:, adata.var["highly_variable"].to_numpy()].copy() if use_hvg else adata.copy()
    # The model trains on the labels minus the held-out ones; the output keeps them all.
    bdata.obs[p.labels_key] = labels.where(~held, p.unlabeled_category).astype("category").to_numpy()
    model = _train(scvi, bdata, io, p, "gpu" if cuda else "cpu")
    adata.obsm["X_scANVI"] = model.get_latent_representation()
    predicted = np.asarray(model.predict()).astype(str)
    adata.obs["scanvi_label"] = pd.Categorical(predicted)
    truth, trained = labels.to_numpy(), known & ~held
    accuracy = round(float((predicted[held] == truth[held]).mean()), 4) if held.any() else None
    sc.pp.neighbors(adata, use_rep="X_scANVI")
    sc.tl.umap(adata)
    sc.tl.leiden(adata, resolution=p.leiden_resolution, flavor="igraph", n_iterations=2, directed=False, key_added="leiden")
    model.save(str(io.results_dir / "scanvi_model"), overwrite=True)
    figure = save_embedding(sc, adata, [p.batch_key, p.labels_key, "scanvi_label", "leiden"], io.results_dir / "umap_scanvi.png")
    write(adata, io.output)
    return {
        "device": torch.cuda.get_device_name(0) if cuda else "cpu",
        "labeled_cells": labeled,
        "unlabeled_cells": int(adata.n_obs - labeled),
        "holdout_cells": int(held.sum()),
        "holdout_accuracy": accuracy,
        "label_agreement_on_training": round(float((predicted[trained] == truth[trained]).mean()), 4),
        "n_labels": int(adata.obs["scanvi_label"].nunique()),
        "n_clusters": int(adata.obs["leiden"].nunique()),
        "genes_used": int(bdata.n_vars),
        "figure": figure,
    }
