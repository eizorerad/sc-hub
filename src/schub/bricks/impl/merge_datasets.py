from __future__ import annotations

from pathlib import Path
from typing import Any

from ..base import BrickError, StepIO
from ..merge_datasets import PREMERGE_MT, MergeParams, label_of
from .common import counts_source, read, setup_scanpy, write


def mito_share(counts: Any, var_names: Any) -> Any:
    """% of each cell's counts on MT- genes, over all of its dataset's genes (NaN if it has none)."""
    import numpy as np

    mito = np.asarray(var_names.str.upper().str.startswith("MT-"))
    if not mito.any():
        return np.full(counts.shape[0], np.nan)
    total = np.asarray(counts.sum(axis=1)).ravel()
    on_mito = np.asarray(counts[:, mito].sum(axis=1)).ravel()
    return 100 * on_mito / np.maximum(total, 1)


def _pinned(io: StepIO, name: str) -> Path:
    """The file the planner saw for `name`, refused if its content changed since."""
    import json

    from ...datasets import dataset_fingerprint
    from .common import library_roots

    pins = json.loads(io.context.get("pins", "{}"))
    if name not in pins:
        raise BrickError(f"dataset '{name}' was not pinned at planning time; plan again")
    path_text, _, fingerprint = pins[name].partition("\t")
    path = Path(path_text)
    if not path.is_file() or dataset_fingerprint(path, library_roots(io)) != fingerprint:
        raise BrickError(f"dataset '{name}' changed after planning ({path}); plan again")
    return path


def _counts_of(path: Path, name: str, label_key: str) -> Any:
    import anndata as ad
    import scipy.sparse as sp

    from ...h5ad_profile import profile_h5ad
    from ..base import StepIO as _StepIO

    state = profile_h5ad(path).state
    probe = _StepIO(input=path, output=None, results_dir=path.parent, state_in=state, context={})
    adata = read(probe)
    counts, _ = counts_source(adata, probe)
    part = ad.AnnData(sp.csr_matrix(counts, dtype="float32"), obs=adata.obs.copy(), var=adata.var[[]].copy())
    part.obs[PREMERGE_MT] = mito_share(part.X, part.var_names)
    part.var_names_make_unique()
    part.obs_names = [f"{name}_{cell}" for cell in part.obs_names]
    part.obs[label_key] = name
    return part


def run(io: StepIO, p: MergeParams) -> dict[str, Any]:
    setup_scanpy(io)
    import anndata as ad
    import scipy.sparse as sp

    primary = read(io)
    counts, _ = counts_source(primary, io)
    first_name = label_of(str(io.input))
    base = ad.AnnData(sp.csr_matrix(counts, dtype="float32"), obs=primary.obs.copy(), var=primary.var[[]].copy())
    base.obs[PREMERGE_MT] = mito_share(base.X, base.var_names)
    base.var_names_make_unique()
    base.obs_names = [f"{first_name}_{cell}" for cell in base.obs_names]
    base.obs[p.label_key] = first_name
    parts = [base] + [_counts_of(_pinned(io, name), label_of(name), p.label_key) for name in p.others]
    names = [first_name, *(label_of(name) for name in p.others)]
    shared_genes = len(set.intersection(*(set(part.var_names) for part in parts)))
    if p.join == "inner" and shared_genes == 0:
        raise BrickError("the datasets share no gene names; check that they use the same gene identifiers")
    merged = ad.concat(parts, join=p.join, fill_value=0 if p.join == "outer" else None, index_unique=None)
    merged.X = sp.csr_matrix(merged.X, dtype="float32")
    merged.obs[p.label_key] = merged.obs[p.label_key].astype("category")
    measured = {name: bool(part.obs[PREMERGE_MT].notna().any()) for name, part in zip(names, parts)}
    if not any(measured.values()):
        del merged.obs[PREMERGE_MT]  # nothing to carry: qc_filter refuses a % mito filter then
    write(merged, io.output)
    return {
        "datasets": {name: {"cells": int(part.n_obs), "genes": int(part.n_vars)} for name, part in zip(names, parts)},
        "shared_genes": shared_genes,
        "n_cells": int(merged.n_obs),
        "n_genes": int(merged.n_vars),
        "join": p.join,
        "label_key": p.label_key,
        "mito_measured": measured,
    }
