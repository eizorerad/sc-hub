"""The dashboard for the Windows mirror (schub-view.ps1), through the sc-hub key's gate.

The gate lets no scp and no shell through, so the mirror asks for the view as one tar
stream: `schub view-sum` rebuilds the dashboard and prints a checksum of its heavy files
(figures, notebooks, reports); `schub view-pack light` streams only the page and its small
per-project scripts, `schub view-pack full` everything. The mirror downloads everything
only when the checksum changed. The page comes last in the stream and is moved last on
the laptop, so an open page never meets a missing project file.
"""

from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path
from typing import BinaryIO

LIGHT = ("index.html", "versions.json", "jproj")  # rewritten on every change; small
SKIP = (".schub-view",)
PAGE = "index.html"


def _files(view: Path) -> list[tuple[str, Path]]:
    found = []
    for path in sorted(p for p in view.rglob("*") if p.is_file() and not p.is_symlink()):
        relative = path.relative_to(view).as_posix()
        if relative.split("/")[0] not in SKIP and not relative.endswith(".tmp"):
            found.append((relative, path))
    return found


def heavy_sum(view: Path) -> str:
    """Names, sizes and times of every file outside LIGHT: it changes when a figure, notebook or report does."""
    digest = hashlib.sha256()
    for relative, path in _files(view):
        if relative.split("/")[0] in LIGHT:
            continue
        stat = path.stat()
        digest.update(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:20]


def pack(view: Path, kind: str, out: BinaryIO) -> int:
    """Stream `view` as tar to `out`: "light" (the page and its scripts) or "full"; the page last."""
    if kind not in ("light", "full"):
        raise ValueError("view-pack takes light or full")
    files = [(r, p) for r, p in _files(view) if kind == "full" or r.split("/")[0] in LIGHT]
    files.sort(key=lambda item: item[0] == PAGE)
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for relative, path in files:
            info = tar.gettarinfo(str(path), arcname=relative)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as handle:
                tar.addfile(info, handle)
    return len(files)
