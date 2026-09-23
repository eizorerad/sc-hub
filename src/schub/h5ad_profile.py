"""Cheap, read-only profiling of .h5ad files with h5py.

Reads only metadata and a small sample of values, so it is safe to run on a
login node even for multi-GB files. The result drives planning-time checks.
Species and gene-id detection are heuristics; plans can override them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

import h5py
import numpy as np

from .state import DatasetState, Frozen, GeneIds, ObsColumn, Species, XKind

SAMPLE_VALUES = 20_000
SAMPLE_ROWS = 50
TOP_LEVELS = 12
MAX_STRING_SCAN = 500_000
MAX_TEXT = 80  # strings from the file reach the agent; keep them short
VAR_PREVIEW = 500
MAX_VAR_SCAN = 200_000

ENSEMBL_HUMAN = re.compile(r"^ENSG\d{11}")
ENSEMBL_MOUSE = re.compile(r"^ENSMUSG\d{11}")


class UnsupportedFile(ValueError):
    """The file is not an AnnData .h5ad layout this profiler understands."""


class DatasetProfile(Frozen):
    path: str
    size_bytes: int
    state: DatasetState
    var_preview: tuple[str, ...]
    notes: tuple[str, ...] = ()


def profile_h5ad(path: Path) -> DatasetProfile:
    try:
        with h5py.File(path, "r") as f:
            reject_external_storage(f)
            return _profile(f, path)
    except OSError as exc:
        raise UnsupportedFile(f"{path} is not a readable HDF5/.h5ad file: {exc}") from exc


def reject_external_storage(group: h5py.Group, prefix: str = "") -> None:
    """Refuse files whose links or datasets point outside the file itself.

    HDF5 external links, external raw storage and virtual datasets would let a
    crafted .h5ad make us read (and return) arbitrary files the user can read.
    """
    for name in group:
        path = f"{prefix}/{name}"
        link = group.get(name, getlink=True)
        if isinstance(link, h5py.ExternalLink):
            raise UnsupportedFile(f"external HDF5 link at {path}; refusing to follow it")
        if isinstance(link, h5py.SoftLink):
            continue
        node = group[name]
        if isinstance(node, h5py.Group):
            reject_external_storage(node, path)
        elif node.external or node.is_virtual:
            raise UnsupportedFile(f"dataset {path} stores data outside the file; refusing")


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 3] + "..."


def classify_values(values: np.ndarray) -> XKind:
    if values.size == 0:
        return "unknown"
    v = np.asarray(values, dtype=np.float64)
    if not np.all(np.isfinite(v)):
        return "unknown"
    if v.min() < 0:
        return "scaled"
    if np.allclose(v, np.round(v), atol=1e-6):
        return "raw_counts" if v.max() >= 1 else "unknown"
    if v.max() <= 30:
        return "normalized_log"
    return "unknown"


def classify_genes(names: Sequence[str]) -> tuple[GeneIds, Species]:
    names = [n for n in names if n]
    if not names:
        return "unknown", "unknown"
    total = len(names)
    human = sum(bool(ENSEMBL_HUMAN.match(n)) for n in names) / total
    mouse = sum(bool(ENSEMBL_MOUSE.match(n)) for n in names) / total
    if human > 0.8:
        return "ensembl", "human"
    if mouse > 0.8:
        return "ensembl", "mouse"
    if human + mouse > 0.8:
        return "ensembl", "unknown"
    lettered = [n for n in names if re.search("[A-Za-z]", n)]
    if not lettered:
        return "unknown", "unknown"
    upper = sum(n == n.upper() for n in lettered) / len(lettered)
    title = sum(n[0].isupper() and n[1:] == n[1:].lower() for n in lettered) / len(lettered)
    species: Species = "human" if upper > 0.8 else "mouse" if title > 0.8 else "unknown"
    return "symbol", species


def _profile(f: h5py.File, path: Path) -> DatasetProfile:
    if not {"X", "obs", "var"} <= set(f.keys()):
        raise UnsupportedFile(f"{path} lacks X/obs/var; not an AnnData file")
    if not isinstance(f["obs"], h5py.Group):
        raise UnsupportedFile("legacy AnnData (<0.7) layout; re-save it with a current anndata")
    n_obs, n_vars = _shape(f["X"])
    x_kind = classify_values(_sample_values(f["X"]))
    layers = tuple(sorted(f["layers"].keys())) if "layers" in f else ()
    counts_layer = "counts" in layers and (
        classify_values(_sample_values(f["layers"]["counts"])) == "raw_counts"
    )
    var_names = _index(f["var"], VAR_PREVIEW)
    gene_ids, species = classify_genes(var_names)
    all_names = _index(f["var"], MAX_VAR_SCAN)
    mito = sum(name.upper().startswith("MT-") for name in all_names)
    obsm = tuple(sorted(f["obsm"].keys())) if "obsm" in f else ()
    state = DatasetState(
        n_obs=n_obs,
        n_vars=n_vars,
        x_kind=x_kind,
        counts_layer=counts_layer,
        norm_target=_estimate_norm_target(f["X"]) if x_kind == "normalized_log" else None,
        gene_ids=gene_ids,
        species=species,
        mito_genes=mito,
        obs=tuple(_obs_columns(f["obs"])),
        obsm=obsm,
        layers=layers,
        flags=_flags(f, obsm),
    )
    return DatasetProfile(
        path=str(path),
        size_bytes=path.stat().st_size,
        state=state,
        var_preview=tuple(_clip(v) for v in var_names[:10]),
        notes=tuple(_notes(f, state)),
    )


def _attr(node: h5py.HLObject, key: str) -> str | None:
    value = node.attrs.get(key)
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def _shape(node: h5py.HLObject) -> tuple[int, int]:
    shape = node.attrs["shape"] if isinstance(node, h5py.Group) else node.shape
    return int(shape[0]), int(shape[1])


def _sample_values(node: h5py.HLObject) -> np.ndarray:
    if isinstance(node, h5py.Group):
        data = node["data"]
        return np.asarray(data[: min(len(data), SAMPLE_VALUES)])
    rows = np.asarray(node[: min(node.shape[0], SAMPLE_ROWS)])
    nonzero = rows[rows != 0]
    return nonzero if nonzero.size else rows.ravel()


def _estimate_norm_target(node: h5py.HLObject) -> float | None:
    """Median per-cell sum of expm1(X) over the first rows, if X is CSR or dense."""
    if isinstance(node, h5py.Group):
        if _attr(node, "encoding-type") != "csr_matrix":
            return None
        indptr = np.asarray(node["indptr"][: SAMPLE_ROWS + 1])
        data = np.asarray(node["data"][int(indptr[0]) : int(indptr[-1])], dtype=np.float64)
        offsets = indptr - indptr[0]
        sums = [np.expm1(data[a:b]).sum() for a, b in zip(offsets[:-1], offsets[1:])]
    else:
        rows = np.asarray(node[: min(node.shape[0], SAMPLE_ROWS)], dtype=np.float64)
        sums = list(np.expm1(rows).sum(axis=1))
    sums = [s for s in sums if s > 0]
    return float(f"{np.median(sums):.3g}") if sums else None


def _strings(node: h5py.HLObject, limit: int | None = None) -> list[str]:
    # anndata >= 0.12 may store string columns/indices as nullable groups (values + mask).
    ds = node["values"] if isinstance(node, h5py.Group) and "values" in node else node
    window = slice(None) if limit is None else slice(0, limit)
    try:
        return [str(x) for x in ds.asstr()[window]]
    except (TypeError, ValueError):
        return [x.decode() if isinstance(x, bytes) else str(x) for x in ds[window]]


def _index(group: h5py.Group, limit: int) -> list[str]:
    name = _attr(group, "_index") or "_index"
    return _strings(group[name], limit) if name in group else []


def _column_names(obs: h5py.Group) -> list[str]:
    order = obs.attrs.get("column-order")
    index = _attr(obs, "_index") or "_index"
    if order is not None and np.size(order) > 0:
        return [n.decode() if isinstance(n, bytes) else str(n) for n in np.atleast_1d(order)]
    return [k for k in obs.keys() if k != index and not k.startswith("__")]


def _obs_columns(obs: h5py.Group) -> list[ObsColumn]:
    return [_describe(name, obs[name]) for name in _column_names(obs) if name in obs]


def _levels(name: str, values: Sequence[str] | np.ndarray, kind: str) -> ObsColumn:
    uniques, counts = np.unique(np.asarray(values), return_counts=True)
    order = np.argsort(-counts)[:TOP_LEVELS]
    top = {_clip(str(uniques[i])): int(counts[i]) for i in order}
    return ObsColumn(name=name, kind=kind, n_unique=len(uniques), top=top)


def _describe(name: str, node: h5py.HLObject) -> ObsColumn:
    encoding = _attr(node, "encoding-type")
    if isinstance(node, h5py.Group):
        if encoding == "categorical":
            return _categorical(name, node)
        if encoding == "nullable-string-array":
            return _levels(name, _strings(node["values"], MAX_STRING_SCAN), "string")
        if encoding in ("nullable-integer", "nullable-boolean"):
            return ObsColumn(name=name, kind="numeric")
        return ObsColumn(name=name, kind="other")
    if node.dtype.kind in "iufb":
        return ObsColumn(name=name, kind="numeric")
    if node.dtype.kind in "SOU":
        return _levels(name, _strings(node, MAX_STRING_SCAN), "string")
    return ObsColumn(name=name, kind="other")


def _categorical(name: str, node: h5py.Group) -> ObsColumn:
    raw = node["categories"]
    categories = _strings(raw) if raw.dtype.kind in "SOU" else [str(x) for x in raw[:]]
    codes = np.asarray(node["codes"][:])
    counts = np.bincount(codes[codes >= 0], minlength=len(categories))
    used = int((counts > 0).sum())  # unused categories are not levels of the data
    order = [i for i in np.argsort(-counts)[:TOP_LEVELS] if counts[i] > 0]
    top = {_clip(categories[i]): int(counts[i]) for i in order}
    return ObsColumn(name=name, kind="categorical", n_unique=used, top=top)


def _flags(f: h5py.File, obsm: tuple[str, ...]) -> tuple[str, ...]:
    flags = set()
    if "obsp" in f and "connectivities" in f["obsp"]:
        flags.add("neighbors")
    if "X_pca" in obsm:
        flags.add("pca")
    if "X_umap" in obsm:
        flags.add("umap")
    if "highly_variable" in f["var"]:
        flags.add("hvg")
    return tuple(sorted(flags))


def _notes(f: h5py.File, state: DatasetState) -> list[str]:
    notes = []
    if state.x_kind == "normalized_log" and not state.counts_layer:
        notes.append(
            "X looks log-normalized and there is no layers['counts']; bricks that need raw "
            "counts (QC, HVG, scVI, pseudobulk DE) will be refused."
        )
    if state.x_kind == "scaled":
        notes.append("X contains negative values (scaled/centered); most bricks need counts.")
    if state.gene_ids == "symbol" and "gene_ids" in f["var"]:
        notes.append("var_names are symbols; Ensembl IDs are available in var['gene_ids'].")
    if state.species == "unknown":
        notes.append("Species not inferred; pass species='human' or 'mouse' when planning.")
    if state.gene_ids == "symbol" and state.mito_genes == 0:
        notes.append("No MT- genes: mitochondrial genes were probably removed upstream.")
    return notes
