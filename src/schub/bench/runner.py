"""The workbench process: takes cell requests from the inbox and runs them, one
thread and one kernel per project.

    python -m schub.bench.runner          (inside the workbench Slurm job)

It first re-executes itself with an allowlisted environment, so a kernel reading
its parent's /proc/<pid>/environ finds no secrets. It stops on its own when idle,
when bench/STOP exists, or when Slurm signals the end of the job (SIGUSR1 before
the limit, SIGTERM on scancel). Then a running cell is marked `retired`, requests
that never started go back to the inbox, and each used project gets a journal note
that the kernel's variables are gone. The whole shutdown fits well inside Slurm's
KillWait. On start it sweeps what dead runners left behind (each runner claims into
its own folder): unfinished cells become `lost`, unstarted ones return to the inbox.
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
from ..locking import LockTimeout, exclusive
from ..projects import ProjectError, ProjectStore
from ..slurm import ACTIVE_STATES, Slurm, SlurmError
from .clock import Clock, seconds_between, stamp
from .fsio import read_json, write_json_atomic
from .inbox import Claimed, Inbox
from .journal import Journal, JournalError
from .kernels import RUNNER_CLEAN, ProjectKernel, runner_env
from .worker import KernelFactory, ProjectWorker, new_entry

HEARTBEAT_S = 10.0
STALE_AFTER_S = 60.0
SHUTDOWN_S = 20.0  # Slurm's KillWait is 30 s by default
WAIT_FOR_OTHER_S = 120.0
RUNNER_LOCK_STALE_S = 90.0
MAX_KERNELS = 6  # live project kernels in one workbench; the least recently used idle one goes first
KERNEL_IDLE_S = 90 * 60  # an idle kernel older than this is shut down even below MAX_KERNELS  # runner.lock untouched this long: its holder died (it is touched every HEARTBEAT_S)
MAX_FAILURES = 20
EXIT_OK, EXIT_BUSY, EXIT_CRASHED = 0, 3, 4
LIVE_STATES = ("running", "retiring")
LOST_BY_DEAD_RUNNER = ("The workbench stopped while this cell ran (job {job}). Its variables are gone; files it "
                       "wrote may be incomplete. Run it again if you need its result.")


def _warn(message: str) -> None:
    sys.stderr.write(f"[bench] {message}\n")


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
        slurm: Slurm | None = None,
        wait_for_other_s: float = WAIT_FOR_OTHER_S,
    ) -> None:
        self.settings = settings
        self.wait_for_other_s = wait_for_other_s
        self.now = now
        self.kernel_factory = kernel_factory
        self.inbox = Inbox(settings.bench_dir)
        self.job_id = job_id or os.environ.get("SLURM_JOB_ID") or f"local-{os.getpid()}"
        self.node = node or socket.gethostname().split(".")[0]
        self.idle_stop_s = idle_stop_s if idle_stop_s is not None else settings.bench.idle_stop_min * 60
        self.monotonic = monotonic
        self.slurm = slurm or Slurm()
        self.successor: str | None = None
        self.workers: dict[str, ProjectWorker] = {}
        self.started = now()
        self._arm_successor = False
        self._retiring = ""
        self._stop_reason = ""
        self._last_activity = monotonic()
        self._lock = threading.Lock()

    # ---- state shared with workers ---------------------------------------------------

    @property
    def retiring(self) -> str:
        return self._retiring

    def retire(self, reason: str, successor: bool = False) -> None:
        """Called from a signal handler: finish soon; near the time limit, hand over to a successor."""
        self._retiring = self._retiring or reason
        self._arm_successor = self._arm_successor or successor

    def touch(self) -> None:
        with self._lock:
            self._last_activity = self.monotonic()

    @property
    def state_path(self):
        return self.settings.bench_dir / "workbench.json"

    def _write_state(self, state: str) -> None:
        projects = {}
        for name, worker in list(self.workers.items()):
            kernel = worker.kernel  # read once: the worker may drop it meanwhile
            projects[name] = {"epoch": kernel.epoch if kernel else "", "busy": worker.busy}
        try:
            write_json_atomic(self.state_path, {
                "job_id": self.job_id, "node": self.node, "pid": os.getpid(), "started": self.started,
                "heartbeat": self.now(), "state": state, "reason": self._stop_reason or self._retiring,
                "projects": projects, "successor": self.successor,
            })
        except OSError as exc:
            _warn(f"state not written: {exc}")

    # ---- other runners -------------------------------------------------------------

    def _record_alive(self, owner: str | None = None) -> bool:
        record = read_json(self.state_path)
        if not record or record.get("state") not in LIVE_STATES or record.get("job_id") == self.job_id:
            return False
        if owner is not None and record.get("job_id") != owner:
            return False
        try:
            return seconds_between(str(record.get("heartbeat")), self.now()) < STALE_AFTER_S
        except (TypeError, ValueError):
            return False

    def other_runner_alive(self) -> bool:
        return self._record_alive()

    def owner_alive(self, owner: str) -> bool:
        """Is the runner that claimed into claimed/<owner>/ still around?"""
        if owner == self.job_id:
            return True
        if self._record_alive(owner):
            return True  # its heartbeat is fresh, whatever Slurm says this minute
        if owner.isdigit():
            try:
                return self.slurm.states([owner]).get(owner) in ACTIVE_STATES
            except SlurmError:
                return True  # unsure: never sweep what may still be running
        return self._record_alive(owner)

    def _wait_for_other(self) -> bool:
        deadline = self.monotonic() + self.wait_for_other_s
        while self.other_runner_alive():
            if self.monotonic() >= deadline:
                return False
            time.sleep(min(5.0, self.settings.bench.poll_s * 10))
        return True

    # ---- lifecycle ---------------------------------------------------------------------

    def run(self) -> int:
        if not self._wait_for_other():
            return EXIT_BUSY
        self.settings.bench_dir.mkdir(parents=True, exist_ok=True)
        # Two workbench jobs starting in the same minute would split a project's cells over two kernels:
        # runner.lock (atomic on Lustre, unlike flock) lets only one run.
        try:
            with exclusive(self.settings.bench_dir / "runner.lock", wait_s=self.wait_for_other_s,
                           stale_after_s=RUNNER_LOCK_STALE_S, heartbeat_s=HEARTBEAT_S):
                return self._run_locked()
        except LockTimeout:
            return EXIT_BUSY

    def _run_locked(self) -> int:
        state = "crashed"
        try:
            self._write_state("running")
            self.sweep()
            state = self._loop()
        finally:
            self._shutdown(state)
        return EXIT_CRASHED if state == "crashed" else EXIT_OK

    def _loop(self) -> str:
        next_beat, failures = 0.0, 0
        while True:
            if self._retiring:
                return "retired"
            if (self.settings.bench_dir / "STOP").exists():
                self._stop_reason = "bench/STOP exists"
                return "stopped"
            try:
                next_beat = self._tick(next_beat)
                failures = 0
            except OSError as exc:  # Lustre hiccups: ESTALE, EIO, ENOSPC...
                failures += 1
                _warn(f"loop: {exc} ({failures}/{MAX_FAILURES})")
                if failures >= MAX_FAILURES:
                    self._stop_reason = f"the file system kept failing: {exc}"
                    return "crashed"
                time.sleep(min(30.0, 2.0 ** failures))
                continue
            if self._idle_too_long():
                self._stop_reason = f"idle for {self.idle_stop_s / 60:.0f} min"
                return "idle-stopped"
            time.sleep(self.settings.bench.poll_s)

    def _tick(self, next_beat: float) -> float:
        for item in self.inbox.claim(self.job_id):
            self._dispatch(item)
        for project, cid, _action in self.inbox.take_controls():
            if project in self.workers:
                self.workers[project].interrupt(cid)
        self._replace_dead_workers()
        self._evict_kernels()
        if self.monotonic() >= next_beat:
            self._write_state("running")
            return self.monotonic() + HEARTBEAT_S
        return next_beat

    def _dispatch(self, item: Claimed) -> None:
        self.touch()
        project = item.request.project
        worker = self.workers.get(project)
        if worker is None:
            worker = self._new_worker(project)
        worker.submit(item)

    def _new_worker(self, project: str) -> ProjectWorker:
        worker = ProjectWorker(self, project)
        self.workers[project] = worker
        worker.start()
        return worker

    def _evict_kernels(self) -> None:
        live = [w for w in self.workers.values() if w.kernel is not None and not w.closing]
        idle = sorted((w for w in live if w.idle()), key=lambda w: w.last_used)
        excess = max(0, len(live) - MAX_KERNELS)
        now = time.monotonic()
        for index, worker in enumerate(idle):
            if index < excess or now - worker.last_used > KERNEL_IDLE_S:
                worker.last_used = now  # asked once; the thread decides when its queue is empty
                worker.evict()

    def _replace_dead_workers(self) -> None:
        for project, worker in list(self.workers.items()):
            if worker.alive() or worker.closing:
                continue
            _warn(f"{project}: worker thread died; starting a new one")
            replacement = self._new_worker(project)
            for item in worker.take_queued():
                replacement.submit(item)

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
            "crashed": f"the workbench failed ({self._stop_reason or 'an internal error'})",
        }.get(state, state)
        self._write_state("retiring")
        if state != "idle-stopped" and not self._retiring:
            self._retiring = reason  # a running cell is interrupted and marked retired
        if state == "retired" and self._arm_successor and self._recently_active():
            self.successor = self._arm()
        self._stop_workers(reason)
        if state in ("idle-stopped", "retired") and not self.successor and self.inbox.pending():
            self.successor = self._arm()  # a cell arrived while this runner was leaving: do not strand it
        self._write_state(state)

    def _stop_workers(self, reason: str) -> None:
        for worker in self.workers.values():
            worker.close()
        deadline = self.monotonic() + SHUTDOWN_S
        for worker in self.workers.values():
            if not worker.join(deadline - self.monotonic()):
                worker.force_retire(reason)
            worker.retire_note(reason)

    def _recently_active(self) -> bool:
        busy = any(not w.idle() for w in self.workers.values())
        with self._lock:
            return busy or self.monotonic() - self._last_activity < self.idle_stop_s

    def _arm(self) -> str | None:
        from .workbench import Workbench

        if not self.job_id.isdigit():
            return None  # not a Slurm job (tests, local runs)
        try:
            return Workbench(self.settings, self.slurm, self.now).arm_successor(self.job_id)
        except (SlurmError, OSError):
            return None  # the watchdog starts one when cells wait

    # ---- what dead runners left behind -------------------------------------------------

    def sweep(self) -> int:
        """Handle the claims of runners that are gone; returns how many there were."""
        store = ProjectStore(self.settings)
        handled = 0
        for owner in self.inbox.owners():
            if self.owner_alive(owner):
                continue
            for item in self.inbox.claimed(owner):
                adopted = self.inbox.adopt(item, self.job_id)  # a rename: two sweepers never handle one claim
                if adopted is not None:
                    self._sweep_one(store, adopted, dead=owner)
                    handled += 1
            try:
                (self.settings.bench_dir / "claimed" / owner).rmdir()
            except OSError:
                pass
        return handled

    def _sweep_one(self, store: ProjectStore, item: Claimed, dead: str) -> None:
        try:
            project_dir = store.require(item.request.project)
        except ProjectError as exc:
            self.inbox.reject(item, str(exc))
            return
        journal = Journal(project_dir, item.request.project, now=self.now)
        entry = journal.raw_cell(item.request.cid)
        if entry is None:
            self.inbox.requeue(item)  # it never started
            return
        if not entry.final:
            try:
                journal.write_cell(entry.model_copy(update={
                    "status": "lost", "finished": self.now(),
                    "message": LOST_BY_DEAD_RUNNER.format(job=dead),
                }))
            except JournalError:
                pass  # it became final meanwhile
        self.inbox.done(item)


def _install_signals(runner: Runner) -> None:
    signal.signal(signal.SIGUSR1, lambda *_: runner.retire(
        "the workbench job is close to its time limit; a successor takes over", successor=True))
    signal.signal(signal.SIGTERM, lambda *_: runner.retire("the workbench job was cancelled or hit its time limit"))
    signal.signal(signal.SIGINT, lambda *_: runner.retire("interrupted"))


def main() -> int:
    if os.environ.get(RUNNER_CLEAN) != "1":
        os.execve(sys.executable, [sys.executable, "-m", "schub.bench.runner"], runner_env(os.environ))
    runner = Runner(load_settings())
    _install_signals(runner)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["Runner", "new_entry", "main"]
