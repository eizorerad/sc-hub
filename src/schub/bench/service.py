"""The bench as the MCP tools and the CLI see it: run a cell, wait for it, read the
journal, stop things. Everything here runs on the login node and only reads and
writes small files or calls Slurm; cells run in the workbench job.
"""

from __future__ import annotations

import time
from typing import Callable, Sequence

from pydantic import ValidationError

from ..config import Settings
from ..projects import ProjectError, ProjectMeta, ProjectStore
from ..slurm import Slurm, SlurmError
from .checkpoint import CheckpointError, CheckpointStore
from .clock import Clock, stamp
from .inbox import Inbox
from .journal import Journal, JournalError, parse_ref
from .models import Actor, CellEntry, CellRequest, Checkpoint, CheckSpec, NoteEntry, WaitingJob
from .results import CellResult, cell_result, waiting_result
from .slots import slot_usage
from .views import JournalView, ProjectCard, journal_view, project_cards
from .workbench import BenchStopped, Workbench


class BenchError(ValueError):
    pass


class BenchService:
    def __init__(
        self,
        settings: Settings,
        slurm: Slurm | None = None,
        now: Clock = stamp,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.slurm = slurm or Slurm()
        self.now = now
        self.sleep = sleep
        self.monotonic = monotonic
        self.inbox = Inbox(settings.bench_dir)
        self.workbench = Workbench(settings, self.slurm, now)
        self.projects = ProjectStore(settings)

    def journal(self, project: str) -> Journal:
        try:
            return Journal(self.projects.require(project), project, now=self.now)
        except ProjectError as exc:
            raise BenchError(str(exc)) from exc

    # ---- cells --------------------------------------------------------------------------

    def run(
        self,
        project: str,
        code: str,
        why: str,
        expect: str,
        checks: Sequence[CheckSpec] = (),
        setup: bool = False,
        data_scope: str = "unknown",
        actor: Actor | None = None,
        wait_s: float | None = None,
    ) -> CellResult:
        journal = self.journal(project)
        if (self.projects.path_of(project) / "STOP").exists():
            raise BenchError(f"project '{project}' has a STOP file; the student removes it to continue")
        if self.workbench.stopped():
            raise BenchError("the bench is stopped (bench/STOP exists); the student removes it to continue")
        request = self._request(journal, project, code, why, expect, checks, setup, data_scope, actor)
        self.inbox.submit(request)
        try:
            self.workbench.ensure()
        except (BenchStopped, SlurmError) as exc:
            return waiting_result(journal.ref(request.cid), "queued", f"the workbench could not start: {exc}")
        return self.wait(journal.ref(request.cid), wait_s)

    def _request(self, journal: Journal, project: str, code: str, why: str, expect: str,
                 checks: Sequence[CheckSpec], setup: bool, data_scope: str, actor: Actor | None) -> CellRequest:
        fields = dict(project=project, code=code, why=why, expect=expect, checks=tuple(checks), setup=setup,
                      data_scope=data_scope, actor=actor or Actor(), created=self.now())
        try:
            CellRequest(cid="c0000", **fields)  # validate before an id is spent
        except ValidationError as exc:
            raise BenchError("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from exc
        return CellRequest(cid=journal.allocate("c"), **fields)

    def wait(self, ref: str, wait_s: float | None = None) -> CellResult:
        project, cid = self._cell_ref(ref)
        journal = self.journal(project)
        deadline = self.monotonic() + (self.settings.bench.run_wait_s if wait_s is None else wait_s)
        while True:
            entry = journal.cell(cid)
            if entry is not None and entry.final:
                break
            if self.monotonic() >= deadline:
                break
            self.sleep(self.settings.bench.poll_s)
        return self._result(journal, cid, entry)

    def _cell_ref(self, ref: str) -> tuple[str, str]:
        try:
            project, record_id = parse_ref(ref)
        except JournalError as exc:
            raise BenchError(str(exc)) from exc
        if not record_id.startswith("c"):
            raise BenchError(f"{ref} is a note, not a cell")
        return project, record_id

    def _result(self, journal: Journal, cid: str, entry: CellEntry | None) -> CellResult:
        ref = journal.ref(cid)
        if entry is None:
            rejected = self.inbox.rejected_reason(journal.project, cid)
            if rejected is not None:
                return waiting_result(ref, "rejected", rejected)
            if not (journal.folder / "ids" / cid).exists():
                raise BenchError(f"{ref} does not exist")
            return waiting_result(ref, "queued", self._where())
        previous = _previous_epoch(journal, entry)
        return cell_result(entry, previous, self._where() if not entry.final else "", self._setup_refs(journal))

    def _where(self) -> str:
        try:
            state = self.workbench.state().summary()
            slots = slot_usage(self.settings, self.slurm).summary()
        except SlurmError as exc:
            return f"Slurm did not answer: {exc}"
        return f"{state} Slots: {slots}."

    def _setup_refs(self, journal: Journal) -> tuple[str, ...]:
        return tuple(e.ref for e in journal.entries(kinds=("cell",)) if isinstance(e, CellEntry) and e.setup
                     and e.status == "ok")

    def interrupt(self, ref: str) -> str:
        project, cid = self._cell_ref(ref)
        self.journal(project)
        self.inbox.control(project, cid, "interrupt")
        return f"asked the workbench to interrupt {ref}"

    # ---- projects and the journal ---------------------------------------------------------

    def projects_list(self) -> list[ProjectCard]:
        return project_cards(self.settings)

    def create_project(self, project: str, question: str, datasets: Sequence[str] = ()) -> ProjectMeta:
        try:
            meta = self.projects.create(project, question, tuple(datasets))
        except ProjectError as exc:
            raise BenchError(str(exc)) from exc
        (self.projects.path_of(project) / "work").mkdir(parents=True, exist_ok=True)
        return meta

    def journal_view(self, project: str, since: str | None = None, kinds: Sequence[str] | None = None,
                     limit: int = 20, max_chars: int = 12_000) -> JournalView:
        self.journal(project)  # a clear error for an unknown project
        return journal_view(self.settings, project, since, kinds, max(1, min(limit, 100)), max_chars)

    def note(self, project: str, kind: str, text: str, because: Sequence[str] = (), reverses_if: str = "",
             verdict: str | None = None, audience: str = "both", actor: Actor | None = None) -> NoteEntry:
        if kind == "handoff":
            raise BenchError("write the hand-over with handoff(), not as a note")
        try:
            return self.journal(project).add_note(kind, text, because, reverses_if, verdict, audience, actor)
        except (JournalError, ValidationError) as exc:
            raise BenchError(str(exc)) from exc

    def handoff(self, project: str, text: str, disposition: str, next_action: str = "",
                waiting_jobs: Sequence[str] = (), actor: Actor | None = None) -> Checkpoint:
        journal = self.journal(project)
        store = CheckpointStore(self.projects.path_of(project), now=self.now)
        try:
            checkpoint = store.write(disposition, next_action, [WaitingJob(job_id=j) for j in waiting_jobs],
                                     actor=actor)
            store.write_handoff(text)
        except (CheckpointError, ValidationError) as exc:
            raise BenchError(str(exc)) from exc
        first = text.strip().splitlines()[0][:300]
        journal.add_note("handoff", f"{disposition}: {first}", actor=actor)
        return checkpoint

    # ---- the workbench ------------------------------------------------------------------

    def status(self) -> str:
        return self._where()

    def stop_workbench(self) -> str:
        return self.workbench.stop().summary()

    def stop(self, target: str) -> str:
        """A cell ref (interrupt it) or "workbench" (free its slot now)."""
        target = target.strip()
        if target == "workbench":
            return self.stop_workbench()
        if "#" in target:
            return self.interrupt(target)
        raise BenchError("stop takes a cell reference like 'project#c0007' or 'workbench'")


def _previous_epoch(journal: Journal, entry: CellEntry) -> str:
    """The kernel epoch of the last cell before `entry` that ran (to detect a restart)."""
    earlier = [e for e in journal.entries(kinds=("cell",))
               if isinstance(e, CellEntry) and e.kernel_epoch and e.created < entry.created]
    return earlier[-1].kernel_epoch if earlier else ""
