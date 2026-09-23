"""Finding datasets and models across the shared library, the local fallback
library and the student's private data.

Lookup order: shared library (read-only, may be absent) -> local library
(downloads land here when the shared one lacks something) -> private data/.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from .config import Settings
from .state import Frozen

DATA_FILE = "data.h5ad"
CATALOG_FILE = "dataset.yaml"
FASTQ_FILE = "fastq.yaml"
CELLTYPIST_MODELS = ("models", "celltypist", "data", "models")
CELLTYPIST_FILES = ("Immune_All_Low.pkl", "Immune_All_High.pkl")
# Prebuilt kallisto|bustools indices: refs/kallisto/<organism>/{index.idx,t2g.txt,ref.yaml}
KALLISTO_REFS = ("refs", "kallisto")
KALLISTO_FILES = ("index.idx", "t2g.txt")
REF_CATALOG = "ref.yaml"
# Tools the owner installs once: tools/<name>/current -> <version>
TOOLS = "tools"

Source = Literal["library", "local", "private"]


class AssetLocation(Frozen):
    name: str
    path: str
    source: Source


def is_file(path: Path) -> bool:
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def _source(settings: Settings, root: Path) -> Source:
    return "library" if root == settings.shared_library else "local"


def dataset_dirs(settings: Settings) -> tuple[tuple[Source, Path], ...]:
    """(source, directory holding <name>/data.h5ad) in lookup order."""
    libraries = tuple((_source(settings, r), r / "datasets") for r in settings.library_roots)
    return (*libraries, ("private", settings.data_dir))


def find_dataset(settings: Settings, name: str) -> AssetLocation | None:
    """A count matrix (data.h5ad) or FASTQ reads (fastq.yaml), first library wins."""
    for source, base in dataset_dirs(settings):
        for filename in (DATA_FILE, FASTQ_FILE):
            candidate = base / name / filename
            if is_file(candidate):
                return AssetLocation(name=name, path=str(candidate), source=source)
    return None


def kallisto_ref(roots: tuple[Path, ...], organism: str) -> Path | None:
    """Folder with index.idx and t2g.txt for this organism, from the first library that has it."""
    for root in roots:
        folder = root.joinpath(*KALLISTO_REFS, organism)
        if all(is_file(folder / name) for name in KALLISTO_FILES):
            return folder
    return None


def find_tool(roots: tuple[Path, ...], name: str, executable: str) -> Path | None:
    """tools/<name>/<version>/<executable> via the `current` link.

    Only the link is resolved (so the version is part of the path): the
    executable itself may be a venv symlink that must stay as it is.
    """
    for root in roots:
        link = root / TOOLS / name / "current"
        try:
            candidate = link.resolve(strict=True) / executable
        except (OSError, RuntimeError):
            continue
        if is_file(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def celltypist_dirs(settings: Settings) -> tuple[Path, ...]:
    return tuple(root.joinpath(*CELLTYPIST_MODELS) for root in settings.library_roots)


def find_celltypist_models(settings: Settings) -> AssetLocation | None:
    for root in settings.library_roots:
        folder = root.joinpath(*CELLTYPIST_MODELS)
        if all(is_file(folder / name) for name in CELLTYPIST_FILES):
            return AssetLocation(name="celltypist", path=str(folder), source=_source(settings, root))
    return None


def find_asset(settings: Settings, name: str) -> AssetLocation | None:
    if name == "celltypist":
        return find_celltypist_models(settings)
    if name.startswith("kallisto-"):
        folder = kallisto_ref(settings.library_roots, name.removeprefix("kallisto-"))
        if folder is None:
            return None
        shared = settings.shared_library is not None and folder.is_relative_to(settings.shared_library)
        return AssetLocation(name=name, path=str(folder), source="library" if shared else "local")
    return find_dataset(settings, name)


def library_mode(settings: Settings) -> str:
    if settings.shared_library is not None:
        return f"shared library at {settings.shared_library}"
    if settings.library is not None:
        return f"fallback: shared library {settings.library} is not readable; using local copies"
    return "local only (no shared library configured)"
