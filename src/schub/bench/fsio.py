"""Durable small-file writes on Lustre and NFS.

- write_json_atomic: a temp file in the same folder, fsync, os.replace, fsync of the
  folder. Readers see the old or the new file, never a mix.
- create_json_exclusive: the file appears complete or not at all, and only once.
  It is written to a temp file and published with link(), which fails if the name
  exists. renameat2(RENAME_NOREPLACE) would be the obvious tool, but it returns
  EINVAL on both NFS and Lustre (found in the VCC2026 project); where link() is
  not allowed, O_CREAT|O_EXCL is the fallback.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any


def fsync_dir(folder: Path) -> None:
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # some file systems refuse fsync on a directory; the data is still written
    finally:
        os.close(fd)


def _encode(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=1, sort_keys=False, default=str) + "\n").encode()


def _write_temp(folder: Path, name: str, data: bytes, mode: int) -> Path:
    temp = folder / f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return temp


def write_json_atomic(path: Path, payload: dict[str, Any], mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = _write_temp(path.parent, path.name, _encode(payload), mode)
    try:
        os.replace(temp, path)
    except OSError:
        temp.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


def create_json_exclusive(path: Path, payload: dict[str, Any], mode: int = 0o644) -> bool:
    """Publish `payload` at `path` unless something is already there. True if it was ours."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _encode(payload)
    temp = _write_temp(path.parent, path.name, data, mode)
    try:
        os.link(temp, path)
    except FileExistsError:
        return False
    except OSError:
        return _create_excl(path, data, mode)
    finally:
        temp.unlink(missing_ok=True)
    fsync_dir(path.parent)
    return True


def _create_excl(path: Path, data: bytes, mode: int) -> bool:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        return False
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(path.parent)
    return True


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
