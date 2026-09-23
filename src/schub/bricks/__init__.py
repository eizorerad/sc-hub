"""Registry of available bricks. Add a brick: write its spec + impl, list it here."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from . import (
    annotate_celltypist,
    cellranger_count,
    export_cellxgene,
    integrate_scanvi,
    integrate_scvi,
    kb_count,
    memento_de,
    normalize_embed,
    pseudobulk_de,
    qc_filter,
)
from .base import BrickError, BrickParams, BrickSpec, PlanContext, Resources, StepIO

REGISTRY: Mapping[str, BrickSpec] = MappingProxyType(
    {
        spec.name: spec
        for spec in (
            kb_count.SPEC,
            cellranger_count.SPEC,
            qc_filter.SPEC,
            normalize_embed.SPEC,
            integrate_scvi.SPEC,
            integrate_scanvi.SPEC,
            annotate_celltypist.SPEC,
            pseudobulk_de.SPEC,
            memento_de.SPEC,
            export_cellxgene.SPEC,
        )
    }
)


class UnknownBrick(KeyError):
    pass


def get_brick(name: str, registry: Mapping[str, BrickSpec] = REGISTRY) -> BrickSpec:
    try:
        return registry[name]
    except KeyError as exc:
        raise UnknownBrick(f"unknown brick '{name}'; available: {', '.join(registry)}") from exc


__all__ = [
    "REGISTRY",
    "BrickError",
    "BrickParams",
    "BrickSpec",
    "PlanContext",
    "Resources",
    "StepIO",
    "UnknownBrick",
    "get_brick",
]
