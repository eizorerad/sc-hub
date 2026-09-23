"""A Jupyter notebook for a run: the pipeline itself, step by step.

Each step shows the exact code sc-hub ran (the brick's implementation) and the
parameters it used, so the student can read it, change a line or a parameter
and run that step again (schub.notebook_kit wires the inputs and outputs).
It runs in JupyterLab on the cluster (start_session kind=jupyter), where the
data and every tool are; anywhere else it can be read.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .brick_code import brick_source, code_changed
from .bricks import REGISTRY
from .datasets import dataset_label
from .hashing import stable_hash
from .headlines import duration, headline
from .notebook_results import TABLE_ROWS, Budget, Problems, anndata_summary, csv_head, step_outputs
from .runs import RunManifest
from .state import Frozen
from .stepfile import STEP_FILE, SUCCESS, SUMMARY_FILE

LONG_STEP_S = 300
TABLES = {"pseudobulk_de": "de_all.csv", "memento_de": "memento_all.csv"}
MODELS = {"integrate_scvi": ("scvi_model", "SCVI"), "integrate_scanvi": ("scanvi_model", "SCANVI")}
# A brick borrowing a helper from another brick (memento uses pseudobulk's _write_csv).
BRICK_IMPORT = re.compile(r"^from schub\.bricks\.impl\.(?P<brick>\w+) import (?P<names>[\w, ]+)$", re.M)


class NotebookStep(Frozen):
    index: int
    brick: str
    step_dir: str
    params: dict[str, Any] = {}
    code_id: str = ""
    headline: str = ""
    seconds: float | None = None
    completed: bool = False


class NotebookRun(Frozen):
    run_id: str
    dataset: str
    created_at: str
    steps: tuple[NotebookStep, ...]
    schub_version: str = ""
    project: str | None = None
    branch: str | None = None
    revision: int | None = None
    question: str = ""


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def load_run(manifest: RunManifest, question: str = "") -> NotebookRun:
    """What the notebook needs, read from the run's step folders."""
    steps = []
    for record in manifest.steps:
        folder = Path(record.step_dir)
        step_file = _read_json(folder / STEP_FILE) or {}
        timing = _read_json(folder / "results" / "timing.json") or {}
        seconds = timing.get("seconds")
        steps.append(NotebookStep(
            index=record.index, brick=record.brick, step_dir=str(folder),
            params=step_file.get("params", {}), code_id=str(step_file.get("code_id", "")),
            headline=headline(record.brick, _read_json(folder / "results" / SUMMARY_FILE)),
            seconds=seconds if isinstance(seconds, (int, float)) else None,
            completed=(folder / SUCCESS).exists(),
        ))
    return NotebookRun(
        run_id=manifest.run_id, dataset=dataset_label(manifest.dataset), created_at=manifest.created_at,
        steps=tuple(steps), schub_version=manifest.schub_version, project=manifest.project,
        branch=manifest.branch, revision=manifest.revision, question=question,
    )


def _cell(kind: str, source: str) -> dict[str, Any]:
    cell: dict[str, Any] = {"cell_type": kind, "metadata": {}, "source": source.strip("\n").splitlines(keepends=True)}
    if kind == "code":
        cell.update(execution_count=None, outputs=[])
    return cell


def _title(run: NotebookRun) -> str:
    if run.project and run.branch:
        return f"{run.project} / {run.branch}" + (f" · r{run.revision}" if run.revision else "")
    return f"sc-hub run {run.run_id}"


def _header(run: NotebookRun, results: bool, figures: bool) -> str:
    results = results and any(s.completed for s in run.steps)
    question = f"**Question:** {run.question}\n\n" if run.question else ""
    if not results:
        shown = ("- **No results inside yet:** no step of this run has finished. Download it again later, or ask "
                 "your assistant for `make_notebook`.\n")
    else:
        what = "its numbers and figures" if figures else "its numbers (figures are left out of this older run's copy; `make_notebook` includes them)"
        shown = (f"- **Results are inside:** under each step you see what the pipeline computed in run `{run.run_id}` "
                 f"({what}, the DE tables, what the final data holds). Running a cell replaces its output "
                 "with what it computes here.\n")
    version = f" · sc-hub {run.schub_version}" if run.schub_version else ""
    return (
        f"# {_title(run)}\n\n{question}"
        f"Run `{run.run_id}` · dataset `{run.dataset}` · created {run.created_at[:16].replace('T', ' ')}{version}\n\n"
        "This notebook is the pipeline itself. Each step shows the exact code sc-hub ran (the brick) and the "
        "parameters it used. Change a parameter or a line, run the step again, and the steps after it use your result.\n\n"
        f"{shown}"
        "- **Where it runs:** JupyterLab on the cluster, where the data and every tool are (ask your assistant to open "
        "this notebook in a JupyterLab session, then run `./schub-lab jupyter` on your laptop). Elsewhere you can read it.\n"
        "- **Start at any step:** a step reads your result of the step before it if you ran that step here, otherwise "
        "the pipeline's saved result. Heavy steps (FASTQ counting, scVI on a GPU) can be skipped.\n"
        "- **Safe to edit:** what you run here goes to the `work` folder next to this notebook, never into the "
        "pipeline's results.\n"
        "- **Keep a change:** ask your assistant to fix the step in the branch (a new revision) or to try it as an "
        "alternative (a new branch), quoting the step reference shown in its section."
    )


def _setup(run: NotebookRun) -> str:
    steps = "".join(f"        ({s.index}, {s.brick!r}, {s.step_dir!r}),\n" for s in run.steps)
    return (
        "from schub.notebook_kit import Pipeline\n\n"
        f"nb = Pipeline(\n    steps=[\n{steps}    ],\n    work={'work/' + run.run_id!r},\n)\n"
        "nb.overview()"
    )


def _step_markdown(run: NotebookRun, step: NotebookStep) -> str:
    spec = REGISTRY.get(step.brick)
    lines = [f"## Step {step.index} · {step.brick}", ""]
    if spec is not None:
        lines += [spec.summary, ""]
    if step.completed:
        took = f" · took {duration(step.seconds)}" if step.seconds else ""
        lines.append(f"**In the pipeline:** {step.headline or 'done'}{took}.")
    else:
        lines.append("**In the pipeline:** no saved result (not finished). Run the steps above first.")
    heavy = []
    if spec is not None and spec.uses_gpu:
        heavy.append("needs a GPU (start the JupyterLab session with a GPU)")
    if step.seconds and step.seconds > LONG_STEP_S:
        heavy.append(f"took {duration(step.seconds)} in the pipeline")
    if heavy:
        skip = " You can skip it: the next step then reads the saved result." if step.completed else ""
        lines.append(f"\nHeavy step: {'; '.join(heavy)}.{skip}")
    if code_changed(step.brick, step.code_id):
        lines.append("\n> sc-hub was updated after this run: the code below is the brick's current version, "
                     "so a re-run may differ from the saved result, or refuse what the older run recorded. "
                     "Then skip this step (the next one reads the saved result) or ask your assistant to run "
                     "the branch again for a notebook that matches the current code.")
    if run.project and run.branch:
        lines.append(f"\nReference for your assistant: `{run.project}/{run.branch}#{step.index}`")
    return "\n".join(lines)


def _code(step: NotebookStep, defined: set[str]) -> str:
    module = REGISTRY[step.brick].impl.partition(":")[0]

    def local(match: re.Match[str]) -> str:
        # The helper's brick has a cell above: use that cell's (maybe edited) helper when it was
        # run, and the library's when the student started below it.
        if match["brick"] not in defined:
            return match[0]
        names = tuple(n.strip() for n in match["names"].split(",") if n.strip())
        return (f"if not all(name in globals() for name in {names!r}):  # else: from the {match['brick']} cell above\n"
                f"    {match[0]}")

    source = BRICK_IMPORT.sub(local, brick_source(step.brick))
    return (f"# The brick {step.brick} exactly as sc-hub runs it ({module.replace('.', '/')}.py).\n"
            "# Edit it and run this cell and the next one again to try a change.\n" + source)


def _params(step: NotebookStep) -> tuple[str, str]:
    model = REGISTRY[step.brick].params_model
    note = ""
    try:
        values = model.model_validate(step.params).model_dump()
    except ValueError:  # recorded by an older brick version: show what was recorded
        values = dict(step.params)
        note = "# Recorded by an older sc-hub: the brick's parameters have changed since; adjust these.\n"
    args = "".join(f"    {key}={value!r},\n" for key, value in values.items())
    call = f"params = {model.__name__}(\n{args})" if args else f"params = {model.__name__}()"
    return f"from {model.__module__} import {model.__name__}", note + call


def _run_cell(step: NotebookStep) -> str:
    imports, params = _params(step)
    return (f"{imports}\n\n{params}\n"
            f"io = nb.io({step.index})\n"
            f"summary = {step.brick}(io, params)\n"
            f"nb.show({step.index}, summary)")


def _with(cell: dict[str, Any], outputs: list[dict[str, Any]]) -> dict[str, Any]:
    return {**cell, "outputs": outputs} if outputs else cell


def _explore(run: NotebookRun, results: bool, problems: Problems) -> list[dict[str, Any]]:
    with_output = [s for s in run.steps if s.brick in REGISTRY and not REGISTRY[s.brick].terminal]
    cells = [_cell("markdown", "## Explore the result\n\nThe data after the last step that writes one "
                   "(yours if you re-ran it here), ready for your own analysis.")]
    if with_output:
        last = with_output[-1]
        saved = anndata_summary(Path(last.step_dir) / "output.h5ad", problems) if results and last.completed else []
        cells.append(_with(_cell("code", f"adata = nb.load({last.index})\nadata"), saved))
        cells.append(_cell("code", "import scanpy as sc\n\n"
                           "categorical = [c for c in adata.obs.columns if adata.obs[c].dtype.name == 'category']\n"
                           "if 'X_umap' in adata.obsm:\n    sc.pl.umap(adata, color=categorical[:4], ncols=2)"))
    for step in run.steps:
        if step.brick in TABLES:
            table = Path(step.step_dir) / "results" / TABLES[step.brick]
            head = csv_head(table, problems) if results and step.completed else []
            cells.append(_with(_cell("code", f"import pandas as pd\n\nde = pd.read_csv(nb.file({step.index}, "
                                     f"{TABLES[step.brick]!r}), index_col=0)\nde.head({TABLE_ROWS})"), head))
        if step.brick in MODELS:
            folder, cls = MODELS[step.brick]
            cells.append(_cell("code", f"# The trained scvi-tools model of step {step.index} (a GPU session is faster)\n"
                               f"import scvi\n\nmodel_dir = nb.file({step.index}, {folder!r})\n"
                               f"# model = scvi.model.{cls}.load(str(model_dir), adata=adata[:, adata.var['highly_variable']].copy())"))
    return cells


def render_notebook(run: NotebookRun, results: bool = True, figures: bool = True) -> dict[str, Any]:
    """The run as a notebook; with `results`, each cell carries the pipeline's saved outputs
    (with `figures`, its images too). Saved files that exist but could not be read are
    listed in metadata.schub_incomplete."""
    cells = [_cell("markdown", _header(run, results, figures)), _cell("code", _setup(run))]
    budget, problems = Budget(enabled=figures), []
    defined: dict[str, int] = {}  # brick -> the step whose section holds its code
    for step in run.steps:
        markdown = _step_markdown(run, step)
        if step.brick in defined:
            markdown += (f"\n\nThe code of `{step.brick}` is in step {defined[step.brick]}'s section above: "
                         "run that code cell first when you start here.")
        cells.append(_cell("markdown", markdown))
        if step.brick not in REGISTRY:
            cells.append(_cell("markdown", f"*The brick `{step.brick}` is not part of this sc-hub version.*"))
            continue
        if step.brick not in defined:
            cells.append(_cell("code", _code(step, set(defined))))
            defined[step.brick] = step.index
        saved = step_outputs(step.step_dir, budget, problems) if results and step.completed else []
        cells.append(_with(_cell("code", _run_cell(step)), saved))
    cells.extend(_explore(run, results, problems))
    for number, cell in enumerate(cells):  # nbformat 4.5 wants stable cell ids
        cell["id"] = f"schub-{number:03d}"
    metadata: dict[str, Any] = {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                                "language_info": {"name": "python"}}
    if problems:
        metadata["schub_incomplete"] = problems[:20]
    return {
        "cells": cells,
        "metadata": metadata,
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def notebook_text(run: NotebookRun, results: bool = True) -> str:
    notebook = render_notebook(run, results)
    notebook["metadata"]["schub_generated"] = _cells_hash(notebook)
    return json.dumps(notebook, indent=1)


def _cells_hash(notebook: dict[str, Any]) -> str:
    return stable_hash([{"type": c.get("cell_type"), "source": c.get("source")} for c in notebook.get("cells", [])])


def _touched(notebook: dict[str, Any]) -> bool:
    """Edited or run by the student: its cells differ from what we wrote, or a cell was run
    (the pipeline's saved outputs come without execution counts)."""
    if notebook.get("metadata", {}).get("schub_generated") != _cells_hash(notebook):
        return True
    return any(c.get("execution_count") for c in notebook.get("cells", []))


def write_notebook(directory: Path, run: NotebookRun) -> Path:
    """Write the notebook, refreshing it (e.g. once the run has finished) only while
    the student has neither edited nor run it: then it is theirs and is kept."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"run_{run.run_id}.ipynb"
    if path.exists():
        try:
            current = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return path
        if _touched(current):
            return path
    text = notebook_text(run)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
    return path
