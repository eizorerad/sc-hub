"""Named pipeline templates that follow the course defaults (CB703/803, scverse +
scvi-tools): the assistant picks one, fills the placeholders from the dataset's
metadata, and saves it as a branch. Nothing here runs by itself.

Placeholders look like "<batch_key>"; `fill_recipe` replaces them and refuses
to leave any unfilled.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .state import Frozen

PLACEHOLDER = re.compile(r"^<([a-z_]+)>$")


class Recipe(Frozen):
    name: str
    title: str
    input: str
    when: str
    steps: tuple[dict[str, Any], ...]
    placeholders: dict[str, str] = {}
    notes: str = ""


_QC = {"brick": "qc_filter", "params": {}}
_NORMALIZE = {"brick": "normalize_embed", "params": {}}
_ANNOTATE = {"brick": "annotate_celltypist", "params": {"model": "<celltypist_model>"}}
_EXPORT = {"brick": "export_cellxgene", "params": {"max_cells": 20000}}
_MODEL = {"celltypist_model": "CellTypist model for the tissue, e.g. Immune_All_Low.pkl for blood/immune cells"}
_BATCH = {"batch_key": "obs column with the technical batch (sample, donor, lane)"}
_DESIGN = {
    "condition_key": "obs column with the condition (e.g. label)",
    "reference": "reference level (e.g. ctrl)",
    "treatment": "level compared with the reference (e.g. stim)",
    "replicate_key": "obs column with biological replicates (donor/sample); never clusters",
    "group_key": "obs column to test per cell type, e.g. cell_type or celltypist_majority_voting",
}

RECIPES: tuple[Recipe, ...] = (
    Recipe(
        name="standard_analysis",
        title="Standard analysis of a count matrix (Scanpy)",
        input="h5ad with raw counts",
        when="One batch, or a first look: QC, normalization, PCA/UMAP/Leiden, CellTypist labels, a cellxgene file.",
        steps=(_QC, _NORMALIZE, _ANNOTATE, _EXPORT),
        placeholders=_MODEL,
    ),
    Recipe(
        name="scvi_integration",
        title="Several batches: integrate with scVI (course default for integration)",
        input="h5ad with raw counts and a batch column",
        when="More than one sample/donor/lane whose technical differences should be removed.",
        steps=(
            {"brick": "qc_filter", "params": {"batch_key": "<batch_key>"}},
            {"brick": "normalize_embed", "params": {"batch_key": "<batch_key>"}},
            {"brick": "integrate_scvi", "params": {"batch_key": "<batch_key>", "condition_key": "<condition_key>"}},
            _ANNOTATE,
            _EXPORT,
        ),
        placeholders={**_BATCH, "condition_key": _DESIGN["condition_key"], **_MODEL},
        notes="If there is no biological condition, drop condition_key from integrate_scvi.",
    ),
    Recipe(
        name="condition_de",
        title="Condition comparison with replicates: pseudobulk DESeq2 (course Lab 8)",
        input="h5ad with raw counts, condition and replicate columns",
        when="Two conditions (e.g. ctrl vs stim) with >= 2 biological replicates per condition.",
        steps=(
            _QC,
            _NORMALIZE,
            {"brick": "pseudobulk_de", "params": {k: f"<{k}>" for k in _DESIGN}},
        ),
        placeholders=_DESIGN,
        notes="For differential variability as well, save a branch that ends with memento_de "
        "(same condition/replicate/group params, plus capture_rate for the chemistry).",
    ),
    Recipe(
        name="fastq_kallisto",
        title="FASTQ to annotated cells with kallisto|bustools (fast; course Lab 7)",
        input="FASTQ dataset (fastq.yaml)",
        when="Raw 10x reads, many samples, or quick uniform reprocessing.",
        steps=({"brick": "kb_count", "params": {}}, _QC, _NORMALIZE, _ANNOTATE, _EXPORT),
        placeholders=_MODEL,
        notes="With several samples, add batch_key='sample' to qc_filter/normalize_embed and consider integrate_scvi.",
    ),
    Recipe(
        name="fastq_cellranger",
        title="FASTQ to annotated cells with Cell Ranger (10x standard; course Lab 7)",
        input="FASTQ dataset (fastq.yaml)",
        when="Results should match what most labs publish with 10x defaults; needs Cell Ranger in the library.",
        steps=({"brick": "cellranger_count", "params": {}}, _QC, _NORMALIZE, _ANNOTATE, _EXPORT),
        placeholders=_MODEL,
    ),
    Recipe(
        name="scanvi_labels",
        title="Label-aware integration with scANVI",
        input="h5ad with raw counts, a batch column and (partial) cell labels",
        when="Trusted labels exist for some cells (authors' cell types, a reference) and should shape the latent space.",
        steps=(
            {"brick": "qc_filter", "params": {"batch_key": "<batch_key>"}},
            {"brick": "normalize_embed", "params": {"batch_key": "<batch_key>"}},
            {"brick": "integrate_scanvi", "params": {"batch_key": "<batch_key>", "labels_key": "<labels_key>"}},
            _EXPORT,
        ),
        placeholders={**_BATCH, "labels_key": "obs column with the known labels (unlabeled cells: 'Unknown')"},
    ),
)


def list_recipes() -> list[Recipe]:
    return list(RECIPES)


def get_recipe(name: str) -> Recipe:
    found = next((r for r in RECIPES if r.name == name), None)
    if found is None:
        raise KeyError(f"unknown recipe '{name}'; available: {', '.join(r.name for r in RECIPES)}")
    return found


def fill_recipe(name: str, values: Mapping[str, str]) -> list[dict[str, Any]]:
    """Steps with every placeholder replaced; parameters left as placeholders are an error."""
    recipe = get_recipe(name)
    missing: set[str] = set()

    def fill(value: Any) -> Any:
        match = PLACEHOLDER.match(value) if isinstance(value, str) else None
        if match is None:
            return value
        key = match.group(1)
        if key not in values:
            missing.add(key)
            return value
        return values[key]

    steps = [{"brick": s["brick"], "params": {k: fill(v) for k, v in s["params"].items()}} for s in recipe.steps]
    if missing:
        raise KeyError(f"recipe '{name}' needs values for: {', '.join(sorted(missing))}")
    return json.loads(json.dumps(steps))
