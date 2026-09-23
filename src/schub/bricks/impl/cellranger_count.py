from __future__ import annotations

import csv
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ...fastq import TENX_NAME, FastqSample, load_manifest
from ..base import BrickError, StepIO
from ..cellranger_count import CellrangerParams, cellranger_binary, cellranger_ref
from .common import library_roots, setup_scanpy, tail, write

METRICS = (
    "Estimated Number of Cells", "Mean Reads per Cell", "Median Genes per Cell",
    "Valid Barcodes", "Sequencing Saturation", "Reads Mapped Confidently to Transcriptome",
)


def stage_fastqs(manifest_dir: Path, sample: FastqSample, into: Path) -> tuple[str, list[Path]]:
    """Link the sample's reads under their 10x names, one folder per source folder (two
    flowcell folders may hold the same file name); return the --sample prefix and folders."""
    prefixes: set[str] = set()
    folders: dict[str, Path] = {}
    for pair in sample.reads:
        for name in pair:
            match = TENX_NAME.match(Path(name).name)
            if match is None:
                raise BrickError(f"Cell Ranger needs 10x FASTQ names (<sample>_S1_L001_R1_001.fastq.gz); got {name}")
            prefixes.add(match["sample"])
            folder = folders.setdefault(Path(name).parent.as_posix(), into / str(len(folders)))
            folder.mkdir(parents=True, exist_ok=True)
            link = folder / Path(name).name
            if not link.exists():
                link.symlink_to((manifest_dir / name).resolve())
    if len(prefixes) != 1:
        raise BrickError(f"sample {sample.name} mixes FASTQ prefixes {sorted(prefixes)}")
    return prefixes.pop(), list(folders.values())


def cellranger_command(binary: Path, ref: Path, sample: FastqSample, prefix: str, fastqs: list[Path],
                       p: CellrangerParams, cpus: str, mem_gb: int) -> list[str]:
    command = [
        str(binary), "count", f"--id={sample.name}", f"--transcriptome={ref}",
        f"--fastqs={','.join(str(f) for f in fastqs)}",
        f"--sample={prefix}", f"--localcores={cpus}", f"--localmem={mem_gb}", "--create-bam=false",
        f"--include-introns={'true' if p.include_introns else 'false'}", "--disable-ui",
    ]
    if p.chemistry != "auto":
        command.append(f"--chemistry={p.chemistry}")
    if p.expect_cells:
        command.append(f"--expect-cells={p.expect_cells}")
    return command


def _metrics(path: Path) -> dict[str, str]:
    try:
        with path.open(newline="") as handle:
            row = next(csv.DictReader(handle), {})
    except (OSError, csv.Error):
        return {}
    return {k: row[k] for k in METRICS if k in row}


def run(io: StepIO, p: CellrangerParams) -> dict[str, Any]:
    sc = setup_scanpy(io)
    import anndata as ad
    import scipy.sparse as sp

    manifest = load_manifest(Path(io.input))
    roots = library_roots(io)
    binary, ref = cellranger_binary(roots), cellranger_ref(roots, io.state_in.species)
    if binary is None or ref is None:
        raise BrickError("Cell Ranger or its reference is no longer in the library; plan again")
    work = io.results_dir.parent / "work"
    shutil.rmtree(work, ignore_errors=True)
    cpus = os.environ.get("OMP_NUM_THREADS", "16")
    mem_gb = max(8, int(os.environ.get("SLURM_MEM_PER_NODE", "65536")) // 1024 - 4)
    parts, metrics = [], {}
    try:
        for sample in manifest.samples:
            prefix, folders = stage_fastqs(Path(io.input).parent, sample, work / "fastq" / sample.name)
            log = io.results_dir / f"cellranger_{sample.name}.log"
            with log.open("w") as handle:
                done = subprocess.run(
                    cellranger_command(binary, ref, sample, prefix, folders, p, cpus, mem_gb),
                    cwd=work, stdout=handle, stderr=subprocess.STDOUT, check=False,
                )
            if done.returncode != 0:
                raise BrickError(f"cellranger count failed for {sample.name}:\n{tail(log, 15)}")
            outs = work / sample.name / "outs"
            counts = sc.read_10x_h5(outs / "filtered_feature_bc_matrix.h5")
            counts.var_names_make_unique()
            counts.obs_names = [f"{sample.name}_{b}" for b in counts.obs_names]
            counts.obs["sample"] = sample.name
            parts.append(counts)
            metrics[sample.name] = _metrics(outs / "metrics_summary.csv")
            summary_html = outs / "web_summary.html"
            if summary_html.is_file():
                shutil.copyfile(summary_html, io.results_dir / f"web_summary_{sample.name}.html")
        adata = ad.concat(parts, join="inner", merge="same") if len(parts) > 1 else parts[0]
        adata.var_names_make_unique()
        adata.X = sp.csr_matrix(adata.X, dtype="float32")
        adata.obs["sample"] = adata.obs["sample"].astype("category")
        adata.uns["schub_counting"] = {"tool": f"cellranger ({binary.parent.name})", "reference": ref.name}
        write(adata, io.output)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {
        "cellranger": binary.parent.name,
        "reference": ref.name,
        "n_cells": int(adata.n_obs),
        "n_genes": int(adata.n_vars),
        "samples": metrics,
    }
