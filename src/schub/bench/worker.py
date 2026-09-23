"""One project's cells, in order, on that project's kernel (a thread in the runner)."""

from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from pydantic import ValidationError

from ..config import Settings
from ..projects import ProjectError, ProjectStore
from .clock import Clock, seconds_between
from .executor import execute
from .filesnap import diff, scan
from .inbox import Claimed, Inbox
from .journal import Journal, JournalError
from .kernels import ProjectKernel, kernel_env, kernel_name
from .ledger import DRAIN_EXPRESSION, parse_user_expression
from .models import Actor, CellEntry, CellRequest, Download, JobRef
from .outputs import OutputCollector

KernelFactory = Callable[..., ProjectKernel]
LOST = ("The kernel stopped while this cell ran (out of memory, or it crashed). Its variables are gone; "
        "files it wrote may be incomplete.")
STOPPED = "The project has a STOP file: nothing runs until it is removed."
PROGRESS_EVERY_S = 1.0


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


class ProjectWorker:
    def __init__(self, host: Host, project: str) -> None:
        self.host = host
        self.project = project
        self.queue: queue.Queue[Claimed | None] = queue.Queue()
        self.kernel: ProjectKernel | None = None
        self.epochs = 0
        self.busy: str | None = None
        self._interrupts: set[str] = set()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name=f"bench-{project}", daemon=True)

    # ---- called by the runner ------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def submit(self, item: Claimed) -> None:
        self.queue.put(item)

    def interrupt(self, cid: str) -> None:
        with self._lock:
            self._interrupts.add(cid)

    def idle(self) -> bool:
        return self.busy is None and self.queue.empty()

    def close(self) -> None:
        self.queue.put(None)

    def join(self, timeout_s: float) -> bool:
        self._thread.join(timeout=timeout_s)
        return not self._thread.is_alive()

    # ---- the thread ----------------------------------------------------------------

    def _loop(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                break
            if self.host.retiring:
                self.host.inbox.requeue(item)  # never started: the successor runs it
                continue
            self.busy = item.request.cid
            try:
                self._run(item)
            except Exception as exc:  # noqa: BLE001 - one broken cell must not stop the project
                self._fail(item, f"sc-hub could not run this cell: {type(exc).__name__}: {exc}")
            finally:
                self.busy = None
                self.host.touch()
        self._shutdown_kernel()

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
        started = self.host.now()
        entry = new_entry(request, journal, status="running", started=started)
        if stop_file(project_dir).exists():
            self._final(journal, item, entry, "interrupted", message=STOPPED)
            return
        try:
            kernel = self._ensure_kernel(project_dir)
        except Exception as exc:  # noqa: BLE001 - reported in the journal
            self._final(journal, item, entry, "error", message=f"the kernel did not start: {exc}")
            return
        entry = entry.model_copy(update={"kernel_epoch": kernel.epoch})
        journal.write_cell(entry)
        self._execute(journal, item, entry, kernel, project_dir)

    def _execute(self, journal: Journal, item: Claimed, entry: CellEntry, kernel: ProjectKernel, project_dir: Path) -> None:
        config = self.host.settings.bench
        before = scan(project_dir, config.snapshot_max_files)
        collector = OutputCollector(journal.folder, entry.cid, config.output_chars)
        last = [0.0]

        def progress() -> None:
            if time.monotonic() - last[0] >= PROGRESS_EVERY_S:
                last[0] = time.monotonic()
                journal.write_cell(entry.model_copy(update={"outputs": collector.snapshot()}))

        result = execute(kernel, item.request.code, collector, poll_s=config.poll_s, on_progress=progress,
                         should_interrupt=lambda: self._should_interrupt(entry.cid, project_dir),
                         user_expressions={"ledger": DRAIN_EXPRESSION})
        after = scan(project_dir, config.snapshot_max_files)
        files = diff(before, after, project_dir, config.snapshot_hash_max_mb * 1024 * 1024)
        downloads, jobs = _events(parse_user_expression(result.user_expressions.get("ledger")))
        status, message = result.status, ""
        if status == "lost":
            message = LOST
            self._shutdown_kernel()
        if self.host.retiring and status in ("interrupted", "lost"):
            status, message = "retired", f"Stopped: {self.host.retiring}"
        elif status == "interrupted" and stop_file(project_dir).exists():
            message = STOPPED
        entry = entry.model_copy(update={
            "outputs": collector.snapshot(), "files": files, "files_truncated": after.truncated or before.truncated,
            "downloads": downloads, "jobs": jobs,
        })
        self._final(journal, item, entry, status, message=message)

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
        found = self._journal(item)
        if found is None:
            return
        journal, _ = found
        entry = new_entry(item.request, journal, status="running", started=self.host.now())
        try:
            self._final(journal, item, entry, "error", message=message)
        except (JournalError, OSError):
            self.host.inbox.reject(item, message)

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
        self.kernel = kernel
        return kernel

    def _shutdown_kernel(self) -> None:
        if self.kernel is not None:
            self.kernel.shutdown()
            self.kernel = None

    def retire_note(self, reason: str) -> None:
        """Tell the project's journal that the kernel is gone (variables lost, files kept)."""
        if self.epochs == 0:
            return
        try:
            project_dir = ProjectStore(self.host.settings).require(self.project)
            journal = Journal(project_dir, self.project, now=self.host.now)
            setup = [e.ref for e in journal.entries(kinds=("cell",)) if getattr(e, "setup", False) and e.status == "ok"]
            replay = f" Setup cells to run again: {', '.join(setup)}." if setup else ""
            journal.add_note("incident", f"The kernel stopped: {reason}. Its variables are gone; files in the project "
                             f"remain.{replay}", actor=Actor(kind="system", client="sc-hub bench"))
        except (ProjectError, JournalError, OSError):
            pass


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
