"""The dashboard for students with dozens of experiments a day: one row per branch
(Experiments), one graph at a time with its relatives as links (Pipelines), graphs
as files loaded on demand, and sc-hub's queue in Runs."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from schub.config import Limits
from schub.dashboard import build_dashboard
from schub.dashboard.collect import collect
from schub.dashboard.experiments import experiments_payload
from schub.dashboard.page import render_site
from schub.dashboard.relatives import MAX_STUBS
from schub.dashboard.views_activity import render_queue
from schub.dashboard.views_projects import RECENT_BRANCHES, render_projects
from schub.datasets import write_catalog_entry
from schub.projects import BranchSpec
from schub.service import Hub
from schub.slurm import Slurm

from .conftest import library_datasets, make_adata
from .dashboard_helpers import graph_of, view_files
from .test_dashboard import finish

MAIN = BranchSpec(dataset="kang2018", steps=(
    {"brick": "qc_filter", "params": {"max_pct_mt": 100}}, {"brick": "normalize_embed"}))


@pytest.fixture
def hub(settings, cluster, ctx, write_h5ad):
    directory = library_datasets(settings) / "kang2018"
    write_catalog_entry(directory, {"title": "Kang"})
    adata = make_adata(n_obs=90)
    adata.obs["cell_type"] = pd.Categorical(["T", "B", "Mono"][i % 3] for i in range(adata.n_obs))
    write_h5ad(adata, directory=directory)
    hub = Hub(replace(settings, limits=Limits(max_active_runs=1)), Slurm(cluster))
    hub.create_project("ifn", question="q")
    hub.save_branch("ifn", "main", MAIN)
    return hub


def run_main(hub, cluster):
    run = hub.submit(hub.plan_branch("ifn", "main").plan_id)
    for step, summary in zip(run.steps, ({"cells_final": 88}, {"n_clusters": 9})):
        finish(Path(step.step_dir), summary)
        cluster.jobs[step.job_id] = "COMPLETED"
    return run


def rows(hub):
    snap = collect(hub)
    views = {f"{b.project}/{b.name}": f"v-{b.project}-{b.name}" for b in snap.branches.values()}
    return {r["id"]: r for r in experiments_payload(snap, views, lambda *_: None)["rows"]}


def test_a_row_per_branch_with_its_numbers_labels_and_sweep(hub, cluster):
    run_main(hub, cluster)
    hub.sweep_branch("ifn", "main", 2, "leiden_resolution", [0.5, 2.0], "res", "resolution")
    hub.label_branch("ifn", "main", add_tags=["baseline"], pinned=True)
    found = rows(hub)
    main = found["ifn/main"]
    assert main["state"] == "COMPLETED" and main["metrics"] == {"cells": 88, "clusters": 9}
    assert main["tags"] == ["baseline"] and main["pinned"] and main["view"] == "v-ifn-main"
    assert [s["short"] for s in main["steps"]] == ["QC", "Normalize"] and main["steps"][0]["shared"]
    variant = found["ifn/res-0-5"]
    assert variant["sweep"] == {"name": "res", "step": 2, "param": "leiden_resolution", "value": 0.5}
    assert variant["origin"] == "fork of main@r1 at step 2" and variant["state"] == "PLANNED"
    assert variant["metrics"] == {"cells": 88}  # its QC is main's QC (same step)


def test_numbers_of_an_outdated_branch_are_marked_as_older(hub, cluster, monkeypatch):
    run_main(hub, cluster)
    monkeypatch.setattr("schub.service.env_id", lambda: "newer")
    main = rows(hub)["ifn/main"]
    assert main["state"] == "OUTDATED" and main["metrics"] == {"cells": 88, "clusters": 9}
    assert set(main["stale"]) == {"cells", "clusters"}


def test_a_branch_waiting_in_sc_hubs_queue_says_so(hub, cluster):
    hub.submit(hub.plan_branch("ifn", "main").plan_id)  # takes the only slot
    hub.save_branch("ifn", "other", BranchSpec(from_branch="main", overrides={"normalize_embed": {"n_pcs": 10}}))
    waiting = hub.submit(hub.plan_branch("ifn", "other").plan_id)
    assert waiting.status == "queued"
    assert rows(hub)["ifn/other"]["state"] == "WAITING"
    queue = render_queue(collect(hub))
    assert "Waiting in sc-hub" in queue and "ifn/other" in queue


def test_the_rows_travel_as_safe_json_in_the_page(hub):
    hub.save_branch("ifn", "tricky", BranchSpec(from_branch="main", description="</script><b>x</b> & <!--"))
    page = render_site(collect(hub), lambda *_: None).index
    raw = re.search(r'<script type="application/json" id="exp-data">(.*?)</script>', page, re.S).group(1)
    assert "<" not in raw and ">" not in raw  # cannot close the script tag or open markup
    payload = json.loads(raw)
    assert next(r for r in payload["rows"] if r["branch"] == "tricky")["desc"] == "</script><b>x</b> & <!--"
    assert "window.SCHUB_EXPERIMENTS" in page and 'data-tab="experiments"' in page


def test_a_branch_graph_links_the_branches_around_it(hub, cluster):
    hub.save_branch("ifn", "coarse", BranchSpec(from_branch="main", overrides={"normalize_embed": {"leiden_resolution": 0.5}}))
    hub.save_branch("ifn", "strict", BranchSpec(from_branch="main", overrides={"qc_filter": {"min_genes": 5}}))
    site = render_site(collect(hub), lambda *_: None)
    main = graph_of(site.files, "v-ifn-main")
    assert 'data-href="#pipelines/v-ifn-coarse"' in main and "leiden_resolution=0.5" in main
    assert 'data-href="#pipelines/v-ifn-strict"' in main and "min_genes=5" in main  # parts at step 1
    assert "project map" in main and "#experiments/project=ifn" in main and 'data-ask="sweep"' in main


def test_a_crowd_of_relatives_folds_into_more(hub):
    values = [0.2, 0.4, 0.6, 0.8, 1.2, 1.4]
    hub.sweep_branch("ifn", "main", 2, "leiden_resolution", values, "res", "resolution")
    main = graph_of(render_site(collect(hub), lambda *_: None).files, "v-ifn-main")
    stubs = re.findall(r'class="node stub[^"]*" data-key="([^"]+)"', main)
    assert len(stubs) == MAX_STUBS and "stub:+" in stubs[-1]
    assert f"+{len(values) - MAX_STUBS + 1} more" in main and 'data-href="#experiments/sweep=res"' in main


def test_graph_files_are_written_once_and_pruned(hub, settings):
    view = hub.settings.view_dir
    build_dashboard(hub)
    assert set(view_files(view)) == {"br/p-ifn.js", "br/v-ifn-main.js"}
    hub.save_branch("ifn", "gone", BranchSpec(from_branch="main"))
    build_dashboard(hub)
    assert (view / "br" / "v-ifn-gone.js").exists()
    (hub.projects.require("ifn") / "pipelines" / "gone.yaml").unlink()
    build_dashboard(hub)
    assert not (view / "br" / "v-ifn-gone.js").exists()
    before = (view / "br" / "v-ifn-main.js").stat().st_mtime_ns
    build_dashboard(hub)
    assert (view / "br" / "v-ifn-main.js").stat().st_mtime_ns == before  # unchanged: not rewritten
    assert len((view / "index.html").read_text()) < 150_000


def test_a_project_card_lists_pinned_and_recent_branches_and_links_the_rest(hub):
    for n in range(RECENT_BRANCHES + 2):
        hub.save_branch("ifn", f"v{n:02d}", BranchSpec(from_branch="main", overrides={"normalize_embed": {"n_pcs": 10 + n}}))
    hub.label_branch("ifn", "main", pinned=True)
    hub.label_branch("ifn", "v00", archived=True)
    card = render_projects(collect(hub))
    shown = re.findall(r"<tr><td><code>([^<]+)</code>", card)
    assert len(shown) == RECENT_BRANCHES and shown[0] == "main" and "v00" not in shown
    assert f"all {RECENT_BRANCHES + 3} in Experiments" in card and "#experiments/project=ifn" in card


def test_a_diverged_model_s_nan_does_not_break_the_page(hub, cluster):
    run = hub.submit(hub.plan_branch("ifn", "main").plan_id)
    for step, summary in zip(run.steps, ({"cells_final": float("nan")}, {"n_clusters": 9})):
        finish(Path(step.step_dir), summary)
        cluster.jobs[step.job_id] = "COMPLETED"
    page = render_site(collect(hub), lambda *_: None).index
    raw = re.search(r'<script type="application/json" id="exp-data">(.*?)</script>', page, re.S).group(1)
    row = next(r for r in json.loads(raw)["rows"] if r["branch"] == "main")  # strict JSON: no NaN token
    assert "cells" not in row["metrics"] and row["metrics"]["clusters"] == 9
    assert page.count("<script>") == 2  # the Experiments script cannot stop the main one
