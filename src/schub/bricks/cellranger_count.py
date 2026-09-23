"""FASTQ -> raw count matrix with 10x Cell Ranger.

The course's choice when results must match what most labs publish ("the
defaults are good"). Cell Ranger is licensed by 10x Genomics: the library owner
installs it once with scripts/install_cellranger.sh after accepting the license.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from ..hashing import stable_hash
from ..library import find_tool
from ..state import DatasetState, Issue, ObsColumn, error, warning
from .base import BrickParams, BrickSpec, PlanContext, Resources
from .kb_count import EXPECTED_GENES, MITO_GENES, ORGANISMS

# refs/cellranger/<organism> -> the 10x reference folder (refdata-gex-...)
CELLRANGER_REFS = ("refs", "cellranger")


class CellrangerParams(BrickParams):
    chemistry: str = Field("auto", pattern=r"^[A-Za-z0-9-]{2,24}$", description="Cell Ranger --chemistry (auto detects it).")
    expect_cells: int | None = Field(None, ge=100, le=200_000, description="Only if auto cell calling fails.")
    include_introns: bool = Field(True, description="Count intronic reads (Cell Ranger 7+ default; needed for nuclei).")


def cellranger_binary(ctx_roots: tuple[Path, ...]) -> Path | None:
    return find_tool(ctx_roots, "cellranger", "cellranger")


def cellranger_ref(roots: tuple[Path, ...], organism: str) -> Path | None:
    for root in roots:
        folder = root.joinpath(*CELLRANGER_REFS, organism)
        if (folder / "reference.json").is_file():
            return folder.resolve()
    return None


def check(state: DatasetState, p: CellrangerParams, ctx: PlanContext) -> list[Issue]:
    issues: list[Issue] = []
    if cellranger_binary(ctx.library_roots) is None:
        issues.append(
            error(
                "cellranger_missing",
                "Cell Ranger is not installed in the library (10x license). Use kb_count, or ask the "
                "library owner to run scripts/install_cellranger.sh with the download link from 10x.",
            )
        )
    if state.species not in ORGANISMS:
        issues.append(error("no_reference", "Set organism (human or mouse) in fastq.yaml."))
    elif cellranger_ref(ctx.library_roots, state.species) is None:
        issues.append(error("reference_missing", f"No Cell Ranger {state.species} reference in the library."))
    if (state.fastq_gb or 0) > 150:
        issues.append(warning("long_run", "Over 150 GB of reads: Cell Ranger may hit the 24 h job limit; consider kb_count."))
    return issues


def transform(state: DatasetState, p: CellrangerParams) -> DatasetState:
    sample = ObsColumn(name="sample", kind="categorical", n_unique=len(state.samples) or None)
    return DatasetState(
        n_obs=state.n_obs, n_vars=EXPECTED_GENES, x_kind="raw_counts", gene_ids="symbol",
        species=state.species, mito_genes=MITO_GENES, obs=(sample,), flags=("counted",),
    )


def resources(state: DatasetState, p: CellrangerParams) -> Resources:
    # 10x: 16 cores / 64 GB; roughly an hour per 10 GB of reads.
    return Resources(cpus=16, mem_gb=64, time_min=int(60 + 8 * (state.fastq_gb or 5.0)))


def key_extra(state: DatasetState, p: CellrangerParams, ctx: PlanContext) -> str:
    """Cell Ranger version and the reference's own description (genome, annotation, version)."""
    binary = cellranger_binary(ctx.library_roots)
    ref = cellranger_ref(ctx.library_roots, state.species)
    described = (ref / "reference.json").read_text() if ref else "none"
    return f"{binary.parent.name if binary else 'none'}|{stable_hash(described)}|{state.species}"


SPEC = BrickSpec(
    name="cellranger_count",
    version="0.1.0",
    summary="FASTQ -> raw counts with 10x Cell Ranger (filtered matrix, one 'sample' per FASTQ sample). "
    "Standard 10x pipeline; slower and heavier than kb_count. First step for FASTQ datasets.",
    params_model=CellrangerParams,
    check=check,
    transform=transform,
    resources=resources,
    impl="schub.bricks.impl.cellranger_count:run",
    source=True,
    key_extra=key_extra,
)
