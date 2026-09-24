"""Static dashboard: `schub dashboard` writes view/ (one HTML page plus small
images), and the laptop mirrors it with `schub-view` (ssh + rsync + a browser)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..state import Frozen
from .collect import StepView, collect
from .notebooks import write_notebooks
from .page import render_site
from .points import write_points

RECENT_FULL_IMAGES = 5
RECENT_CELL_MAPS = 12  # ~90 KB each: more runs than full images


class DashboardInfo(Frozen):
    path: str
    runs: int
    projects: int
    bytes: int
    how_to_open: str


def _write(path: Path, text: str) -> None:
    """Atomic write with a unique temporary name (concurrent refreshes are fine)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, partial = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    os.chmod(partial, 0o644)
    os.replace(partial, path)


def _copy(source: Path, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size == source.stat().st_size:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dest)


class _Images:
    """Copies only what the page shows: thumbnails, and full images of recent runs."""

    def __init__(self, view: Path, full_keys: set[str], map_keys: set[str] | None = None) -> None:
        self.view = view
        self.full_keys = full_keys
        self.map_keys = full_keys if map_keys is None else map_keys
        self.used: set[Path] = set()

    def __call__(self, step: StepView, name: str, full: bool) -> str | None:
        figure = Path(step.step_dir) / "results" / name
        thumb = figure.with_name(figure.stem + "_thumb.png")
        if full:
            chosen = figure if step.key in self.full_keys and figure.is_file() else None
        else:
            chosen = thumb if thumb.is_file() else (figure if step.key in self.full_keys and figure.is_file() else None)
        if chosen is None:
            return None
        relative = Path("img") / step.key / chosen.name
        _copy(chosen, self.view / relative)
        self.used.add(self.view / relative)
        return relative.as_posix()

    def points(self, step: StepView) -> str | None:
        """pts/<key>.js with a subsampled cell map, for steps of recent runs."""
        if step.key not in self.map_keys:
            return None
        try:
            relative = write_points(Path(step.step_dir), step.key, self.view)
        except OSError:
            return None
        if relative:
            self.used.add(self.view / relative)
        return relative

    def prune(self) -> None:
        for path in (self.view / "pts").glob("*.js") if (self.view / "pts").is_dir() else []:
            if path not in self.used:
                path.unlink(missing_ok=True)
        root = self.view / "img"
        if not root.is_dir():
            return
        # A build from the assistant can overlap with the viewer's refresh: tolerate
        # files and folders that the other build adds or removes meanwhile.
        for path in root.rglob("*"):
            if path.is_file() and path not in self.used:
                path.unlink(missing_ok=True)
        # Deepest first, so folders emptied above go too.
        for folder in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                if not any(folder.iterdir()):
                    folder.rmdir()
            except OSError:
                pass


def _journal_files(settings: Any, view: Path, snapshot: Any) -> None:
    """Figures the Journal tab shows, and each bench project's notebook (also kept in its journal)."""
    from ..bench.journal import Journal
    from ..bench.render_nb import render

    used: set[Path] = set()
    for card in snapshot.journals:
        for source, relative in card.figures:
            target = view / relative
            try:
                _copy(Path(source), target)
                used.add(target)
            except OSError:
                continue
        project_dir = settings.projects_dir / card.project
        journal = Journal(project_dir, card.project)
        text = json.dumps(render(card.project, card.question, journal.entries(), journal.folder), indent=1)
        _write(view / f"{card.notebook}.ipynb", text)
        _write(view / f"{card.notebook}.js", f"window.SCHUB_JNB=window.SCHUB_JNB||{{}};"
                                             f"window.SCHUB_JNB[{json.dumps(card.project)}]={text};\n")
        try:
            _write(journal.folder / "notebook.ipynb", text)
        except OSError:
            pass  # the view copy is what the page links to
        used |= _report_files(view, card)
    for root in (view / "jfig", view / "jrep"):
        for path in root.rglob("*") if root.is_dir() else []:
            if path.is_file() and path not in used:
                path.unlink(missing_ok=True)


def _report_files(view: Path, card: Any) -> set[Path]:
    """Each published report's page, and its notebook as a script (scripts load on file:// pages, fetch does not)."""
    from .views_journal import report_key

    used: set[Path] = set()
    for report in card.reports[-1:]:  # the page links only the newest
        folder = view / report["view"]
        try:
            if report["html"]:
                _copy(Path(report["html"]), folder / "report.html")
                used.add(folder / "report.html")
            # Parsed and dumped again: the file is data from the project folder, never script.
            notebook = json.dumps(json.loads(Path(report["notebook"]).read_text()))
            key = report_key(card.project, report["folder"])
            _write(folder / "report.js", f"window.SCHUB_JNB=window.SCHUB_JNB||{{}};"
                                         f"window.SCHUB_JNB[{json.dumps(key)}]={notebook};\n")
            used.add(folder / "report.js")
        except (OSError, ValueError):
            continue  # the page links only what was copied
    return used


def build_dashboard(hub: Any, out: Path | None = None) -> DashboardInfo:
    view = out or hub.settings.view_dir
    snapshot = collect(hub)
    snapshot = snapshot.model_copy(update={"notebooks": frozenset(write_notebooks(view, snapshot, _write))})
    full_keys = {s.key for run in snapshot.runs[:RECENT_FULL_IMAGES] for s in run.steps}
    map_keys = {s.key for run in snapshot.runs[:RECENT_CELL_MAPS] for s in run.steps}
    images = _Images(view, full_keys, map_keys)
    site = render_site(snapshot, images)
    _write(view / "index.html", site.index)
    shutil.rmtree(view / "br", ignore_errors=True)  # graph files of the brick-era Pipelines tab
    images.prune()
    _journal_files(hub.settings, view, snapshot)
    shutil.rmtree(view / "runs", ignore_errors=True)  # per-run pages of the previous layout
    size = sum(p.stat().st_size for p in view.rglob("*") if p.is_file())
    return DashboardInfo(
        path=str(view / "index.html"),
        runs=len(snapshot.runs),
        projects=len(snapshot.projects),
        bytes=size,
        how_to_open="On your laptop run ./schub-view in ~/sc-hub-workspace (mirrors this folder and opens it).",
    )
