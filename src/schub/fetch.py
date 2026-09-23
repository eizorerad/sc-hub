"""Download starter datasets and models.

Assets already present in the shared library (or the local one) are skipped.
Anything missing is downloaded into the student's local library, so a student
without access to the shared library can still work. Run it inside a Slurm job:
the login node's Lustre client has shown write errors on large downloads.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .datasets import write_catalog_entry
from .library import CELLTYPIST_FILES, CELLTYPIST_MODELS, CATALOG_FILE, DATA_FILE, find_asset

PBMC3K_URL = "https://cf.10xgenomics.com/samples/cell-exp/1.1.0/pbmc3k/pbmc3k_filtered_gene_bc_matrices.tar.gz"
PBMC3K_SHA256 = "847d6ebd9a1ec9a768f2be7e40ca42cbfe75ebeb6d76a4c24167041699dc28b5"
KANG_URL = "https://exampledata.scverse.org/pertpy/kang_2018.h5ad"
KANG_SHA256 = "e6a5adac64dcdeb36eaba27db49b63e0c64bb0ed4a64c6705971506b41c39830"
TIMEOUT_S = 120


class FetchError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, dest: Path, sha256: str) -> Path:
    """Download to a temporary name, verify the checksum, then move into place."""
    # Some CDNs (10x) reject urllib's default User-Agent with 403.
    request = urllib.request.Request(url, headers={"User-Agent": "sc-hub/0.1 (+https://mbzuai.ac.ae)"})
    partial = dest.with_name(dest.name + ".partial")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
    except (urllib.error.URLError, TimeoutError) as exc:
        partial.unlink(missing_ok=True)
        raise FetchError(f"download failed for {url}: {exc}") from exc
    actual = sha256_file(partial)
    if actual != sha256:
        partial.unlink(missing_ok=True)
        raise FetchError(f"checksum mismatch for {url}: expected {sha256}, got {actual}")
    os.replace(partial, dest)
    return dest


def _publish_dataset(adata: Any, target: Path, meta: dict[str, Any]) -> Path:
    """Write the catalog, then move data.h5ad into place last.

    The data file is what marks an asset as present, so a job that dies half-way
    never leaves a dataset without its catalog. Partial names are unique per
    process, so two concurrent fetches cannot interleave writes.
    """
    target.mkdir(parents=True, exist_ok=True)
    fd, partial_name = tempfile.mkstemp(prefix=".data.", suffix=".h5ad.partial", dir=target)
    os.close(fd)
    partial = Path(partial_name)
    try:
        adata.write_h5ad(partial)
        write_catalog_entry(target, {**meta, "file_sha256": sha256_file(partial), "file_size": partial.stat().st_size})
        final = target / DATA_FILE
        os.replace(partial, final)
    finally:
        partial.unlink(missing_ok=True)
    os.utime(target / CATALOG_FILE)  # catalog must not look older than the data
    return final


def fetch_pbmc3k(into: Path, scratch: Path) -> Path:
    import scanpy as sc

    with tempfile.TemporaryDirectory(dir=scratch) as tmp:
        archive = _download(PBMC3K_URL, Path(tmp) / "pbmc3k.tar.gz", PBMC3K_SHA256)
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")
        adata = sc.read_10x_mtx(Path(tmp) / "filtered_gene_bc_matrices" / "hg19", var_names="gene_symbols")
    adata.var_names_make_unique()
    adata.obs["sample"] = "pbmc3k"
    return _publish_dataset(
        adata,
        into / "datasets" / "pbmc3k",
        {
            "title": "10x PBMC 3k (healthy donor), raw counts",
            "organism": "human",
            "license": "CC BY 4.0 (10x Genomics)",
            "citation": "10x Genomics, 3k PBMCs from a Healthy Donor (2016)",
            "description": "~2.7k PBMCs, one sample. Good for QC -> clustering -> annotation.",
            "source_url": PBMC3K_URL,
            "source_sha256": PBMC3K_SHA256,
        },
    )


def fetch_kang2018(into: Path, scratch: Path) -> Path:
    import anndata as ad

    with tempfile.TemporaryDirectory(dir=scratch) as tmp:
        raw = _download(KANG_URL, Path(tmp) / "kang_2018.h5ad", KANG_SHA256)
        # Published in the pre-0.7 AnnData layout; re-save in the current one.
        adata = ad.read_h5ad(raw)
    return _publish_dataset(
        adata,
        into / "datasets" / "kang2018",
        {
            "title": "Kang et al. 2018: PBMCs, control vs IFN-beta stimulated, 8 donors",
            "organism": "human",
            "license": "Public GEO data (GSE96583); check terms before redistribution",
            "citation": "Kang et al., Nat Biotechnol 36, 89-94 (2018), doi:10.1038/nbt.4042",
            "description": "Paired ctrl/stim design with donors as replicates. Good for pseudobulk DE.",
            "source_url": KANG_URL,
            "source_sha256": KANG_SHA256,
            "notes": "Re-saved from the legacy AnnData layout. Columns: label (ctrl/stim), "
            "replicate (donor), cell_type (authors' annotation).",
        },
    )


def fetch_celltypist(into: Path, scratch: Path) -> Path:
    os.environ["CELLTYPIST_FOLDER"] = str(into / "models" / "celltypist")
    from celltypist import models

    models.download_models(model=list(CELLTYPIST_FILES))
    return into.joinpath(*CELLTYPIST_MODELS)


def _kallisto(organism: str) -> Callable[[Path, Path], Path]:
    def fetch_index(into: Path, scratch: Path) -> Path:
        from .fetch_reads import fetch_kallisto_index

        return fetch_kallisto_index(organism, into, scratch)

    return fetch_index


def fetch_pbmc1k_v3_fastq(into: Path, scratch: Path) -> Path:
    from .fetch_reads import fetch_pbmc1k_v3_fastq as fetch_reads

    return fetch_reads(into, scratch)


FETCHERS: dict[str, Callable[[Path, Path], Path]] = {
    "pbmc3k": fetch_pbmc3k,
    "kang2018": fetch_kang2018,
    "celltypist": fetch_celltypist,
    "kallisto-human": _kallisto("human"),
    "kallisto-mouse": _kallisto("mouse"),
    "pbmc1k_v3_fastq": fetch_pbmc1k_v3_fastq,
}
# Downloaded only on request (fetch_asset), not by bootstrap: several GB.
ON_DEMAND = ("kallisto-mouse", "pbmc1k_v3_fastq")


def _validate(names: list[str]) -> None:
    unknown = [n for n in names if n not in FETCHERS]
    if unknown:
        raise ValueError(f"unknown assets {unknown}; available: {', '.join(FETCHERS)}")


def missing(settings: Settings, names: list[str]) -> list[str]:
    """Assets not found in any library (shared or local)."""
    _validate(names)
    return [n for n in names if find_asset(settings, n) is None]


def fetch(
    settings: Settings, names: list[str], force: bool = False, into: Path | None = None
) -> dict[str, str]:
    """Download `names` into `into` (default: the local library) unless present."""
    _validate(names)
    target = into or settings.local_library
    todo = list(names) if force else missing(settings, names)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    report = {}
    for name in names:
        if name not in todo:
            found = find_asset(settings, name)
            report[name] = f"present in {found.source}: {found.path}" if found else "present"
            continue
        report[name] = f"downloaded: {FETCHERS[name](target, settings.cache_dir)}"
    return report
