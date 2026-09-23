"""Dataset catalog across the shared library, the local fallback and private data."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml

from .config import Settings
from .hashing import file_fingerprint, stable_hash
from .fastq import FastqError, fastq_fingerprint, load_manifest
from .library import CATALOG_FILE, DATA_FILE, FASTQ_FILE, Source, dataset_dirs
from .state import Frozen

MAX_USER_FILES = 200
MAX_TEXT = 300  # catalog text reaches the agent; keep it bounded


class DatasetEntry(Frozen):
    name: str
    path: str
    source: Source
    kind: Literal["h5ad", "fastq"] = "h5ad"
    title: str = ""
    organism: str = "unknown"
    license: str = "unknown"
    citation: str = ""
    description: str = ""
    size_mb: float = 0.0


def dataset_label(path: str) -> str:
    """The dataset's name: its folder for data.h5ad / fastq.yaml, else the file name."""
    p = Path(path)
    return p.parent.name if p.name in (DATA_FILE, FASTQ_FILE) else p.stem


def _size_mb(path: Path) -> float:
    return round(path.stat().st_size / 1e6, 1) if path.exists() else 0.0


def read_catalog(directory: Path) -> dict[str, Any]:
    meta_file = directory / CATALOG_FILE
    try:
        loaded = yaml.safe_load(meta_file.read_text()) if meta_file.is_file() else {}
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _text(meta: dict[str, Any], key: str, default: str = "") -> str:
    return str(meta.get(key, default))[:MAX_TEXT]


def _catalogued(source: Source, base: Path) -> list[DatasetEntry]:
    entries = []
    for meta_file in sorted(base.glob(f"*/{CATALOG_FILE}")):
        meta = read_catalog(meta_file.parent)
        data = meta_file.parent / DATA_FILE
        if not data.is_file():
            continue
        entries.append(
            DatasetEntry(
                name=meta_file.parent.name,
                path=str(data),
                source=source,
                title=_text(meta, "title"),
                organism=_text(meta, "organism", "unknown"),
                license=_text(meta, "license", "unknown"),
                citation=_text(meta, "citation"),
                description=_text(meta, "description"),
                size_mb=_size_mb(data),
            )
        )
    return entries


def _fastq_entries(source: Source, base: Path) -> list[DatasetEntry]:
    entries = []
    for manifest_file in sorted(base.glob(f"*/{FASTQ_FILE}")):
        if (manifest_file.parent / DATA_FILE).is_file():
            continue  # a folder is one dataset; the count matrix wins
        try:
            m = load_manifest(manifest_file)
        except FastqError:
            continue
        size = sum(_size_mb(manifest_file.parent / f) for f in m.read_files())
        entries.append(
            DatasetEntry(
                name=manifest_file.parent.name, path=str(manifest_file), source=source, kind="fastq",
                title=m.title[:MAX_TEXT], organism=m.organism, license=m.license[:MAX_TEXT],
                citation=m.citation[:MAX_TEXT],
                description=(f"FASTQ, {len(m.samples)} sample(s), {m.technology or 'technology not set'}. "
                             + m.description)[:MAX_TEXT],
                size_mb=round(size, 1),
            )
        )
    return entries


def _loose_private(settings: Settings, known: set[str]) -> list[DatasetEntry]:
    base = settings.data_dir
    if not base.is_dir():
        return []
    files = sorted(f for f in base.rglob("*.h5ad") if "twins" not in f.relative_to(base).parts)[:MAX_USER_FILES]
    return [
        DatasetEntry(name=str(f.relative_to(base)), path=str(f), source="private", size_mb=_size_mb(f))
        for f in files
        if str(f) not in known
    ]


def list_datasets(settings: Settings) -> list[DatasetEntry]:
    """First occurrence of a name wins (shared library over local over private)."""
    seen: dict[str, DatasetEntry] = {}
    for source, base in dataset_dirs(settings):
        found = (_catalogued(source, base) + _fastq_entries(source, base)) if base.is_dir() else []
        for entry in found:
            seen.setdefault(entry.name, entry)
    entries = list(seen.values())
    return entries + _loose_private(settings, {e.path for e in entries})


def write_catalog_entry(directory: Path, meta: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / CATALOG_FILE).write_text(yaml.safe_dump(meta, sort_keys=False, allow_unicode=True))


def dataset_fingerprint(path: Path, trusted_roots: tuple[Path, ...] = ()) -> str:
    """Content identity for catalogued library datasets, else path/size/mtime.

    With a checksum, the shared-library copy and a student's fallback download of
    the same dataset produce the same step keys. The checksum is trusted only for
    files under a library root and only if the catalog is not older than the
    data (fetch writes the catalog last); an in-place edit falls back to mtime.
    """
    resolved = path.resolve()
    trusted = any(resolved.is_relative_to(root.resolve()) for root in trusted_roots)
    if path.name == FASTQ_FILE:
        return fastq_fingerprint(path, trusted)
    catalog = path.parent / CATALOG_FILE
    if trusted and path.name == DATA_FILE and catalog.is_file():
        meta = read_catalog(path.parent)
        sha, size = meta.get("file_sha256"), meta.get("file_size")
        stat = path.stat()
        if sha and size == stat.st_size and catalog.stat().st_mtime >= stat.st_mtime:
            return stable_hash("content", sha, size)
    return file_fingerprint(path)
