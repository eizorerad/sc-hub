"""The workbench process: takes cell requests from the inbox and runs them, one
thread and one kernel per project.

    python -m schub.bench.runner          (inside the workbench Slurm job)

It stops on its own when idle, when bench/STOP exists, or when Slurm signals the
end of the job (SIGUSR1 some time before the limit, SIGTERM on scancel). Then a
cell still running is marked `retired`, requests that never started go back to
the inbox for the successor, and each used project gets a journal note that the
kernel's variables are gone. On start it sweeps what a dead runner left behind:
unfinished cells become `lost`, unstarted ones return to the inbox.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading
import time
from typing import Callable

from ..config import Settings, load_settings
from ..projects import ProjectError, ProjectStore
from .clock import Clock, seconds_between, stamp
from .fsio import read_json, write_json_atomic
from .inbox import Claimed, Inbox
from .journal import Journal
from .kernels import ProjectKernel
from .worker import KernelFactory, ProjectWorker, new_entry

HEARTBEAT_S = 10.0
STALE_AFTER_S = 60.0
JOIN_TIMEOUT_S = 90.0
EXIT_OK, EXIT_BUSY = 0, 3
LOST_BY_DEAD_RUNNER = ("The workbench stopped while this cell ran (job {job}). Its variables are gone; files it "
                       "wrote may be incomplete. Run it again if you need its result.")


class Runner:
    def __init__(
        self,
        settings: Settings,
        *,
        now: Clock = stamp,
        kernel_factory: KernelFactory = ProjectKernel,
        job_id: str | None = None,
        node: str | None = None,
        idle_stop_s: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.now = now
        self.kernel_factory = kernel_factory
        self.inbox = Inbox(settings.bench_dir)
        self.job_id = job_id or os.environ.get("SLURM_JOB_ID") or f"local-{os.getpid()}"
        self.node = node or socket.gethostname().split(".")[0]
        self.idle_stop_s = idle_stop_s if idle_stop_s is not None else settings.bench.idle_stop_min * 60
        self.monotonic = monotonic
        self.workers: dict[str, ProjectWorker] = {}
        self.started = now()
        self._retiring = ""
        self._stop_reason = ""
        self._last_activity = monotonic()
        self._lock = threading.Lock()

    # ---- state shared with workers ---------------------------------------------------

    @property
    def retiring(self) -> str:
        return self._retiring

    def retire(self, reason: str) -> None:
        """Called from a signal handler: finish soon, hand over to a successor."""
        self._retiring = self._retiring or reason

    def touch(self) -> None:
        with self._lock:
            self._last_activity = self.monotonic()

    @property
    def state_path(self):
        return self.settings.bench_dir / "workbench.json"

    def _write_state(self, state: str) -> None:
        write_json_atomic(self.state_path, {
            "job_id": self.job_id, "node": self.node, "pid": os.getpid(), "started": self.started,
            "heartbeat": self.now(), "state": state, "reason": self._stop_reason or self._retiring,
            "projects": {p: {"epoch": w.kernel.epoch if w.kernel else "", "busy": w.busy} for p, w in self.workers.items()},
        })

    # ---- lifecycle ---------------------------------------------------------------------

    def other_runner_alive(self) -> bool:
        record = read_json(self.state_path)
        if not record or record.get("state") != "running" or record.get("job_id") == self.job_id:
            return False
        try:
            return seconds_between(str(record.get("heartbeat")), self.now()) < STALE_AFTER_S
        except (TypeError, ValueError):
            return False

    def run(self) -> int:
        if self.other_runner_alive():
            return EXIT_BUSY
        self.settings.bench_dir.mkdir(parents=True, exist_ok=True)
        self._write_state("running")
        self.sweep()
        state = self._loop()
        self._shutdown(state)
        return EXIT_OK

    def _loop(self) -> str:
        next_beat = 0.0
        while True:
            if self._retiring:
                return "retired"
            if (self.settings.bench_dir / "STOP").exists():
                self._stop_reason = "bench/STOP exists"
                return "stopped"
            for item in self.inbox.claim():
                self._dispatch(item)
            for project, cid, _action in self.inbox.take_controls():
                if project in self.workers:
                    self.workers[project].interrupt(cid)
            if self.monotonic() >= next_beat:
                self._write_state("running")
                next_beat = self.monotonic() + HEARTBEAT_S
            if self._idle_too_long():
                self._stop_reason = f"idle for {self.idle_stop_s / 60:.0f} min"
                return "idle-stopped"
            time.sleep(self.settings.bench.poll_s)

    def _dispatch(self, item: Claimed) -> None:
        self.touch()
        project = item.request.project
        worker = self.workers.get(project)
        if worker is None:
            worker = ProjectWorker(self, project)
            self.workers[project] = worker
            worker.start()
        worker.submit(item)

    def _idle_too_long(self) -> bool:
        if any(not w.idle() for w in self.workers.values()):
            self.touch()
            return False
        with self._lock:
            return self.monotonic() - self._last_activity > self.idle_stop_s

    def _shutdown(self, state: str) -> None:
        reason = {
            "retired": self._retiring,
            "stopped": "the bench was stopped (bench/STOP)",
            "idle-stopped": f"nothing ran for {self.idle_stop_s / 60:.0f} minutes, so the workbench stopped to free its job slot",
        }.get(state, state)
        if state == "stopped" and not self._retiring:
            self._retiring = reason  # a running cell is interrupted and marked retired
        for worker in self.workers.values():
            worker.close()
        for worker in self.workers.values():
            worker.join(JOIN_TIMEOUT_S)
            worker.retire_note(reason)
        self._write_state(state)

    # ---- what a dead runner left behind ------------------------------------------------

    def sweep(self) -> None:
        store = ProjectStore(self.settings)
        for item in self.inbox.claimed():
            try:
                project_dir = store.require(item.request.project)
            except ProjectError as exc:
                self.inbox.reject(item, str(exc))
                continue
            journal = Journal(project_dir, item.request.project, now=self.now)
            entry = journal.raw_cell(item.request.cid)
            if entry is None:
                self.inbox.requeue(item)  # it never started
            elif entry.final:
                self.inbox.done(item)
            else:
                job = entry.kernel_epoch.split(".")[0] or "unknown"
                journal.write_cell(entry.model_copy(update={
                    "status": "lost", "finished": self.now(), "message": LOST_BY_DEAD_RUNNER.format(job=job),
                }))
                self.inbox.done(item)


def _install_signals(runner: Runner) -> None:
    signal.signal(signal.SIGUSR1, lambda *_: runner.retire(
        "the workbench job is close to its time limit; a successor takes over"))
    signal.signal(signal.SIGTERM, lambda *_: runner.retire("the workbench job was cancelled or hit its time limit"))
    signal.signal(signal.SIGINT, lambda *_: runner.retire("interrupted"))


def main() -> int:
    runner = Runner(load_settings())
    _install_signals(runner)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["Runner", "new_entry", "main"]
