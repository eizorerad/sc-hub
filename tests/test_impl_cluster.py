"""End-to-end check of brick implementations against the planner's state.

Needs the analysis extras (scanpy, scvi-tools, pydeseq2); skipped otherwise.
Run on the cluster:  srun ... ~/schub/env/bin/python -m pytest tests/test_impl_cluster.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("scanpy")
pytest.importorskip("scvi")
pytest.importorskip("pydeseq2")

import anndata as ad  # noqa: E402
from scipy import sparse  # noqa: E402

from schub.bricks import REGISTRY, BrickError, StepIO  # noqa: E402
from schub.execute import load_impl  # noqa: E402
from schub.h5ad_profile import profile_h5ad  # noqa: E402
from schub.planner import StepRequest, build_plan  # noqa: E402

pytestmark = pytest.mark.cluster
PLANTED = [f"GENE{i}" for i in range(8)]


def synthetic(n_per_group: int = 60, seed: int = 0) -> ad.AnnData:
    """4 donors x 2 conditions x 3 cell types; genes 0-7 up in stim, type A only."""
    rng = np.random.default_rng(seed)
    genes = [f"GENE{i}" for i in range(290)] + [f"MT-G{i}" for i in range(10)]
    rows, obs = [], []
    for donor in range(4):
        donor_scale = rng.uniform(0.8, 1.2)
        for label in ("ctrl", "stim"):
            for cell_type, base in (("A", 3.0), ("B", 1.5), ("C", 0.8)):
                mean = np.full(len(genes), base) * donor_scale
                mean[: 40] *= {"A": 3, "B": 0.3, "C": 1}[cell_type]  # separable types
                if label == "stim" and cell_type == "A":
                    mean[:8] *= 4
                rows.append(rng.poisson(mean, size=(n_per_group, len(genes))))
                obs += [{"donor": f"d{donor}", "label": label, "cell_type": cell_type}] * n_per_group
    frame = pd.DataFrame(obs).astype("category")
    frame.index = [f"c{i}" for i in range(len(frame))]
    return ad.AnnData(
        X=sparse.csr_matrix(np.vstack(rows).astype(np.float32)), obs=frame, var=pd.DataFrame(index=genes)
    )


def run_pipeline(path, steps, ctx, workdir):
    plan = build_plan(profile_h5ad(path), "fp", steps, ctx)
    assert plan.ok, plan.issues
    current, summaries = path, []
    for step in plan.steps:
        spec = REGISTRY[step.brick]
        out = None if step.terminal else workdir / f"{step.index:02d}.h5ad"
        results = workdir / f"{step.index:02d}_results"
        results.mkdir(parents=True)
        io = StepIO(input=current, output=out, results_dir=results, state_in=step.state_in,
                    context={"celltypist_dirs": ":".join(str(d) for d in ctx.celltypist_dirs)})
        summaries.append(load_impl(spec.impl)(io, spec.params_model.model_validate(step.params)))
        current = out or current
    return summaries, workdir


def test_qc_normalize_scvi_pseudobulk_chain(tmp_path, ctx):
    path = tmp_path / "synthetic.h5ad"
    synthetic().write_h5ad(path)
    steps = [
        StepRequest(brick="qc_filter", params={"min_genes": 5, "detect_doublets": False, "max_pct_mt": 100}),
        StepRequest(brick="normalize_embed", params={"n_top_genes": 150, "n_pcs": 10}),
        StepRequest(brick="integrate_scvi", params={"batch_key": "donor", "condition_key": "label", "max_epochs": 5}),
        StepRequest(
            brick="pseudobulk_de",
            params={"condition_key": "label", "reference": "ctrl", "treatment": "stim",
                    "replicate_key": "donor", "group_key": "cell_type", "paired": True},
        ),
    ]
    summaries, workdir = run_pipeline(path, steps, ctx, tmp_path / "work")
    qc, norm, scvi, de = summaries
    assert qc["cells_final"] == 1440 and norm["n_hvg"] == 150
    assert scvi["epochs"] >= 1
    group_a = de["groups"]["A"]
    assert group_a["design"] == "~replicate + condition" and group_a["replicates"] == {"ctrl": 4, "stim": 4}
    assert set(group_a["top_up"]) <= set(PLANTED) and len(group_a["top_up"]) == 5
    table = pd.read_csv(workdir / "04_results" / "de_all.csv", index_col=0)
    assert table.query("group == 'B'")["padj"].min() > 0.01  # no planted effect in B


def test_scvi_on_counts_in_x_without_layer(tmp_path, ctx):
    path = tmp_path / "raw.h5ad"
    synthetic(n_per_group=20).write_h5ad(path)
    steps = [StepRequest(brick="integrate_scvi", params={"batch_key": "donor", "condition_key": "label", "max_epochs": 2})]
    (summary,), _ = run_pipeline(path, steps, ctx, tmp_path / "work")
    assert summary["genes_used"] == 300


def test_confounded_batch_is_refused_in_the_job(tmp_path, ctx):
    data = synthetic(n_per_group=20)
    data.obs["run"] = data.obs["label"].astype(str).map({"ctrl": "run1", "stim": "run2"}).astype("category")
    path = tmp_path / "confounded.h5ad"
    data.write_h5ad(path)
    steps = [StepRequest(brick="integrate_scvi", params={"batch_key": "run", "condition_key": "label", "max_epochs": 1})]
    with pytest.raises(BrickError, match="confounded"):
        run_pipeline(path, steps, ctx, tmp_path / "work")
