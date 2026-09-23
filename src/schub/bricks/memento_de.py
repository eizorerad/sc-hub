"""Differential mean and variability with memento (method of moments, Cell 2024).

Complements pseudobulk_de: memento tests single cells while accounting for
capture efficiency, and also reports differential variability. Replicates are
used as sample groups when given.
"""

from __future__ import annotations

import importlib.metadata

from pydantic import Field

from ..state import DatasetState, Issue, warning
from .base import BrickParams, BrickSpec, PlanContext, Resources, scaled
from .pseudobulk_de import PseudobulkParams
from .pseudobulk_de import check as check_pseudobulk


class MementoParams(BrickParams):
    condition_key: str = Field(description="obs column with the condition, e.g. 'label'.")
    reference: str = Field(description="Reference level, e.g. 'ctrl'.")
    treatment: str = Field(description="Level compared against the reference, e.g. 'stim'.")
    replicate_key: str | None = Field(None, description="obs column with biological replicates (donor/sample).")
    group_key: str | None = Field(None, description="Optional obs column: one test per cell type.")
    capture_rate: float = Field(
        0.07, gt=0, le=1, description="Overall UMI capture efficiency (memento tutorials: 0.07 for 10x; ~0.15 for v3)."
    )
    num_boot: int = Field(5000, ge=500, le=20000, description="Bootstrap iterations for p-values.")
    min_cells: int = Field(50, ge=10, description="Groups with fewer cells per condition are skipped.")
    alpha: float = Field(0.05, gt=0, lt=1)


def check(state: DatasetState, p: MementoParams, ctx: PlanContext) -> list[Issue]:
    # Same metadata rules as pseudobulk (existing columns, levels, no derived design columns).
    proxy = PseudobulkParams(
        condition_key=p.condition_key, reference=p.reference, treatment=p.treatment,
        replicate_key=p.replicate_key or p.condition_key, group_key=p.group_key,
    )
    issues = [
        i for i in check_pseudobulk(state, proxy, ctx)
        if p.replicate_key is not None or i.code not in {"condition_is_replicate", "too_few_replicates"}
    ]
    issues = [i.model_copy(update={"message": i.message.replace("pseudobulk_de", "memento_de").replace("DESeq2", "memento")}) for i in issues]
    if p.replicate_key is None:
        issues.append(warning("no_replicates", "No replicate_key: p-values treat cells as independent; add donors if you have them."))
    return issues


def transform(state: DatasetState, p: MementoParams) -> DatasetState:
    return state.with_flags("de")


def resources(state: DatasetState, p: MementoParams) -> Resources:
    return Resources(cpus=16, mem_gb=scaled(state, 24, 24), time_min=scaled(state, 30, 60) + p.num_boot // 500)


def key_extra(state: DatasetState, p: MementoParams, ctx: PlanContext) -> str:
    try:
        return f"memento-de {importlib.metadata.version('memento-de')}"
    except importlib.metadata.PackageNotFoundError:
        return "memento-de absent"


SPEC = BrickSpec(
    name="memento_de",
    version="0.1.0",
    summary="memento: differential mean AND variability between two conditions (per group), "
    "capture-rate aware, replicates as groups. Tables only.",
    params_model=MementoParams,
    check=check,
    transform=transform,
    resources=resources,
    impl="schub.bricks.impl.memento_de:run",
    terminal=True,
    key_extra=key_extra,
)
