"""One project's cells, in order, on that project's kernel (a thread in the runner).

The thread never dies of a bad cell: every failure ends as a journal entry. If
something goes wrong after the code reached the kernel, the kernel is shut down
first, so the code does not keep running unseen behind the next cell.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from pydantic import ValidationError

from ..config import Settings
from ..projects import ProjectError, ProjectStore
from .clock import Clock, seconds_between
from .checks import run_checks
from .executor import announce, drain_ledger, execute, prime
from .filesnap import diff, scan
from .inbox import Claimed, Inbox
from .journal import Journal, JournalError
from .kernels import ProjectKernel, kernel_env, kernel_name
from .ledger import parse_user_expression
from .models import Actor, CellEntry, CellRequest, Download, JobRef
from .outputs import OutputCollector

KernelFactory = Callable[..., ProjectKernel]
LOST = ("The kernel stopped while this cell ran (out of memory, or it crashed). Its variables are gone; "
        "files it wrote may be incomplete.")
STOPPED = "The project has a STOP file: nothing runs until it is removed."
PROGRESS_EVERY_S = 1.0
SETUP_SCAN = 200  # newest cells looked at for setup cells in a retire note


class Host(Protocol):
    settings: Settings
    now: Clock
    inbox: Inbox
    job_id: str
    kernel_factory: KernelFactory

    @property
    def retiring(self) -> str: ...

    def touch(self) -> None: ...


def new_entry(request: CellRequest, journal: Journal, **changes: Any) -> CellEntry:
    fields = request.model_dump(exclude={"project", "cid", "created"})
    return CellEntry(ref=journal.ref(request.cid), project=request.project, cid=request.cid,
                     created=request.created, **{**fields, **changes})


def stop_file(project_dir: Path) -> Path:
    return project_dir / "STOP"


def _warn(message: str) -> None:
    sys.stderr.write(f"[bench] {message}\n")


class ProjectWorker:
    def __init__(self, host: Host, project: str) -> None:
        self.host = host
        self.project = project
        self.queue: queue.Queue[Claimed | None] = queue.Queue()
        self.kernel: ProjectKernel | None = None
        self.epochs = 0
        self.busy: str | None = None
        self.closing = False
        self._interrupts: set[str] = set()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name=f"bench-{project}", daemon=True)

    # ---- called by the runner ------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def alive(self) -> bool:
        return self._thread.is_alive()

    def submit(self, item: Claimed) -> None:
        self.queue.put(item)

    def interrupt(self, cid: str) -> None:
        with self._lock:
            self._interrupts.add(cid)

    def idle(self) -> bool:
        return self.busy is None and self.queue.empty()

    def close(self) -> None:
        self.closing = True
        self.queue.put(None)

    def join(self, timeout_s: float) -> bool:
        self._thread.join(timeout=max(0.0, timeout_s))
        return not self._thread.is_alive()

    def take_queued(self) -> list[Claimed]:
        """Items still waiting in this worker's queue (to hand them to a replacement)."""
        items = []
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                return items
            if item is not None:
                items.append(item)

    # ---- the thread ----------------------------------------------------------------

    def _loop(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                break
            try:
                self._handle(item)
            except Exception as exc:  # noqa: BLE001 - the thread must survive anything a cell does
                _warn(f"{self.project}: {type(exc).__name__}: {exc}")
        self._shutdown_kernel()

    def _handle(self, item: Claimed) -> None:
        if self.host.retiring:
            self.host.inbox.requeue(item)  # never started: the successor runs it
            return
        self.busy = item.request.cid
        try:
            self._run(item)
        except Exception as exc:  # noqa: BLE001 - recorded in the journal below
            self._shutdown_kernel()  # the code may still run; stop it before the next cell
            self._fail(item, f"sc-hub could not run this cell: {type(exc).__name__}: {exc}")
        finally:
            self.busy = None
            self.host.touch()

    def _journal(self, item: Claimed) -> tuple[Journal, Path] | None:
        try:
            project_dir = ProjectStore(self.host.settings).require(item.request.project)
        except ProjectError as exc:
            self.host.inbox.reject(item, str(exc))
            return None
        return Journal(project_dir, item.request.project, now=self.host.now), project_dir

    def _run(self, item: Claimed) -> None:
        found = self._journal(item)
        if found is None:
            return
        journal, project_dir = found
        request = item.request
        existing = journal.raw_cell(request.cid)
        if existing is not None and existing.final:
            self.host.inbox.done(item)  # already done by an earlier runner
            return
        entry = new_entry(request, journal, status="running", started=self.host.now())
        if stop_file(project_dir).exists():
            self._final(journal, item, entry, "interrupted", message=STOPPED)
            return
        try:
            kernel = self._ensure_kernel(project_dir)
        except Exception as exc:  # noqa: BLE001 - reported in the journal
            self._final(journal, item, entry, "error", message=f"the kernel did not start: {exc}")
            return
        if self.host.retiring:
            self.host.inbox.requeue(item)  # still not started: hand it over
            return
        entry = entry.model_copy(update={"kernel_epoch": kernel.epoch})
        journal.write_cell(entry)
        self._execute(journal, item, entry, kernel, project_dir)

    def _execute(self, journal: Journal, item: Claimed, entry: CellEntry, kernel: ProjectKernel, project_dir: Path) -> None:
        config = self.host.settings.bench
        before = scan(project_dir, config.snapshot_max_files)
        collector = OutputCollector(journal.folder, entry.cid, config.output_chars)
        announce(kernel, entry.ref, [c.model_dump() for c in entry.checks])
        result = execute(kernel, item.request.code, collector, poll_s=config.poll_s,
                         on_progress=self._progress(journal, entry, collector),
                         should_interrupt=lambda: self._should_interrupt(entry.cid, project_dir),
                         should_kill=lambda: bool(self.host.retiring))
        after = scan(project_dir, config.snapshot_max_files)
        files = diff(before, after, project_dir, config.snapshot_hash_max_mb * 1024 * 1024)
        events = [] if result.status == "lost" else parse_user_expression(drain_ledger(kernel))
        downloads, jobs = _events(events)
        bricks = tuple(f"{e.get('brick')} {e.get('version')} {str(e.get('code_id', ''))[:12]}"
                       for e in events if e.get("kind") == "note" and e.get("brick"))
        status, message = self._outcome(result.status, project_dir)
        # A cell that sent a job is checked by the job when it ends (its outputs appear then).
        checks = run_checks(self.host.settings, entry.project, entry.checks) \
            if status == "ok" and entry.checks and not jobs else ()
        entry = entry.model_copy(update={
            "outputs": collector.snapshot(), "files": files, "files_truncated": after.truncated or before.truncated,
            "downloads": downloads, "jobs": jobs, "check_results": checks, "bricks": bricks,
        })
        self._final(journal, item, entry, status, message=message)

    def _progress(self, journal: Journal, entry: CellEntry, collector: OutputCollector) -> Callable[[], None]:
        last = [0.0]

        def progress() -> None:
            if time.monotonic() - last[0] < PROGRESS_EVERY_S:
                return
            last[0] = time.monotonic()
            try:
                journal.write_cell(entry.model_copy(update={"outputs": collector.snapshot()}))
            except (OSError, JournalError) as exc:  # progress is a courtesy; the final write matters
                _warn(f"{entry.ref}: progress not written: {exc}")

        return progress

    def _outcome(self, status: str, project_dir: Path) -> tuple[str, str]:
        if status == "lost":
            self._shutdown_kernel()
        if self.host.retiring and status in ("interrupted", "lost"):
            return "retired", f"Stopped: {self.host.retiring}"
        if status == "lost":
            return status, LOST
        if status == "interrupted" and stop_file(project_dir).exists():
            return status, STOPPED
        return status, ""

    def _should_interrupt(self, cid: str, project_dir: Path) -> bool:
        with self._lock:
            asked = cid in self._interrupts
        return asked or bool(self.host.retiring) or stop_file(project_dir).exists()

    def _final(self, journal: Journal, item: Claimed, entry: CellEntry, status: str, message: str = "") -> None:
        finished = self.host.now()
        duration = seconds_between(entry.started, finished) if entry.started else None
        journal.write_cell(entry.model_copy(update={
            "status": status, "finished": finished, "duration_s": duration, "message": message,
        }))
        self.host.inbox.done(item)
        with self._lock:
            self._interrupts.discard(entry.cid)

    def _fail(self, item: Claimed, message: str) -> None:
        try:
            found = self._journal(item)
            if found is None:
                return
            journal, _ = found
            existing = journal.raw_cell(item.request.cid)
            if existing is not None and existing.final:
                self.host.inbox.done(item)
                return
            entry = existing or new_entry(item.request, journal, started=self.host.now())
            self._final(journal, item, entry.model_copy(update={"started": entry.started or self.host.now()}),
                        "error", message=message)
        except Exception as exc:  # noqa: BLE001 - last resort: keep the request, with the reason
            _warn(f"{item.request.project}#{item.request.cid}: could not record the failure: {exc}")
            self.host.inbox.reject(item, message)

    def force_retire(self, reason: str) -> None:
        """Shutdown ran out of time: kill the kernel and close the busy cell (runner thread)."""
        cid = self.busy
        self._shutdown_kernel()
        if cid is None:
            return
        try:
            project_dir = ProjectStore(self.host.settings).require(self.project)
            journal = Journal(project_dir, self.project, now=self.host.now)
            entry = journal.raw_cell(cid)
            if entry is not None and not entry.final:
                journal.write_cell(entry.model_copy(update={
                    "status": "retired", "finished": self.host.now(), "message": f"Stopped: {reason}"}))
        except (ProjectError, JournalError, OSError) as exc:
            _warn(f"{self.project}#{cid}: not closed on shutdown: {exc}")

    # ---- the kernel ----------------------------------------------------------------

    def _ensure_kernel(self, project_dir: Path) -> ProjectKernel:
        if self.kernel is not None and self.kernel.alive():
            return self.kernel
        self._shutdown_kernel()
        self.epochs += 1
        epoch = f"{self.host.job_id}.{self.epochs}"
        env = kernel_env(os.environ, {
            "SCHUB_PROJECT": self.project, "SCHUB_PROJECT_DIR": str(project_dir), "SCHUB_KERNEL_EPOCH": epoch,
        })
        kernel = self.host.kernel_factory(kernel_name(self.host.settings, self.project), project_dir / "work", env, epoch)
        kernel.start()
        if not prime(kernel):
            _warn(f"{self.project}: sc-hub is not importable in the kernel; bench.* and %%slurm are unavailable")
        self.kernel = kernel
        return kernel

    def _shutdown_kernel(self) -> None:
        kernel, self.kernel = self.kernel, None
        if kernel is not None:
            kernel.shutdown()

    def retire_note(self, reason: str) -> None:
        """Tell the project's journal that the kernel is gone (variables lost, files kept)."""
        if self.epochs == 0:
            return
        try:
            project_dir = ProjectStore(self.host.settings).require(self.project)
            journal = Journal(project_dir, self.project, now=self.host.now)
            setup = [e.ref for e in journal.latest_cells(SETUP_SCAN) if e.setup and e.status == "ok"]
            replay = f" Setup cells to run again: {', '.join(setup)}." if setup else ""
            journal.add_note("incident", f"The kernel stopped: {reason}. Its variables are gone; files in the project "
                             f"remain.{replay}", actor=Actor(kind="system", client="sc-hub bench"))
        except (ProjectError, JournalError, OSError) as exc:
            _warn(f"{self.project}: retire note not written: {exc}")


def _events(events: list[dict[str, Any]]) -> tuple[tuple[Download, ...], tuple[JobRef, ...]]:
    downloads, jobs = [], []
    for event in events:
        payload = {k: v for k, v in event.items() if k != "kind"}
        try:
            if event["kind"] == "download":
                downloads.append(Download.model_validate(payload))
            elif event["kind"] == "job":
                jobs.append(JobRef.model_validate(payload))
        except ValidationError:
            continue
    return tuple(downloads), tuple(jobs)
