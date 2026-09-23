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
CELLTYPIST_MODELS = ("models", "celltypist", "data", "models")
CELLTYPIST_FILES = ("Immune_All_Low.pkl", "Immune_All_High.pkl")

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
    for source, base in dataset_dirs(settings):
        candidate = base / name / DATA_FILE
        if is_file(candidate):
            return AssetLocation(name=name, path=str(candidate), source=source)
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
    return find_celltypist_models(settings) if name == "celltypist" else find_dataset(settings, name)


def library_mode(settings: Settings) -> str:
    if settings.shared_library is not None:
        return f"shared library at {settings.shared_library}"
    if settings.library is not None:
        return f"fallback: shared library {settings.library} is not readable; using local copies"
    return "local only (no shared library configured)"
