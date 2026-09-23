"""Checks on AnnData files: counts, metadata columns, group sizes, DE design."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from ...bricks import REGISTRY, PlanContext
from ...config import Limits
from ...h5ad_profile import profile_h5ad
from ..models import CheckResult
from . import CheckDef, failed, passed

PathOf = Callable[[str], Path]


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PathParams(_Params):
    path: str


class ObsParams(_Params):
    path: str
    columns: list[str] = Field(min_length=1)


class MinCellsParams(_Params):
    path: str
    groupby: str
    min_cells: int = Field(20, ge=1)


class DesignParams(_Params):
    path: str
    condition: str
    reference: str
    treatment: str
    replicate: str
    min_replicates: int = Field(2, ge=2)


def read_obs_column(path: Path, column: str) -> pd.Series:
    """One obs column without loading the matrix."""
    import anndata.io
    import h5py

    with h5py.File(path, "r") as f:
        if column not in f["obs"]:
            raise KeyError(column)
        values = anndata.io.read_elem(f["obs"][column])
    return pd.Series(np.asarray(values) if not isinstance(values, pd.Categorical) else values, name=column)


def check_counts(path_of: PathOf, p: PathParams) -> CheckResult:
    state = profile_h5ad(path_of(p.path)).state
    if state.has_raw_counts():
        where = "X" if state.x_kind == "raw_counts" else "layers['counts']"
        return passed("h5ad_counts", f"{p.path}: raw counts in {where}", x_kind=state.x_kind)
    return failed("h5ad_counts", f"{p.path}: X looks like '{state.x_kind}' and there is no layers['counts']",
                  x_kind=state.x_kind)


def check_obs(path_of: PathOf, p: ObsParams) -> CheckResult:
    state = profile_h5ad(path_of(p.path)).state
    missing = [c for c in p.columns if not state.has_obs(c)]
    if missing:
        available = [c.name for c in state.obs][:40]
        return failed("h5ad_obs", f"{p.path} lacks obs columns {missing}; it has {available}")
    return passed("h5ad_obs", f"{p.path} has {', '.join(p.columns)}")


def check_min_cells(path_of: PathOf, p: MinCellsParams) -> CheckResult:
    counts = read_obs_column(path_of(p.path), p.groupby).value_counts()
    small = counts[counts < p.min_cells]
    details = {"groups": int(len(counts)), "smallest": int(counts.min()) if len(counts) else 0}
    if len(counts) == 0 or len(small):
        shown = {str(k): int(v) for k, v in small.head(10).items()}
        return failed("min_cells", f"{len(small)} of {len(counts)} groups of {p.groupby!r} have fewer than "
                      f"{p.min_cells} cells: {shown}", **details)
    return passed("min_cells", f"all {len(counts)} groups of {p.groupby!r} have at least {p.min_cells} cells",
                  **details)


def check_design(path_of: PathOf, p: DesignParams) -> CheckResult:
    path = path_of(p.path)
    state = profile_h5ad(path).state
    spec = REGISTRY["pseudobulk_de"]
    params = spec.params_model.model_validate({
        "condition_key": p.condition, "reference": p.reference, "treatment": p.treatment,
        "replicate_key": p.replicate, "min_replicates": p.min_replicates,
    })
    errors = [i.message for i in spec.check(state, params, PlanContext(celltypist_dirs=(), limits=Limits()))
              if i.level == "error"]
    if errors:
        return failed("de_design", "; ".join(errors))
    table = pd.crosstab(read_obs_column(path, p.condition).astype(str).values,
                        read_obs_column(path, p.replicate).astype(str).values)
    per_level = {level: int((table.loc[level] > 0).sum()) for level in (p.reference, p.treatment) if level in table.index}
    short = {k: v for k, v in per_level.items() if v < p.min_replicates}
    if len(per_level) < 2 or short:
        return failed("de_design", f"replicates per condition {per_level}; each needs at least {p.min_replicates} "
                      "(cells are not replicates)", replicates=per_level)
    return passed("de_design", f"replicates per condition {per_level}", replicates=per_level)


CHECKS = (
    CheckDef("h5ad_counts", "an .h5ad has raw integer counts (X or layers['counts'])", PathParams, check_counts),
    CheckDef("h5ad_obs", "an .h5ad has these obs columns", ObsParams, check_obs),
    CheckDef("min_cells", "every group of an obs column has at least min_cells cells", MinCellsParams, check_min_cells),
    CheckDef("de_design", "a condition comparison has real biological replicates on both sides (pseudobulk DE)",
             DesignParams, check_design),
)
