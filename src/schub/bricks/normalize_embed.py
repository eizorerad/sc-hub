"""Normalization, highly variable genes, PCA, neighbors, UMAP and Leiden."""

from __future__ import annotations

from pydantic import Field

from ..state import DatasetState, Issue, warning
from .base import BrickParams, BrickSpec, PlanContext, Resources, need_obs, need_raw_counts, scaled


class NormalizeParams(BrickParams):
    target_sum: float = Field(1e4, gt=0, description="Counts per cell after normalization.")
    n_top_genes: int = Field(2000, ge=50, le=20000, description="Highly variable genes.")
    batch_key: str | None = Field(None, description="obs column for batch-aware HVG selection.")
    n_pcs: int = Field(50, ge=2, le=200)
    n_neighbors: int = Field(15, ge=2, le=200)
    leiden_resolution: float = Field(1.0, gt=0, le=10)


def check(state: DatasetState, p: NormalizeParams, ctx: PlanContext) -> list[Issue]:
    issues = need_raw_counts(
        state, "normalize_embed", "log data cannot be re-normalized; seurat_v3 HVG needs counts"
    )
    issues += need_obs(state, p.batch_key, "batch_key")
    if p.n_top_genes > state.n_vars:
        issues.append(
            warning("hvg_exceeds_genes", f"n_top_genes={p.n_top_genes} > {state.n_vars} genes.")
        )
    if "qc" not in state.flags:
        issues.append(warning("no_qc", "No qc_filter step before normalization."))
    return issues


def transform(state: DatasetState, p: NormalizeParams) -> DatasetState:
    new = state.update(x_kind="normalized_log", norm_target=p.target_sum, counts_layer=True)
    return (
        new.with_layers("counts")
        .with_obsm("X_pca", "X_umap")
        .with_obs("leiden")
        .with_flags("hvg", "pca", "neighbors", "umap")
    )


def resources(state: DatasetState, p: NormalizeParams) -> Resources:
    return Resources(cpus=8, mem_gb=scaled(state, 16, 20), time_min=scaled(state, 15, 20))


SPEC = BrickSpec(
    name="normalize_embed",
    version="0.1.0",
    summary="normalize_total + log1p, seurat_v3 HVG on counts, PCA, kNN graph, UMAP, Leiden.",
    params_model=NormalizeParams,
    check=check,
    transform=transform,
    resources=resources,
    impl="schub.bricks.impl.normalize_embed:run",
)
