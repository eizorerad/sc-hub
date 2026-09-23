from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from ...fastq import FastqManifest, load_manifest
from ...library import kallisto_ref
from ..base import BrickError, StepIO
from ..kb_count import KbCountParams
from .common import library_roots, setup_scanpy, tail, write

MEM_FOR_SORT_GB = 8


def _kb_command(manifest_dir: Path, reads: tuple[tuple[str, str], ...], ref: Path, tech: str,
                out: Path, filter_cells: bool) -> list[str]:
    threads = os.environ.get("OMP_NUM_THREADS", "8")
    command = [
        str(Path(sys.executable).with_name("kb")), "count",
        "-i", str(ref / "index.idx"), "-g", str(ref / "t2g.txt"), "-x", tech.upper(),
        "-o", str(out), "-t", threads, "-m", f"{MEM_FOR_SORT_GB}G", "--tmp", str(out / "tmp"),
        "--h5ad", "--gene-names",
    ]
    if filter_cells:
        command += ["--filter", "bustools"]
    return command + [str(manifest_dir / name) for pair in reads for name in pair]


def _count_sample(io: StepIO, manifest: FastqManifest, index: int, ref: Path, tech: str,
                  work: Path, p: KbCountParams) -> tuple[Any, Any, dict[str, Any]]:
    import anndata as ad
    import numpy as np

    sample = manifest.samples[index]
    out = work / sample.name
    log = io.results_dir / f"kb_{sample.name}.log"
    with log.open("w") as handle:
        done = subprocess.run(
            _kb_command(Path(io.input).parent, sample.reads, ref, tech, out, p.filter_cells),
            stdout=handle, stderr=subprocess.STDOUT, check=False,
        )
    if done.returncode != 0:
        raise BrickError(f"kb count failed for sample {sample.name}:\n{tail(log, 12)}")
    counts = ad.read_h5ad(out / ("counts_filtered" if p.filter_cells else "counts_unfiltered") / "adata.h5ad")
    # Only the per-barcode totals are kept for the knee plot: the unfiltered
    # matrix holds every barcode and would not fit in memory for many samples.
    raw = ad.read_h5ad(out / "counts_unfiltered" / "adata.h5ad")
    totals = np.sort(np.asarray(raw.X.sum(axis=1)).ravel())[::-1]
    del raw
    info = json.loads((out / "run_info.json").read_text())
    counts.var_names_make_unique()  # concat needs unique names per part
    counts.obs_names = [f"{sample.name}_{b}" for b in counts.obs_names]
    counts.obs["sample"] = sample.name
    umis = np.asarray(counts.X.sum(axis=1)).ravel()
    stats = {
        "reads": int(info.get("n_processed", 0)),
        "pct_pseudoaligned": round(float(info.get("p_pseudoaligned", 0.0)), 1),
        "cells": int(counts.n_obs),
        "median_umis_per_cell": int(np.median(umis)) if umis.size else 0,
    }
    return counts, totals[totals > 0], stats


def _knee_plot(knees: dict[str, Any], cells: dict[str, int], path: Path) -> None:
    import matplotlib.pyplot as plt
    import numpy as np

    fig, ax = plt.subplots(figsize=(5.5, 4))
    for name, totals in knees.items():
        ax.loglog(np.arange(1, len(totals) + 1), totals, label=f"{name} ({cells[name]:,} cells)")
        ax.axvline(cells[name], color="#c44e52", linestyle="--", linewidth=0.8)
    ax.set_xlabel("barcode rank")
    ax.set_ylabel("UMIs")
    ax.set_title("Knee plot: cells are left of the dashed line")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    fig.savefig(path.with_name(path.stem + "_thumb.png"), dpi=36)
    plt.close(fig)


def run(io: StepIO, p: KbCountParams) -> dict[str, Any]:
    setup_scanpy(io)
    import anndata as ad
    import scipy.sparse as sp

    manifest = load_manifest(Path(io.input))
    # The organism the plan was checked with (a species override may differ from the manifest).
    organism = io.state_in.species
    ref = kallisto_ref(library_roots(io), organism)
    if ref is None:
        raise BrickError(f"no kallisto index for {organism} in {library_roots(io)}")
    tech = (p.technology or manifest.technology or "").lower()
    work = io.results_dir.parent / "work"
    shutil.rmtree(work, ignore_errors=True)
    try:
        parts, knees, stats = [], {}, {}
        for index, sample in enumerate(manifest.samples):
            counts, knees[sample.name], stats[sample.name] = _count_sample(io, manifest, index, ref, tech, work, p)
            parts.append(counts)
            shutil.rmtree(work / sample.name, ignore_errors=True)
        _knee_plot(knees, {k: v["cells"] for k, v in stats.items()}, io.results_dir / "knee.png")
        adata = ad.concat(parts, join="inner", merge="same") if len(parts) > 1 else parts[0]
        adata.var_names_make_unique()
        adata.X = sp.csr_matrix(adata.X, dtype="float32")
        adata.obs["sample"] = adata.obs["sample"].astype("category")
        adata.uns["schub_counting"] = {"tool": "kallisto|bustools", "technology": tech, "index": str(ref)}
        write(adata, io.output)
    finally:
        shutil.rmtree(work, ignore_errors=True)  # BUS files are several GB per sample
    return {
        "technology": tech,
        "index": str(ref),
        "n_cells": int(adata.n_obs),
        "n_genes": int(adata.n_vars),
        "samples": stats,
        "figure": str(io.results_dir / "knee.png"),
    }
