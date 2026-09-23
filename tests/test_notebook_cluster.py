"""A run's notebook really runs: every code cell executes with the analysis extras,
re-runs the pipeline's steps with the brick code and matches the job's numbers.

Needs scanpy; skipped otherwise. Run on the cluster like test_impl_cluster.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("scanpy")

from schub.execute import run_step  # noqa: E402
from schub.notebook import load_run, render_notebook  # noqa: E402
from schub.service import Hub  # noqa: E402
from schub.slurm import Slurm  # noqa: E402
from schub.stepfile import SUMMARY_FILE  # noqa: E402

from .conftest import make_adata  # noqa: E402

pytestmark = pytest.mark.cluster


def test_generated_notebook_re_runs_the_pipeline(settings, cluster, ctx, tmp_path, monkeypatch):
    data = settings.data_dir / "mini.h5ad"
    make_adata(n_obs=300, seed=3).write_h5ad(data)
    hub = Hub(settings, Slurm(cluster))
    plan = hub.plan(str(data), [
        {"brick": "qc_filter", "params": {"min_genes": 5, "max_pct_mt": 100, "detect_doublets": False}},
        {"brick": "normalize_embed", "params": {"n_top_genes": 50, "n_pcs": 10}},
    ])
    assert plan.ok, plan.issues
    manifest = hub.submit(plan.plan_id)
    for step in manifest.steps:  # what the Slurm jobs would do
        assert run_step(Path(step.step_dir)) == 0
    before = os.listdir(manifest.steps[1].step_dir)
    notebook = render_notebook(load_run(manifest))
    monkeypatch.chdir(tmp_path)
    namespace: dict = {}
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            exec(compile("".join(cell["source"]), "<cell>", "exec"), namespace)  # noqa: S102 - generated notebook
    saved = json.loads((Path(manifest.steps[1].step_dir) / "results" / SUMMARY_FILE).read_text())
    assert namespace["summary"]["n_clusters"] == saved["n_clusters"]
    work = tmp_path / "work" / manifest.run_id
    assert (work / "1_qc_filter" / "output.h5ad").is_file() and (work / "2_normalize_embed" / "results").is_dir()
    assert namespace["adata"].n_obs == saved["cells"]
    assert sorted(os.listdir(manifest.steps[1].step_dir)) == sorted(before)  # the pipeline's cache is untouched
