from __future__ import annotations

import os
from typing import Any

from ..base import BrickError, StepIO
from ..integrate_scvi import ScviParams
from .common import counts_source, read, save_embedding, setup_scanpy, write


def guard_confounding(obs: Any, p: ScviParams) -> None:
    """Refuse when every batch holds a single condition: scVI would erase the effect."""
    import pandas as pd

    batches = obs[p.batch_key].astype(str)
    if batches.nunique() < 2:
        raise BrickError(f"'{p.batch_key}' has a single level; nothing to integrate.")
    if p.condition_key is None:
        return
    table = pd.crosstab(batches, obs[p.condition_key].astype(str))
    if table.shape[1] > 1 and ((table > 0).sum(axis=1) == 1).all():
        raise BrickError(
            f"Every '{p.batch_key}' level contains exactly one '{p.condition_key}' level: batch and "
            "condition are confounded, so integration would remove the condition effect. "
            "Integrate on another key or skip integration."
        )


def _last(history: dict[str, Any], key: str) -> float | None:
    frame = history.get(key)
    return None if frame is None or frame.empty else float(frame.iloc[-1, 0])


def run(io: StepIO, p: ScviParams) -> dict[str, Any]:
    sc = setup_scanpy(io)
    import scvi
    import torch

    adata = read(io)
    guard_confounding(adata.obs, p)
    scvi.settings.seed = 0
    use_hvg = "highly_variable" in adata.var.columns
    bdata = adata[:, adata.var["highly_variable"].to_numpy()].copy() if use_hvg else adata.copy()
    _, layer = counts_source(bdata, io)
    scvi.model.SCVI.setup_anndata(bdata, layer=layer, batch_key=p.batch_key)
    model = scvi.model.SCVI(bdata, n_latent=p.n_latent)
    cuda = torch.cuda.is_available()
    if not cuda and os.environ.get("CUDA_VISIBLE_DEVICES"):
        raise BrickError(
            f"The job has a GPU but torch {torch.__version__} (CUDA {torch.version.cuda}) cannot use it; "
            "the driver is probably too old for this build. Rebuild the env with SCHUB_TORCH_BACKEND=cu128."
        )
    model.train(
        max_epochs=p.max_epochs, accelerator="gpu" if cuda else "cpu", devices=1, early_stopping=True
    )
    adata.obsm["X_scVI"] = model.get_latent_representation()
    sc.pp.neighbors(adata, use_rep="X_scVI")
    sc.tl.umap(adata)
    sc.tl.leiden(adata, resolution=p.leiden_resolution, flavor="igraph", n_iterations=2, directed=False, key_added="leiden")
    model.save(str(io.results_dir / "scvi_model"), overwrite=True)
    figure = save_embedding(
        sc, adata, [p.batch_key, p.condition_key, "leiden"], io.results_dir / "umap_scvi.png"
    )
    write(adata, io.output)
    return {
        "device": torch.cuda.get_device_name(0) if cuda else "cpu",
        "epochs": int(len(model.history["elbo_train"])),
        "elbo_train_last": _last(model.history, "elbo_train"),
        "elbo_validation_last": _last(model.history, "elbo_validation"),
        "genes_used": int(bdata.n_vars),
        "n_clusters": int(adata.obs["leiden"].nunique()),
        "figure": figure,
    }
