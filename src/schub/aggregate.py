"""Pseudobulk aggregation: sum raw counts of cells sharing a sample label."""

from __future__ import annotations

from typing import Sequence

import numpy as np
from scipy import sparse


def pseudobulk(counts: np.ndarray | sparse.spmatrix, labels: Sequence[str]) -> tuple[list[str], np.ndarray, np.ndarray]:
    """Return (sample labels, samples x genes summed counts, cells per sample)."""
    labels = np.asarray(labels, dtype=str)
    if labels.shape[0] != counts.shape[0]:
        raise ValueError("labels must have one entry per cell (row)")
    samples, codes = np.unique(labels, return_inverse=True)
    indicator = sparse.csr_matrix(
        (np.ones(len(codes)), (codes, np.arange(len(codes)))), shape=(len(samples), len(codes))
    )
    summed = indicator @ counts
    dense = summed.toarray() if sparse.issparse(summed) else np.asarray(summed)
    n_cells = np.bincount(codes, minlength=len(samples))
    return [str(s) for s in samples], dense, n_cells
