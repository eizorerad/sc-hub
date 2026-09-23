"""Download FASTQ-side assets: prebuilt kallisto indices and a demo FASTQ dataset.

Big downloads go through a checksum-verified cache (`<cache>/downloads`), so a
file staged there by hand (or by an earlier attempt) is not fetched again.
Assets appear atomically: they are built in a hidden folder and renamed.
"""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from pathlib import Path

import yaml

from .fastq import MANIFEST_FILE, write_manifest
from .fetch import FetchError, _download, sha256_file
from .library import KALLISTO_FILES, KALLISTO_REFS, REF_CATALOG

INDEX_RELEASE = "https://github.com/pachterlab/kallisto-transcriptome-indices/releases/download/v1"
KALLISTO_INDICES = {
    "human": ("human_index_standard.tar.xz", "a4c2c007efaa7df4c5d9fb7c798d5ffdd0fc04279de325a4744fb4e8ccec68bd"),
    "mouse": ("mouse_index_standard.tar.xz", "aa05a4762c63019dbd42ddfc47b14d05f896e37818302a7d220497304b5de20f"),
}
PBMC1K_V3_URL = "https://cf.10xgenomics.com/samples/cell-exp/3.0.0/pbmc_1k_v3/pbmc_1k_v3_fastqs.tar"
PBMC1K_V3_SHA256 = "873f6511a7cb732c0a709d2fb9201ca4f1230a70f3a04e46c0198a9e4be896fd"


def cached_download(url: str, sha256: str, cache: Path) -> Path:
    """The verified file in the download cache, downloading it only when absent or corrupt."""
    folder = cache / "downloads"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{sha256[:16]}-{url.rsplit('/', 1)[-1]}"
    if target.is_file() and sha256_file(target) == sha256:
        return target
    target.unlink(missing_ok=True)
    return _download(url, target, sha256)


def _place(staged: Path, final: Path) -> Path:
    """Rename a finished folder into place; an existing asset is kept, not replaced."""
    final.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(staged, final)
    except OSError as exc:
        shutil.rmtree(staged, ignore_errors=True)
        if not final.exists():
            raise FetchError(f"could not place {final}: {exc}") from exc
    return final


def fetch_kallisto_index(organism: str, into: Path, scratch: Path) -> Path:
    name, sha256 = KALLISTO_INDICES[organism]
    url = f"{INDEX_RELEASE}/{name}"
    archive = cached_download(url, sha256, scratch)
    final = into.joinpath(*KALLISTO_REFS, organism)
    final.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{organism}.", dir=final.parent))
    try:
        with tarfile.open(archive) as tar:
            tar.extractall(staged, filter="data")
        for needed in KALLISTO_FILES:
            found = next(staged.rglob(needed), None)
            if found is None:
                raise FetchError(f"{name} has no {needed}")
            if found.parent != staged:
                found.rename(staged / needed)
        meta = {
            "organism": organism,
            "source_url": url,
            "source_sha256": sha256,
            "index_sha256": sha256_file(staged / "index.idx"),
            "notes": "Prebuilt by the kallisto team (kallisto-transcriptome-indices v1, standard workflow, "
            "Ensembl transcriptome with D-list); readable by kallisto >= 0.50.1.",
        }
        (staged / REF_CATALOG).write_text(yaml.safe_dump(meta, sort_keys=False))
        os.chmod(staged, 0o755)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    placed = _place(staged, final)
    archive.unlink(missing_ok=True)  # 100+ MB; the extracted index is what is used
    return placed


def fetch_pbmc1k_v3_fastq(into: Path, scratch: Path) -> Path:
    archive = cached_download(PBMC1K_V3_URL, PBMC1K_V3_SHA256, scratch)
    final = into / "datasets" / "pbmc1k_v3_fastq"
    final.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=".pbmc1k_v3_fastq.", dir=final.parent))
    try:
        with tarfile.open(archive) as tar:
            # R1/R2 only: the index reads (I1) are not used for counting.
            members = [m for m in tar.getmembers() if m.isfile() and ("_R1_" in m.name or "_R2_" in m.name)]
            tar.extractall(staged, members=members, filter="data")
        reads = sorted(p.relative_to(staged).as_posix() for p in staged.rglob("*.fastq.gz"))
        pairs = [(r, r.replace("_R1_", "_R2_")) for r in reads if "_R1_" in r]
        meta = {
            "title": "10x PBMC 1k v3: FASTQ reads (healthy donor)",
            "organism": "human",
            "technology": "10xv3",
            "expected_cells": 1200,
            "samples": [{"name": "pbmc_1k_v3", "reads": pairs}],
            "files": {r: {"sha256": sha256_file(staged / r), "size": (staged / r).stat().st_size} for r in reads},
            "license": "CC BY 4.0 (10x Genomics)",
            "citation": "10x Genomics, 1k PBMCs from a Healthy Donor (v3 chemistry), 2018",
            "description": "~1.2k PBMCs, 2 lanes, ~5 GB of reads. Demo for kb_count / cellranger_count.",
            "source_url": PBMC1K_V3_URL,
        }
        write_manifest(staged, meta)  # written last inside the staged folder
        os.chmod(staged, 0o755)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    placed = _place(staged, final)
    archive.unlink(missing_ok=True)  # 5.5 GB
    return placed / MANIFEST_FILE
