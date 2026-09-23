"""Every check passes on good input and fails on a known-bad one (a check that
cannot fail proves nothing)."""

from __future__ import annotations

from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from scipy import sparse

from schub.bench.checks import describe, registry, run_checks
from schub.bench.models import CheckSpec
from schub.config import Settings
from schub.projects import ProjectStore
from tests.conftest import make_adata


@pytest.fixture
def project(settings: Settings) -> Path:
    ProjectStore(settings).create("p")
    return settings.projects_dir / "p"


def status(settings: Settings, name: str, **params) -> tuple[str, str]:
    [result] = run_checks(settings, "p", [CheckSpec(name=name, params=params)])
    return result.status, result.message


def perturb_adata(knockdown: bool = True, controls: int = 300, per_target: int = 60) -> ad.AnnData:
    rng = np.random.default_rng(1)
    genes = [f"G{i}" for i in range(40)]
    targets = genes[:10]
    labels = ["non-targeting"] * controls + [t for t in targets for _ in range(per_target)]
    counts = rng.poisson(20, size=(len(labels), len(genes))).astype(np.float32)
    if knockdown:
        for row, label in enumerate(labels):
            if label in targets:
                counts[row, genes.index(label)] = rng.poisson(2)
    return ad.AnnData(sparse.csr_matrix(counts), obs=pd.DataFrame({"gene": pd.Categorical(labels)},
                                                                  index=[f"c{i}" for i in range(len(labels))]),
                      var=pd.DataFrame(index=genes))


def test_file_and_tables(settings: Settings, project: Path) -> None:
    work = project / "work"
    (work / "empty.csv").write_text("")
    (work / "t.csv").write_text("gene,lfc,padj\nIFIT1,3.2,0.001\nISG15,2.9,0.01\n")
    (work / "loss.csv").write_text("epoch,loss\n1,0.9\n2,nan\n")
    assert status(settings, "file", path="work/t.csv")[0] == "pass"
    assert status(settings, "file", path="work/empty.csv")[0] == "fail"
    assert status(settings, "file", path="work/missing.csv")[0] == "fail"
    assert status(settings, "table_columns", path="work/t.csv", columns=["gene", "lfc"], min_rows=2)[0] == "pass"
    assert status(settings, "table_columns", path="work/t.csv", columns=["gene", "pvalue"])[0] == "fail"
    assert status(settings, "table_columns", path="work/t.csv", columns=["gene"], min_rows=3)[0] == "fail"
    assert status(settings, "finite_values", path="work/loss.csv", column="loss")[0] == "fail"
    assert status(settings, "finite_values", path="work/t.csv", column="lfc")[0] == "pass"


def test_h5ad_checks(settings: Settings, project: Path) -> None:
    make_adata().write_h5ad(project / "work" / "counts.h5ad")
    make_adata(x="lognorm").write_h5ad(project / "work" / "lognorm.h5ad")
    assert status(settings, "h5ad_counts", path="work/counts.h5ad")[0] == "pass"
    assert status(settings, "h5ad_counts", path="work/lognorm.h5ad")[0] == "fail"
    assert status(settings, "h5ad_obs", path="work/counts.h5ad", columns=["donor", "label"])[0] == "pass"
    assert status(settings, "h5ad_obs", path="work/counts.h5ad", columns=["batch"])[0] == "fail"
    assert status(settings, "min_cells", path="work/counts.h5ad", groupby="donor", min_cells=15)[0] == "pass"
    assert status(settings, "min_cells", path="work/counts.h5ad", groupby="donor", min_cells=16)[0] == "fail"


def test_de_design_needs_replicates(settings: Settings, project: Path) -> None:
    adata = make_adata()
    adata.write_h5ad(project / "work" / "good.h5ad")
    one = adata.copy()
    one.obs["donor"] = pd.Categorical(["d0"] * one.n_obs)
    one.write_h5ad(project / "work" / "one_donor.h5ad")
    design = {"condition": "label", "reference": "ctrl", "treatment": "stim", "replicate": "donor"}
    assert status(settings, "de_design", path="work/good.h5ad", **design)[0] == "pass"
    result, message = status(settings, "de_design", path="work/one_donor.h5ad", **design)
    assert result == "fail" and "replicates" in message
    assert status(settings, "de_design", path="work/good.h5ad", **{**design, "replicate": "label"})[0] == "fail"


def test_perturbation_needs_controls_and_knockdown(settings: Settings, project: Path) -> None:
    perturb_adata().write_h5ad(project / "work" / "good.h5ad")
    perturb_adata(knockdown=False).write_h5ad(project / "work" / "no_kd.h5ad")
    perturb_adata(controls=5).write_h5ad(project / "work" / "few_controls.h5ad")
    args = {"perturbation": "gene", "control": "non-targeting"}
    result, message = status(settings, "perturbation", path="work/good.h5ad", **args)
    assert result == "pass" and "knockdown in 10 of 10" in message
    assert status(settings, "perturbation", path="work/no_kd.h5ad", **args)[0] == "fail"
    assert status(settings, "perturbation", path="work/few_controls.h5ad", **args)[0] == "fail"
    assert status(settings, "perturbation", path="work/no_kd.h5ad", knockdown=False, **args)[0] == "pass"


def test_gpu_check_fails_without_a_gpu(settings: Settings, project: Path) -> None:
    assert status(settings, "gpu_visible")[0] == "fail"  # no CUDA on the test machine


def test_bad_specs_are_errors_not_crashes(settings: Settings, project: Path) -> None:
    assert status(settings, "nonsense")[0] == "error"
    assert status(settings, "file")[0] == "error"  # missing path
    assert status(settings, "file", path="../../../etc/passwd")[0] == "fail"


def test_every_check_is_documented() -> None:
    text = describe()
    for name in registry():
        assert f"`{name}(" in text
