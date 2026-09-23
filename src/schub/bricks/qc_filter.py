"""Quality control: QC metrics, cell/gene filters and doublet removal.

% mitochondrial counts needs MT- genes. By default the filter is used at 20% when
the data has them and left out, with a warning, when it has none (Kang 2018: removed
upstream). An explicit max_pct_mt below 100 on such data is refused: it would look
like a filter while removing nothing.
"""

from __future__ import annotations

from pydantic import Field

from ..state import DatasetState, Issue, error, warning
from .base import BrickParams, BrickSpec, PlanContext, Resources, need_obs, need_raw_counts, scaled
from .merge_datasets import PREMERGE_MT


DEFAULT_MAX_PCT_MT = 20.0


class QcParams(BrickParams):
    min_genes: int = Field(200, ge=0, description="Drop cells with fewer detected genes.")
    max_genes: int | None = Field(None, ge=1, description="Drop cells with more genes.")
    min_cells: int = Field(3, ge=0, description="Drop genes detected in fewer cells.")
    max_pct_mt: float | None = Field(
        None, ge=0, le=100,
        description=f"Max % mitochondrial counts. Default: {DEFAULT_MAX_PCT_MT:g} when the data has MT- genes, "
        "not used (with a warning) when it has none. 100 = not used.",
    )
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
    issues += _mito_issues(state, p)
    if "qc" in state.flags:
        issues.append(warning("qc_repeated", "qc_filter already ran on this data."))
    if p.remove_doublets and not p.detect_doublets:
        issues.append(warning("doublets_ignored", "remove_doublets has no effect without detection."))
    return issues


def mito_threshold(p: QcParams, measurable: bool) -> float | None:
    """The % mito cut-off a job applies: the explicit one, else the default if MT- genes exist."""
    if p.max_pct_mt is not None:
        return p.max_pct_mt
    return DEFAULT_MAX_PCT_MT if measurable else None


def _mito_issues(state: DatasetState, p: QcParams) -> list[Issue]:
    explicit = p.max_pct_mt is not None and p.max_pct_mt < 100
    if state.has_obs(PREMERGE_MT):  # merged: each dataset's own % mito (merge_datasets warns about gaps)
        return []
    if state.gene_ids == "ensembl":
        why = "Mitochondrial genes are found by the 'MT-' symbol prefix, which Ensembl IDs lack"
        code = "mt_needs_symbols"
    elif state.mito_genes == 0:
        why = "The dataset has no MT- genes (often removed upstream after the authors' own QC)"
        code = "no_mito_genes"
    else:
        return []
    if explicit:
        return [error(code, f"{why}, so max_pct_mt={p.max_pct_mt:g} would filter nothing while looking like "
                            "a filter. Leave max_pct_mt unset (or 100) to say that % mito is not used.")]
    if p.max_pct_mt is None:
        return [warning(code, f"{why}: % mito is not used, so damaged cells are not removed by this criterion.")]
    return []


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
