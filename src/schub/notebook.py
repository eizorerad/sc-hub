"""Generate a marimo notebook for a finished run: the manual-control layer.

marimo notebooks are plain .py files: reactive in the browser, runnable as a
script, reviewable in git. The generated notebook loads the run's final
output and tables; students edit and extend it by hand.
"""

from __future__ import annotations

import json
from pathlib import Path

from .runs import RunManifest, RunResults

TEMPLATE = '''import marimo

__generated_with = "0.24"
app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo
    import pandas as pd
    import scanpy as sc
    return mo, pd, sc


@app.cell
def _(mo):
    mo.md(
        r"""
        # sc-hub run `{run_id}`

        Dataset: `{dataset}`

        {steps_md}
        """
    )
    return


@app.cell
def _(sc):
    FINAL_OUTPUT = {final_output!r}
    adata = sc.read_h5ad(FINAL_OUTPUT) if FINAL_OUTPUT else None
    adata
    return (adata,)


@app.cell
def _(adata, mo):
    _columns = [c for c in adata.obs.columns if adata.obs[c].dtype.name == "category"] if adata is not None else []
    color = mo.ui.dropdown(options=_columns, value=_columns[0] if _columns else None, label="color by")
    color
    return (color,)


@app.cell
def _(adata, color, sc):
    _fig = None
    if adata is not None and color.value and "X_umap" in adata.obsm:
        _fig = sc.pl.umap(adata, color=color.value, show=False, return_fig=True)
    _fig
    return


@app.cell
def _(mo, pd):
    DE_TABLE = {de_table!r}
    de = pd.read_csv(DE_TABLE, index_col=0) if DE_TABLE else None
    mo.ui.table(de.head(200)) if de is not None else mo.md("No DE table in this run.")
    return (de,)


if __name__ == "__main__":
    app.run()
'''


def _steps_markdown(results: RunResults) -> str:
    lines = ["| # | brick | summary |", "|---|---|---|"]
    for step in results.steps:
        summary = json.dumps(step.summary or {}, default=str)[:160].replace("|", "/")
        lines.append(f"| {step.index} | {step.brick} | `{summary}` |")
    return "\n        ".join(lines)


def render_notebook(manifest: RunManifest, results: RunResults) -> str:
    de_table = next(
        (f for step in results.steps for f in step.files if f.endswith("de_all.csv")), None
    )
    return TEMPLATE.format(
        run_id=manifest.run_id,
        dataset=manifest.dataset,
        steps_md=_steps_markdown(results),
        final_output=results.final_output,
        de_table=de_table,
    )


def write_notebook(directory: Path, manifest: RunManifest, results: RunResults) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"run_{manifest.run_id}.py"
    path.write_text(render_notebook(manifest, results))
    return path
