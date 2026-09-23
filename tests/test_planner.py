from __future__ import annotations

from dataclasses import replace

import pytest

from schub.config import Limits
from schub.h5ad_profile import profile_h5ad
from schub.planner import DatasetOverrides, StepRequest, build_plan

from .conftest import make_adata

QC = StepRequest(brick="qc_filter")
NORM = StepRequest(brick="normalize_embed")
ANNOTATE = StepRequest(brick="annotate_celltypist")
DE = StepRequest(
    brick="pseudobulk_de",
    params={"condition_key": "label", "reference": "ctrl", "treatment": "stim", "replicate_key": "donor"},
)


@pytest.fixture
def raw_profile(write_h5ad):
    return profile_h5ad(write_h5ad(make_adata()))


def codes(plan, level="error"):
    return {i.code for i in plan.issues if i.level == level}


def test_happy_path_is_ok_and_propagates_state(raw_profile, ctx):
    plan = build_plan(raw_profile, "fp", [QC, NORM, ANNOTATE], ctx)
    assert plan.ok, plan.issues
    assert [s.brick for s in plan.steps] == ["qc_filter", "normalize_embed", "annotate_celltypist"]
    assert plan.final_state.x_kind == "normalized_log"
    assert plan.final_state.has_obs("celltypist_majority_voting")
    summary = plan.summary()
    assert summary.ok and "leiden" in summary.final_obs_columns


def test_plan_id_is_deterministic_and_prefix_keys_are_reused(raw_profile, ctx):
    first = build_plan(raw_profile, "fp", [QC, NORM], ctx)
    again = build_plan(raw_profile, "fp", [QC, NORM], ctx)
    changed = build_plan(
        raw_profile, "fp", [QC, StepRequest(brick="normalize_embed", params={"n_top_genes": 500})], ctx
    )
    assert first.plan_id == again.plan_id
    assert changed.plan_id != first.plan_id
    assert changed.steps[0].step_key == first.steps[0].step_key
    assert changed.steps[1].step_key != first.steps[1].step_key


def test_celltypist_needs_lognormalized_input(raw_profile, ctx):
    plan = build_plan(raw_profile, "fp", [ANNOTATE], ctx)
    assert "needs_lognorm" in codes(plan)
    assert not plan.ok


def test_celltypist_rejects_wrong_target_sum(raw_profile, ctx):
    norm_1e6 = StepRequest(brick="normalize_embed", params={"target_sum": 1e6})
    plan = build_plan(raw_profile, "fp", [QC, norm_1e6, ANNOTATE], ctx)
    assert "wrong_target_sum" in codes(plan)


def test_celltypist_species_and_model_checks(raw_profile, ctx):
    mouse = build_plan(raw_profile, "fp", [QC, NORM, ANNOTATE], ctx, DatasetOverrides(species="mouse"))
    assert "species_mismatch" in codes(mouse)
    missing = StepRequest(brick="annotate_celltypist", params={"model": "Nope.pkl"})
    assert "model_missing" in codes(build_plan(raw_profile, "fp", [QC, NORM, missing], ctx))
    unknown = build_plan(raw_profile, "fp", [QC, NORM, ANNOTATE], ctx, DatasetOverrides(species="unknown"))
    assert "species_unknown" in codes(unknown, "warning")


def test_celltypist_rejects_ensembl_and_missing_cluster_column(raw_profile, ctx):
    ens = build_plan(raw_profile, "fp", [QC, NORM, ANNOTATE], ctx, DatasetOverrides(gene_ids="ensembl"))
    assert "needs_symbols" in codes(ens)
    voting = StepRequest(brick="annotate_celltypist", params={"over_clustering": "clusters"})
    assert "missing_obs" in codes(build_plan(raw_profile, "fp", [QC, NORM, voting], ctx))
    no_col = StepRequest(brick="annotate_celltypist", params={"over_clustering": None})
    assert "slow_voting" in codes(build_plan(raw_profile, "fp", [QC, NORM, no_col], ctx), "warning")


def test_lognorm_only_data_cannot_be_renormalized(write_h5ad, ctx):
    profile = profile_h5ad(write_h5ad(make_adata(x="lognorm")))
    plan = build_plan(profile, "fp", [NORM], ctx)
    assert "needs_raw_counts" in codes(plan)
    assert "no_qc" in codes(plan, "warning")


def test_scvi_refuses_integrating_the_condition(raw_profile, ctx):
    bad = StepRequest(brick="integrate_scvi", params={"batch_key": "label", "condition_key": "label"})
    plan = build_plan(raw_profile, "fp", [QC, NORM, bad], ctx)
    assert "batch_is_condition" in codes(plan)
    good = StepRequest(brick="integrate_scvi", params={"batch_key": "donor", "condition_key": "label"})
    ok = build_plan(raw_profile, "fp", [QC, NORM, good], ctx)
    assert ok.ok and ok.gpu_hours > 0
    assert "no_hvg" in codes(build_plan(raw_profile, "fp", [QC, good], ctx), "warning")


def test_pseudobulk_checks(raw_profile, ctx):
    assert build_plan(raw_profile, "fp", [QC, DE], ctx).ok
    wrong_level = StepRequest(brick="pseudobulk_de", params={**DE.params, "treatment": "IFN"})
    assert "missing_level" in codes(build_plan(raw_profile, "fp", [wrong_level], ctx))
    same = StepRequest(brick="pseudobulk_de", params={**DE.params, "replicate_key": "label"})
    assert "condition_is_replicate" in codes(build_plan(raw_profile, "fp", [same], ctx))
    equal = StepRequest(brick="pseudobulk_de", params={**DE.params, "treatment": "ctrl"})
    assert "same_levels" in codes(build_plan(raw_profile, "fp", [equal], ctx))
    missing = StepRequest(brick="pseudobulk_de", params={**DE.params, "replicate_key": "patient"})
    assert "missing_obs" in codes(build_plan(raw_profile, "fp", [missing], ctx))


def test_pseudobulk_needs_replicates_and_must_be_last(raw_profile, ctx):
    few = StepRequest(brick="pseudobulk_de", params={**DE.params, "min_replicates": 5})
    assert "too_few_replicates" in codes(build_plan(raw_profile, "fp", [few], ctx))
    numeric = StepRequest(brick="pseudobulk_de", params={**DE.params, "replicate_key": "score"})
    assert "numeric_replicate" in codes(build_plan(raw_profile, "fp", [numeric], ctx), "warning")
    assert "after_terminal" in codes(build_plan(raw_profile, "fp", [DE, QC], ctx))


def test_unknown_brick_bad_params_and_empty_plan(raw_profile, ctx):
    plan = build_plan(
        raw_profile, "fp", [StepRequest(brick="magic"), StepRequest(brick="qc_filter", params={"min_genes": -1, "typo": 1})], ctx
    )
    assert {"unknown_brick", "bad_params"} <= codes(plan)
    assert not build_plan(raw_profile, "fp", [], ctx).ok


def test_limits_on_steps_gpu_and_resources(raw_profile, ctx):
    tight = replace(ctx, limits=Limits(max_steps=1, max_gpu_hours_per_plan=0.1, max_mem_gb=8))
    scvi = StepRequest(brick="integrate_scvi", params={"batch_key": "donor"})
    plan = build_plan(raw_profile, "fp", [QC, scvi], tight)
    assert {"too_many_steps", "gpu_budget"} <= codes(plan)
    assert "resources_clamped" in codes(plan, "warning")
    assert all(s.resources.mem_gb <= 8 for s in plan.steps)


def test_qc_warnings(raw_profile, ctx):
    twice = build_plan(raw_profile, "fp", [QC, QC], ctx)
    assert "qc_repeated" in codes(twice, "warning")
    ignored = StepRequest(brick="qc_filter", params={"detect_doublets": False})
    assert "doublets_ignored" in codes(build_plan(raw_profile, "fp", [ignored], ctx), "warning")
    batch = StepRequest(brick="qc_filter", params={"batch_key": "nope"})
    assert "missing_obs" in codes(build_plan(raw_profile, "fp", [batch], ctx))


def test_pseudo_replication_and_design_key_clashes(raw_profile, ctx):
    clusters_as_replicates = StepRequest(brick="pseudobulk_de", params={**DE.params, "replicate_key": "leiden"})
    plan = build_plan(raw_profile, "fp", [QC, NORM, clusters_as_replicates], ctx)
    assert "derived_column" in codes(plan)
    grouped = StepRequest(brick="pseudobulk_de", params={**DE.params, "group_key": "donor"})
    assert "group_is_design" in codes(build_plan(raw_profile, "fp", [grouped], ctx))
    by_type = StepRequest(brick="pseudobulk_de", params={**DE.params, "group_key": "celltypist_majority_voting"})
    assert build_plan(raw_profile, "fp", [QC, NORM, ANNOTATE, by_type], ctx).ok


def test_mito_filter_needs_symbols_and_scvi_condition_warning(raw_profile, ctx):
    ens = DatasetOverrides(gene_ids="ensembl")
    assert "mt_needs_symbols" in codes(build_plan(raw_profile, "fp", [QC], ctx, ens))
    no_mito = StepRequest(brick="qc_filter", params={"max_pct_mt": 100})
    assert build_plan(raw_profile, "fp", [no_mito], ctx, ens).ok
    scvi = StepRequest(brick="integrate_scvi", params={"batch_key": "donor"})
    assert "no_condition_key" in codes(build_plan(raw_profile, "fp", [QC, NORM, scvi], ctx), "warning")


def test_code_and_environment_are_part_of_the_key(raw_profile, ctx):
    base = build_plan(raw_profile, "fp", [QC], ctx)
    new_env = build_plan(raw_profile, "fp", [QC], replace(ctx, env_id="scanpy-upgraded"))
    new_code = build_plan(raw_profile, "fp", [QC], replace(ctx, code_ids={"qc_filter": "edited"}))
    assert len({base.steps[0].step_key, new_env.steps[0].step_key, new_code.steps[0].step_key}) == 3
    assert new_code.steps[0].code_id == "edited"


def test_missing_mito_genes_warns(write_h5ad, ctx):
    no_mt = [g for g in make_adata().var_names if not g.startswith("MT-")]
    profile = profile_h5ad(write_h5ad(make_adata(genes=no_mt)))
    assert profile.state.mito_genes == 0 and any("MT-" in n for n in profile.notes)
    assert "no_mito_genes" in codes(build_plan(profile, "fp", [QC], ctx), "warning")
    off = StepRequest(brick="qc_filter", params={"max_pct_mt": 100})
    assert "no_mito_genes" not in codes(build_plan(profile, "fp", [off], ctx), "warning")
