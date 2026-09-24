"""Identities of the code and environment that produce a step's output.

They are part of every step key, so upgrading scanpy or editing a brick
invalidates cached outputs instead of silently serving stale ones. The sc-hub
version itself is not: a release that changes the dashboard or the MCP tools
must not make students re-run finished experiments. Instead a brick's code_id
hashes every sc-hub module its job code can reach (its spec, its implementation,
shared helpers, other bricks' code it borrows, aggregate.py, ...: the import
closure) plus the job runner, and env_id the versions of the packages that
compute results.
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
from functools import lru_cache
from pathlib import Path

from .bricks import BrickSpec
from .hashing import stable_hash

KEY_PACKAGES = (
    "anndata", "scanpy", "scvi-tools", "celltypist", "pydeseq2", "memento-de", "kb-python",
    "numpy", "scipy", "pandas", "scikit-learn", "torch", "statsmodels", "h5py", "numba",
    "leidenalg", "igraph", "umap-learn", "pynndescent", "scikit-misc",
)
SHARED_IMPL = "schub.bricks.impl.common"
RUNNER = "schub.execute"  # the job's entry point: its own code counts, not what it imports
# Reached by imports but not computing results: settings and the brick registry (which
# imports every brick, so any brick's edit would otherwise change every key).
NOT_RESULTS = frozenset({"schub.config", "schub.bricks"})


def _version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "absent"


@lru_cache(maxsize=1)
def env_id() -> str:
    return stable_hash({p: _version(p) for p in KEY_PACKAGES})


def _source(module: str) -> bytes:
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):  # e.g. a module injected without __spec__
        return b""
    if spec is None or spec.origin is None or not Path(spec.origin).is_file():
        return b""
    return Path(spec.origin).read_bytes()


def _origin(module: str) -> tuple[Path | None, bool]:
    """The module's file and whether it is a package (None for a module without a file)."""
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):  # e.g. a module injected without __spec__
        return None, False
    if spec is None or spec.origin is None or not Path(spec.origin).is_file():
        return None, False
    return Path(spec.origin), spec.submodule_search_locations is not None


FileStamp = tuple[int, int, int, int]


def file_stamp(path: Path) -> FileStamp:
    """A file version for caches. Not the mtime alone: the installer's reproducible
    archive gives every file mtime 0, so a new release would look unchanged. The
    ctime (set by the extraction, not by tar), size and inode change with it."""
    st = path.stat()
    return (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)


def _stamp(module: str) -> FileStamp:
    path, _ = _origin(module)
    return file_stamp(path) if path is not None else (0, 0, 0, 0)


def _schub_imports(module: str) -> frozenset[str]:
    return _schub_imports_at(module, _stamp(module))


@lru_cache(maxsize=1024)
def _schub_imports_at(module: str, stamp: FileStamp) -> frozenset[str]:
    """sc-hub modules `module` imports anywhere in its source (lazy imports included).
    Cached per file version: planning every branch of a big project asks this often."""
    path, is_package = _origin(module)
    if path is None:
        return frozenset()
    package = module if is_package else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names if a.name.split(".")[0] == "schub"}
        elif isinstance(node, ast.ImportFrom):
            base = importlib.util.resolve_name("." * node.level + (node.module or ""), package) if node.level else (node.module or "")
            if base.split(".")[0] != "schub":
                continue
            found.add(base)
            found |= {f"{base}.{a.name}" for a in node.names if _is_submodule(base, a.name)}
    return frozenset(found)


def _is_submodule(package: str, name: str) -> bool:
    """`from package import name` names a module file (checked on disk: find_spec would
    import the package's modules, and an impl module's heavy imports with them)."""
    path, is_package = _origin(package)
    if path is None or not is_package:
        return False
    return (path.parent / f"{name}.py").is_file() or (path.parent / name / "__init__.py").is_file()


def code_modules(brick: BrickSpec) -> tuple[str, ...]:
    """The sc-hub modules whose code can change what this brick's job writes."""
    todo = [brick.params_model.__module__, brick.impl.partition(":")[0], SHARED_IMPL]
    seen: set[str] = set()
    while todo:
        module = todo.pop()
        if module in seen or module in NOT_RESULTS:
            continue
        seen.add(module)
        todo.extend(_schub_imports(module))
    return tuple(sorted(seen | {RUNNER}))


def code_id(brick: BrickSpec) -> str:
    """Hash of every module in code_modules (by name and content).

    Uses find_spec and ast, so heavy implementation imports (scanpy, torch) are not loaded.
    """
    modules = code_modules(brick)
    return _code_id_at(tuple((m, _stamp(m)) for m in modules))


@lru_cache(maxsize=256)
def _code_id_at(stamped: tuple[tuple[str, FileStamp], ...]) -> str:
    return stable_hash(*(f"{m}:{_source(m).hex()}" for m, _ in stamped))
