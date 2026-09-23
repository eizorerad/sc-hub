"""FASTQ -> raw count matrix with kallisto|bustools (kb-python).

The course default for large or uniform reprocessing, and light enough for any
node: a prebuilt index (~400 MB) and a few GB of memory. Cell Ranger
(`cellranger_count`) is the choice when results must match what most labs do.
"""

from __future__ import annotations

import importlib.metadata

from pydantic import Field

from ..fastq import TECHNOLOGIES
from ..hashing import file_fingerprint
from ..library import REF_CATALOG, kallisto_ref
from ..state import DatasetState, Issue, ObsColumn, error, warning
from .base import BrickParams, BrickSpec, PlanContext, Resources

ORGANISMS = ("human", "mouse")
EXPECTED_GENES = 40_000  # planning estimate; the index decides
MITO_GENES = 13  # MT- (human) / mt- (mouse) protein-coding and rRNA genes in the prebuilt t2g


class KbCountParams(BrickParams):
    technology: str | None = Field(
        None, description="kb technology (10xv2, 10xv3, ...). Default: 'technology' in the dataset's fastq.yaml."
    )
    filter_cells: bool = Field(
        True, description="Keep barcodes above the bustools knee (cells); false keeps every barcode."
    )


def technology(state: DatasetState, p: KbCountParams) -> str:
    return (p.technology or state.technology or "").lower()


def check(state: DatasetState, p: KbCountParams, ctx: PlanContext) -> list[Issue]:
    issues: list[Issue] = []
    tech = technology(state, p)
    if not tech:
        issues.append(error("no_technology", "Set technology (e.g. 10xv3) in fastq.yaml or in the kb_count params."))
    elif tech not in TECHNOLOGIES:
        issues.append(error("bad_technology", f"technology '{tech}'; supported: {', '.join(TECHNOLOGIES)}"))
    if state.species not in ORGANISMS:
        issues.append(error("no_index", "kb_count has prebuilt indices for human and mouse; set organism in fastq.yaml."))
    elif kallisto_ref(ctx.library_roots, state.species) is None:
        issues.append(
            error(
                "index_missing",
                f"The kallisto {state.species} index is not in the library; "
                f"fetch_asset('kallisto-{state.species}') downloads it (about 5 minutes).",
            )
        )
    if not p.filter_cells:
        issues.append(warning("unfiltered", "Every barcode is kept, including empty droplets; qc_filter must remove them."))
    return issues


def transform(state: DatasetState, p: KbCountParams) -> DatasetState:
    sample = ObsColumn(name="sample", kind="categorical", n_unique=len(state.samples) or None)
    return DatasetState(
        n_obs=state.n_obs,
        n_vars=EXPECTED_GENES,
        x_kind="raw_counts",
        gene_ids="symbol",
        species=state.species,
        mito_genes=MITO_GENES,
        obs=(sample,),
        flags=("counted",),
    )


def resources(state: DatasetState, p: KbCountParams) -> Resources:
    # kallisto with 8 threads maps ~1 GB of gzipped reads per minute; bustools sorts in 8 GB.
    minutes = 20 + 4 * (state.fastq_gb or 5.0)
    return Resources(cpus=8, mem_gb=24, time_min=int(minutes))


def key_extra(state: DatasetState, p: KbCountParams, ctx: PlanContext) -> str:
    """Index identity and kb version: a new index or kb release means new counts."""
    folder = kallisto_ref(ctx.library_roots, state.species)
    if folder is None:
        return "no-index"
    catalog = folder / REF_CATALOG
    ref = catalog.read_text() if catalog.is_file() else file_fingerprint(folder / "index.idx")
    try:
        kb = importlib.metadata.version("kb-python")
    except importlib.metadata.PackageNotFoundError:
        kb = "absent"
    return f"{ref}|kb-python {kb}|{technology(state, p)}|{state.species}"


SPEC = BrickSpec(
    name="kb_count",
    version="0.1.0",
    summary="FASTQ -> raw counts with kallisto|bustools (prebuilt human/mouse index), cells called by "
    "the bustools knee, one 'sample' per FASTQ sample. First step for FASTQ datasets.",
    params_model=KbCountParams,
    check=check,
    transform=transform,
    resources=resources,
    impl="schub.bricks.impl.kb_count:run",
    source=True,
    key_extra=key_extra,
)
