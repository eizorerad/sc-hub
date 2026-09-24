"""A cross-process lock that works on Lustre/NFS (no flock needed).

MBZUAI's Lustre is mounted with localflock: flock() only excludes processes on the
same node, so jobs on two nodes would both "hold" it. This lock is a file created
with O_CREAT|O_EXCL, which is atomic across nodes. A holder that may keep it for
long (a download, an environment build) passes `heartbeat_s`: a thread touches the
file meanwhile, so only a holder that died leaves a lock old enough to be broken.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

STALE_AFTER_S = 120.0
WAIT_S = 30.0
POLL_S = 0.2
MAX_POLL_S = 5.0  # a long wait asks the file server less and less often


class LockTimeout(RuntimeError):
    pass


def _beat(path: Path, every_s: float, stop: threading.Event) -> None:
    while not stop.wait(every_s):
        try:
            os.utime(path)
        except OSError:
            return


@contextmanager
def exclusive(path: Path, wait_s: float = WAIT_S, stale_after_s: float = STALE_AFTER_S,
              heartbeat_s: float | None = None) -> Iterator[None]:
    """Hold `path` via O_CREAT|O_EXCL; break it if untouched for `stale_after_s`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline, poll = time.monotonic() + wait_s, POLL_S
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
                raise LockTimeout(f"another process holds {path}; try again shortly") from None
            time.sleep(poll)
            poll = min(MAX_POLL_S, poll * 1.5)
    stop = threading.Event()
    beater = None
    try:
        os.write(fd, f"{os.uname().nodename} {os.getpid()} {time.time()}\n".encode())
        os.close(fd)
        if heartbeat_s:
            beater = threading.Thread(target=_beat, args=(path, heartbeat_s, stop), daemon=True)
            beater.start()
        yield
    finally:
        stop.set()
        if beater is not None:
            beater.join(timeout=5)
        path.unlink(missing_ok=True)


@contextmanager
def long_held(path: Path) -> Iterator[None]:
    """For work of minutes to hours that another process may wait for (a download, a build)."""
    with exclusive(path, wait_s=12 * 3600, stale_after_s=180, heartbeat_s=30):
        yield
