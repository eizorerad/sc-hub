from __future__ import annotations

import os

import pytest

from schub.datasets import write_catalog_entry
from schub.service import Hub, HubError
from schub.slurm import Slurm

from .conftest import library_datasets, make_adata

QC = {"brick": "qc_filter"}


@pytest.fixture
def hub(settings, cluster, ctx):
    return Hub(settings, Slurm(cluster))


@pytest.fixture
def shared_pbmc(settings, write_h5ad):
    directory = library_datasets(settings) / "pbmc3k"
    write_catalog_entry(directory, {"title": "PBMC", "organism": "human", "license": "CC BY 4.0"})
    return write_h5ad(make_adata(), directory=directory)


def test_resolves_catalog_names_and_relative_paths(hub, settings, shared_pbmc, write_h5ad):
    assert hub.resolve_dataset("pbmc3k") == shared_pbmc.resolve()
    mine = write_h5ad(make_adata(), name="lab/sample.h5ad", directory=settings.data_dir)
    assert hub.resolve_dataset("data/lab/sample.h5ad") == mine.resolve()
    names = {d.name for d in hub.datasets()}
    assert names == {"pbmc3k", "lab/sample.h5ad"}


def test_rejects_paths_outside_roots_wrong_suffix_and_missing(hub, tmp_path, write_h5ad, settings):
    outside = write_h5ad(make_adata(), name="elsewhere.h5ad", directory=tmp_path / "outside")
    with pytest.raises(HubError, match="outside"):
        hub.resolve_dataset(str(outside))
    link = settings.data_dir / "link.h5ad"
    os.symlink(outside, link)
    with pytest.raises(HubError, match="outside"):
        hub.resolve_dataset(str(link))
    text = settings.data_dir / "notes.txt"
    text.write_text("x")
    with pytest.raises(HubError, match="not an .h5ad"):
        hub.resolve_dataset(str(text))
    with pytest.raises(HubError, match="fetch_asset"):
        hub.resolve_dataset("does-not-exist")
    with pytest.raises(HubError, match="not found"):
        hub.resolve_dataset("data/missing.h5ad")


def test_plan_submit_status_roundtrip(hub, shared_pbmc, cluster):
    plan = hub.plan("pbmc3k", [QC, {"brick": "normalize_embed"}])
    assert plan.ok
    manifest = hub.submit(plan.plan_id)
    assert len(cluster.jobs) == 2
    assert hub.status(manifest.run_id).state == "PENDING"
    assert hub.runs()[0].run_id == manifest.run_id
    assert "no log yet" in hub.logs(manifest.run_id, 1)
    assert hub.results(manifest.run_id).final_output is None
    notebook = hub.notebook(manifest.run_id)
    assert notebook.path.endswith(".py") and "schub-notebook" in notebook.how_to_open
    assert hub.cancel(manifest.run_id).state == "FAILED"


def test_submit_refuses_when_dataset_changed(hub, shared_pbmc):
    plan = hub.plan("pbmc3k", [QC])
    make_adata(n_obs=30).write_h5ad(shared_pbmc)
    with pytest.raises(HubError, match="changed after planning"):
        hub.submit(plan.plan_id)


def test_ids_are_validated(hub):
    with pytest.raises(HubError, match="invalid plan_id"):
        hub.load_plan("../../etc")
    with pytest.raises(HubError, match="not found"):
        hub.load_plan("0123456789ab")
    with pytest.raises(HubError, match="invalid run_id"):
        hub.status("../x")


def test_bricks_cluster_and_inspect(hub, shared_pbmc):
    names = [b["name"] for b in hub.bricks()]
    assert names == ["qc_filter", "normalize_embed", "integrate_scvi", "annotate_celltypist", "pseudobulk_de"]
    assert "params_schema" in hub.brick("pseudobulk_de")
    with pytest.raises(HubError):
        hub.brick("nope")
    status = hub.cluster()
    assert status.partitions[0].name == "ws-ia" and status.my_pipeline_jobs == ()
    assert hub.inspect("pbmc3k").state.n_obs == 60


def test_project_branch_flow_links_runs_and_logs_once(hub, shared_pbmc, cluster, settings):
    from pathlib import Path

    from schub.projects import BranchSpec
    from schub.stepfile import SUCCESS

    hub.create_project("pbmc", question="Which clusters?")
    plan = hub.save_branch("pbmc", "main", BranchSpec(dataset="pbmc3k", steps=({"brick": "qc_filter"},)))
    assert plan.ok and (plan.project, plan.branch) == ("pbmc", "main")
    run = hub.submit(plan.plan_id)
    assert (settings.projects_dir / "pbmc" / "runs" / run.run_id).is_symlink()
    assert hub.projects.summary("pbmc").runs == (run.run_id,)
    step_dir = Path(run.steps[0].step_dir)
    (step_dir / "results").mkdir(parents=True)
    (step_dir / "results" / "summary.json").write_text('{"cells_final": 42}')
    (step_dir / SUCCESS).write_text("done")
    hub.results(run.run_id)
    hub.results(run.run_id)
    entries = hub.projects.logbook_tail("pbmc", 10)
    assert len(entries) == 1 and "42 cells kept" in entries[0] and "branch main" in entries[0]
    assert hub.plan_branch("pbmc", "main").plan_id == plan.plan_id


def test_fetch_asset_queues_a_job_only_when_missing(hub, shared_pbmc, cluster, settings):
    job = hub.fetch_asset("kang2018")
    assert job.job_id in cluster.jobs and "schub.cli fetch kang2018" in cluster.scripts[job.job_id].replace("'", "")
    with pytest.raises(HubError, match="already available"):
        hub.fetch_asset("pbmc3k")
    with pytest.raises(HubError, match="unknown asset"):
        hub.fetch_asset("imagenet")


def test_save_branch_validates_before_writing(hub, shared_pbmc, settings):
    from schub.projects import BranchSpec

    hub.create_project("pbmc")
    good = BranchSpec(dataset="pbmc3k", steps=({"brick": "qc_filter"},))
    hub.save_branch("pbmc", "main", good)
    bad = BranchSpec(dataset="pbmc3k", steps=({"brick": "annotate_celltypist"},))
    with pytest.raises(HubError, match="already exists"):
        hub.save_branch("pbmc", "main", bad)
    with pytest.raises(HubError, match="not saved"):
        hub.save_branch("pbmc", "main", bad, overwrite=True)
    assert hub.projects.load_branch("pbmc", "main") == good
    with pytest.raises(HubError, match="not saved"):
        hub.save_branch("pbmc", "broken", bad)
    assert not hub.projects.branch_exists("pbmc", "broken")


def test_ids_are_fully_matched_and_hidden_names_rejected(hub, shared_pbmc, cluster):
    with pytest.raises(HubError):
        hub.load_plan("0123456789ab\n")
    with pytest.raises(HubError):
        hub.resolve_dataset("..")
    hub.fetch_asset("kang2018")
    with pytest.raises(HubError, match="already queued"):
        hub.fetch_asset("kang2018")
