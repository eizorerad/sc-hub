"""Batch integration with scVI on raw counts (GPU)."""

from __future__ import annotations

from pydantic import Field

from ..state import DatasetState, Issue, error, warning
from .base import BrickParams, BrickSpec, PlanContext, Resources, need_obs, need_raw_counts, scaled


class ScviParams(BrickParams):
    batch_key: str = Field(description="obs column with the technical batch to remove.")
    condition_key: str | None = Field(
        None, description="Biological condition of interest; guarded against being integrated away."
    )
    n_latent: int = Field(30, ge=2, le=100)
    max_epochs: int | None = Field(None, ge=1, le=1000, description="Default: scvi heuristic.")
    leiden_resolution: float = Field(1.0, gt=0, le=10)


def check(state: DatasetState, p: ScviParams, ctx: PlanContext) -> list[Issue]:
    issues = need_raw_counts(state, "integrate_scvi", "scVI models count data")
    issues += need_obs(state, p.batch_key, "batch_key")
    issues += need_obs(state, p.condition_key, "condition_key")
    if p.condition_key == p.batch_key:
        issues.append(
            error(
                "batch_is_condition",
                "batch_key equals condition_key: integration would remove the effect under study.",
            )
        )
    batch = state.obs_column(p.batch_key)
    if batch is not None and batch.n_unique == 1:
        issues.append(error("single_batch", f"'{p.batch_key}' has one level; nothing to integrate."))
    if p.condition_key is None:
        issues.append(
            warning(
                "no_condition_key",
                "No condition_key: the check that batch and condition are not confounded is skipped.",
            )
        )
    if "hvg" not in state.flags:
        issues.append(warning("no_hvg", "No HVG selection yet; scVI will use all genes (slower)."))
    return issues


def transform(state: DatasetState, p: ScviParams) -> DatasetState:
    return (
        state.with_obsm("X_scVI", "X_umap")
        .with_obs("leiden")
        .with_flags("integrated", "neighbors", "umap")
    )


def resources(state: DatasetState, p: ScviParams) -> Resources:
    return Resources(
        cpus=8, mem_gb=scaled(state, 24, 16), time_min=scaled(state, 20, 30), gpus=1
    )


SPEC = BrickSpec(
    name="integrate_scvi",
    version="0.1.0",
    summary="scVI latent space over HVGs, then neighbors/UMAP/Leiden on X_scVI. Needs 1 GPU.",
    params_model=ScviParams,
    check=check,
    transform=transform,
    resources=resources,
    impl="schub.bricks.impl.integrate_scvi:run",
    uses_gpu=True,
)
