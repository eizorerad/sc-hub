"""Identities of the code and environment that produce a step's output.

They are part of every step key, so upgrading scanpy or editing a brick
invalidates cached outputs instead of silently serving stale ones.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
from functools import lru_cache
from pathlib import Path

from . import __version__
from .bricks import BrickSpec
from .hashing import stable_hash

KEY_PACKAGES = (
    "anndata", "scanpy", "scvi-tools", "celltypist", "pydeseq2",
    "numpy", "scipy", "pandas", "scikit-learn", "torch",
)
SHARED_IMPL = "schub.bricks.impl.common"


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "absent"


@lru_cache(maxsize=1)
def env_id() -> str:
    return stable_hash(__version__, {p: _version(p) for p in KEY_PACKAGES})


def _source(module: str) -> bytes:
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):  # e.g. a module injected without __spec__
        return b""
    if spec is None or spec.origin is None or not Path(spec.origin).is_file():
        return b""
    return Path(spec.origin).read_bytes()


def code_id(brick: BrickSpec) -> str:
    """Hash of the brick's spec module, its implementation and shared helpers.

    Uses find_spec, so heavy implementation imports (scanpy, torch) are not loaded.
    """
    modules = (brick.params_model.__module__, brick.impl.partition(":")[0], SHARED_IMPL)
    return stable_hash(*(_source(m).hex() for m in modules))
