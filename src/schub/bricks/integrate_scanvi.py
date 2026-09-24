"""Semi-supervised integration with scANVI: scVI plus known cell labels (GPU).

Use it when some cells carry trusted labels (an annotated reference, the
authors' cell types, or CellTypist calls to refine): the latent space respects
the labels, and unlabeled cells get predicted ones.

A share of the labeled cells is hidden during training (holdout_fraction), and
the step reports accuracy on them. Agreement on the training labels says little:
on Kang 2018, with every cell labeled, it was 0.998 by construction.
"""

from __future__ import annotations

from pydantic import Field

from ..state import DatasetState, Issue, error, warning
from .base import BrickParams, BrickSpec, PlanContext, Resources, need_obs, need_raw_counts, scaled
from .integrate_scvi import ScviParams
from .integrate_scvi import check as check_scvi


class ScanviParams(ScviParams):
    labels_key: str = Field(description="obs column with cell labels; cells with unlabeled_category are predicted.")
    unlabeled_category: str = Field("Unknown", description="Label value that marks unlabeled cells.")
    scanvi_epochs: int = Field(20, ge=1, le=500, description="scANVI fine-tuning epochs after scVI pretraining.")
    holdout_fraction: float = Field(
        0.1, ge=0, le=0.5,
        description="Share of labeled cells (per label) whose labels are hidden during training; accuracy "
        "is measured on them. 0 disables the check.",
    )


def check(state: DatasetState, p: ScanviParams, ctx: PlanContext) -> list[Issue]:
    issues = check_scvi(state, p, ctx)
    issues += need_obs(state, p.labels_key, "labels_key")
    if p.labels_key in (p.batch_key, p.condition_key):
        issues.append(error("labels_is_design", "labels_key must differ from batch_key and condition_key."))
    labels = state.obs_column(p.labels_key)
    if labels is not None and labels.kind == "numeric":
        issues.append(error("numeric_labels", f"'{p.labels_key}' is numeric; scANVI needs categorical labels."))
    fully_labeled = (labels is not None and labels.levels_known and p.unlabeled_category not in labels.top
                     and sum(labels.top.values()) >= state.n_obs)  # no NaN labels either
    if fully_labeled and p.holdout_fraction == 0:
        issues.append(warning(
            "no_holdout",
            f"Every cell has a label in '{p.labels_key}' and holdout_fraction is 0: the reported agreement "
            "would be measured on scANVI's own training labels. Keep a holdout to measure accuracy.",
        ))
    return [i for i in issues if i.code != "needs_raw_counts"] + need_raw_counts(state, "integrate_scanvi", "scANVI models counts")


def writes_obs(p: ScanviParams) -> tuple[str, ...]:
    return ("leiden", "scanvi_label", p.labels_key)  # unlabeled cells become unlabeled_category


def label_columns(p: ScanviParams) -> tuple[str, ...]:
    return ("scanvi_label",)


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
    keeps_counts=True,
    writes_obs=writes_obs,
    label_columns=label_columns,
)
