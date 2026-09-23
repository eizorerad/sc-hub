from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from schub.dashboard import build_dashboard
from schub.dashboard.collect import NodeView, RunView, StepView, collect, run_state, step_state
from schub.dashboard.lineage import pipeline_views, render_lineage, view_id
from schub.dashboard.steps import StepExtras, duration, slurm_seconds, step_extras
from schub.dashboard.views_main import render_jobs
from schub.dashboard.views_runs import run_detail
from schub.datasets import write_catalog_entry
from schub.projects import BranchSpec
from schub.service import Hub
from schub.slurm import QueueJob, Slurm
from schub.stepfile import ERROR_FILE, SUCCESS, SUMMARY_FILE

from .conftest import library_datasets, make_adata

MAIN = BranchSpec(dataset="kang2018", steps=({"brick": "qc_filter"}, {"brick": "normalize_embed"}))


@pytest.fixture
def hub(settings, cluster, ctx, write_h5ad):
    directory = library_datasets(settings) / "kang2018"
    write_catalog_entry(directory, {"title": "Kang"})
    write_h5ad(make_adata(), directory=directory)
    hub = Hub(settings, Slurm(cluster))
    hub.create_project("ifn", question="IFN response by cell type")
    return hub


def finish(step_dir: Path, summary: dict) -> None:
    results = step_dir / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / SUMMARY_FILE).write_text(json.dumps(summary))
    (results / "timing.json").write_text(json.dumps({"started": "a", "finished": "2026-09-23 10:00 UTC", "seconds": 125}))
    (results / "umap_leiden.png").write_bytes(b"full" * 100)
    (results / "umap_leiden_thumb.png").write_bytes(b"thumb")
    (step_dir / "slurm-1.log").write_text("\n".join(f"line {i}" for i in range(40)) + "\n\x1b[31mred\x1b[0m\n")
    (step_dir / SUCCESS).write_text("done")


@pytest.fixture
def two_branches(hub, cluster):
    plan = hub.save_branch("ifn", "main", MAIN)
    run = hub.submit(plan.plan_id)
    qc, norm = run.steps
    finish(Path(qc.step_dir), {"cells_final": 55})
    cluster.jobs[qc.job_id] = "COMPLETED"
    cluster.jobs[norm.job_id] = "RUNNING"
    hub.save_branch("ifn", "res-2", BranchSpec(from_branch="main", overrides={"normalize_embed": {"leiden_resolution": 2.0}}))
    hub.add_idea("ifn", "resolution", "Is clustering resolution-sensitive?")
    hub.update_idea("ifn", "resolution", status="planned", branches=("res-2",))
    return run


def test_snapshot_merges_runs_and_planned_branches(hub, two_branches):
    snap = collect(hub)
    states = {n.brick + ":" + n.state for n in snap.nodes}
    assert {"qc_filter:COMPLETED", "normalize_embed:RUNNING", "normalize_embed:PLANNED"} <= states
    qc_node = next(n for n in snap.nodes if n.brick == "qc_filter")
    assert qc_node.labels == ("ifn/main", "ifn/res-2") and qc_node.headline == "55 cells kept"
    step = snap.steps_by_key[qc_node.key]
    assert step.extras.seconds == 125 and step.extras.log_tail[-1] == "red" and step.extras.resources["cpus"] == "8"
    views = {v.view_id: v for v in pipeline_views(snap.nodes)}
    assert {"v-all", "v-ifn-main", "v-ifn-res-2"} <= set(views) and views["v-ifn-main"].group == "ifn"


def test_page_has_every_view_selectors_and_step_templates(hub, two_branches, settings):
    info = build_dashboard(hub)
    page = Path(info.path).read_text()
    for view in ("overview", "pipelines", "runs", "jobs", "library", "projects"):
        assert f'data-view="{view}"' in page
    assert 'data-pipe="v-ifn-main"' in page and 'data-pipe-view="v-ifn-res-2"' in page
    assert f'data-run="{two_branches.run_id}"' in page and 'id="run-search"' in page
    qc_key = two_branches.steps[0].step_key
    assert f'<template data-node="{qc_key}">' in page and "55 cells kept" in page and "line 39" in page
    assert "Is clustering resolution-sensitive?" in page and "st-PLANNED" in page
    assert (settings.view_dir / "img" / qc_key / "umap_leiden_thumb.png").read_bytes() == b"thumb"
    assert (settings.view_dir / "img" / qc_key / "umap_leiden.png").exists()  # recent run: full size too
    assert info.bytes < 200_000 and "<script>" in page and "http" not in re.sub(r"https?://[a-z./]*sc-hub", "", page.split("<script>")[1])


def test_old_layout_and_unused_images_are_removed(hub, settings):
    stale_page = settings.view_dir / "runs" / "gone.html"
    stale_img = settings.view_dir / "img" / "old" / "x.png"
    for path in (stale_page, stale_img):
        path.parent.mkdir(parents=True)
        path.write_text("old")
    build_dashboard(hub)
    assert not stale_page.exists() and not stale_img.exists() and not stale_img.parent.exists()


def test_step_and_run_states(tmp_path):
    assert step_state(tmp_path, "7", {"7": "PENDING"}) == ("PENDING", "")
    (tmp_path / ERROR_FILE).write_text(json.dumps({"message": "boom"}))
    assert step_state(tmp_path, "7", {}) == ("FAILED", "boom")
    (tmp_path / ERROR_FILE).unlink()
    assert step_state(tmp_path, "7", {})[0] == "STOPPED"
    assert step_state(tmp_path, None, {})[0] == "MISSING"
    assert run_state(["COMPLETED", "RUNNING"]) == "RUNNING"
    assert run_state(["COMPLETED", "STOPPED"]) == "FAILED"
    assert run_state(["COMPLETED"]) == "COMPLETED"


def test_queue_errors_mark_live_runs_unknown_not_failed(hub, cluster, monkeypatch):
    plan = hub.save_branch("ifn", "main", MAIN)
    hub.submit(plan.plan_id)

    def broken(_args):
        raise __import__("schub.slurm", fromlist=["SlurmError"]).SlurmError("squeue down")

    monkeypatch.setattr(hub.slurm, "_run", broken)
    snap = collect(hub)
    assert snap.jobs == () and "squeue down" in snap.jobs_error
    assert snap.runs[0].state == "UNKNOWN"


def test_untrusted_text_is_escaped(hub, settings):
    evil = '"><img src=x onerror=alert(1)>'
    hub.add_idea("ifn", "xss", evil, hypothesis=evil)
    hub.add_log("ifn", evil)
    fake = settings.runs_dir / "x onfocus=alert(1) autofocus"
    fake.mkdir(parents=True)
    (fake / "manifest.json").write_text(json.dumps({
        "run_id": "x onfocus=alert(1) autofocus", "plan_id": "p", "created_at": "z",
        "dataset": "d", "steps": [], "schub_version": "0"}))
    page = Path(build_dashboard(hub).path).read_text()
    assert "<img src=x" not in page and "onfocus=alert(1) autofocus" not in page


def test_run_detail_lists_top_de_genes_and_groups(tmp_path):
    step_dir = tmp_path / "step"
    (step_dir / "results").mkdir(parents=True)
    (step_dir / "results" / "de_all.csv").write_text(
        ",baseMean,log2FoldChange,lfcSE,stat,pvalue,padj,group\n"
        "ISG15,10,5.1,0.1,9,1e-30,1e-28,B cells\nACTB,10,0.1,0.1,1,0.5,nan,B cells\nIFI6,9,4.0,0.2,8,1e-20,1e-18,NK cells\n"
    )
    summary = {"contrast": "stim vs ctrl", "groups": {"B cells": {"significant": 2, "top_up": ["ISG15"], "top_down": []}}}
    step = StepView(index=1, brick="pseudobulk_de", key="k", state="COMPLETED", summary=summary,
                    step_dir=str(step_dir), extras=StepExtras(seconds=30))
    run = RunView(run_id="r", project=None, branch=None, created_at="now", dataset="kang", state="COMPLETED", steps=(step,))
    html = run_detail(run, lambda _s, _n, _f: None)
    assert html.index("ISG15") < html.index("IFI6") and "1.0e-28" in html and "ACTB" not in html.split("Top 12")[1]
    assert "ad-hoc run" in html and "stim vs ctrl" in html and "30s" in html


def test_broken_step_files_do_not_break_the_run_page(tmp_path, monkeypatch):
    step_dir = tmp_path / "step"
    (step_dir / "results").mkdir(parents=True)
    (step_dir / "results" / "de_all.csv").write_text(",log2FoldChange,padj,group\nISG15,abc,1e-3,B\n")
    de = StepView(index=1, brick="pseudobulk_de", key="k", state="COMPLETED", step_dir=str(step_dir))
    other = StepView(index=2, brick="qc_filter", key="q", state="COMPLETED", step_dir=str(step_dir),
                     extras=StepExtras(figures=("umap.png",)))
    run = RunView(run_id="r", project=None, branch=None, created_at="now", dataset="d", state="COMPLETED", steps=(de, other))

    def unreadable(step, _name, _full):
        if step.index == 2:
            raise PermissionError("no access")
        return None

    html = run_detail(run, unreadable)
    assert "Could not read de_all.csv (ValueError)" in html
    assert "Could not show this step (PermissionError)" in html and "1. pseudobulk_de" in html


def test_similar_labels_get_distinct_pipeline_ids():
    nodes = tuple(
        NodeView(key=k, parent="ds:d", dataset="d", brick="qc_filter", state="COMPLETED", labels=(label,))
        for k, label in (("a", "Proj_1/main"), ("b", "proj-1/main"), ("c", "PROJ 1/main"))
    )
    ids = [v.view_id for v in pipeline_views(nodes)]
    assert len(set(ids)) == len(ids) == 4 and "v-proj-1-main-2" in ids and "v-proj-1-main-3" in ids


def test_branch_points_show_the_differing_param():
    def node(key, parent, latent):
        return NodeView(key=key, parent=parent, dataset="d", brick="integrate_scvi", state="COMPLETED",
                        headline="326 epochs, GPU", params={"batch_key": "donor", "n_latent": latent})

    svg = render_lineage((
        NodeView(key="q", parent="ds:d", dataset="d", brick="qc_filter", state="COMPLETED", headline="10 cells kept"),
        node("a", "q", 30), node("b", "q", 10),
    ))
    assert "n_latent=30" in svg and "n_latent=10" in svg and "10 cells kept" in svg
    assert 'data-path="a q"' in svg and view_id("Proj_1/main") == "v-proj-1-main"


def test_jobs_view_shows_progress_and_plain_reasons():
    jobs = (
        QueueJob(job_id="1", name="schub-abc-01-qc", state="RUNNING", elapsed="30:00", reason="ws-l1-001",
                 partition="ws-ia", time_limit="1:00:00"),
        QueueJob(job_id="2", name="schub-abc-02-norm", state="PENDING", elapsed="0:00",
                 reason="(QOSMaxJobsPerUserLimit)", partition="ws-ia", time_limit="1:00:00"),
        QueueJob(job_id="3", name="vcc-other", state="RUNNING", elapsed="1:00", reason="n", partition="gpu"),
    )
    from schub.dashboard.collect import Snapshot

    snap = Snapshot(generated_at="t", user="u", library_mode="m", env_id="e", versions={}, jobs=jobs, runs=(),
                    projects=(), datasets=(), models=(), nodes=())
    html = render_jobs(snap)
    assert "width:50%" in html and "max 2 running jobs" in html and "1 other jobs" in html


def test_time_helpers(tmp_path):
    assert slurm_seconds("45:00") == 2700 and slurm_seconds("1-02:00:00") == 93600
    assert slurm_seconds("UNLIMITED") is None and slurm_seconds("") is None
    assert duration(None) == "" and duration(42) == "42s" and duration(125) == "2m 05s" and duration(7300) == "2h 01m"
    (tmp_path / "job.sbatch").write_text("#SBATCH --cpus-per-task=8\n#SBATCH --mem=20G\n#SBATCH --gres=gpu:1\n")
    (tmp_path / SUCCESS).write_text("x")
    extras = step_extras(tmp_path, with_log=True)
    assert extras.resources == {"cpus": "8", "mem": "20G", "gpu": "gpu:1"} and extras.finished and extras.log_tail == ()


def test_cell_map_points_are_small_scaled_and_cached(tmp_path, monkeypatch):
    from schub.dashboard import _Images
    from schub.dashboard import points as points_module
    from schub.dashboard.points import points_payload
    from schub.dashboard.views_runs import cell_map

    adata = make_adata(n_obs=50)
    adata.obs["leiden"] = pd.Categorical([str(i % 3) for i in range(50)])
    adata.obs["weird"] = pd.Categorical(["</script><b>" if i % 2 else "ok" for i in range(50)])
    adata.obsm["X_umap"] = np.random.default_rng(0).normal(size=(50, 2))
    step_dir = tmp_path / "step"
    step_dir.mkdir()
    adata.write_h5ad(step_dir / "output.h5ad")
    monkeypatch.setattr(points_module, "MAX_POINTS", 20)
    payload = points_payload(step_dir / "output.h5ad")
    assert payload["n"] == 50 and len(payload["x"]) == 20 and min(payload["x"]) >= 0 and max(payload["y"]) <= 1000
    assert list(payload["cols"])[0] == "leiden" and "score" not in payload["cols"]

    step = StepView(index=1, brick="normalize_embed", key="k1", state="COMPLETED", step_dir=str(step_dir))
    images = _Images(tmp_path / "view", {"k1"})
    assert cell_map(step, images, folded=False) == ""  # no _SUCCESS marker yet
    (step_dir / SUCCESS).write_text("ok")
    html = cell_map(step, images, folded=True)
    assert 'data-pts="pts/k1.js"' in html and html.startswith("<details")
    script = (tmp_path / "view" / "pts" / "k1.js").read_text()
    assert "</script>" not in script and script.startswith("(window.SCHUB_PTS")
    assert cell_map(step.model_copy(update={"key": "other"}), images, folded=False) == ""  # not a recent run
    stale = tmp_path / "view" / "pts" / "old.js"
    stale.write_text("x")
    images.prune()
    assert not stale.exists() and (tmp_path / "view" / "pts" / "k1.js").exists()


def test_library_shows_references_tools_and_fastq(hub, settings):
    from schub.dashboard.collect import collect
    from schub.dashboard.views_main import render_library

    tool = settings.library / "tools" / "cellxgene" / "1.3.0" / "bin" / "python"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    (settings.library / "tools" / "cellxgene" / "current").symlink_to("1.3.0")
    snap = collect(hub)
    status = {r.name: r.status for r in snap.references}
    assert status["cellxgene"] == "1.3.0" and status["cellranger"] == "not installed"
    assert status["kallisto index (human)"].startswith("not installed")
    assert "References and tools" in render_library(snap)


def test_overview_lists_sessions_with_how_to_open(hub, settings, cluster):
    from schub.dashboard.collect import collect
    from schub.dashboard.views_main import render_overview
    from schub.sessions import session_dir, write_connection

    info = hub.start_session("jupyter", hours=3)
    cluster.jobs[info.session_id] = "RUNNING"
    write_connection(session_dir(settings, info.session_id), "ws-l1-004", 40999, "/lab?token=t")
    snap = collect(hub)
    assert snap.sessions[0].node == "ws-l1-004"
    html = render_overview(snap)
    assert "JupyterLab" in html and "./schub-lab jupyter" in html and "token" not in html
