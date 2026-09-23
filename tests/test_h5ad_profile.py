from __future__ import annotations

import numpy as np
import pytest

from schub.h5ad_profile import UnsupportedFile, classify_genes, classify_values, profile_h5ad

from .conftest import make_adata


def test_raw_counts_sparse_profile(write_h5ad):
    profile = profile_h5ad(write_h5ad(make_adata()))
    state = profile.state
    assert (state.n_obs, state.n_vars) == (60, 34)
    assert state.x_kind == "raw_counts"
    assert state.gene_ids == "symbol"
    assert state.species == "human"
    donor = state.obs_column("donor")
    assert donor is not None and donor.kind == "categorical" and donor.n_unique == 4
    assert donor.levels_known and sum(donor.top.values()) == 60
    assert state.obs_column("score").kind == "numeric"
    sample = state.obs_column("sample_name")
    assert sample.kind in ("string", "categorical") and sample.n_unique == 3


def test_lognorm_estimates_target_sum(write_h5ad):
    state = profile_h5ad(write_h5ad(make_adata(x="lognorm"))).state
    assert state.x_kind == "normalized_log"
    assert state.norm_target == pytest.approx(1e4, rel=0.01)
    assert not state.counts_layer


def test_dense_lognorm_with_counts_layer(write_h5ad):
    profile = profile_h5ad(write_h5ad(make_adata(x="dense_lognorm", counts_layer=True)))
    assert profile.state.x_kind == "normalized_log"
    assert profile.state.counts_layer
    assert profile.state.norm_target == pytest.approx(1e4, rel=0.01)
    assert profile.state.layers == ("counts",)


def test_scaled_matrix_is_flagged(write_h5ad):
    profile = profile_h5ad(write_h5ad(make_adata(x="scaled")))
    assert profile.state.x_kind == "scaled"
    assert any("negative" in note for note in profile.notes)


def test_mouse_ensembl_ids(write_h5ad):
    genes = [f"ENSMUSG{str(i).zfill(11)}" for i in range(34)]
    state = profile_h5ad(write_h5ad(make_adata(genes=genes))).state
    assert (state.gene_ids, state.species) == ("ensembl", "mouse")


def test_not_an_h5ad(tmp_path):
    bogus = tmp_path / "bogus.h5ad"
    bogus.write_text("not hdf5")
    with pytest.raises(UnsupportedFile):
        profile_h5ad(bogus)


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (np.array([]), "unknown"),
        (np.array([1.0, 2.0, 5.0]), "raw_counts"),
        (np.array([0.5, 1.7, 2.3]), "normalized_log"),
        (np.array([-1.0, 0.5]), "scaled"),
        (np.array([np.nan, 1.0]), "unknown"),
        (np.array([150.5, 900.2]), "unknown"),
        (np.array([0.0, 0.0]), "unknown"),
    ],
)
def test_classify_values(values, expected):
    assert classify_values(values) == expected


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ([], ("unknown", "unknown")),
        (["CD3E", "LYZ", "MT-CO1", "GAPDH", "NKG7"], ("symbol", "human")),
        (["Cd3e", "Lyz2", "Gapdh", "Nkg7", "Actb"], ("symbol", "mouse")),
        (["ENSG00000167286", "ENSG00000090382"], ("ensembl", "human")),
        (["ENSG00000167286", "ENSMUSG00000032094"], ("ensembl", "unknown")),
        (["123", "456"], ("unknown", "unknown")),
        (["CD3E", "Gapdh", "nkg7", "Actb"], ("symbol", "unknown")),
    ],
)
def test_classify_genes(names, expected):
    assert classify_genes(names) == expected


def test_rejects_external_links_and_external_storage(write_h5ad, tmp_path):
    import h5py

    secret = tmp_path / "secret.h5"
    with h5py.File(secret, "w") as f:
        f["data"] = np.array([b"SECRET"], dtype="S6")
    linked = write_h5ad(make_adata(), name="linked.h5ad")
    with h5py.File(linked, "a") as f:
        f["uns/evil"] = h5py.ExternalLink(str(secret), "/data")
    with pytest.raises(UnsupportedFile, match="external HDF5 link"):
        profile_h5ad(linked)

    raw = tmp_path / "secret.bin"
    raw.write_bytes(b"0123456789")
    external = write_h5ad(make_adata(), name="external.h5ad")
    with h5py.File(external, "a") as f:
        f.create_dataset("uns/ext", shape=(10,), dtype="u1", external=[(str(raw), 0, 10)])
    with pytest.raises(UnsupportedFile, match="outside the file"):
        profile_h5ad(external)


def test_unused_categories_and_long_levels(write_h5ad):
    import pandas as pd

    adata = make_adata()
    adata.obs["donor"] = adata.obs["donor"].cat.add_categories(["never_used"])
    adata.obs["note"] = pd.Categorical(["x" * 500] * adata.n_obs)
    state = profile_h5ad(write_h5ad(adata)).state
    assert state.obs_column("donor").n_unique == 4
    assert "never_used" not in state.obs_column("donor").top
    assert all(len(level) <= 80 for level in state.obs_column("note").top)
