from __future__ import annotations

import pytest

from schub.projects import BranchSpec, ProjectError
from schub.revisions import describe_changes, parse_step_ref
from schub.service import Hub, HubError
from schub.slurm import Slurm

from .conftest import library_datasets, make_adata

MAIN = BranchSpec(
    dataset="pbmc3k",
    steps=(
        {"brick": "qc_filter", "params": {"min_genes": 10}},
        {"brick": "normalize_embed"},
        {"brick": "annotate_celltypist", "params": {"model": "Immune_All_Low.pkl"}},
    ),
)


@pytest.fixture
def hub(settings, cluster, ctx, write_h5ad) -> Hub:
    write_h5ad(make_adata(n_obs=80), name="data.h5ad", directory=library_datasets(settings) / "pbmc3k")
    hub = Hub(settings, Slurm(cluster))
    hub.create_project("pbmc")
    hub.save_branch("pbmc", "main", MAIN)
    return hub


def test_revise_keeps_the_branch_and_its_history(hub):
    plan = hub.revise_branch("pbmc", "main", 1, "doublets looked wrong", params={"min_genes": 50})
    assert plan.revision == 2 and plan.branch == "main"
    assert hub.projects.load_branch("pbmc", "main").steps[0].params == {"min_genes": 50}
    revisions = hub.branch_history("pbmc", "main")
    assert [r.revision for r in revisions] == [1, 2]
    assert revisions[1].reason == "doublets looked wrong"
    assert revisions[1].changes == ("step 1 qc_filter: min_genes 10 → 50",)
    hub.revise_branch("pbmc", "main", 1, "back to the default", params={"min_genes": None})
    assert hub.branch_history("pbmc", "main")[2].changes == ("step 1 qc_filter: min_genes 50 → default",)
    assert hub.projects.branches("pbmc") == ["main"]


def test_revise_can_swap_a_brick_and_needs_a_reason(hub):
    with pytest.raises(HubError, match="reason"):
        hub.revise_branch("pbmc", "main", 3, "  ", params={})
    with pytest.raises(ProjectError, match="steps 1-3"):
        hub.revise_branch("pbmc", "main", 9, "nope")
    plan = hub.revise_branch("pbmc", "main", 3, "use scANVI-free export instead", brick="export_cellxgene",
                             params={"max_cells": 500})
    assert [s.brick for s in plan.steps] == ["qc_filter", "normalize_embed", "export_cellxgene"]
    assert "step 3: annotate_celltypist → export_cellxgene" in hub.branch_history("pbmc", "main")[-1].changes


def test_revise_a_derived_branch_uses_overrides(hub):
    hub.save_branch("pbmc", "res-2", BranchSpec(from_branch="main", overrides={"normalize_embed": {"leiden_resolution": 2.0}}))
    hub.revise_branch("pbmc", "res-2", 1, "stricter QC here only", params={"min_genes": 30})
    spec = hub.projects.load_branch("pbmc", "res-2")
    assert spec.from_branch == "main" and spec.overrides["1"] == {"min_genes": 30}
    assert hub.projects.resolve("pbmc", "main").steps[0].params == {"min_genes": 10}


def test_fork_copies_the_prefix_and_leaves_the_parent(hub):
    parent_keys = [s.step_key for s in hub.plan_branch("pbmc", "main").steps]
    plan = hub.fork_branch("pbmc", "main", 2, "res-3", "more clusters", params={"leiden_resolution": 3.0})
    keys = [s.step_key for s in plan.steps]
    assert keys[0] == parent_keys[0] and keys[1] != parent_keys[1]  # step 1 reused from cache
    spec = hub.projects.load_branch("pbmc", "res-3")
    assert spec.forked_from == "main@r1#2" and spec.reason == "more clusters"
    assert [s.step_key for s in hub.plan_branch("pbmc", "main").steps] == parent_keys
    with pytest.raises(HubError, match="already exists"):
        hub.fork_branch("pbmc", "main", 2, "res-3", "again")
    other = hub.fork_branch("pbmc", "main", 3, "explore", "look at cells instead",
                            brick="export_cellxgene", params={"max_cells": 500})
    assert [s.brick for s in other.steps] == ["qc_filter", "normalize_embed", "export_cellxgene"]


def test_inspect_step_and_references(hub):
    detail = hub.inspect_step("pbmc/main#2")
    assert detail.brick == "normalize_embed" and detail.state == "NOT_RUN" and detail.revision == 1
    assert "revise_branch" in detail.how_to_change and "fork_branch" in detail.how_to_change
    with pytest.raises(HubError, match="steps 1-3"):
        hub.inspect_step("pbmc/main#7")
    assert parse_step_ref("a/b-c/main#12") == ("a/b-c", "main", 12)
    with pytest.raises(ProjectError, match="must look like"):
        parse_step_ref("main step 2")


def test_describe_changes_for_derived_branches():
    old = BranchSpec(from_branch="main", overrides={"2": {"a": 1}})
    new = BranchSpec(from_branch="main", overrides={"2": {"a": 2}}, append=({"brick": "memento_de"},))
    assert describe_changes(old, new) == ["override step 2: a 1 → 2", "appended step 1: added memento_de"]


def test_runs_record_the_revision_they_ran(hub):
    hub.revise_branch("pbmc", "main", 1, "stricter", params={"min_genes": 40})
    plan = hub.plan_branch("pbmc", "main")
    manifest = hub.submit(plan.plan_id)
    assert manifest.revision == 2 and hub.load_plan(plan.plan_id).revision == 2


def test_revision_edge_cases(hub):
    from schub.projects import BranchSpec

    hub.save_branch("pbmc", "sub", BranchSpec(from_branch="main", overrides={"qc_filter": {"min_genes": 20}}))
    hub.revise_branch("pbmc", "sub", 1, "back to default", params={"min_genes": None})
    assert hub.projects.resolve("pbmc", "sub").steps[0].params["min_genes"] == 200  # the brick default wins
    with pytest.raises(HubError, match="nothing to revise"):
        hub.revise_branch("pbmc", "sub", 1, "again", params={"min_genes": 200})
    hub.projects.save_branch("pbmc", "sub", BranchSpec(from_branch="main", species="human"))
    hub.revise_branch("pbmc", "sub", 3, "swap", brick="export_cellxgene", params={"max_cells": 500})
    assert hub.projects.load_branch("pbmc", "sub").species == "human"
    # a deleted and recreated branch starts fresh; the old history is kept aside
    path = hub.settings.projects_dir / "pbmc" / "pipelines" / "sub.yaml"
    path.unlink()
    hub.save_branch("pbmc", "sub", BranchSpec(from_branch="main"))
    assert [r.revision for r in hub.branch_history("pbmc", "sub")] == [1]
    assert any(p.name.startswith("sub.old-") for p in (path.parent / ".history").iterdir())
    # a broken current file is archived as is and replaced with the next revision
    path.write_text("steps: [not: valid")
    saved = hub.projects.save_branch("pbmc", "sub", BranchSpec(from_branch="main"))
    assert saved.revision == 2
