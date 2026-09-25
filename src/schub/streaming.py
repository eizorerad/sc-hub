"""A build step run from a bench cell: its output streamed into the cell, stopped as a whole.

The step runs in its own process group, so an interrupted cell stops it and everything it
started (uv, micromamba, R), not only the shell that launched them.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Mapping, Sequence, TextIO


def run_streamed(argv: Sequence[str], out: TextIO | None = None, env: Mapping[str, str] | None = None,
                 cwd: str | None = None, keep: int = 40) -> tuple[int, list[str]]:
    """(exit code, last `keep` lines). `out` is looked up at call time (a kernel replaces sys.stdout)."""
    out = out or sys.stdout
    process = subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               env=dict(env) if env is not None else None, cwd=cwd, start_new_session=True)
    tail: list[str] = []
    try:
        for line in process.stdout or ():
            out.write(line)
            tail = (tail + [line])[-keep:]
        return process.wait(), tail
    except BaseException:
        _stop_group(process)
        raise


def _stop_group(process: subprocess.Popen) -> None:
    for sig, grace_s in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=grace_s)
            return
        except subprocess.TimeoutExpired:
            continue
