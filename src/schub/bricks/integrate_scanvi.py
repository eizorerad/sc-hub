"""Semi-supervised integration with scANVI: scVI plus known cell labels (GPU).

Use it when some cells carry trusted labels (an annotated reference, the
authors' cell types, or CellTypist calls to refine): the latent space respects
the labels, and unlabeled cells get predicted ones.
"""

from __future__ import annotations

from pydantic import Field

from ..state import DatasetState, Issue, error
from .base import BrickParams, BrickSpec, PlanContext, Resources, need_obs, need_raw_counts, scaled
from .integrate_scvi import ScviParams
from .integrate_scvi import check as check_scvi


class ScanviParams(ScviParams):
    labels_key: str = Field(description="obs column with cell labels; cells with unlabeled_category are predicted.")
    unlabeled_category: str = Field("Unknown", description="Label value that marks unlabeled cells.")
    scanvi_epochs: int = Field(20, ge=1, le=500, description="scANVI fine-tuning epochs after scVI pretraining.")


def check(state: DatasetState, p: ScanviParams, ctx: PlanContext) -> list[Issue]:
    issues = check_scvi(state, p, ctx)
    issues += need_obs(state, p.labels_key, "labels_key")
    if p.labels_key in (p.batch_key, p.condition_key):
        issues.append(error("labels_is_design", "labels_key must differ from batch_key and condition_key."))
    labels = state.obs_column(p.labels_key)
    if labels is not None and labels.kind == "numeric":
        issues.append(error("numeric_labels", f"'{p.labels_key}' is numeric; scANVI needs categorical labels."))
    return [i for i in issues if i.code != "needs_raw_counts"] + need_raw_counts(state, "integrate_scanvi", "scANVI models counts")


def transform(state: DatasetState, p: ScanviParams) -> DatasetState:
    return (
        state.with_obsm("X_scANVI", "X_umap")
        .with_obs("leiden", "scanvi_label")
        .with_flags("integrated", "neighbors", "umap")
    )


def resources(state: DatasetState, p: ScanviParams) -> Resources:
    return Resources(cpus=8, mem_gb=scaled(state, 24, 16), time_min=scaled(state, 30, 40), gpus=1)


SPEC = BrickSpec(
    name="integrate_scanvi",
    version="0.1.0",
    summary="scVI pretraining + scANVI with cell labels: label-aware latent (X_scANVI), predicted "
    "'scanvi_label' for every cell, then neighbors/UMAP/Leiden. Needs 1 GPU.",
    params_model=ScanviParams,
    check=check,
    transform=transform,
    resources=resources,
    impl="schub.bricks.impl.integrate_scanvi:run",
    uses_gpu=True,
)
