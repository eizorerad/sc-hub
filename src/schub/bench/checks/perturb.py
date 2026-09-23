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
ROW_BLOCK = 2000  # cells read at once (a dense block of 8,563 genes is ~70 MB)


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
        return failed("perturbation", "knockdown cannot be tested: no targeted gene is both in the matrix and "
                      "expressed in the controls (set gene_column, or knockdown=false)", **details)
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
        first = {g: i for i, g in reversed(list(enumerate(genes)))}  # repeated symbols: the first column
        tested = [t for t in targets.index[: p.max_targets * 4] if t in first][: p.max_targets]
        if not tested:
            return {"tested": 0, "knocked": 0, "fraction": 0.0, "no_baseline": 0}
        rng = np.random.default_rng(0)
        groups = {t: _sample(np.flatnonzero(labels.values == t), p.max_cells_per_group, rng) for t in tested}
        controls = _sample(np.flatnonzero(labels.values == p.control), p.max_controls, rng)
        rows = np.unique(np.concatenate([controls, *groups.values()]))
        columns = sorted({first[t] for t in tested})
        matrix = _expression(adata, rows, columns)
    finally:
        adata.file.close()
    return _compare(matrix, rows, controls, groups, {t: columns.index(first[t]) for t in tested}, p)


def _expression(adata, rows: np.ndarray, columns: list[int]) -> np.ndarray:
    """Library-size-normalized expression of the target genes in the sampled cells (dense).

    Whole rows are read in blocks, sparse or dense alike: a cell's library size needs all
    its genes (comparing raw counts called a perturbation that shrinks every gene's
    counts a knockdown; Codex found it in the K562 PoC)."""
    from scipy import sparse

    targets, totals, first = [], [], None
    for start in range(0, len(rows), ROW_BLOCK):
        block = adata[rows[start:start + ROW_BLOCK]].to_memory().X
        block = block.tocsr() if sparse.issparse(block) else np.asarray(block)
        first = block if first is None else first
        part = block[:, columns]
        targets.append(np.asarray(part.todense() if sparse.issparse(part) else part, dtype=float))
        totals.append(np.asarray(block.sum(axis=1), dtype=float).ravel())
    target = np.vstack(targets) if targets else np.zeros((0, len(columns)))
    if first is None or not _looks_like_counts(first):
        return _unlog(target)
    library = np.concatenate(totals)
    library[library == 0] = 1
    return target * (1e4 / library)[:, None]


def _looks_like_counts(matrix) -> bool:
    from scipy import sparse

    sample = matrix[: min(50, matrix.shape[0])]
    values = sample.data if sparse.issparse(sample) else np.asarray(sample).ravel()
    return values.size > 0 and bool(np.allclose(values, np.round(values)))


def _unlog(values: np.ndarray) -> np.ndarray:
    """log1p-normalized data (non-integer, small) back to its linear scale; ratios of logs mislead."""
    return np.expm1(values) if values.size and values.max() < 30 else values


def _compare(matrix: np.ndarray, rows: np.ndarray, controls: np.ndarray, groups: dict, column: dict,
             p: PerturbParams) -> dict:
    position = {row: i for i, row in enumerate(rows)}
    control_rows = [position[r] for r in controls]
    knocked = tested = no_baseline = 0
    for target, members in groups.items():
        base = float(matrix[control_rows, column[target]].mean())
        if base <= 0:
            no_baseline += 1  # not expressed in controls: knockdown cannot be seen, not a failure
            continue
        tested += 1
        mean = float(matrix[[position[r] for r in members], column[target]].mean())
        knocked += int(mean / base < p.knockdown_ratio)
    return {"tested": tested, "knocked": knocked, "fraction": knocked / tested if tested else 0.0,
            "no_baseline": no_baseline}


def _sample(indices: np.ndarray, limit: int, rng: np.random.Generator) -> np.ndarray:
    return np.sort(rng.choice(indices, size=limit, replace=False)) if len(indices) > limit else indices


CHECKS = (
    CheckDef("perturbation", "Perturb-seq: control cells present, enough cells per perturbation, and the target "
             "gene knocked down versus controls", PerturbParams, check_perturbation),
)
