"""Quality control: QC metrics, cell/gene filters and doublet removal."""

from __future__ import annotations

from pydantic import Field

from ..state import DatasetState, Issue, error, warning
from .base import BrickParams, BrickSpec, PlanContext, Resources, need_obs, need_raw_counts, scaled


class QcParams(BrickParams):
    min_genes: int = Field(200, ge=0, description="Drop cells with fewer detected genes.")
    max_genes: int | None = Field(None, ge=1, description="Drop cells with more genes.")
    min_cells: int = Field(3, ge=0, description="Drop genes detected in fewer cells.")
    max_pct_mt: float = Field(20.0, ge=0, le=100, description="Max % mitochondrial counts.")
    detect_doublets: bool = Field(True, description="Score doublets with Scrublet.")
    remove_doublets: bool = Field(True, description="Drop predicted doublets.")
    batch_key: str | None = Field(None, description="obs column; doublets scored per batch.")


def check(state: DatasetState, p: QcParams, ctx: PlanContext) -> list[Issue]:
    issues = need_raw_counts(state, "qc_filter", "QC metrics are defined on counts")
    issues += need_obs(state, p.batch_key, "batch_key")
    if state.species == "unknown":
        issues.append(
            warning(
                "species_unknown",
                "Species not inferred; mitochondrial genes are matched by a "
                "case-insensitive 'MT-' prefix.",
            )
        )
    if state.gene_ids == "ensembl" and p.max_pct_mt < 100:
        issues.append(
            error(
                "mt_needs_symbols",
                "Mitochondrial genes are found by the 'MT-' symbol prefix, which Ensembl IDs lack; "
                "the % mito filter would silently do nothing. Use symbols or set max_pct_mt=100.",
            )
        )
    if state.gene_ids != "ensembl" and state.mito_genes == 0 and p.max_pct_mt < 100:
        issues.append(
            warning(
                "no_mito_genes",
                "The dataset has no MT- genes (likely removed upstream), so max_pct_mt filters "
                "nothing; damaged cells are not removed by this criterion.",
            )
        )
    if "qc" in state.flags:
        issues.append(warning("qc_repeated", "qc_filter already ran on this data."))
    if p.remove_doublets and not p.detect_doublets:
        issues.append(warning("doublets_ignored", "remove_doublets has no effect without detection."))
    return issues


def transform(state: DatasetState, p: QcParams) -> DatasetState:
    new = state.update(x_kind="raw_counts", counts_layer=True, norm_target=None)
    new = new.with_layers("counts").with_obs(
        "n_genes_by_counts", "total_counts", "pct_counts_mt", kind="numeric"
    )
    if p.detect_doublets:
        new = new.with_obs("doublet_score", kind="numeric").with_obs("predicted_doublet", kind="other")
    return new.with_flags("qc")


def resources(state: DatasetState, p: QcParams) -> Resources:
    return Resources(cpus=8, mem_gb=scaled(state, 16, 16), time_min=scaled(state, 20, 20))


SPEC = BrickSpec(
    name="qc_filter",
    version="0.1.0",
    summary="QC metrics, cell/gene filtering and Scrublet doublet removal on raw counts.",
    params_model=QcParams,
    check=check,
    transform=transform,
    resources=resources,
    impl="schub.bricks.impl.qc_filter:run",
)
