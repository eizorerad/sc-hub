"""The dashboard tells the current state of each branch apart from its history.

Found on the live pilot: after an sc-hub update every finished branch showed as
'queued' with its old results beside planned copies, the 'All pipelines' graph was
mostly superseded steps, node subtitles compared unrelated bricks
("Merge · batch_key=None"), and plan warnings never reached the step panel."""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest

from schub.dashboard.collect import NodeView, collect
from schub.dashboard.lineage import render_graph
from schub.dashboard.views_pipelines import render_pipelines
from schub.dashboard.views_projects import render_projects
from schub.datasets import write_catalog_entry
from schub.projects import BranchSpec
from schub.service import Hub
from schub.slurm import Slurm

from .conftest import library_datasets, make_adata
from .test_dashboard import MAIN, finish

DESIGN = {"condition_key": "label", "reference": "ctrl", "treatment": "stim", "replicate_key": "donor"}


@pytest.fixture
def hub(settings, cluster, ctx, write_h5ad):
    directory = library_datasets(settings) / "kang2018"
    write_catalog_entry(directory, {"title": "Kang"})
    adata = make_adata(n_obs=90)
    adata.obs["cell_type"] = pd.Categorical(["T", "B", "Mono"][i % 3] for i in range(adata.n_obs))
    write_h5ad(adata, directory=directory)
    hub = Hub(settings, Slurm(cluster))
    hub.create_project("ifn", question="IFN response by cell type")
    return hub


@pytest.fixture
def finished_main(hub, cluster):
    plan = hub.save_branch("ifn", "main", MAIN)
    run = hub.submit(plan.plan_id)
    for step, summary in zip(run.steps, ({"cells_final": 88, "warnings": ["no MT- genes: filter had no effect"]},
                                         {"n_clusters": 9})):
        finish(Path(step.step_dir), summary)
        cluster.jobs[step.job_id] = "COMPLETED"
    return run


def sdk_update(monkeypatch):
    """What publishing a new sc-hub does to every step key."""
    monkeypatch.setattr("schub.service.env_id", lambda: "a-newer-environment")


def svg_keys(html: str) -> set[str]:
    return set(re.findall(r'<g class="node[^"]*" data-key="([^"]+)"', html))


def variant(page: str, view: str, history: bool) -> str:
    """The graph of one pipeline view, with or without older versions."""
    start = page.index(f'data-pipe-view="{view}"')
    graph = page[start:page.index('data-pipe-view="', start + 10)] if page.count('data-pipe-view="', start + 10) else page[start:]
    part = graph.split(f'data-history="{int(history)}"', 1)
    return part[1].split("data-history=", 1)[0] if len(part) == 2 else ""


def test_a_finished_branch_is_outdated_after_an_update_not_queued(hub, finished_main, monkeypatch):
    before = collect(hub)
    assert before.branches["ifn/main"].state == "COMPLETED"
    sdk_update(monkeypatch)
    snap = collect(hub)
    assert snap.branches["ifn/main"].state == "OUTDATED"
    old = [s.step_key for s in finished_main.steps]
    nodes = {n.key: n for n in snap.nodes}
    assert all(not nodes[k].current for k in old)
    new = [k for k in snap.branches["ifn/main"].keys]
    assert all(nodes[k].current and nodes[k].state == "PLANNED" for k in new)
    assert [nodes[k].earlier for k in new] == [{"ifn/main": k} for k in old]  # each step's earlier result


def test_a_fork_that_never_ran_is_not_outdated_by_its_parent(hub, finished_main, monkeypatch):
    sdk_update(monkeypatch)
    hub.save_branch("ifn", "twin", MAIN)  # same steps as main, so the same new keys
    snap = collect(hub)
    assert snap.branches["ifn/main"].state == "OUTDATED" and snap.branches["ifn/twin"].state == "PLANNED"
    page = render_pipelines(snap, lambda *_: None)
    assert "↻" not in variant(page, "v-ifn-twin", history=False)
    assert "↻" in variant(page, "v-ifn-main", history=False)
    assert "Not run in ifn/main as the branch is now" in page  # the shared step says which branch


def test_graphs_hide_older_versions_until_asked(hub, finished_main, monkeypatch):
    sdk_update(monkeypatch)
    snap = collect(hub)
    page = render_pipelines(snap, lambda *_: None)
    old = {s.step_key for s in finished_main.steps}
    new = set(snap.branches["ifn/main"].keys)
    for view in ("v-all", "v-ifn-main"):
        assert svg_keys(variant(page, view, history=False)) & (old | new) == new
        assert svg_keys(variant(page, view, history=True)) >= old | new
    assert "↻ 88 cells kept" in page and "QC · needs re-run; before: 88 cells kept" in page
    assert "sc-hub or a step before it changed" in page  # why the step has to run again
    button = re.search(r'<button class="pipe" data-pipe="v-ifn-main".*?</button>', page).group(0)
    assert 'dot OUTDATED' in button and '<span class="muted small">2</span>' in button
    assert 'data-select-node="' + finished_main.steps[0].step_key + '"' in page  # panel links the old result


def test_the_reason_for_a_re_run_names_defaults_the_student_never_set(hub, finished_main, monkeypatch):
    from schub.dashboard.views_pipelines import _why

    snap = collect(hub)
    qc = next(n for n in snap.nodes if n.brick == "qc_filter")
    new_default = qc.model_copy(update={"params": {**qc.params, "max_pct_mt": None}})
    assert _why(new_default, qc.model_copy(update={"params": {**qc.params, "max_pct_mt": 20.0}}), snap) == \
        "sc-hub changed the defaults of this step"
    revised = qc.model_copy(update={"params": {**qc.params, "min_genes": 5}})
    assert _why(revised, qc, snap) == "its parameters changed"


def test_all_pipelines_shows_current_state_without_history(hub, finished_main):
    snap = collect(hub)
    page = render_pipelines(snap, lambda *_: None)
    assert variant(page, "v-all", history=True) == ""  # nothing is older, so no second graph
    button = re.search(r'<button class="pipe" data-pipe="v-ifn-main".*?</button>', page).group(0)
    assert "dot COMPLETED" in button
    everything = re.search(r'<button class="pipe" data-pipe="v-all".*?</button>', page).group(0)
    assert "dot COMPLETED" in everything  # the state of all branches, not of a mix of steps


def test_plan_warnings_and_step_warnings_reach_the_step_panel(hub, finished_main):
    hub.save_branch("ifn", "labels", BranchSpec(dataset="kang2018", steps=(
        {"brick": "qc_filter"}, {"brick": "normalize_embed"}, {"brick": "annotate_celltypist"},
        {"brick": "pseudobulk_de", "params": {**DESIGN, "group_key": "cell_type"}})))
    snap = collect(hub)
    de = next(n for n in snap.nodes if n.brick == "pseudobulk_de")
    assert [i.code for i in de.issues] == ["upstream_unused"]
    page = render_pipelines(snap, lambda *_: None)
    template = page[page.index(f'<template data-node="{de.key}">'):]
    assert "celltypist_majority_voting" in template.split("</template>")[0]
    assert re.search(rf'class="node [^"]*flagged[^"]*" data-key="{de.key}"', page)
    qc = page[page.index(f'<template data-node="{finished_main.steps[0].step_key}">'):].split("</template>")[0]
    assert "no MT- genes: filter had no effect" in qc


def test_a_branch_that_cannot_be_planned_is_blocked(hub):
    # Saved when the rules were looser; a stricter check now refuses its plan.
    hub.projects.save_branch("ifn", "broken", BranchSpec(dataset="kang2018", steps=(
        {"brick": "qc_filter"}, {"brick": "pseudobulk_de", "params": {**DESIGN, "group_key": "nope"}})), "")
    snap = collect(hub)
    assert snap.branches["ifn/broken"].state == "BLOCKED"
    de = next(n for n in snap.nodes if n.brick == "pseudobulk_de")
    assert any(i.level == "error" and i.code == "missing_obs" for i in de.issues)
    projects = render_projects(snap)
    assert "can&#x27;t plan" in projects  # the branch's state, not its last run's


def test_active_projects_are_not_shown_as_running(hub):
    projects = render_projects(collect(hub))
    assert "dot RUNNING" not in projects and "pill RUNNING" not in projects and "dot ACTIVE" in projects


# ---- the graph itself ---------------------------------------------------------


def node(key, parent, brick, **extra):
    return NodeView(key=key, parent=parent, dataset="d", brick=brick, state="COMPLETED", **extra)


def test_branch_points_compare_only_steps_of_the_same_brick():
    nodes = (
        node("m", "ds:a", "merge_datasets", params={"others": ["b"], "join": "inner"}, inputs=("ds:b",),
             headline="2 datasets, 27,373 cells"),
        node("q1", "ds:a", "qc_filter", params={"batch_key": "replicate", "min_genes": 200}, headline="24,549 cells kept"),
        node("q2", "ds:a", "qc_filter", params={"batch_key": "replicate", "min_genes": 20}, headline="24,600 cells kept"),
        node("q3", "ds:a", "qc_filter", params={"batch_key": "replicate", "min_genes": 20}, headline="24,600 cells kept"),
        node("b1", "ds:b", "qc_filter", params={"min_genes": 200}, headline="2,659 cells kept"),
    )
    svg = render_graph(nodes, tuple(n.key for n in nodes))
    assert "batch_key=None" not in svg and "min_genes=200" in svg and "min_genes=20" in svg
    assert "+ b" in svg  # a merge names what it adds instead of an edge across the graph
    assert 'class="edge merge"' not in svg


def test_subtitles_are_cut_with_an_ellipsis_and_kept_whole_on_hover():
    long = node("p", "ds:d", "pseudobulk_de", headline="7 groups, 195–3,406 DE genes each")
    svg = render_graph((long,), ("p",))
    assert "…</text>" in svg and "<title>Pseudobulk DE · 7 groups, 195–3,406 DE genes each</title>" in svg


def test_the_selected_path_includes_its_dataset_and_merged_inputs():
    nodes = (
        node("m", "ds:a", "merge_datasets", params={"others": ["b"]}, inputs=("ds:b",)),
        node("q", "m", "qc_filter"),
        node("b1", "ds:b", "qc_filter"),
    )
    svg = render_graph(nodes, ("m", "q", "b1"))
    assert 'data-key="q" data-path="q m ds:a ds:b"' in svg
