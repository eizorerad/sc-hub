"""A cross-process lock that works on Lustre/NFS (no flock needed)."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

STALE_AFTER_S = 120.0
WAIT_S = 30.0
POLL_S = 0.2


class LockTimeout(RuntimeError):
    pass


@contextmanager
def exclusive(path: Path, wait_s: float = WAIT_S, stale_after_s: float = STALE_AFTER_S) -> Iterator[None]:
    """Hold `path` via O_CREAT|O_EXCL; break it if older than `stale_after_s`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_s
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale_after_s:
                    path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise LockTimeout(f"another submission holds {path}; try again shortly") from None
            time.sleep(POLL_S)
    try:
        os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)
