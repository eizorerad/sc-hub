"""Pure helpers of the job code (pandas/numpy only, so they run without scanpy)."""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import sparse

from schub.bricks.impl.annotate_celltypist import reference_agreement
from schub.bricks.impl.integrate_scanvi import holdout_mask
from schub.bricks.impl.merge_datasets import mito_share
from schub.bricks.impl.qc_filter import _mito_warnings


def test_reference_agreement_shows_a_known_type_absorbed_by_another_label():
    # Kang 2018 with voting over coarse Leiden: most CD8 T cells were called NK.
    reference = pd.Series(["CD8 T"] * 97 + ["NK"] * 100 + ["CD4 T"] * 100 + ["CD8 T"] * 3)
    labels = pd.Series(["CD16+ NK"] * 197 + ["Tcm/Naive helper T"] * 103)
    result = reference_agreement(labels, reference)
    assert result["by_reference"]["CD8 T"] == {"label": "CD16+ NK", "share": 0.97, "cells": 100}
    assert result["by_reference"]["CD4 T"]["label"] == "Tcm/Naive helper T"
    assert result["purity"] == round((100 + 100) / 300, 3)  # the NK label mixes two known types
    assert int(result["table"].to_numpy().sum()) == 300


def test_reference_agreement_ignores_cells_without_a_known_label():
    result = reference_agreement(pd.Series(["a", "a", "b"]), pd.Series(["x", None, "y"]))
    assert result["purity"] == 1.0 and set(result["by_reference"]) == {"x", "y"}


def test_holdout_is_per_label_reproducible_and_skips_unlabeled_cells():
    labels = pd.Series(["T"] * 50 + ["B"] * 30 + ["Unknown"] * 20 + ["rare"] * 5)
    held = holdout_mask(labels, "Unknown", 0.1)
    assert held.sum() == 5 + 3  # rare: int(0.5) = 0 cells, so small labels keep all their cells
    assert not held[80:100].any() and not held[100:].any()
    assert (held == holdout_mask(labels, "Unknown", 0.1)).all()
    assert not holdout_mask(labels, "Unknown", 0.0).any()


def test_mito_share_is_per_cell_over_all_genes_and_nan_without_mt_genes():
    counts = sparse.csr_matrix(np.array([[8, 2, 0], [5, 0, 5]], dtype=np.float32))
    share = mito_share(counts, pd.Index(["GENE1", "MT-CO1", "mt-nd1"]))
    assert share.tolist() == [20.0, 50.0]
    assert np.isnan(mito_share(counts, pd.Index(["A", "B", "C"]))).all()


def test_qc_mito_warnings_name_what_was_not_filtered():
    assert _mito_warnings(20.0, measurable=True, unmeasured=0) == []
    assert _mito_warnings(None, measurable=False, unmeasured=0) == ["no MT- genes: % mito was not used"]
    assert _mito_warnings(20.0, measurable=False, unmeasured=0) == [
        "no MT- genes: the % mitochondrial filter had no effect"]
    assert "24,673 cells come from datasets without MT- genes" in _mito_warnings(20.0, True, 24673)[0]
    assert _mito_warnings(100.0, measurable=False, unmeasured=0) == []  # the student said: not used


def test_reference_agreement_works_with_repeated_cell_names():
    index = ["AAAC-1", "AAAC-1", "CCCT-1"]  # the same barcode in two samples
    labels, reference = pd.Series(["a", "b", "b"], index=index), pd.Series(["x", "y", "y"], index=index)
    assert reference_agreement(labels, reference)["purity"] == 1.0
