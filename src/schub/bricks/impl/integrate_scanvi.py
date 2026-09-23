from __future__ import annotations

from typing import Any

from ..base import BrickError, StepIO
from ..integrate_scanvi import ScanviParams
from .common import counts_source, read, save_embedding, setup_scanpy, write
from .integrate_scvi import guard_confounding


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


def run(io: StepIO, p: ScanviParams) -> dict[str, Any]:
    sc = setup_scanpy(io)
    import scvi

    adata = read(io)
    guard_confounding(adata.obs, p)
    raw_labels = adata.obs[p.labels_key]
    labels = raw_labels.astype(object).where(raw_labels.notna(), p.unlabeled_category).astype(str)
    labeled = int((labels != p.unlabeled_category).sum())
    if labeled == 0:
        raise BrickError(f"No cell has a label in '{p.labels_key}' (all are '{p.unlabeled_category}').")
    adata.obs[p.labels_key] = labels.astype("category")
    scvi.settings.seed = 0
    torch, cuda = _device()
    use_hvg = "highly_variable" in adata.var.columns
    bdata = adata[:, adata.var["highly_variable"].to_numpy()].copy() if use_hvg else adata.copy()
    _, layer = counts_source(bdata, io)
    scvi.model.SCVI.setup_anndata(bdata, layer=layer, batch_key=p.batch_key, labels_key=p.labels_key)
    accelerator = "gpu" if cuda else "cpu"
    vae = scvi.model.SCVI(bdata, n_latent=p.n_latent)
    vae.train(max_epochs=p.max_epochs, accelerator=accelerator, devices=1, early_stopping=True)
    model = scvi.model.SCANVI.from_scvi_model(vae, unlabeled_category=p.unlabeled_category, labels_key=p.labels_key)
    model.train(max_epochs=p.scanvi_epochs, accelerator=accelerator, devices=1)
    adata.obsm["X_scANVI"] = model.get_latent_representation()
    adata.obs["scanvi_label"] = model.predict().astype(str)
    adata.obs["scanvi_label"] = adata.obs["scanvi_label"].astype("category")
    known = labels != p.unlabeled_category
    agreement = float((adata.obs["scanvi_label"].astype(str)[known] == labels[known]).mean())
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
        "label_agreement_on_labeled": round(agreement, 3),
        "n_labels": int(adata.obs["scanvi_label"].nunique()),
        "n_clusters": int(adata.obs["leiden"].nunique()),
        "genes_used": int(bdata.n_vars),
        "figure": figure,
    }
