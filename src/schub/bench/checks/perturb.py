"""Perturb-seq quality: controls present, enough cells per perturbation, and the
targeted gene actually knocked down relative to non-targeting controls.

The knockdown test samples cells (at most max_cells_per_group per perturbation and
max_controls controls) so it stays cheap on genome-wide screens; run it on a twin
first and on the full data before drawing conclusions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from ..models import CheckResult
from . import CheckDef, failed, passed
from .h5ad import read_obs_column

PathOf = Callable[[str], Path]


class PerturbParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    perturbation: str = Field(description="obs column with the perturbation (e.g. the targeted gene)")
    control: str = Field(description="its value for non-targeting control cells")
    min_controls: int = Field(100, ge=1)
    min_cells_per_perturbation: int = Field(20, ge=1)
    knockdown: bool = True
    knockdown_ratio: float = Field(0.7, gt=0, lt=1)  # target expression in its cells / in controls
    min_knockdown_fraction: float = Field(0.5, ge=0, le=1)
    max_targets: int = Field(50, ge=1, le=1000)
    max_cells_per_group: int = Field(200, ge=10)
    max_controls: int = Field(2000, ge=10)
    gene_column: str | None = Field(None, description="var column with gene symbols (default: var_names)")


def check_perturbation(path_of: PathOf, p: PerturbParams) -> CheckResult:
    path = path_of(p.path)
    labels = read_obs_column(path, p.perturbation).astype(str)
    counts = labels.value_counts()
    controls = int(counts.get(p.control, 0))
    if controls < p.min_controls:
        return failed("perturbation", f"{controls} control cells labelled {p.control!r} in {p.perturbation!r}; "
                      f"expected at least {p.min_controls}", controls=controls)
    targets = counts.drop(p.control, errors="ignore")
    median = float(targets.median()) if len(targets) else 0.0
    details = {"controls": controls, "perturbations": int(len(targets)), "median_cells": median,
               "below_min": int((targets < p.min_cells_per_perturbation).sum())}
    if not len(targets) or median < p.min_cells_per_perturbation:
        return failed("perturbation", f"median {median:.0f} cells per perturbation over {len(targets)} "
                      f"perturbations; expected at least {p.min_cells_per_perturbation}", **details)
    if not p.knockdown:
        return passed("perturbation", _summary(details), **details)
    knock = _knockdown(path, labels, targets, p)
    details.update(knock)
    if knock["tested"] == 0:
        return failed("perturbation", "no perturbation label matches a gene, so knockdown cannot be tested "
                      "(set gene_column or knockdown=false)", **details)
    if knock["fraction"] < p.min_knockdown_fraction:
        return failed("perturbation", f"target knocked down (ratio < {p.knockdown_ratio}) in {knock['knocked']} "
                      f"of {knock['tested']} tested perturbations; expected at least "
                      f"{p.min_knockdown_fraction:.0%}", **details)
    return passed("perturbation", f"{_summary(details)}; knockdown in {knock['knocked']} of {knock['tested']} "
                  "tested targets", **details)


def _summary(d: dict) -> str:
    return (f"{d['controls']} controls, {d['perturbations']} perturbations, median {d['median_cells']:.0f} "
            f"cells each")


def _knockdown(path: Path, labels: pd.Series, targets: pd.Series, p: PerturbParams) -> dict:
    import anndata as ad

    adata = ad.read_h5ad(path, backed="r")
    try:
        genes = pd.Index(adata.var[p.gene_column].astype(str) if p.gene_column else adata.var_names.astype(str))
        tested = [t for t in targets.index[: p.max_targets * 4] if t in genes][: p.max_targets]
        if not tested:
            return {"tested": 0, "knocked": 0, "fraction": 0.0}
        rng = np.random.default_rng(0)
        groups = {t: _sample(np.flatnonzero(labels.values == t), p.max_cells_per_group, rng) for t in tested}
        controls = _sample(np.flatnonzero(labels.values == p.control), p.max_controls, rng)
        rows = np.unique(np.concatenate([controls, *groups.values()]))
        matrix = _normalized(adata[rows].to_memory().X)
    finally:
        adata.file.close()
    position = {row: i for i, row in enumerate(rows)}
    control_rows = [position[r] for r in controls]
    knocked = 0
    for target, members in groups.items():
        column = genes.get_loc(target)
        base = float(np.asarray(matrix[control_rows, column].mean()))
        mean = float(np.asarray(matrix[[position[r] for r in members], column].mean()))
        knocked += int(base > 0 and mean / base < p.knockdown_ratio)
    return {"tested": len(tested), "knocked": knocked, "fraction": knocked / len(tested)}


def _sample(indices: np.ndarray, limit: int, rng: np.random.Generator) -> np.ndarray:
    return np.sort(rng.choice(indices, size=limit, replace=False)) if len(indices) > limit else indices


def _normalized(matrix):
    """Counts per 10k per cell (raw counts); other matrices are compared as they are."""
    from scipy import sparse

    dense_sample = matrix[: min(50, matrix.shape[0])]
    values = dense_sample.data if sparse.issparse(dense_sample) else np.asarray(dense_sample).ravel()
    if values.size == 0 or not np.allclose(values, np.round(values)):
        return matrix
    totals = np.asarray(matrix.sum(axis=1)).ravel()
    totals[totals == 0] = 1
    scale = sparse.diags(1e4 / totals) if sparse.issparse(matrix) else (1e4 / totals)[:, None]
    return scale @ matrix if sparse.issparse(matrix) else matrix * scale


CHECKS = (
    CheckDef("perturbation", "Perturb-seq: control cells present, enough cells per perturbation, and the target "
             "gene knocked down versus controls", PerturbParams, check_perturbation),
)
