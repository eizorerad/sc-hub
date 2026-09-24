"""A cross-process lock that works on Lustre/NFS (no flock needed).

MBZUAI's Lustre is mounted with localflock: flock() only excludes processes on the
same node, so jobs on two nodes would both "hold" it. This lock is a file created
with O_CREAT|O_EXCL, which is atomic across nodes. A holder that may keep it for
long (a download, an environment build) passes `heartbeat_s`: a thread touches the
file meanwhile, so only a holder that died leaves a lock old enough to be broken.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

STALE_AFTER_S = 120.0
WAIT_S = 30.0
POLL_S = 0.2
MAX_POLL_S = 5.0  # a long wait asks the file server less and less often
BREAK_STALE_S = 30.0  # a <lock>.break left by a breaker that died


class LockTimeout(RuntimeError):
    pass


def _beat(path: Path, every_s: float, stop: threading.Event) -> None:
    """Keep the lock fresh while held; a passing file-server error (ESTALE, EIO) must not stop it."""
    while not stop.wait(every_s):
        try:
            os.utime(path)
        except FileNotFoundError:
            return  # broken by someone who thought us dead: nothing left to keep fresh
        except OSError:
            continue


def _break_if_stale(path: Path, stale_after_s: float) -> None:
    """Remove a lock nobody touched for `stale_after_s` (its holder died). One waiter at a time breaks, under
    `<lock>.break`, and only the very file it judged stale (same inode, still untouched): a lock another
    waiter created meanwhile is never removed."""
    try:
        judged = path.stat()
    except FileNotFoundError:
        return
    if time.time() - judged.st_mtime <= stale_after_s:
        return
    guard = path.with_name(path.name + ".break")
    try:
        os.close(os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        try:
            if time.time() - guard.stat().st_mtime > BREAK_STALE_S:
                guard.unlink(missing_ok=True)  # a breaker died halfway
        except FileNotFoundError:
            pass
        return
    try:
        now = path.stat()
        if now.st_ino == judged.st_ino and time.time() - now.st_mtime > stale_after_s:
            path.unlink()
    except FileNotFoundError:
        pass
    finally:
        guard.unlink(missing_ok=True)


def _release(path: Path, token: str) -> None:
    """Remove the lock only if it is still ours (it may have been broken and taken by another)."""
    try:
        if path.read_text().split("\n", 1)[0] == token:
            path.unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def exclusive(path: Path, wait_s: float = WAIT_S, stale_after_s: float = STALE_AFTER_S,
              heartbeat_s: float | None = None) -> Iterator[None]:
    """Hold `path` via O_CREAT|O_EXCL; break it if untouched for `stale_after_s`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.uname().nodename} {os.getpid()} {secrets.token_hex(8)}"
    deadline, poll = time.monotonic() + wait_s, POLL_S
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            _break_if_stale(path, stale_after_s)
            if path.exists() and time.monotonic() > deadline:
                raise LockTimeout(f"another process holds {path}; try again shortly") from None
            if path.exists():
                time.sleep(poll)
                poll = min(MAX_POLL_S, poll * 1.5)
    try:
        os.write(fd, f"{token}\n".encode())
    finally:
        os.close(fd)
    stop = threading.Event()
    beater = None
    try:
        if heartbeat_s:
            beater = threading.Thread(target=_beat, args=(path, heartbeat_s, stop), daemon=True)
            beater.start()
        yield
    finally:
        stop.set()
        if beater is not None:
            beater.join(timeout=5)
        _release(path, token)


@contextmanager
def long_held(path: Path) -> Iterator[None]:
    """For work of minutes to hours that another process may wait for (a download, a build)."""
    with exclusive(path, wait_s=12 * 3600, stale_after_s=180, heartbeat_s=30):
        yield
