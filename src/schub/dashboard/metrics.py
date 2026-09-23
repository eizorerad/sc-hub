"""The key numbers of an experiment, taken from its steps' results, for the
Experiments table and for comparing branches side by side."""

from __future__ import annotations

import math
from typing import Any, Mapping

from ..state import Frozen


class Metric(Frozen):
    key: str
    label: str
    bricks: tuple[str, ...]  # the last step of these bricks in a branch gives the value
    field: str
    kind: str  # "int", "float", "pct"
    hint: str = ""


METRICS: tuple[Metric, ...] = (
    Metric(key="counted", label="cells counted", bricks=("kb_count", "cellranger_count"), field="n_cells", kind="int"),
    Metric(key="cells", label="cells after QC", bricks=("qc_filter",), field="cells_final", kind="int"),
    Metric(key="clusters", label="clusters", bricks=("normalize_embed", "integrate_scvi", "integrate_scanvi"),
           field="n_clusters", kind="int", hint="Leiden clusters of the last embedding"),
    Metric(key="val_loss", label="scVI val. loss", bricks=("integrate_scvi",), field="elbo_validation_last",
           kind="float", hint="validation ELBO, lower is better (same data only)"),
    Metric(key="labels", label="labels", bricks=("annotate_celltypist", "integrate_scanvi"), field="n_labels", kind="int"),
    Metric(key="vs_known", label="vs known labels", bricks=("annotate_celltypist",), field="reference_purity",
           kind="pct", hint="share of cells whose label's main known type is their own"),
    Metric(key="heldout", label="held-out acc.", bricks=("integrate_scanvi",), field="holdout_accuracy", kind="pct"),
    Metric(key="de_genes", label="DE genes", bricks=("pseudobulk_de",), field="significant_genes", kind="int",
           hint="unique genes significant in any group"),
    Metric(key="mean_genes", label="memento mean", bricks=("memento_de",), field="significant_genes", kind="int"),
    Metric(key="var_genes", label="memento var.", bricks=("memento_de",), field="variability_genes", kind="int"),
)


def _number(value: Any) -> float | int | None:
    """A finite number, else None (a diverged model's NaN loss is no number to show)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value


def metrics_of(steps: list[tuple[str, str, Mapping[str, Any] | None]]) -> tuple[dict[str, float | int], dict[str, str]]:
    """(brick, step key, summary) of a branch's steps in order -> {metric: value}, {metric: step key}.
    The step key says which step gave a number: equal numbers from one shared step are no news."""
    found: dict[str, float | int] = {}
    sources: dict[str, str] = {}
    for metric in METRICS:
        for brick, key, summary in steps:
            if brick in metric.bricks and summary and (value := _number(summary.get(metric.field))) is not None:
                found[metric.key], sources[metric.key] = value, key  # later steps win (the last embedding)
    return found, sources


def metric_catalog() -> list[dict[str, str]]:
    return [{"key": m.key, "label": m.label, "kind": m.kind, "hint": m.hint} for m in METRICS]
