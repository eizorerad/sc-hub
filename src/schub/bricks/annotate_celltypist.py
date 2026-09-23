"""Automated cell-type annotation with a pre-staged CellTypist model."""

from __future__ import annotations

from pydantic import Field

from ..state import DatasetState, Issue, error, warning
from .base import BrickParams, BrickSpec, PlanContext, Resources, need_obs, scaled

MOUSE_PREFIXES = ("mouse", "adult_mouse", "developing_mouse")
PREFIX = "celltypist_"


class CelltypistParams(BrickParams):
    model: str = Field(
        "Immune_All_Low.pkl",
        pattern=r"^[A-Za-z0-9_.-]+\.pkl$",
        description="Model file name in the shared CellTypist folder.",
    )
    majority_voting: bool = Field(True, description="Smooth labels over clusters.")
    over_clustering: str | None = Field("leiden", description="obs column used for voting.")


def _check_input(state: DatasetState) -> list[Issue]:
    if state.x_kind != "normalized_log":
        return [
            error(
                "needs_lognorm",
                "CellTypist expects X log1p-normalized to 10,000 counts per cell; "
                "add normalize_embed (target_sum=1e4) before this step.",
            )
        ]
    if state.norm_target is not None and abs(state.norm_target - 1e4) / 1e4 > 0.02:
        return [
            error(
                "wrong_target_sum",
                f"X is normalized to ~{state.norm_target:g} counts per cell; CellTypist needs 1e4.",
            )
        ]
    return []


def _check_model(state: DatasetState, p: CelltypistParams, ctx: PlanContext) -> list[Issue]:
    issues = []
    available = ctx.celltypist_models()
    if p.model not in available:
        issues.append(
            error(
                "model_missing",
                f"CellTypist model '{p.model}' is not staged; available: "
                f"{', '.join(available) or 'none (run: schub fetch celltypist)'}",
            )
        )
    is_mouse_model = p.model.lower().startswith(MOUSE_PREFIXES)
    if state.species == "mouse" and not is_mouse_model:
        issues.append(error("species_mismatch", f"Data looks mouse; '{p.model}' is a human model."))
    if state.species == "human" and is_mouse_model:
        issues.append(error("species_mismatch", f"Data looks human; '{p.model}' is a mouse model."))
    if state.species == "unknown":
        issues.append(warning("species_unknown", "Species not inferred; model species unchecked."))
    return issues


def check(state: DatasetState, p: CelltypistParams, ctx: PlanContext) -> list[Issue]:
    issues = _check_input(state) + _check_model(state, p, ctx)
    if state.gene_ids == "ensembl":
        issues.append(error("needs_symbols", "CellTypist models use gene symbols, not Ensembl IDs."))
    if p.majority_voting and p.over_clustering is not None:
        issues += need_obs(state, p.over_clustering, "over_clustering")
    if p.majority_voting and p.over_clustering is None:
        issues.append(warning("slow_voting", "CellTypist will compute its own over-clustering."))
    return issues


def transform(state: DatasetState, p: CelltypistParams) -> DatasetState:
    labels = [f"{PREFIX}predicted_labels"] + ([f"{PREFIX}majority_voting"] if p.majority_voting else [])
    return (
        state.with_obs(*labels)
        .with_obs(f"{PREFIX}conf_score", kind="numeric")
        .with_flags("annotated")
    )


def resources(state: DatasetState, p: CelltypistParams) -> Resources:
    return Resources(cpus=8, mem_gb=scaled(state, 16, 16), time_min=scaled(state, 10, 15))


SPEC = BrickSpec(
    name="annotate_celltypist",
    version="0.1.0",
    summary="CellTypist labels (+ majority voting over clusters) into obs['celltypist_*'].",
    params_model=CelltypistParams,
    check=check,
    transform=transform,
    resources=resources,
    impl="schub.bricks.impl.annotate_celltypist:run",
)
