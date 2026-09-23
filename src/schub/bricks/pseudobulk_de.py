"""Condition comparison by pseudobulk (sum of raw counts per replicate) + DESeq2."""

from __future__ import annotations

from pydantic import Field

from ..state import DatasetState, Issue, error, warning
from .base import BrickParams, BrickSpec, PlanContext, Resources, need_obs, need_raw_counts, scaled


class PseudobulkParams(BrickParams):
    condition_key: str = Field(description="obs column with the condition, e.g. 'label'.")
    reference: str = Field(description="Reference level, e.g. 'ctrl'.")
    treatment: str = Field(description="Level compared against the reference, e.g. 'stim'.")
    replicate_key: str = Field(description="obs column with biological replicates (donor/sample).")
    group_key: str | None = Field(None, description="Optional obs column: one test per cell type.")
    min_cells: int = Field(10, ge=1, description="Minimum cells per pseudobulk sample.")
    min_replicates: int = Field(2, ge=2, description="Minimum replicates per condition.")
    paired: bool = Field(
        False,
        description="Same replicates in both conditions (e.g. donor before/after); adds replicate "
        "to the design. Replicates missing a condition within a group are left out of that test.",
    )
    alpha: float = Field(0.05, gt=0, lt=1)


def _check_levels(state: DatasetState, p: PseudobulkParams) -> list[Issue]:
    column = state.obs_column(p.condition_key)
    if column is None or not column.levels_known:
        return []
    missing = [lvl for lvl in (p.reference, p.treatment) if lvl not in column.top]
    if not missing:
        return []
    return [
        error(
            "missing_level",
            f"Levels {missing} not in '{p.condition_key}'; available: {', '.join(column.top)}",
        )
    ]


def _check_replicates(state: DatasetState, p: PseudobulkParams) -> list[Issue]:
    column = state.obs_column(p.replicate_key)
    if column is None:
        return []
    issues = []
    if column.kind == "numeric":
        issues.append(warning("numeric_replicate", f"'{p.replicate_key}' is numeric; treated as labels."))
    if column.n_unique is not None and column.n_unique < p.min_replicates:
        issues.append(
            error(
                "too_few_replicates",
                f"'{p.replicate_key}' has {column.n_unique} level(s); pseudobulk DE needs "
                f">= {p.min_replicates} replicates per condition (cells are not replicates).",
            )
        )
    return issues


def check(state: DatasetState, p: PseudobulkParams, ctx: PlanContext) -> list[Issue]:
    issues = need_raw_counts(state, "pseudobulk_de", "DESeq2 statistics are defined on counts")
    for column, role in (
        (p.condition_key, "condition_key"),
        (p.replicate_key, "replicate_key"),
        (p.group_key, "group_key"),
    ):
        issues += need_obs(state, column, role)
    if p.condition_key == p.replicate_key:
        issues.append(error("condition_is_replicate", "condition_key must differ from replicate_key."))
    if p.group_key is not None and p.group_key in (p.replicate_key, p.condition_key):
        issues.append(error("group_is_design", "group_key must differ from condition and replicate keys."))
    for column, role in ((p.replicate_key, "replicate_key"), (p.condition_key, "condition_key")):
        found = state.obs_column(column)
        if found is not None and found.derived:
            issues.append(
                error(
                    "derived_column",
                    f"{role} '{column}' was computed by the pipeline (e.g. clusters); replicates and "
                    "conditions must be experimental metadata, otherwise the test pseudo-replicates.",
                )
            )
    if p.reference == p.treatment:
        issues.append(error("same_levels", "reference and treatment must differ."))
    return issues + _check_levels(state, p) + _check_replicates(state, p)


def transform(state: DatasetState, p: PseudobulkParams) -> DatasetState:
    return state.with_flags("de")


def resources(state: DatasetState, p: PseudobulkParams) -> Resources:
    return Resources(cpus=8, mem_gb=scaled(state, 16, 16), time_min=scaled(state, 15, 10))


SPEC = BrickSpec(
    name="pseudobulk_de",
    version="0.1.0",
    summary="Sum raw counts per replicate x condition (x group), test with PyDESeq2. Tables only.",
    params_model=PseudobulkParams,
    check=check,
    transform=transform,
    resources=resources,
    impl="schub.bricks.impl.pseudobulk_de:run",
    terminal=True,
)
