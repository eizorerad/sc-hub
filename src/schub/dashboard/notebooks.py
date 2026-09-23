"""Notebooks and brick code for the page.

Every run gets nb/<run_id>.js: its notebook as a script the page loads on demand
(file:// pages cannot fetch files, but they can load scripts) and offers as a
.ipynb download. A file is rewritten only when its content differs, so the
laptop's mirror (rsync, or the Windows change check) stays cheap, and two
overlapping builds converge on the newest content. Brick code is embedded once
per brick for the "Code" sections.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

from ..brick_code import brick_source, code_changed
from ..bricks import REGISTRY
from ..notebook import NotebookRun, NotebookStep, render_notebook
from .collect import RUN_ID, RunView, Snapshot
from .html import esc

FOLDER = "nb"


def notebook_run(run: RunView, question: str = "") -> NotebookRun:
    return NotebookRun(
        run_id=run.run_id, dataset=run.dataset, created_at=run.created_at, schub_version=run.schub_version,
        project=run.project, branch=run.branch, revision=run.revision, question=question,
        steps=tuple(
            NotebookStep(index=s.index, brick=s.brick, step_dir=s.step_dir, params=s.params, code_id=s.code_id,
                         headline=s.headline, seconds=s.extras.seconds, completed=s.state == "COMPLETED")
            for s in run.steps
        ),
    )


def download_name(run: RunView) -> str:
    """A readable file name: <project>-<branch>-r<N> or sc-hub-run-<id>."""
    if run.project and run.branch:
        base = f"{run.project}-{run.branch}" + (f"-r{run.revision}" if run.revision else "")
    else:
        base = f"sc-hub-run-{run.run_id}"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-")


def _same(path: Path, text: str) -> bool:
    """The file already holds exactly this text (size first: most files differ there or not at all)."""
    try:
        return path.stat().st_size == len(text.encode()) and path.read_text() == text
    except OSError:
        return False


def write_notebooks(view: Path, snap: Snapshot, write: Callable[[Path, str], None]) -> set[str]:
    """nb/<run_id>.js for every run on the page; returns the run ids that have one."""
    folder = view / FOLDER
    questions = {p.path: p.meta.question for p in snap.projects}
    done: set[str] = set()
    for run in snap.runs:
        if not RUN_ID.fullmatch(run.run_id):  # it becomes a file name
            continue
        try:
            notebook = render_notebook(notebook_run(run, questions.get(run.project or "", "")))
        except (KeyError, ValueError, OSError):  # one odd run must not break the page
            continue
        script = (f"window.SCHUB_NB=window.SCHUB_NB||{{}};"
                  f"window.SCHUB_NB[{json.dumps(run.run_id)}]={json.dumps(notebook)};\n")
        path = folder / f"{run.run_id}.js"
        if not _same(path, script):
            write(path, script)
        done.add(run.run_id)
    if folder.is_dir():
        # Only finished files: a concurrent build's temporary files (.<name>.<random>) stay.
        stale = [p for p in folder.glob("*.js") if p.stem not in done] + [folder / "index.json"]
        for path in stale:
            path.unlink(missing_ok=True)  # runs no longer shown, and the index of an earlier version
    return done


def used_bricks(snap: Snapshot) -> list[str]:
    found = {s.brick for run in snap.runs for s in run.steps} | {n.brick for n in snap.nodes}
    return sorted(b for b in found if b in REGISTRY)


def code_templates(snap: Snapshot) -> str:
    """Each brick's code once; the page copies it into a step's Code section when opened."""
    parts = []
    for brick in used_bricks(snap):
        try:
            source = brick_source(brick)
        except (KeyError, ValueError, OSError):
            continue
        parts.append(f'<template data-code-src="{esc(brick)}">{esc(source)}</template>')
    return "".join(parts)


def code_section(brick: str, recorded_code_id: str = "") -> str:
    """A folded 'Code' section for one step (filled by the page script when opened)."""
    if brick not in REGISTRY:
        return ""
    module = REGISTRY[brick].impl.partition(":")[0].replace(".", "/") + ".py"
    changed = ('<p class="note warn small">This step ran with an earlier version of this code '
               "(sc-hub was updated since); shown is the current one.</p>") if code_changed(brick, recorded_code_id) else ""
    return (f'<details class="code" data-code="{esc(brick)}"><summary>Code of this step '
            f'<span class="muted small">{esc(module)}</span></summary>{changed}<pre class="code"></pre></details>')


def notebook_button(run: RunView, have: set[str], label: str = "Download notebook") -> str:
    if run.run_id not in have:
        return ""
    return (f'<button type="button" class="nb-download" data-notebook="{esc(run.run_id)}" '
            f'data-name="{esc(download_name(run))}">{esc(label)}</button>')
