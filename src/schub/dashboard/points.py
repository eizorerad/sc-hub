"""Cell maps for the dashboard: a subsample of UMAP coordinates and cell labels
per step, written once as pts/<step key>.js (a script, so it loads from file://).

Read with h5py straight from the step's output.h5ad (metadata and two small
arrays), so the login node never loads a full AnnData. Step outputs never change
once written, so an existing file is reused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import h5py
import numpy as np

from ..h5ad_profile import UnsupportedFile, reject_external_storage

MAX_POINTS = 4000
MAX_COLUMNS = 6
MAX_LEVELS = 40
SCALE = 1000  # coordinates are sent as integers 0..SCALE
PREFERRED = ("celltypist_majority_voting", "scanvi_label", "cell_type", "leiden", "label", "sample", "condition")


def _decode(values: Any) -> list[str]:
    return [v.decode() if isinstance(v, bytes) else str(v) for v in values]


def _categoricals(obs: h5py.Group) -> dict[str, tuple[list[str], np.ndarray]]:
    """obs columns stored as AnnData categoricals with few levels."""
    found = {}
    for name in obs:
        node = obs[name]
        if isinstance(node, h5py.Group) and {"categories", "codes"} <= set(node):
            levels = _decode(node["categories"][()])
            if 1 < len(levels) <= MAX_LEVELS:
                found[name] = (levels, node["codes"][()])
    return found


def _pick(columns: dict[str, Any]) -> list[str]:
    ordered = [c for c in PREFERRED if c in columns] + sorted(c for c in columns if c not in PREFERRED)
    return ordered[:MAX_COLUMNS]


def points_payload(h5ad: Path) -> dict[str, Any] | None:
    """Subsampled UMAP + labels, or None when the file has no 2-D UMAP."""
    try:
        with h5py.File(h5ad, "r") as handle:
            reject_external_storage(handle)
            umap = handle.get("obsm/X_umap")
            if not isinstance(umap, h5py.Dataset) or umap.ndim != 2 or umap.shape[1] < 2:
                return None
            n = umap.shape[0]
            keep = np.sort(np.random.default_rng(0).choice(n, MAX_POINTS, replace=False)) if n > MAX_POINTS else np.arange(n)
            xy = np.asarray(umap[:, :2], dtype="float64")[keep]
            columns = _categoricals(handle["obs"]) if "obs" in handle else {}
    except (OSError, KeyError, UnsupportedFile):
        return None
    low, span = xy.min(axis=0), np.ptp(xy, axis=0)
    span[span == 0] = 1.0
    scaled = np.rint((xy - low) / span * SCALE).astype(int)
    cols = {
        name: {"levels": columns[name][0], "codes": columns[name][1][keep].astype(int).tolist()}
        for name in _pick(columns)
    }
    return {"n": int(n), "x": scaled[:, 0].tolist(), "y": scaled[:, 1].tolist(), "cols": cols}


def write_points(step_dir: Path, key: str, view: Path) -> str | None:
    """Relative path of pts/<key>.js, writing it on first use."""
    relative = Path("pts") / f"{key}.js"
    target = view / relative
    if target.is_file():
        return relative.as_posix()
    output = step_dir / "output.h5ad"
    if not (step_dir / "_SUCCESS").exists() or not output.is_file():
        return None
    payload = points_payload(output)
    if payload is None:
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    # json.dumps escapes quotes and (ensure_ascii) U+2028/2029; '<' only occurs inside
    # strings and becomes \u003c, so labels from the data can never close a script tag.
    data = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    body = f"(window.SCHUB_PTS=window.SCHUB_PTS||{{}})[{json.dumps(key)}]={data};\n"
    partial = target.with_name(f".{target.name}.partial")
    partial.write_text(body)
    partial.replace(target)
    return relative.as_posix()
