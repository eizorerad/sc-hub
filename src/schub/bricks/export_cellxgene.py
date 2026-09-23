"""A compact .h5ad for cellxgene: subsampled cells, log-normalized X, embeddings
and cell metadata. Open it in an sc-hub cellxgene session (no install on the
laptop) or download it and run `cellxgene launch` locally."""

from __future__ import annotations

from pydantic import Field

from ..state import DatasetState, Issue, error, warning
from .base import BrickParams, BrickSpec, PlanContext, Resources, scaled


class CellxgeneParams(BrickParams):
    max_cells: int = Field(
        20_000, ge=500, le=200_000,
        description="Subsample to at most this many cells (random, seed 0); the course uses 5-10k for laptops.",
    )


def check(state: DatasetState, p: CellxgeneParams, ctx: PlanContext) -> list[Issue]:
    issues = []
    if "X_umap" not in state.obsm:
        issues.append(error("no_embedding", "export_cellxgene needs a UMAP; put it after normalize_embed or integrate_scvi."))
    if not state.has_raw_counts() and state.x_kind != "normalized_log":
        issues.append(error("no_expression", "Needs raw counts or log-normalized X to show gene expression."))
    if state.n_obs > p.max_cells:
        issues.append(warning("subsampled", f"{state.n_obs:,} cells will be subsampled to {p.max_cells:,}."))
    return issues


def transform(state: DatasetState, p: CellxgeneParams) -> DatasetState:
    return state.with_flags("exported")


def resources(state: DatasetState, p: CellxgeneParams) -> Resources:
    return Resources(cpus=4, mem_gb=scaled(state, 16, 16), time_min=scaled(state, 15, 10))


SPEC = BrickSpec(
    name="export_cellxgene",
    version="0.1.0",
    summary="Write results/cellxgene.h5ad: subsampled cells, log-normalized X, UMAP and metadata, "
    "ready for a cellxgene session. Files only.",
    params_model=CellxgeneParams,
    check=check,
    transform=transform,
    resources=resources,
    impl="schub.bricks.impl.export_cellxgene:run",
    terminal=True,
)
