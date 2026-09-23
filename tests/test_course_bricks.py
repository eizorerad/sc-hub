from __future__ import annotations

import pytest

from schub.bricks import REGISTRY
from schub.h5ad_profile import profile_h5ad
from schub.headlines import headline
from schub.planner import StepRequest, build_plan
from schub.recipes import fill_recipe, get_recipe, list_recipes

from .conftest import make_adata

DESIGN = {"condition_key": "label", "reference": "ctrl", "treatment": "stim"}


@pytest.fixture
def profile(write_h5ad):
    return profile_h5ad(write_h5ad(make_adata(n_obs=80)))


def plan(profile, ctx, *steps):
    return build_plan(profile, "fp", [StepRequest.model_validate(s) for s in steps], ctx)


def errors(result) -> set[str]:
    return {i.code for i in result.issues if i.level == "error"}


def test_scanvi_needs_labels_distinct_from_the_design(profile, ctx):
    base = {"brick": "integrate_scanvi", "params": {"batch_key": "donor", "condition_key": "label"}}
    missing = plan(profile, ctx, {**base, "params": {**base["params"], "labels_key": "cell_type"}})
    assert "missing_obs" in errors(missing)
    clash = plan(profile, ctx, {**base, "params": {**base["params"], "labels_key": "donor"}})
    assert "labels_is_design" in errors(clash)
    numeric = plan(profile, ctx, {**base, "params": {**base["params"], "labels_key": "score"}})
    assert "numeric_labels" in errors(numeric)
    good = plan(profile, ctx, {**base, "params": {**base["params"], "labels_key": "sample_name"}})
    assert good.ok and good.steps[0].resources.gpus == 1
    assert {"X_scANVI", "X_umap"} <= set(good.final_state.obsm) and good.final_state.has_obs("scanvi_label")


def test_scanvi_on_normalized_data_names_itself(write_h5ad, ctx):
    lognorm = profile_h5ad(write_h5ad(make_adata(x="lognorm"), name="ln.h5ad"))
    result = plan(lognorm, ctx, {"brick": "integrate_scanvi", "params": {"batch_key": "donor", "labels_key": "sample_name"}})
    messages = [i.message for i in result.issues if i.code == "needs_raw_counts"]
    assert len(messages) == 1 and "integrate_scanvi" in messages[0]


def test_memento_shares_the_design_rules(profile, ctx):
    ok = plan(profile, ctx, {"brick": "memento_de", "params": {**DESIGN, "replicate_key": "donor"}})
    assert ok.ok and ok.steps[0].terminal
    no_reps = plan(profile, ctx, {"brick": "memento_de", "params": DESIGN})
    assert no_reps.ok and "no_replicates" in {i.code for i in no_reps.issues}
    wrong = plan(profile, ctx, {"brick": "memento_de", "params": {**DESIGN, "treatment": "IFN"}})
    assert "missing_level" in errors(wrong)
    derived = plan(profile, ctx, {"brick": "normalize_embed"},
                   {"brick": "memento_de", "params": {**DESIGN, "replicate_key": "leiden"}})
    assert "derived_column" in errors(derived)
    assert all("pseudobulk" not in i.message for i in derived.issues)


def test_memento_version_is_part_of_the_key(profile, ctx, monkeypatch):
    import importlib.metadata

    request = {"brick": "memento_de", "params": {**DESIGN, "replicate_key": "donor"}}
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.1.3")
    first = plan(profile, ctx, request).steps[0].step_key
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.2.0")
    assert plan(profile, ctx, request).steps[0].step_key != first


def test_export_cellxgene_needs_an_embedding(profile, ctx):
    assert "no_embedding" in errors(plan(profile, ctx, {"brick": "export_cellxgene"}))
    exported = plan(profile, ctx, {"brick": "normalize_embed"}, {"brick": "export_cellxgene", "params": {"max_cells": 500}})
    assert exported.ok and exported.steps[-1].terminal


def test_every_recipe_uses_real_bricks_and_valid_params(profile, ctx):
    values = {
        "celltypist_model": "Immune_All_Low.pkl", "batch_key": "donor", "labels_key": "sample_name",
        "replicate_key": "donor", "group_key": "sample_name", "other_dataset": "pbmc3k", **DESIGN,
    }
    for recipe in list_recipes():
        steps = fill_recipe(recipe.name, values)
        for step in steps:
            REGISTRY[step["brick"]].params_model.model_validate(step["params"])
        assert set(recipe.placeholders) >= {
            v[1:-1] for s in recipe.steps for v in s["params"].values() if isinstance(v, str) and v.startswith("<")
        }
    standard = plan(profile, ctx, *fill_recipe("standard_analysis", values))
    assert standard.ok, standard.issues
    de = plan(profile, ctx, *fill_recipe("condition_de", values))
    assert de.ok, de.issues


def test_recipe_errors():
    with pytest.raises(KeyError, match="unknown recipe"):
        get_recipe("nope")
    with pytest.raises(KeyError, match="batch_key"):
        fill_recipe("scvi_integration", {"celltypist_model": "m.pkl", "condition_key": "label"})


def test_headlines_for_new_bricks():
    assert headline("kb_count", {"n_cells": 1187, "samples": {"a": {}}}) == "1,187 cells from 1 sample(s)"
    assert headline("memento_de", {"groups": {"B": {"significant": 3, "variability_significant": 1}}}) == "3 mean / 1 variability genes"
    assert headline("integrate_scanvi", {"n_labels": 8, "label_agreement_on_labeled": 0.93}) == "8 labels"
    assert headline("export_cellxgene", {"n_cells": 5000, "size_mb": 12.5}) == "5,000 cells, 12.5 MB"


def test_merge_datasets_plans_a_combined_state(settings, ctx, write_h5ad):
    from schub.service import Hub
    from schub.slurm import Slurm

    from .conftest import FakeCluster, library_datasets

    for name, n in (("study_a", 40), ("study_b", 60)):
        write_h5ad(make_adata(n_obs=n), name="data.h5ad", directory=library_datasets(settings) / name)
    hub = Hub(settings, Slurm(FakeCluster()))
    plan = hub.plan("study_a", [
        {"brick": "merge_datasets", "params": {"others": ["study_b"]}},
        {"brick": "qc_filter", "params": {"batch_key": "dataset", "min_genes": 5}},
        {"brick": "normalize_embed", "params": {"batch_key": "dataset"}},
        {"brick": "integrate_scvi", "params": {"batch_key": "dataset", "condition_key": "label"}},
    ])
    assert plan.ok, plan.issues
    merged = plan.steps[1].state_in
    assert merged.n_obs == 100 and merged.has_obs("dataset") and merged.has_obs("donor") and merged.x_kind == "raw_counts"
    key = plan.steps[0].step_key
    make_adata(n_obs=61, seed=3).write_h5ad(library_datasets(settings) / "study_b" / "data.h5ad")
    assert hub.build("study_a", [{"brick": "merge_datasets", "params": {"others": ["study_b"]}}]).steps[0].step_key != key
    missing = hub.build("study_a", [{"brick": "merge_datasets", "params": {"others": ["nope"]}}])
    assert "missing_dataset" in {i.code for i in missing.issues}
    late = hub.build("study_a", [{"brick": "qc_filter"}, {"brick": "merge_datasets", "params": {"others": ["study_b"]}}])
    assert "must_be_first" in {i.code for i in late.issues}
    lognorm = write_h5ad(make_adata(x="lognorm"), name="data.h5ad", directory=library_datasets(settings) / "normed")
    assert lognorm.exists()
    bad = hub.build("study_a", [{"brick": "merge_datasets", "params": {"others": ["normed"]}}])
    assert "merge_needs_counts" in {i.code for i in bad.issues}
    empty = hub.build("study_a", [{"brick": "merge_datasets", "params": {"others": []}}])
    assert "bad_params" in {i.code for i in empty.issues}


def test_merged_datasets_are_pinned_and_checked_again(settings, write_h5ad):
    from schub.service import Hub, HubError
    from schub.slurm import Slurm

    from .conftest import FakeCluster, library_datasets

    for name, n in (("study_a", 40), ("study_b", 60)):
        write_h5ad(make_adata(n_obs=n), name="data.h5ad", directory=library_datasets(settings) / name)
    hub = Hub(settings, Slurm(FakeCluster()))
    small = hub.build("study_a", [{"brick": "qc_filter"}]).steps[0].resources.mem_gb
    plan = hub.plan("study_a", [{"brick": "merge_datasets", "params": {"others": ["study_b"]}}])
    pin = plan.steps[0].pins["study_b"]
    assert pin.startswith(str(library_datasets(settings) / "study_b" / "data.h5ad") + "\t")
    assert plan.steps[0].resources.mem_gb >= small  # sized on the merged cells
    make_adata(n_obs=61, seed=5).write_h5ad(library_datasets(settings) / "study_b" / "data.h5ad")
    with pytest.raises(HubError, match="'study_b' .* changed after planning"):
        hub.submit(plan.plan_id)
    itself = hub.build("study_a", [{"brick": "merge_datasets", "params": {"others": ["study_a"]}}])
    assert "merge_with_itself" in {i.code for i in itself.issues}
    # a copy of the same data elsewhere, under the same name: caught by content and by label
    copy = settings.data_dir / "study_a.h5ad"
    copy.write_bytes((library_datasets(settings) / "study_a" / "data.h5ad").read_bytes())
    twin = hub.build("study_a", [{"brick": "merge_datasets", "params": {"others": [str(copy)]}}])
    assert {"duplicate_label"} <= {i.code for i in twin.issues}
