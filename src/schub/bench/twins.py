"""Mini twins of datasets: `bench.twin(path, stratify="perturbation", keep=["control"])`.

A twin is a small, stratified sample in the same format (a few % of the cells, at
least `min_per_group` per group, the `keep` groups such as non-targeting controls
kept up to `max_keep`), so code can be tried in seconds before it runs on the full
data. Its folder is named by the hash of what defines it (the source's identity,
the rule, the seed), so asking again returns the same twin, and a changed source
gives a new one. twin.json records the rule and the group sizes.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
from pathlib import Path
from typing import Sequence

import numpy as np

from ..hashing import file_fingerprint, stable_hash
from .clock import stamp
from .fsio import write_json_atomic


class TwinError(ValueError):
    pass


def twins_home(source: Path) -> Path:
    """Next to the dataset if that folder is writable, else in the student's local library."""
    beside = source.parent / "twins"
    if os.access(source.parent, os.W_OK):
        return beside
    root = Path(os.environ.get("SCHUB_ROOT", Path.home() / "schub"))
    return root / "library-local" / "twins" / source.stem


def select(labels: np.ndarray | None, n_obs: int, fraction: float, min_per_group: int, keep: Sequence[str],
           max_keep: int, max_cells: int, rng: np.random.Generator) -> np.ndarray:
    """Row indices of the twin, sorted."""
    if labels is None:
        size = min(n_obs, max(min_per_group, int(round(fraction * n_obs))), max_cells)
        return np.sort(rng.choice(n_obs, size=size, replace=False))
    chosen = []
    for group in np.unique(labels):
        members = np.flatnonzero(labels == group)
        want = max_keep if group in keep else max(min_per_group, int(round(fraction * len(members))))
        chosen.append(members if len(members) <= want else rng.choice(members, size=want, replace=False))
    rows = np.sort(np.concatenate(chosen)) if chosen else np.array([], dtype=int)
    if len(rows) > max_cells:
        raise TwinError(f"the twin would have {len(rows)} cells (more than max_cells={max_cells}); "
                        "lower fraction or min_per_group")
    return rows


def twin(source: str | os.PathLike, stratify: str | None = None, keep: Sequence[str] = (), fraction: float = 0.05,
         min_per_group: int = 20, max_keep: int = 2000, max_cells: int = 50_000, seed: int = 0) -> Path:
    """Path of the twin's data.h5ad (built if needed)."""
    path = Path(source).resolve()
    if not 0 < fraction <= 1:
        raise TwinError("fraction must be in (0, 1]")
    rule = {"source": str(path), "identity": file_fingerprint(path), "stratify": stratify,
            "keep": sorted(map(str, keep)), "fraction": fraction, "min_per_group": min_per_group,
            "max_keep": max_keep, "max_cells": max_cells, "seed": seed}
    folder = twins_home(path) / stable_hash(rule)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder.with_name(folder.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # two jobs asking for the same twin: one builds, the other finds it
        if (folder / "data.h5ad").exists() and (folder / "twin.json").exists():
            print(f"twin already built: {folder / 'data.h5ad'}")
            return folder / "data.h5ad"
        return _build(path, folder, rule, stratify, keep)


def _build(path: Path, folder: Path, rule: dict, stratify: str | None, keep: Sequence[str]) -> Path:
    from .checks.h5ad import read_obs_column
    from .h5rows import subset

    labels = read_obs_column(path, stratify).astype(str).to_numpy() if stratify else None
    n_obs = len(labels) if labels is not None else _count_obs(path)
    rows = select(labels, n_obs, rule["fraction"], rule["min_per_group"], [str(k) for k in keep], rule["max_keep"],
                  rule["max_cells"], np.random.default_rng(rule["seed"]))
    small, dropped = subset(path, rows)
    partial = folder / f".data.{os.getpid()}.{secrets.token_hex(4)}.partial"
    small.write_h5ad(partial)
    partial.replace(folder / "data.h5ad")
    groups = {} if labels is None else {str(k): int(v) for k, v in zip(*np.unique(labels[rows], return_counts=True))}
    write_json_atomic(folder / "twin.json", {**rule, "n_obs_source": n_obs, "n_obs_twin": int(small.n_obs),
                                             "groups": groups, "dropped": dropped, "created": stamp()})
    print(f"twin of {path.name}: {small.n_obs} cells -> {folder / 'data.h5ad'}"
          + (f" (left out: {', '.join(dropped)})" if dropped else ""))
    return folder / "data.h5ad"


def _count_obs(path: Path) -> int:
    import anndata.io

    from .h5rows import h5ad

    with h5ad(path) as f:
        obs = f["obs"]
        return len(anndata.io.read_elem(obs[obs.attrs["_index"]]))  # the index alone, not every column


def describe(folder: Path) -> dict:
    return json.loads((folder / "twin.json").read_text())
