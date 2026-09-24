"""What a cell changed in the project folder: a scan before and after, then a diff.

Symlinked folders are never entered (in VCC2026 they point at terabytes on Lustre),
generated folders are skipped (a check that walked 580k scratch files once blocked
17 commits there, incident A686), and the scan stops at a file limit.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .models import FileChange

SKIP_DIRS = frozenset({"journal", "jobs", ".ipynb_checkpoints", "__pycache__", "scratch", ".git", "envs", "tmp",
                       ".cache", "node_modules", ".venv", "repos"})  # a clone is recorded by its commit


@dataclass(frozen=True)
class Snapshot:
    files: dict[str, tuple[int, int]]  # relative path -> (size, mtime_ns)
    truncated: bool


def scan(root: Path, max_files: int, skip: frozenset[str] = SKIP_DIRS) -> Snapshot:
    files: dict[str, tuple[int, int]] = {}
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in sorted(entries, key=lambda e: e.name):
            try:
                if entry.is_dir(follow_symlinks=False):
                    # a variant (a subproject) keeps its own journal: its files are not this project's
                    if entry.name not in skip and not os.path.exists(os.path.join(entry.path, "project.yaml")):
                        stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if len(files) >= max_files:
                return Snapshot(files=files, truncated=True)
            files[Path(entry.path).relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return Snapshot(files=files, truncated=False)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(chunk):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def diff(before: Snapshot, after: Snapshot, root: Path, hash_max_bytes: int) -> tuple[FileChange, ...]:
    changes: list[FileChange] = []
    for path, (size, mtime) in sorted(after.files.items()):
        old = before.files.get(path)
        if old == (size, mtime):
            continue
        digest = sha256_file(root / path) if size <= hash_max_bytes else None
        changes.append(FileChange(path=path, change="created" if old is None else "modified", size=size, sha256=digest))
    if not after.truncated:  # a truncated scan cannot tell a deletion from a file it did not reach
        for path in sorted(set(before.files) - set(after.files)):
            changes.append(FileChange(path=path, change="deleted"))
    return tuple(changes)
