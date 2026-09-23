from __future__ import annotations

import json
from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from scipy import sparse

from schub.bench.checks import run_checks
from schub.bench.models import CheckSpec
from schub.bench.twins import TwinError, select, twin
from schub.config import Settings
from schub.projects import ProjectStore


def screen(controls: int = 1000, targets: int = 30, per_target: int = 200) -> ad.AnnData:
    rng = np.random.default_rng(3)
    genes = [f"G{i}" for i in range(60)]
    labels = ["control"] * controls + [genes[t] for t in range(targets) for _ in range(per_target)]
    counts = rng.poisson(15, size=(len(labels), len(genes))).astype(np.float32)
    for row, label in enumerate(labels):
        if label != "control":
            counts[row, genes.index(label)] = rng.poisson(1)
    obs = pd.DataFrame({"perturbation": pd.Categorical(labels)}, index=[f"c{i}" for i in range(len(labels))])
    return ad.AnnData(sparse.csr_matrix(counts), obs=obs, var=pd.DataFrame(index=genes))


@pytest.fixture
def data(tmp_path: Path) -> Path:
    path = tmp_path / "k562" / "data.h5ad"
    path.parent.mkdir()
    screen().write_h5ad(path)
    return path


def test_twin_keeps_controls_and_every_group(data: Path) -> None:
    path = twin(data, stratify="perturbation", keep=["control"], fraction=0.05, min_per_group=20, max_keep=500)
    small = ad.read_h5ad(path)
    counts = small.obs["perturbation"].value_counts()
    assert counts["control"] == 500 and counts.drop("control").min() == 20 and len(counts) == 31
    info = json.loads((path.parent / "twin.json").read_text())
    assert info["n_obs_twin"] == small.n_obs and info["groups"]["control"] == 500
    assert path.parent.parent == data.parent / "twins"


def test_the_same_rule_gives_the_same_twin(data: Path, capsys) -> None:
    first = twin(data, stratify="perturbation", keep=["control"])
    again = twin(data, stratify="perturbation", keep=["control"])
    assert first == again and "already built" in capsys.readouterr().out
    other = twin(data, stratify="perturbation", keep=["control"], seed=1)
    assert other != first


def test_a_twin_passes_the_checks_the_full_data_passes(settings: Settings, data: Path) -> None:
    ProjectStore(settings).create("p")
    work = settings.projects_dir / "p" / "work"
    good = twin(data, stratify="perturbation", keep=["control"], max_keep=300)
    bad = twin(data, stratify="perturbation", keep=[], fraction=0.01, min_per_group=5)  # controls thinned out
    for name, source in (("good", good), ("bad", bad)):
        (work / f"{name}.h5ad").write_bytes(source.read_bytes())
    spec = {"perturbation": "perturbation", "control": "control", "min_controls": 100}
    [ok] = run_checks(settings, "p", [CheckSpec(name="perturbation", params={"path": "work/good.h5ad", **spec})])
    [fail] = run_checks(settings, "p", [CheckSpec(name="perturbation", params={"path": "work/bad.h5ad", **spec})])
    assert ok.status == "pass" and fail.status == "fail"  # a twin without controls is caught


def test_limits_and_rules() -> None:
    rng = np.random.default_rng(0)
    labels = np.array(["a"] * 100 + ["b"] * 10)
    rows = select(labels, 110, 0.1, 20, [], 50, 1000, rng)
    assert len(rows) == 20 + 10  # b keeps all it has
    assert len(select(None, 1000, 0.05, 10, [], 0, 1000, rng)) == 50
    with pytest.raises(TwinError):
        select(labels, 110, 0.5, 20, [], 50, 10, rng)
