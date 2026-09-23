from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from schub.dashboard import build_dashboard
from schub.dashboard.collect import NodeView, RunView, Snapshot, StepView, collect, run_state, step_state
from schub.dashboard.lineage import pipeline_views, render_lineage, view_id
from schub.dashboard.steps import StepExtras, duration, slurm_seconds, step_extras
from schub.dashboard.views_activity import render_queue, status_chip
from schub.dashboard.views_runs import run_detail
from schub.datasets import write_catalog_entry
from schub.projects import BranchSpec
from schub.service import Hub
from schub.slurm import QueueJob, Slurm
from schub.stepfile import ERROR_FILE, SUCCESS, SUMMARY_FILE

from .conftest import library_datasets, make_adata
from .dashboard_helpers import graph_of, templates_of, view_files

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
    views = {v.view_id: v for v in pipeline_views(snap.nodes, ["ifn"])}
    assert {"p-ifn", "v-ifn-main", "v-ifn-res-2"} == set(views) and views["v-ifn-main"].group == "ifn"
    assert views["p-ifn"].kind == "project" and set(views["p-ifn"].keys) >= set(views["v-ifn-main"].keys)


def test_page_has_every_view_selectors_and_step_templates(hub, two_branches, settings):
    info = build_dashboard(hub)
    page = Path(info.path).read_text()
    for view in ("projects", "pipelines", "runs", "library", "cluster"):
        assert f'data-view="{view}"' in page
    tabs = re.findall(r'data-tab="([a-z]+)"', page)
    assert tabs == ["journal", "projects", "pipelines", "experiments"]  # the bench's journal first, then the brick era
    menu = page[page.index('class="account"'):page.index('class="tabs"')]  # the sc-hub square holds the rest
    assert all(f'href="#{v}"' in menu for v in ("runs", "library", "runs/sessions", "cluster"))
    assert '<h1 class="view-title">Runs</h1>' in page  # a page opened from the menu says where you are
    assert 'class="account"' in page and 'id="autorefresh"' in page and 'data-ago="' in page
    assert page.index('class="account"') < page.index('class="tabs"')  # the sc-hub square is the menu
    files = view_files(settings.view_dir)
    assert 'data-pipe="p-ifn"' in page and 'id="exp-data"' in page  # graphs load on demand, rows are in the page
    assert {"br/p-ifn.js", "br/v-ifn-main.js", "br/v-ifn-res-2.js"} == set(files)
    assert 'data-pipe-view="v-ifn-res-2"' in graph_of(files, "v-ifn-res-2")
    assert f'data-run="{two_branches.run_id}"' in page and 'id="run-search"' in page
    qc_key = two_branches.steps[0].step_key
    assert f'<template data-node="{qc_key}">' in templates_of(files, "v-ifn-main") and f'data-node="{qc_key}"' not in page
    assert "55 cells kept" in page and "line 39" in page
    assert "Is clustering resolution-sensitive?" in page and "st-PLANNED" in graph_of(files, "v-ifn-res-2")
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
    assert len(set(ids)) == len(ids) == 3 and "v-proj-1-main-2" in ids and "v-proj-1-main-3" in ids


def test_branch_points_show_the_differing_param():
    def node(key, parent, latent):
        return NodeView(key=key, parent=parent, dataset="d", brick="integrate_scvi", state="COMPLETED",
                        headline="326 epochs, GPU", params={"batch_key": "donor", "n_latent": latent})

    svg = render_lineage((
        NodeView(key="q", parent="ds:d", dataset="d", brick="qc_filter", state="COMPLETED", headline="10 cells kept"),
        node("a", "q", 30), node("b", "q", 10),
    ))
    assert "n_latent=30" in svg and "n_latent=10" in svg and "10 cells kept" in svg
    assert 'data-path="a q ds:d"' in svg and view_id("Proj_1/main") == "v-proj-1-main"


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
    html = render_queue(snap)
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
    from schub.dashboard.views_library import render_library

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
    from schub.dashboard.views_activity import render_sessions
    from schub.sessions import session_dir, write_connection

    info = hub.start_session("jupyter", hours=3)
    cluster.jobs[info.session_id] = "RUNNING"
    write_connection(session_dir(settings, info.session_id), "ws-l1-004", 40999, "/lab?token=t")
    snap = collect(hub)
    assert snap.sessions[0].node == "ws-l1-004"
    html = render_sessions(snap)
    assert "JupyterLab" in html and "./schub-lab jupyter" in html and "token" not in html


def test_revisions_forks_subprojects_and_merges_are_visible(hub, two_branches, settings, write_h5ad):
    from schub.dashboard.page import render_site
    from schub.dashboard.views_projects import render_projects

    write_h5ad(make_adata(n_obs=30, seed=2), directory=library_datasets(settings) / "atlas")
    hub.revise_branch("ifn", "main", 1, "stricter QC", params={"min_genes": 20})
    hub.fork_branch("ifn", "main", 2, "coarse", "fewer clusters", params={"leiden_resolution": 0.4})
    hub.create_project("ifn/atlas", question="Does the atlas agree?")
    hub.save_branch("ifn/atlas", "joint", BranchSpec(dataset="kang2018", steps=(
        {"brick": "merge_datasets", "params": {"others": ["atlas"]}}, {"brick": "qc_filter", "params": {"min_genes": 5}})))
    snap = collect(hub)
    assert snap.branches["ifn/main"].revision == 2 and snap.branches["ifn/coarse"].forked_from == "main@r2#2"
    assert snap.branches["ifn/atlas/joint"].datasets == ("kang2018", "atlas")
    merge = next(n for n in snap.nodes if n.brick == "merge_datasets")
    assert merge.inputs == ("ds:atlas",) and "ifn/atlas/joint#1" in merge.refs
    projects = render_projects(snap)
    assert 'data-project="ifn/atlas"' in projects and "fork</span> of <code>main@r2</code> at step 2" in projects
    assert "stricter QC" in projects and "step 1 qc_filter: min_genes default → 20" in projects
    site = render_site(snap, lambda *_: None)
    page = site.index + "".join(site.files.values())
    assert "+ atlas" in page and 'data-ref=\\"ifn/atlas/joint#1\\"' in page and 'data-ask=\\"fork\\"' in page
    assert "revise_branch" in page and "fork_branch" in page and 'href="#cluster"' in page


def test_long_lists_get_a_filter_and_show_all(settings):
    from schub.dashboard.html import listing, subtabs

    rows = [(f"dataset {i}", f"<td>d{i}</td>") for i in range(40)]
    html = listing(("Dataset",), rows, "Filter datasets")
    assert 'class="list-filter"' in html and 'class="more"' in html and html.count("data-row") == 40
    short = listing(("Dataset",), rows[:3], "Filter datasets")
    assert "list-filter" not in short and "more" not in short
    tabs = subtabs("library", [("a", "Datasets", 40, html), ("b", "Models", 2, "m")])
    assert tabs.count('data-subview="library"') == 2 and "<span class=count>40</span>" in tabs


def test_cluster_view_renders_limits_storage_and_load():
    from schub.dashboard.views_cluster import render_cluster
    from schub.overview import Job, Limit, Overview, PartitionLoad, QosLimits, Storage

    ov = Overview(
        user="me", login_node="lo-02", generated_at="now", logins=("pts/1 today",),
        storage=(Storage(name="Lustre (/l)", path="/l", used_gb=2069, limit_gb=3072, files=10, files_limit=100),),
        limits=(QosLimits(qos="ia-std", partitions=("ws-ia",), limits=(Limit(name="running jobs", used=2, limit=2),
                                                                         Limit(name="GPUs", used=0, limit=None))),),
        jobs=(Job(job_id="1", name="x", state="PENDING", partition="ws-ia", cpus=8, mem_gb=32, gpus=1,
                  elapsed="0:00", time_limit="1:00:00", node="", reason="QOSMaxJobsPerUserLimit"),),
        partitions=(PartitionLoad(name="gpu", qos="gpu-1", max_time="UNLIMITED", nodes=4, cpus_alloc=34,
                                  cpus_total=512, gpus_used=2, gpus_total=32, mem_gb_per_node=754),),
    )
    html = render_cluster(ov)
    assert "2 of 2 (100%)" in html and 'class="bar bad"' in html and "no per-user limit" in html
    assert "waiting for a free job slot" in html and "2.0 TB of 3.0 TB" in html and "lo-02" in html
    assert "10 of 100" in html


def test_runs_carry_their_notebook_and_steps_show_their_code(hub, two_branches, settings, cluster):
    info = build_dashboard(hub)
    page = Path(info.path).read_text()
    run_id = two_branches.run_id
    script = settings.view_dir / "nb" / f"{run_id}.js"
    notebook = json.loads(script.read_text().split("]=", 1)[1].rstrip(";\n"))
    text = json.dumps(notebook)
    assert "def qc_filter(" in text and "nb.io(2)" in text and "ifn/main#1" in text
    assert f'data-notebook="{run_id}"' in page and 'data-name="ifn-main-r1"' in page and 'data-ask="jupyter"' in page
    assert '<template data-code-src="qc_filter">' in page and 'data-code="normalize_embed"' in page
    assert "def qc_filter(" in page and "earlier version of this code" not in page
    before = script.stat().st_mtime_ns
    partial = script.with_name(f".{script.name}.abc123")  # another build's file, not yet renamed
    partial.write_text("x")
    (script.parent / "index.json").write_text("{}")  # left by an earlier version
    build_dashboard(hub)
    assert script.stat().st_mtime_ns == before  # unchanged notebooks are not rewritten
    assert partial.exists() and not (script.parent / "index.json").exists()
    (settings.runs_dir / run_id / "manifest.json").unlink()
    build_dashboard(hub)
    assert not script.exists()  # notebooks of runs no longer shown are removed


def test_status_chip_says_what_runs_now():
    base = dict(generated_at="t", user="u", library_mode="m", env_id="e", versions={}, runs=(), projects=(),
                datasets=(), models=(), nodes=())
    idle = Snapshot(jobs=(), **base)
    assert "nothing running" in status_chip(idle)
    busy = Snapshot(jobs=(
        QueueJob(job_id="1", name="schub-abc-01-qc", state="RUNNING", partition="ws-ia", elapsed="1:00", time_limit="2:00", reason="None"),
        QueueJob(job_id="2", name="schub-abc-02-norm", state="PENDING", partition="ws-ia", elapsed="0:00", time_limit="2:00", reason="Dependency"),
        QueueJob(job_id="3", name="other", state="RUNNING", partition="ws-ia", elapsed="1:00", time_limit="2:00", reason="None"),
    ), **base)
    chip = status_chip(busy)
    assert "1 running · 1 queued" in chip and 'href="#runs/queue"' in chip
    assert "queue unreadable" in status_chip(idle.model_copy(update={"jobs_error": "down"}))


def test_code_of_a_step_run_with_older_code_is_flagged():
    from schub.dashboard.notebooks import code_section

    assert "earlier version of this code" in code_section("qc_filter", "0" * 16)
    assert "earlier version" not in code_section("qc_filter", "") and code_section("no_such", "") == ""
