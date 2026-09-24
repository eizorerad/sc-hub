"""Checkpoints a training run can trust after a crash, a time limit or scancel.

    from schub_ckpt import Run
    run = Run(bench.work_dir() / "runs" / "small", registration={"config": cfg, "data": data_sha, "code": commit})
    resumed = run.start()          # None, or the last complete checkpoint of this same registration
    step = resumed["step"] if resumed else 0
    if resumed:
        model.load_state_dict(torch.load(resumed["dir"] / "model.pt"))
    with run.signals():            # SIGTERM / SIGUSR1 (time limit near) only set run.stop_requested
        while step < total and not run.stop_requested and not run.segment_over(hours=7.5):
            ...                    # train; every N steps:
            run.save(step, lambda d: torch.save(model.state_dict(), d / "model.pt"), loss=loss)
    run.save(step, lambda d: torch.save(model.state_dict(), d / "model.pt"),
             status="done" if step >= total else "paused")

A checkpoint counts only when complete (the VCC2026 transaction): its files are
written and fsynced, then complete.json with their sha256, then latest.json points
at it (atomic replace). A crash in between leaves the previous checkpoint as the
latest. Resuming under another registration (config, data, code) is refused: that
would mix two experiments; start a new run folder instead. Stdlib only.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


class CheckpointError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _fsync_dir(folder: Path) -> None:
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _durable_json(path: Path, value: Any) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    with temp.open("w") as handle:
        json.dump(value, handle, indent=1, sort_keys=True, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    _fsync_dir(path.parent)


def _plain(value: Any) -> Any:
    """JSON for what a registration may hold: sets become sorted lists, paths strings; anything else whose
    text would change between processes (an object's repr, a set's order) is refused."""
    if isinstance(value, (set, frozenset)):
        return sorted((_plain(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    raise CheckpointError(f"a registration holds only JSON values, sets and paths, not {type(value).__name__}")


def _canonical(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=_plain))


class Run:
    def __init__(self, folder: str | os.PathLike, registration: dict, keep: int = 3) -> None:
        self.folder = Path(folder)
        self.registration = _canonical(registration)
        self.registration_sha256 = hashlib.sha256(json.dumps(self.registration, sort_keys=True).encode()).hexdigest()
        self.keep = max(1, keep)
        self._asked = False
        self.stop_signal = ""
        self._lock = None
        self._started = 0.0

    @property
    def stop_requested(self) -> bool:
        """A signal (Run.signals()) or the job's time-limit file ($SCHUB_STOP_FILE, written by sc-hub's jobrun,
        which also reaches a paper's own --python process) asks to save and stop."""
        if self._asked:
            return True
        path = os.environ.get("SCHUB_STOP_FILE")
        if path and os.path.exists(path):
            self.stop_signal = self.stop_signal or "time limit near"
            return True
        return False

    def __enter__(self) -> "Run":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def checkpoints(self) -> Path:
        return self.folder / "checkpoints"

    # ---- start and resume --------------------------------------------------------------

    def start(self) -> dict | None:
        """Take the run (one process at a time); returns the latest complete checkpoint, or None."""
        self.folder.mkdir(parents=True, exist_ok=True)
        lock = (self.folder / ".lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.close()
            raise CheckpointError(f"another process trains {self.folder}") from exc
        self._lock, self._started = lock, time.monotonic()
        path = self.folder / "registration.json"
        if not path.exists():
            _durable_json(path, self.registration)
            return None
        if json.loads(path.read_text()) != self.registration:
            self.close()
            raise CheckpointError(f"the registration differs from the one {self.folder} was started with; "
                                  "start a new run folder (resuming would mix two experiments)")
        try:
            return self.latest()
        except Exception:
            self.close()  # a refused resume must not keep the run locked for the next attempt
            raise

    def close(self) -> None:
        if self._lock is not None:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    def latest(self) -> dict | None:
        """The checkpoint latest.json points at, after checking every file against complete.json."""
        try:
            pointer = json.loads((self.folder / "latest.json").read_text())
        except FileNotFoundError:
            return None
        name = str(pointer.get("checkpoint", ""))
        folder = self.checkpoints / name
        if not name or "/" in name or pointer.get("registration_sha256") != self.registration_sha256:
            raise CheckpointError(f"latest.json of {self.folder} points outside this run")
        complete = folder / "complete.json"
        if not complete.is_file() or _sha256(complete) != pointer.get("complete_sha256"):
            raise CheckpointError(f"checkpoint {name} changed or is incomplete")
        record = json.loads(complete.read_text())
        for relative, digest in record["files"].items():
            path = folder / relative
            if not path.is_file() or _sha256(path) != digest:
                raise CheckpointError(f"checkpoint {name} changed: {relative}")
        return {**record, "dir": folder}

    # ---- save --------------------------------------------------------------------------

    def save(self, step: int, write: Callable[[Path], None], status: str = "running", **info: Any) -> dict:
        """write(folder) writes the checkpoint's files; then they are made durable and published."""
        if self._lock is None:
            raise CheckpointError("call start() before save()")
        self.checkpoints.mkdir(exist_ok=True)
        folder = self.checkpoints / f"{int(step):09d}-{uuid.uuid4().hex[:8]}"
        folder.mkdir()
        write(folder)
        files = {}
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
            files[path.relative_to(folder).as_posix()] = _sha256(path)
        if not files:
            shutil.rmtree(folder, ignore_errors=True)
            raise CheckpointError("the write function wrote nothing into the checkpoint folder")
        for sub in sorted({p.parent for p in folder.rglob("*") if p.is_file()}, reverse=True):
            _fsync_dir(sub)
        record = {"step": int(step), "status": status, "files": files, "info": _canonical(info),
                  "registration_sha256": self.registration_sha256,
                  "saved": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        _durable_json(folder / "complete.json", record)
        _fsync_dir(self.checkpoints)  # the new folder's own entry, before anything points at it
        _durable_json(self.folder / "latest.json", {"checkpoint": folder.name, "registration_sha256":
                                                    self.registration_sha256,
                                                    "complete_sha256": _sha256(folder / "complete.json")})
        self._prune(folder.name)
        return {**record, "dir": folder}

    def _prune(self, current: str) -> None:
        """Keep the newest `keep` complete checkpoints; drop older ones and abandoned partial folders."""
        folders = sorted(p for p in self.checkpoints.iterdir() if p.is_dir())
        complete = [p for p in folders if (p / "complete.json").is_file()]
        keep = {p.name for p in complete[-self.keep:]} | {current}
        for path in folders:
            if path.name not in keep and path.name < current:
                shutil.rmtree(path, ignore_errors=True)

    # ---- stopping in time --------------------------------------------------------------

    @contextlib.contextmanager
    def signals(self) -> Iterator["Run"]:
        """SIGTERM (scancel, time limit) and SIGUSR1 (time limit near) set stop_requested instead of killing."""
        def ask(number: int, _frame: Any) -> None:
            self._asked = True
            self.stop_signal = signal.Signals(number).name

        previous = {number: signal.signal(number, ask) for number in (signal.SIGTERM, signal.SIGUSR1)}
        try:
            yield self
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)

    def segment_over(self, hours: float | None = None, seconds: float | None = None) -> bool:
        """True once this process has trained for the given time (end a segment, continue in a new job)."""
        if (hours is None) == (seconds is None):
            raise ValueError("give hours or seconds")
        limit = seconds if seconds is not None else hours * 3600
        return time.monotonic() - self._started >= limit
