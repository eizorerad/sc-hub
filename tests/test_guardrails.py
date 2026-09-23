"""Planner guardrails found by looking at real runs (Kang 2018, PBMC 1k, Kang + PBMC 3k):
a mitochondrial filter that filters nothing, mitochondrial genes lost in a merge,
CellTypist voting over coarse clusters, scANVI judged on its own training labels,
and DE that silently ignores the labels a branch computes."""

from __future__ import annotations

import pandas as pd
import pytest

from schub.h5ad_profile import profile_h5ad
from schub.headlines import headline
from schub.planner import StepRequest, build_plan
from schub.service import Hub
from schub.slurm import Slurm

from .conftest import FakeCluster, library_datasets, make_adata

DESIGN = {"condition_key": "label", "reference": "ctrl", "treatment": "stim", "replicate_key": "donor"}
NO_MT = [g for g in make_adata().var_names if not g.startswith("MT-")]


def with_cell_types(adata):
    types = pd.Categorical(["T", "B", "Mono"][i % 3] for i in range(adata.n_obs))
    adata.obs["cell_type"] = types
    return adata


@pytest.fixture
def typed_profile(write_h5ad):
    return profile_h5ad(write_h5ad(with_cell_types(make_adata(n_obs=90))))


def plan(profile, ctx, *steps):
    return build_plan(profile, "fp", [StepRequest.model_validate(s) for s in steps], ctx)


def issues(result, level):
    return {i.code: i for i in result.issues if i.level == level}


# ---- mitochondrial filter -------------------------------------------------


def test_mito_filter_that_matches_nothing_is_refused(write_h5ad, ctx):
    profile = profile_h5ad(write_h5ad(make_adata(genes=NO_MT)))
    refused = plan(profile, ctx, {"brick": "qc_filter", "params": {"max_pct_mt": 20}})
    assert "no_mito_genes" in issues(refused, "error") and not refused.ok
    assert "unset (or 100)" in issues(refused, "error")["no_mito_genes"].message
    assert plan(profile, ctx, {"brick": "qc_filter", "params": {"max_pct_mt": 100}}).ok
    # The default says what it does instead: % mito not used, in plain sight.
    default = plan(profile, ctx, {"brick": "qc_filter"})
    assert default.ok and "not used" in issues(default, "warning")["no_mito_genes"].message


def test_default_mito_threshold_follows_the_data():
    from schub.bricks.qc_filter import QcParams, mito_threshold

    assert mito_threshold(QcParams(), measurable=True) == 20
    assert mito_threshold(QcParams(), measurable=False) is None
    assert mito_threshold(QcParams(max_pct_mt=5), measurable=False) == 5


def test_every_recipe_plans_on_data_without_mito_genes(write_h5ad, ctx):
    from schub.recipes import fill_recipe, list_recipes

    profile = profile_h5ad(write_h5ad(with_cell_types(make_adata(n_obs=90, genes=NO_MT))))
    values = {"batch_key": "donor", "celltypist_model": "Immune_All_Low.pkl", "labels_key": "cell_type",
              "group_key": "cell_type", "other_dataset": "x", **DESIGN, "condition_key": "label"}
    for recipe in list_recipes():
        if recipe.input.startswith(("FASTQ", "two or more")):
            continue
        result = build_plan(profile, "fp", [StepRequest.model_validate(s) for s in fill_recipe(recipe.name, values)], ctx)
        assert result.ok, (recipe.name, result.issues)


def _hub(settings, write_h5ad, datasets):
    for name, genes in datasets.items():
        write_h5ad(make_adata(n_obs=40, genes=genes), directory=library_datasets(settings) / name)
    return Hub(settings, Slurm(FakeCluster()))


@pytest.mark.parametrize("primary, other", [("with_mt", "no_mt"), ("no_mt", "with_mt")])
def test_merge_keeps_each_datasets_own_mito_share(settings, write_h5ad, primary, other):
    hub = _hub(settings, write_h5ad, {"with_mt": None, "no_mt": NO_MT})
    merged = hub.build(primary, [
        {"brick": "merge_datasets", "params": {"others": [other]}},
        {"brick": "qc_filter", "params": {"batch_key": "dataset", "min_genes": 5}},
    ])
    # The inner join drops MT- genes, but each dataset's own % mito travels with its cells.
    assert merged.ok, merged.issues
    state = merged.steps[1].state_in
    assert state.mito_genes == 0 and state.has_obs("premerge_pct_counts_mt")
    lost = issues(merged, "warning")["mito_partial"]
    assert lost.step == 1 and "no_mt" in lost.message and "with_mt" in lost.message


def test_merge_of_datasets_without_mito_genes_still_refuses_the_filter(settings, write_h5ad):
    hub = _hub(settings, write_h5ad, {"a": NO_MT, "b": NO_MT})
    merged = hub.build("a", [
        {"brick": "merge_datasets", "params": {"others": ["b"]}},
        {"brick": "qc_filter", "params": {"batch_key": "dataset", "min_genes": 5, "max_pct_mt": 20}},
    ])
    assert "no_mito_genes" in issues(merged, "error")
    assert not merged.steps[1].state_in.has_obs("premerge_pct_counts_mt")


def test_outer_merge_counts_mito_genes_of_any_dataset(settings, write_h5ad):
    hub = _hub(settings, write_h5ad, {"with_mt": None, "no_mt": NO_MT})
    merged = hub.build("no_mt", [{"brick": "merge_datasets", "params": {"others": ["with_mt"], "join": "outer"}}])
    assert merged.final_state.mito_genes == 2 and "mito_partial" in issues(merged, "warning")


# ---- CellTypist -------------------------------------------------------------

NORM = {"brick": "normalize_embed"}
QC = {"brick": "qc_filter"}


def test_celltypist_votes_over_its_own_over_clustering(typed_profile, ctx):
    result = plan(typed_profile, ctx, QC, NORM, {"brick": "annotate_celltypist"})
    assert result.ok and result.steps[2].params["over_clustering"] is None
    # The neighbour graph exists after normalize_embed, so CellTypist's own clustering is cheap.
    assert "slow_voting" not in issues(result, "warning")


def test_celltypist_without_a_graph_warns_that_voting_is_slow(write_h5ad, ctx):
    profile = profile_h5ad(write_h5ad(make_adata(x="lognorm")))
    assert "slow_voting" in issues(plan(profile, ctx, {"brick": "annotate_celltypist"}), "warning")


def test_celltypist_can_be_compared_with_known_labels(typed_profile, ctx):
    ok = plan(typed_profile, ctx, QC, NORM, {"brick": "annotate_celltypist", "params": {"reference_key": "cell_type"}})
    assert ok.ok, ok.issues
    missing = plan(typed_profile, ctx, QC, NORM, {"brick": "annotate_celltypist", "params": {"reference_key": "nope"}})
    assert "missing_obs" in issues(missing, "error")


# ---- scANVI -------------------------------------------------------------------

SCANVI = {"brick": "integrate_scanvi", "params": {"batch_key": "donor", "labels_key": "cell_type"}}


def test_scanvi_holds_out_labels_by_default(typed_profile, ctx):
    default = plan(typed_profile, ctx, QC, NORM, SCANVI)
    assert default.ok and default.steps[2].params["holdout_fraction"] == pytest.approx(0.1)
    assert "no_holdout" not in issues(default, "warning")


def test_scanvi_on_fully_labeled_data_without_holdout_warns(typed_profile, ctx):
    none = {**SCANVI, "params": {**SCANVI["params"], "holdout_fraction": 0}}
    warned = issues(plan(typed_profile, ctx, QC, NORM, none), "warning")
    assert "no_holdout" in warned and "training" in warned["no_holdout"].message
    too_many = {**SCANVI, "params": {**SCANVI["params"], "holdout_fraction": 0.9}}
    assert "bad_params" in issues(plan(typed_profile, ctx, QC, NORM, too_many), "error")


# ---- DE that ignores the branch's own labels -----------------------------------


def _de(group_key):
    return {"brick": "pseudobulk_de", "params": {**DESIGN, "group_key": group_key}}


def test_de_grouped_by_dataset_labels_after_annotation_is_flagged(typed_profile, ctx):
    scvi = {"brick": "integrate_scvi", "params": {"batch_key": "donor", "condition_key": "label"}}
    result = plan(typed_profile, ctx, QC, NORM, scvi, {"brick": "annotate_celltypist"}, _de("cell_type"))
    found = issues(result, "warning")["upstream_unused"]
    assert found.step == 5 and result.ok
    assert "steps 2-4" in found.message and "integrate_scvi" in found.message
    assert "celltypist_majority_voting" in found.message  # the label column to use instead


def test_de_on_the_computed_labels_or_straight_after_qc_is_not_flagged(typed_profile, ctx):
    uses_labels = plan(typed_profile, ctx, QC, NORM, {"brick": "annotate_celltypist"}, _de("celltypist_majority_voting"))
    assert "upstream_unused" not in issues(uses_labels, "warning")
    recipe = plan(typed_profile, ctx, QC, NORM, _de("cell_type"))  # course recipe: normalize is preparation
    assert "upstream_unused" not in issues(recipe, "warning")


def test_memento_is_checked_the_same_way(typed_profile, ctx):
    memento = {"brick": "memento_de", "params": {**DESIGN, "group_key": "cell_type"}}
    result = plan(typed_profile, ctx, QC, NORM, {"brick": "annotate_celltypist"}, memento)
    assert "upstream_unused" in issues(result, "warning")


# ---- headlines say what the numbers are --------------------------------------


def test_de_headline_counts_genes_not_gene_group_pairs():
    groups = {"B": {"significant": 778}, "Mono": {"significant": 3406}, "Mk": {"skipped": "too few"}}
    assert headline("pseudobulk_de", {"groups_tested": 2, "groups": groups, "significant_genes": 3600}) == \
        "3,600 DE genes in 2 groups"
    assert headline("pseudobulk_de", {"groups_tested": 2, "groups": groups}) == "2 groups, 778–3,406 DE genes each"
    memento = {"groups": {"B": {"significant": 3, "variability_significant": 1}}, "significant_genes": 3,
               "variability_genes": 1}
    assert headline("memento_de", memento) == "3 mean / 1 variability genes"


def test_scanvi_headline_reports_held_out_accuracy_not_training_fit():
    assert headline("integrate_scanvi", {"n_labels": 8, "holdout_cells": 2455, "holdout_accuracy": 0.9713}) == \
        "8 labels, 97.1% held-out accuracy"
    old = {"n_labels": 8, "unlabeled_cells": 0, "label_agreement_on_labeled": 0.998}
    assert headline("integrate_scanvi", old) == "8 labels (every cell labeled, no check)"


def test_celltypist_headline_shows_agreement_with_known_labels():
    summary = {"n_labels": 9, "reference_key": "cell_type", "reference_purity": 0.916}
    assert headline("annotate_celltypist", summary) == "9 labels, 91.6% consistent with cell_type"
    assert headline("annotate_celltypist", {"n_labels": 9}) == "9 labels"


def test_no_dependency_claim_across_a_step_that_could_not_be_planned(typed_profile, ctx):
    broken = {"brick": "integrate_scvi", "params": {"batch_key": "donor", "n_latent": -1}}
    result = plan(typed_profile, ctx, QC, NORM, broken, {"brick": "annotate_celltypist"}, _de("cell_type"))
    assert "bad_params" in issues(result, "error") and "upstream_unused" not in issues(result, "warning")


def test_scanvi_labels_with_gaps_are_not_called_complete(write_h5ad, ctx):
    adata = with_cell_types(make_adata(n_obs=90))
    adata.obs["cell_type"] = adata.obs["cell_type"].astype(object).where(adata.obs_names != "cell0", None).astype("category")
    profile = profile_h5ad(write_h5ad(adata))
    none = {**SCANVI, "params": {**SCANVI["params"], "holdout_fraction": 0}}
    assert "no_holdout" not in issues(plan(profile, ctx, QC, NORM, none), "warning")
