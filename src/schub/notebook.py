"""Generate a Jupyter notebook for a finished run: the manual-control layer.

The notebook loads the run's final output, tables and trained scvi-tools model
(when there is one), in the course's format (Jupyter + scanpy + scvi-tools).
Open it in a Jupyter session on a compute node (start_session kind=jupyter).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .hashing import stable_hash
from .runs import RunManifest, RunResults


def _cell(kind: str, source: str) -> dict[str, Any]:
    cell: dict[str, Any] = {"cell_type": kind, "metadata": {}, "source": source.strip("\n").splitlines(keepends=True)}
    if kind == "code":
        cell.update(execution_count=None, outputs=[])
    return cell


def _steps_markdown(results: RunResults) -> str:
    lines = ["| # | brick | summary |", "|---|---|---|"]
    for step in results.steps:
        summary = json.dumps(step.summary or {}, default=str)[:160].replace("|", "/")
        lines.append(f"| {step.index} | {step.brick} | `{summary}` |")
    return "\n".join(lines)


def _first(results: RunResults, suffix: str) -> str | None:
    return next((f for step in results.steps for f in step.files if f.endswith(suffix)), None)


def render_notebook(manifest: RunManifest, results: RunResults) -> dict[str, Any]:
    de_table = _first(results, "de_all.csv") or _first(results, "memento_all.csv")
    model = next(
        (f.rsplit("/", 1)[0] for step in results.steps for f in step.files if f.endswith("model.pt")), None
    )
    cells = [
        _cell("markdown", f"# sc-hub run `{manifest.run_id}`\n\nDataset: `{manifest.dataset}`\n\n{_steps_markdown(results)}"),
        _cell("code", "import pandas as pd\nimport scanpy as sc\n\nsc.settings.set_figure_params(dpi=80)"),
        _cell("code", f"FINAL_OUTPUT = {results.final_output!r}\nadata = sc.read_h5ad(FINAL_OUTPUT) if FINAL_OUTPUT else None\nadata"),
        _cell("code", "categorical = [c for c in adata.obs.columns if adata.obs[c].dtype.name == 'category']\n"
              "if 'X_umap' in adata.obsm:\n    sc.pl.umap(adata, color=categorical[:4], ncols=2)"),
        _cell("code", f"DE_TABLE = {de_table!r}\nde = pd.read_csv(DE_TABLE, index_col=0) if DE_TABLE else None\n"
              "de.head(30) if de is not None else 'No DE table in this run.'"),
    ]
    if model:
        cells.append(_cell("code", "# The trained scvi-tools model of this run (GPU session recommended)\n"
                           f"import scvi\nMODEL_DIR = {model!r}\n"
                           "model_cls = scvi.model.SCANVI if 'scanvi' in MODEL_DIR else scvi.model.SCVI\n"
                           "# model = model_cls.load(MODEL_DIR, adata=adata[:, adata.var['highly_variable']].copy())"))
    return {
        "cells": cells,
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                     "language_info": {"name": "python"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _cells_hash(notebook: dict[str, Any]) -> str:
    return stable_hash([{"type": c.get("cell_type"), "source": c.get("source")} for c in notebook.get("cells", [])])


def write_notebook(directory: Path, manifest: RunManifest, results: RunResults) -> Path:
    """Write the starter notebook, refreshing it (e.g. once the run has finished) only
    while the student has not edited it: an edited notebook is theirs and is kept."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"run_{manifest.run_id}.ipynb"
    if path.exists():
        try:
            current = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return path
        if current.get("metadata", {}).get("schub_generated") != _cells_hash(current):
            return path
    notebook = render_notebook(manifest, results)
    notebook["metadata"]["schub_generated"] = _cells_hash(notebook)
    path.write_text(json.dumps(notebook, indent=1))
    return path
