"""Build a demo dashboard from synthetic runs, for the browser click-through.

One brick-era project with a finished branch, variants of one parameter (two runs done,
others running or queued in Slurm), a variant never run, and two bench projects with
journals. Usage:
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
    for value in (0.3, 0.6, 1.2, 2.4):
        hub.save_branch("ifn", f"res-{str(value).replace('.', '-')}",
                        BranchSpec(from_branch="main", overrides={"normalize_embed": {"leiden_resolution": value}}))
    for i, name in enumerate(["res-0-3", "res-0-6"]):
        _finish(cluster, hub.submit(hub.plan_branch("ifn", name).plan_id),
                [{"cells_final": 88}, {"n_clusters": 5 + 3 * i}, {"n_labels": 6 + i},
                 {"groups_tested": 3, "significant_genes": 420, "groups": {}}])
    for name in ("res-1-2", "res-2-4"):  # running and queued in Slurm
        hub.submit(hub.plan_branch("ifn", name).plan_id)
    hub.save_branch("ifn", "strict", BranchSpec(from_branch="main", overrides={"qc_filter": {"min_genes": 5}}))
    _bench_project(hub)
    info = build_dashboard(hub, out / "view")
    print(info.path)


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8cfc0f01f0005"
    "00020101b6e36b790000000049454e44ae426082")


def _bench_project(hub: Hub) -> None:
    """Two bench projects with cells, a figure, checks, a job, notes and a hand-over."""
    from schub.bench.checkpoint import CheckpointStore
    from schub.bench.journal import Journal
    from schub.bench.models import Actor, CellEntry, CheckResult, JobRef, OutputItem

    for name, question in (("k562-qc", "Is the K562 essential screen good enough to model?"),
                           ("cell-jepa", "Does the cell-JEPA paper reproduce on one GPU?")):
        hub.projects.create(name, question=question)
        journal = Journal(hub.settings.projects_dir / name, name)
        codex, claude = Actor(client="codex-mcp-client"), Actor(client="claude-code")
        lab = Actor(kind="lab_agent", client="claude-code", engine="claude", session_id="s-1")
        cells = (
            ("download K562 essential", "one .h5ad with raw counts", "ok", codex,
             {"outputs": (OutputItem(kind="stream", text="fetched ... (1.9 GB, sha256 5f1e...)\n<b>not bold</b>"),)}),
            ("QC on the twin", "a histogram and a QC table", "ok", claude,
             {"outputs": (OutputItem(kind="display", image="cells/c0002/fig-001.png"),),
              "check_results": (CheckResult(name="perturbation", status="pass", message="knockdown in 41 of 50"),),
              "data_scope": "twin"}),
            ("full QC as a job", "a job id", "ok", lab,
             {"outputs": (OutputItem(kind="stream", text="Submitted Slurm job 812\n" + "line\n" * 30),),
              "jobs": (JobRef(job_id="812", state="COMPLETED", exit_code=0),), "data_scope": "full"}),
            ("a failing cell", "no error", "error", codex,
             {"outputs": (OutputItem(kind="error", ename="KeyError", text="KeyError: 'gene'"),)}),
        )
        for why, expect, status, actor, extra in cells:
            cid = journal.allocate("c")
            journal.write_cell(CellEntry(ref=f"{name}#{cid}", project=name, cid=cid, why=why, expect=expect,
                                         code=f"# {why}\nprint('x')", created=journal.now(), status=status,
                                         actor=actor, duration_s=12.0, **extra))
        figure = journal.artifacts_dir("c0002") / "fig-001.png"
        figure.parent.mkdir(parents=True, exist_ok=True)
        figure.write_bytes(PNG)
        journal.add_note("registration", "knockdown in at least half of the targets, else stop")
        journal.add_note("decision", "use K562 essential, not genome-wide", because=[f"{name}#c0001"],
                         reverses_if="genome-wide fits in 90 GB backed")
        journal.add_note("finding", "median 2,000 cells per guide", because=[f"{name}#c0002"],
                         unresolved_numbers=("2,000",))
        journal.add_note("note", "please confirm the control label", audience="human")
        store = CheckpointStore(hub.settings.projects_dir / name)
        store.write("active", next_action="full QC, then pseudobulk")
        store.write_handoff("# Where we are\n- twin QC done (c0002)\n- full QC job 812 done")
    _report(hub, "k562-qc")


def _report(hub: Hub, name: str) -> None:
    """A published report of the first project (the line under its question)."""
    from schub.bench.report_spec import Block, ReportSpec
    from schub.bench.service import BenchService

    spec = ReportSpec(title="K562 essential screen: good enough to model?",
                      summary="Yes on the twin: knockdown in 41 of 50 targets; the full QC job 812 finished.",
                      blocks=(Block(text="## Data"), Block(cell="c0001", show="outputs"),
                              Block(figure="c0002", caption="QC on the twin"), Block(note="n0003")))
    BenchService(hub.settings, hub.slurm).report(name, spec, publish=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
