"""Build a demo dashboard from synthetic runs, for the browser click-through.

One project with a finished branch, a sweep (two runs done, one running, one queued,
one waiting in sc-hub's queue), a variant never run, tags and a pin. Usage:
    python tests/e2e/demo_site.py <out dir>   ->   <out dir>/view/index.html
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "src"), str(REPO)]

import pandas as pd  # noqa: E402

from schub.config import Limits, Settings  # noqa: E402
from schub.dashboard import build_dashboard  # noqa: E402
from schub.datasets import write_catalog_entry  # noqa: E402
from schub.projects import BranchSpec  # noqa: E402
from schub.service import Hub  # noqa: E402
from schub.slurm import Slurm  # noqa: E402
from schub.stepfile import SUCCESS, SUMMARY_FILE  # noqa: E402
from tests.conftest import FakeCluster, make_adata  # noqa: E402

STEPS = ({"brick": "qc_filter"}, {"brick": "normalize_embed"}, {"brick": "annotate_celltypist"},
         {"brick": "pseudobulk_de", "params": {"condition_key": "label", "reference": "ctrl", "treatment": "stim",
                                               "replicate_key": "donor", "group_key": "cell_type"}})


def _hub(out: Path) -> tuple[Hub, FakeCluster]:
    root, library = out / "root", out / "library"
    for folder in (root / "data", library / "datasets", library / "models/celltypist/data/models"):
        folder.mkdir(parents=True, exist_ok=True)
    (library / "models/celltypist/data/models/Immune_All_Low.pkl").write_bytes(b"m")
    settings = Settings(root=root, python=root / "env/bin/python", library=library, limits=Limits(max_active_runs=2))
    directory = library / "datasets" / "kang2018"
    write_catalog_entry(directory, {"title": "Kang"})
    adata = make_adata(n_obs=90)
    adata.obs["cell_type"] = pd.Categorical(["T", "B", "Mono"][i % 3] for i in range(90))
    adata.write_h5ad(directory / "data.h5ad")
    cluster = FakeCluster()
    return Hub(settings, Slurm(cluster)), cluster


def _finish(cluster: FakeCluster, run, summaries: list[dict]) -> None:
    for step, summary in zip(run.steps, summaries):
        folder = Path(step.step_dir)
        (folder / "results").mkdir(parents=True, exist_ok=True)
        (folder / "results" / SUMMARY_FILE).write_text(json.dumps(summary))
        (folder / SUCCESS).write_text("ok")
        cluster.jobs[step.job_id] = "COMPLETED"


def main(out: Path) -> None:
    hub, cluster = _hub(out)
    hub.create_project("ifn", question="How does IFN-beta change each cell type?")
    reference = BranchSpec(dataset="kang2018", steps=STEPS, description="Reference analysis <b>not bold</b>")  # text, not markup
    hub.save_branch("ifn", "main", reference)
    _finish(cluster, hub.submit(hub.plan_branch("ifn", "main").plan_id),
            [{"cells_final": 88}, {"n_clusters": 9}, {"n_labels": 7, "reference_key": "cell_type", "reference_purity": 0.91},
             {"groups_tested": 3, "significant_genes": 420, "groups": {}}])
    hub.sweep_branch("ifn", "main", 2, "leiden_resolution", [0.3, 0.6, 1.2, 2.4, 4.8], "res", "how many clusters?")
    for i, name in enumerate(["res-0-3", "res-0-6"]):
        _finish(cluster, hub.submit(hub.plan_branch("ifn", name).plan_id),
                [{"cells_final": 88}, {"n_clusters": 5 + 3 * i}, {"n_labels": 6 + i},
                 {"groups_tested": 3, "significant_genes": 420, "groups": {}}])
    for name in ("res-1-2", "res-2-4", "res-4-8"):  # running, queued, waiting in sc-hub's queue
        hub.submit(hub.plan_branch("ifn", name).plan_id)
    hub.save_branch("ifn", "strict", BranchSpec(from_branch="main", overrides={"qc_filter": {"min_genes": 5}}))
    hub.label_branch("ifn", "main", add_tags=["baseline"], pinned=True)
    hub.label_branch("ifn", "strict", add_tags=["qc"])
    info = build_dashboard(hub, out / "view")
    print(info.path)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
