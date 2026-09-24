"""Rows of an .h5ad without loading the rest: X, layers and obsm for chosen cells.

anndata's backed mode keeps X on disk but loads layers, obsm and obsp whole; one dense
counts layer of the K562 screen (310k x 8.5k float32) is 10.6 GB. Twins and the
perturbation check read through h5py instead, a block of rows at a time.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import numpy as np

BLOCK = 2000  # rows read at once (a dense block of 8,563 genes is ~70 MB)


def rows_of(element, rows: np.ndarray):
    """Rows `rows` (sorted, unique) of a dense dataset or a sparse group; sparse stays sparse (CSR)."""
    import anndata.io
    import h5py
    from scipy import sparse

    if isinstance(element, h5py.Dataset):
        parts = [element[rows[i:i + BLOCK]] for i in range(0, len(rows), BLOCK)]
        return np.concatenate(parts) if parts else element[rows]
    encoding = str(element.attrs.get("encoding-type", ""))
    if encoding in ("csr_matrix", "csc_matrix"):
        matrix = anndata.io.sparse_dataset(element)
        parts = [matrix[rows[i:i + BLOCK]] for i in range(0, len(rows), BLOCK)]
        return sparse.vstack(parts).tocsr() if parts else matrix[rows]
    value = anndata.io.read_elem(element)  # e.g. a dataframe in obsm (scvi's covariates): small per row
    return value.iloc[rows] if hasattr(value, "iloc") else value[rows]


@contextmanager
def h5ad(path: Path) -> Iterator:
    import h5py

    with h5py.File(path, "r") as handle:
        yield handle


def frame(handle, key: str):
    import anndata.io

    return anndata.io.read_elem(handle[key])


def _raw(f, rows: np.ndarray):
    """The rows of .raw (CELLxGENE files keep their counts there), as an AnnData, or None."""
    import anndata as ad

    if "raw" not in f:
        return None
    raw = f["raw"]
    x = rows_of(raw["X"], rows) if "X" in raw else None
    var = frame(f, "raw/var") if "var" in raw else None
    varm = frame(f, "raw/varm") if "varm" in raw else {}
    return ad.AnnData(X=x, var=var, varm=dict(varm)) if x is not None else None


def subset(path: Path, rows: np.ndarray):
    """An in-memory AnnData of the chosen cells: X, layers, obsm and .raw rows, all of obs/var/varm/uns.
    Pairwise obsp/varp are left out (they would need the whole graph); returns (adata, dropped keys)."""
    import anndata as ad

    with h5ad(path) as f:
        obs, var = frame(f, "obs"), frame(f, "var")
        layers = {k: rows_of(f["layers"][k], rows) for k in f["layers"]} if "layers" in f else {}
        obsm = {k: rows_of(f["obsm"][k], rows) for k in f["obsm"]} if "obsm" in f else {}
        varm = frame(f, "varm") if "varm" in f else {}
        uns = frame(f, "uns") if "uns" in f else {}
        dropped = [k for k in ("obsp", "varp") if k in f and len(f[k])]
        x = rows_of(f["X"], rows) if "X" in f else None
        raw = _raw(f, rows)
    adata = ad.AnnData(X=x, obs=obs.iloc[rows], var=var, layers=layers, obsm=obsm, varm=dict(varm), uns=dict(uns))
    if raw is not None:
        raw.obs_names = adata.obs_names
        adata.raw = raw
    return adata, dropped
