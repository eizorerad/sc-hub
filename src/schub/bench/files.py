"""Read-only view of project and library files for the agent.

Only projects/ and the libraries are visible, never the rest of the sc-hub root
(bench/, sessions/ with their tokens, logs/). Writing happens through cells, so
it is recorded in the journal. An .h5ad answers with its metadata profile, not
bytes; other binary files only with their size.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from ..config import Settings
from ..h5ad_profile import UnsupportedFile, profile_h5ad
from ..projects import ProjectError, ProjectStore
from ..state import Frozen

MAX_LISTED = 200
DEFAULT_CHARS = 20_000
HIDDEN = frozenset({"sessions", ".cache", "envs", ".ssh", ".git", "__pycache__", "logs", "bench"})


class FilesError(ValueError):
    pass


class Entry(Frozen):
    name: str
    kind: Literal["file", "folder", "link"]
    size: int | None = None


class FileView(Frozen):
    path: str
    kind: Literal["folder", "text", "h5ad", "binary"]
    entries: tuple[Entry, ...] = ()
    more_entries: int = 0
    text: str = ""
    truncated_chars: int = 0
    size: int | None = None
    profile: dict | None = None


def project_dir(settings: Settings, project: str | None) -> Path:
    """The project's folder, only for a valid, existing project name (never '..' or '/')."""
    if not project:
        return settings.projects_dir
    try:
        return ProjectStore(settings).require(project)
    except ProjectError as exc:
        raise FilesError(str(exc)) from exc


def roots(settings: Settings, project: str | None) -> list[Path]:
    return [project_dir(settings, project)] + list(settings.library_roots)


def resolve(settings: Settings, project: str | None, raw: str) -> Path:
    base = project_dir(settings, project)
    candidate = Path(raw).expanduser()
    candidate = candidate if candidate.is_absolute() else base / candidate
    try:
        target = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FilesError(f"'{raw}' does not exist") from exc
    allowed = [r.resolve() for r in roots(settings, project) if r.exists()]
    inside = next((r for r in allowed if target == r or target.is_relative_to(r)), None)
    if inside is None:
        raise FilesError(f"'{raw}' is outside the project and the libraries")
    if HIDDEN.intersection(target.relative_to(inside).parts):
        raise FilesError(f"'{raw}' is not readable through sc-hub")
    return target


def view(settings: Settings, project: str | None, raw: str = ".", max_chars: int = DEFAULT_CHARS) -> FileView:
    target = resolve(settings, project, raw)
    if target.is_dir():
        return _folder(target, raw)
    size = target.stat().st_size
    if target.suffix == ".h5ad":
        try:
            return FileView(path=raw, kind="h5ad", size=size, profile=profile_h5ad(target).model_dump(mode="json"))
        except (UnsupportedFile, OSError) as exc:
            raise FilesError(f"could not read the metadata of '{raw}': {exc}") from exc
    with target.open("rb") as handle:
        head = handle.read(max(1, max_chars) * 4)
    if b"\0" in head[:8192]:
        return FileView(path=raw, kind="binary", size=size)
    text = head.decode("utf-8", errors="replace")
    kept = text[:max_chars]
    return FileView(path=raw, kind="text", size=size, text=kept, truncated_chars=max(0, size - len(kept.encode())))


def _folder(target: Path, raw: str) -> FileView:
    entries = []
    names = sorted(os.listdir(target))
    for name in names[:MAX_LISTED]:
        if name in HIDDEN:
            continue
        path = target / name
        try:
            if path.is_symlink():
                entries.append(Entry(name=name, kind="link"))
            elif path.is_dir():
                entries.append(Entry(name=name, kind="folder"))
            else:
                entries.append(Entry(name=name, kind="file", size=path.stat().st_size))
        except OSError:
            continue
    return FileView(path=raw, kind="folder", entries=tuple(entries), more_entries=max(0, len(names) - MAX_LISTED))
