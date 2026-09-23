"""FASTQ datasets: a folder of reads described by `fastq.yaml`.

    title: 10x PBMC 1k v3
    organism: human
    technology: 10xv3          # kb -x name; Cell Ranger detects chemistry itself
    expected_cells: 1000
    samples:
      - name: pbmc_1k_v3
        reads:                 # (R1, R2) pairs, paths relative to this file
          - [fastqs/pbmc_1k_v3_S1_L001_R1_001.fastq.gz, fastqs/pbmc_1k_v3_S1_L001_R2_001.fastq.gz]
    files:                     # optional checksums; library datasets have them
      fastqs/pbmc_1k_v3_S1_L001_R1_001.fastq.gz: {sha256: ..., size: ...}

Planning never opens the reads: the manifest is the whole profile.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError, field_validator

from .h5ad_profile import DatasetProfile
from .hashing import stable_hash
from .state import DatasetState, Frozen, ObsColumn, Species

MANIFEST_FILE = "fastq.yaml"
# kb technologies sc-hub supports: 10x droplet 3' kits with paired R1/R2 reads
# (10xv1 needs a third read file per lane, which manifests do not describe).
TECHNOLOGIES = ("10xv2", "10xv3", "10xv4")
DEFAULT_CELLS_PER_SAMPLE = 5000
MAX_SAMPLES = 96
# bcl2fastq / Cell Ranger names: <sample>_S<n>_L<lane>_R<1|2>_001.fastq.gz (lane optional)
TENX_NAME = re.compile(r"^(?P<sample>.+?)_S\d+(?:_(?P<lane>L\d{3}))?_(?P<read>R[12])_\d{3}\.f(?:ast)?q\.gz$")


class FastqError(ValueError):
    """The manifest or the folder does not describe usable FASTQ reads."""


class FastqSample(Frozen):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
    reads: tuple[tuple[str, str], ...] = Field(min_length=1)

    @field_validator("reads")
    @classmethod
    def _relative(cls, reads: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        for pair in reads:
            for name in pair:
                path = Path(name)
                if path.is_absolute() or ".." in path.parts or not name.endswith((".fastq.gz", ".fq.gz")):
                    raise ValueError(f"'{name}': reads must be .fastq.gz paths inside the dataset folder")
        return reads


class FileSum(Frozen):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class FastqManifest(Frozen):
    title: str = ""
    organism: Species = "unknown"
    technology: str | None = None
    expected_cells: int | None = Field(None, ge=1, le=10_000_000)
    samples: tuple[FastqSample, ...] = Field(min_length=1, max_length=MAX_SAMPLES)
    files: dict[str, FileSum] = Field(default_factory=dict)
    license: str = "unknown"
    citation: str = ""
    description: str = ""
    source_url: str = ""

    @field_validator("technology")
    @classmethod
    def _known(cls, value: str | None) -> str | None:
        if value is not None and value.lower() not in TECHNOLOGIES:
            raise ValueError(f"technology '{value}' not supported; use one of {', '.join(TECHNOLOGIES)}")
        return value.lower() if value else None

    def read_files(self) -> tuple[str, ...]:
        return tuple(name for s in self.samples for pair in s.reads for name in pair)


def is_manifest(path: Path) -> bool:
    return path.name == MANIFEST_FILE


def load_manifest(path: Path) -> FastqManifest:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise FastqError(f"cannot read {path}: {exc}") from exc
    try:
        manifest = FastqManifest.model_validate(raw if isinstance(raw, dict) else {})
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
        raise FastqError(f"{path}: {problems}") from exc
    names = [s.name for s in manifest.samples]
    if len(set(names)) != len(names):
        raise FastqError(f"{path}: sample names must be unique")
    missing = [f for f in manifest.read_files() if not (path.parent / f).is_file()]
    if missing:
        raise FastqError(f"{path}: missing read files {missing[:3]}")
    return manifest


def fastq_profile(path: Path) -> DatasetProfile:
    manifest = load_manifest(path)
    names = tuple(s.name for s in manifest.samples)
    cells = manifest.expected_cells or DEFAULT_CELLS_PER_SAMPLE * len(names)
    sample = ObsColumn(name="sample", kind="categorical", n_unique=len(names))
    state = DatasetState(
        n_obs=cells,
        n_vars=0,
        x_kind="unknown",
        species=manifest.organism,
        source="fastq",
        technology=manifest.technology,
        samples=names,
        obs=(sample,),
    )
    size = sum((path.parent / f).stat().st_size for f in manifest.read_files())
    state = state.update(fastq_gb=round(size / 1e9, 2))
    note = (
        f"FASTQ reads: {len(names)} sample(s), {len(manifest.read_files()) // 2} read pair(s), "
        f"technology {manifest.technology or 'not set'}, organism {manifest.organism}"
    )
    return DatasetProfile(path=str(path), size_bytes=size, state=state, var_preview=(), notes=(note,))


def fastq_fingerprint(path: Path, trusted: bool) -> str:
    """Content identity when every file carries a checksum that still matches its size
    (library datasets); otherwise the manifest plus each file's size and mtime."""
    manifest = load_manifest(path)
    files = manifest.read_files()
    sized = {f: (path.parent / f).stat() for f in files}
    if trusted and all(f in manifest.files and manifest.files[f].size == sized[f].st_size for f in files):
        return stable_hash("fastq-content", sorted((f, manifest.files[f].sha256) for f in files),
                           [(s.name, s.reads) for s in manifest.samples])
    return stable_hash("fastq", path.read_text(), sorted((f, st.st_size, st.st_mtime_ns) for f, st in sized.items()))


def detect_samples(folder: Path) -> tuple[FastqSample, ...]:
    """Group 10x-named FASTQ files into samples with (R1, R2) pairs per lane."""
    pairs: dict[str, dict[str, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    for path in sorted(folder.rglob("*.f*q.gz")):
        match = TENX_NAME.match(path.name)
        if match is None:
            continue
        lane_key = f"{path.parent.relative_to(folder)}|{match['lane'] or ''}"
        pairs[match["sample"]][lane_key][match["read"]] = path.relative_to(folder).as_posix()
    samples = []
    for name, lanes in sorted(pairs.items()):
        reads = tuple((lane["R1"], lane["R2"]) for _, lane in sorted(lanes.items()) if {"R1", "R2"} <= lane.keys())
        if reads:
            samples.append(FastqSample(name=re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:80], reads=reads))
    if not samples:
        raise FastqError(
            f"no paired 10x FASTQ files (<sample>_S1_L001_R1_001.fastq.gz and _R2_) found under {folder}"
        )
    return tuple(samples)


def write_manifest(folder: Path, meta: dict[str, Any]) -> Path:
    """Validate, then write fastq.yaml (the caller has checked the folder is the student's)."""
    manifest = FastqManifest.model_validate(meta)
    path = folder / MANIFEST_FILE
    data = manifest.model_dump(mode="json", exclude_defaults=True)
    data["samples"] = [{"name": s.name, "reads": [list(pair) for pair in s.reads]} for s in manifest.samples]
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    load_manifest(path)
    return path
