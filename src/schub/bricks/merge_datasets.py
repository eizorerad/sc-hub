"""Start a pipeline from several datasets: concatenate their raw counts into one
matrix, with a column recording where each cell came from.

This is the step for a project that starts from two or more datasets (two
studies, a new batch next to a published atlas). It must be the first step;
integrate afterwards (integrate_scvi with batch_key = label_key) so the datasets'
technical differences do not dominate the embedding.

An inner join keeps only shared genes, which drops every MT- gene as soon as one
dataset lacks them (Kang 2018 + PBMC 3k). Each cell's % mitochondrial counts is
therefore computed on its own dataset before the join and kept in
obs['premerge_pct_counts_mt'] (empty for datasets without MT- genes); qc_filter uses it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..state import DatasetState, Issue, ObsColumn, error, warning
from .base import BrickParams, BrickSpec, PlanContext, Resources, scaled

MAX_DATASETS = 8
PREMERGE_MT = "premerge_pct_counts_mt"


class MergeParams(BrickParams):
    others: list[str] = Field(min_length=1, max_length=MAX_DATASETS - 1,
                              description="Datasets to add to the branch's dataset (catalog names or paths).")
    label_key: str = Field("dataset", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,40}$",
                           description="New obs column naming each cell's dataset.")
    join: Literal["inner", "outer"] = Field("inner", description="inner: shared genes only; outer: all genes (0 where absent).")


def label_of(ref: str) -> str:
    """The name a dataset gets in obs['dataset'] and in the dashboard."""
    path = Path(ref)
    if "/" not in ref and not ref.endswith((".h5ad", ".yaml")):
        return ref
    return path.parent.name if path.name in ("data.h5ad", "fastq.yaml") else path.stem


def _looked_up(p: MergeParams, ctx: PlanContext) -> tuple[list[tuple[str, Any, str]], list[Issue]]:
    found, issues = [], []
    if ctx.dataset_lookup is None:
        return [], [error("no_lookup", "merge_datasets needs the sc-hub dataset catalog (plan it through sc-hub).")]
    for name in p.others:
        try:
            profile, fingerprint = ctx.dataset_lookup(name)
        except (KeyError, ValueError, OSError) as exc:
            issues.append(error("missing_dataset", f"dataset '{name}': {exc}"))
            continue
        found.append((name, profile, fingerprint))
    return found, issues


def _others(p: MergeParams, ctx: PlanContext) -> tuple[list[tuple[str, DatasetState, str]], list[Issue]]:
    found, issues = _looked_up(p, ctx)
    return [(name, profile.state, fingerprint) for name, profile, fingerprint in found], issues


def check(state: DatasetState, p: MergeParams, ctx: PlanContext) -> list[Issue]:
    others, issues = _others(p, ctx)
    if len(set(p.others)) != len(p.others):
        issues.append(error("duplicate_dataset", "each dataset may appear once in others."))
    found = _looked_up(p, ctx)[0]
    paths = [str(Path(profile.path).resolve()) for _, profile, _ in found]
    same_content = ctx.primary_fingerprint and any(fp == ctx.primary_fingerprint for _, _, fp in found)
    if (ctx.primary_path and str(Path(ctx.primary_path).resolve()) in paths) or same_content:
        issues.append(error("merge_with_itself", "others contains the branch's own dataset (or a copy of it); "
                                                   "its cells would be duplicated."))
    labels = ([label_of(ctx.primary_path)] if ctx.primary_path else []) + [label_of(name) for name in p.others]
    if len(set(labels)) != len(labels):
        issues.append(error("duplicate_label", f"two datasets would get the same '{p.label_key}' label: {labels}"))
    for name, other, _ in [("the branch dataset", state, "")] + others:
        if other.source != "h5ad":
            issues.append(error("merge_fastq", f"{name} is FASTQ: count it in its own branch first."))
        elif not other.has_raw_counts():
            issues.append(error("merge_needs_counts", f"{name} has no raw counts (X or layers['counts'])."))
    if {o.gene_ids for _, o, _ in others} - {state.gene_ids}:
        issues.append(error("gene_id_mismatch", "datasets name genes differently (symbols vs Ensembl); they cannot be merged."))
    species = {o.species for _, o, _ in others} | {state.species}
    if len(species - {"unknown"}) > 1:
        issues.append(error("species_mismatch", f"datasets come from different species: {sorted(species)}"))
    if state.has_obs(p.label_key):
        issues.append(warning("label_overwritten", f"obs column '{p.label_key}' exists and will be replaced."))
    return issues + _mito_issues(state, p, ctx, others)


def _mito_issues(state: DatasetState, p: MergeParams, ctx: PlanContext,
                 others: list[tuple[str, DatasetState, str]]) -> list[Issue]:
    named = [(label_of(ctx.primary_path) if ctx.primary_path else "the branch dataset", state)]
    named += [(label_of(name), other) for name, other, _ in others]
    without = [name for name, s in named if s.mito_genes == 0]
    measured = [name for name, s in named if s.mito_genes]
    if not without or not measured:
        return []
    return [warning(
        "mito_partial",
        f"{', '.join(without)} has no MT- genes, so its cells cannot be filtered by % mito; "
        f"{', '.join(measured)} keeps its own % mito, computed before the merge.",
    )]


def _merged_mito(states: list[DatasetState], join: str) -> int | None:
    counts = [s.mito_genes for s in states]
    if any(c is None for c in counts):
        return None
    return min(counts) if join == "inner" else max(counts)


def transform(state: DatasetState, p: MergeParams, ctx: PlanContext | None = None) -> DatasetState:
    others = _others(p, ctx)[0] if ctx else []
    states = [state] + [o for _, o, _ in others]
    shared = set.intersection(*({c.name for c in s.obs} for s in states))
    obs = tuple(ObsColumn(name=c.name, kind=c.kind) for c in state.obs if c.name in shared and c.name != p.label_key)
    label = ObsColumn(name=p.label_key, kind="categorical", n_unique=len(states))
    if any(s.mito_genes for s in states):
        label_and_mito = (label, ObsColumn(name=PREMERGE_MT, kind="numeric", derived=True))
    else:
        label_and_mito = (label,)
    genes = [s.n_vars for s in states]
    return DatasetState(
        n_obs=sum(s.n_obs for s in states),
        n_vars=min(genes) if p.join == "inner" else max(genes),
        x_kind="raw_counts",
        gene_ids=state.gene_ids,
        species=state.species,
        mito_genes=_merged_mito(states, p.join),
        obs=tuple(c for c in obs if c.name != PREMERGE_MT) + label_and_mito,
        flags=("merged",),
    )


def transform_ctx(state: DatasetState, p: MergeParams, ctx: PlanContext) -> DatasetState:
    return transform(state, p, ctx)


def input_pins(state: DatasetState, p: MergeParams, ctx: PlanContext) -> dict[str, str]:
    """Where each added dataset was found and what it contained at planning time."""
    return {name: f"{profile.path}\t{fingerprint}" for name, profile, fingerprint in _looked_up(p, ctx)[0]}


def resources(state: DatasetState, p: MergeParams) -> Resources:
    return Resources(cpus=4, mem_gb=scaled(state, 16, 24), time_min=scaled(state, 15, 10))


def key_extra(state: DatasetState, p: MergeParams, ctx: PlanContext) -> str:
    """The added datasets' content identities: new data under the same name means new results."""
    return "|".join(fingerprint for _, _, fingerprint in _others(p, ctx)[0])


SPEC = BrickSpec(
    name="merge_datasets",
    version="0.1.0",
    summary="First step for several datasets: concatenate raw counts (shared or all genes) and add "
    "obs['dataset']; integrate afterwards with batch_key='dataset'.",
    params_model=MergeParams,
    check=check,
    transform=transform,
    resources=resources,
    impl="schub.bricks.impl.merge_datasets:run",
    first_only=True,
    transform_ctx=transform_ctx,
    key_extra=key_extra,
    input_pins=input_pins,
)
