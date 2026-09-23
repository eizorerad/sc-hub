"""Static dashboard: `schub dashboard` writes view/ (one HTML page plus small
images), and the laptop mirrors it with `schub-view` (ssh + rsync + a browser)."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..state import Frozen
from .collect import StepView, collect
from .page import render_page

RECENT_FULL_IMAGES = 5


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

    def __init__(self, view: Path, full_keys: set[str]) -> None:
        self.view = view
        self.full_keys = full_keys
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

    def prune(self) -> None:
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


def build_dashboard(hub: Any, out: Path | None = None) -> DashboardInfo:
    view = out or hub.settings.view_dir
    snapshot = collect(hub)
    full_keys = {s.key for run in snapshot.runs[:RECENT_FULL_IMAGES] for s in run.steps}
    images = _Images(view, full_keys)
    _write(view / "index.html", render_page(snapshot, images))
    images.prune()
    shutil.rmtree(view / "runs", ignore_errors=True)  # per-run pages of the previous layout
    size = sum(p.stat().st_size for p in view.rglob("*") if p.is_file())
    return DashboardInfo(
        path=str(view / "index.html"),
        runs=len(snapshot.runs),
        projects=len(snapshot.projects),
        bytes=size,
        how_to_open="On your laptop run ./schub-view in ~/sc-hub-workspace (mirrors this folder and opens it).",
    )
