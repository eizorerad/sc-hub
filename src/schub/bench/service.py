"""The bench as the MCP tools and the CLI see it: run a cell, wait for it, read the
journal, stop things. Everything here runs on the login node and only reads and
writes small files or calls Slurm; cells run in the workbench job.
"""

from __future__ import annotations

import difflib
import time
from typing import Callable, Sequence

from pydantic import ValidationError

from ..config import Settings
from ..projects import ProjectError, ProjectMeta, ProjectStore
from ..slurm import Slurm, SlurmError
from .checkpoint import CheckpointError, CheckpointStore, check_handoff
from .clock import Clock, stamp
from .inbox import Inbox
from .jobs import FINAL_JOB_STATES, lookup
from .journal import Journal, JournalError, parse_ref
from .models import Actor, CellEntry, CellRequest, Checkpoint, CheckSpec, NoteEntry, WaitingJob
from .numbers import unresolved
from .report_build import ReportError, build as build_report
from .report_spec import ReportAnswer, ReportSpec
from .report_store import ReportStore
from .results import CellResult, cell_result, waiting_result
from .slots import slot_usage
from .views import JournalView, ProjectCard, journal_view, project_cards
from .workbench import BenchStopped, Workbench


NUMBERED_KINDS = ("finding", "verdict", "decision")
JOB_POLL_S = 5.0


def _unreported(entry: CellEntry) -> list:
    """The cell's jobs without a final state in the journal yet."""
    return [j for j in entry.jobs if j.state not in FINAL_JOB_STATES]


def _open(entry: CellEntry) -> list:
    """Jobs still queued or running (a job gone from the queue without a report is not waited for)."""
    return [j for j in _unreported(entry) if not j.state.startswith("ENDED")]


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
        if actor is not None and actor.role == "writer" and code.lstrip().startswith("%%slurm"):
            raise BenchError("the report writer does not send Slurm jobs: the report shows what the study already ran")
        request = self._request(journal, project, code, why, expect, checks, setup, data_scope, actor)
        self.inbox.submit(request)
        try:
            self.workbench.ensure()
        except (BenchStopped, SlurmError) as exc:
            return waiting_result(journal.ref(request.cid), "queued", f"the workbench could not start: {exc}")
        return self.wait(journal.ref(request.cid), wait_s, for_jobs=False)  # a %%slurm cell returns at once

    def _request(self, journal: Journal, project: str, code: str, why: str, expect: str,
                 checks: Sequence[CheckSpec], setup: bool, data_scope: str, actor: Actor | None) -> CellRequest:
        from .checks import canonical

        checks = canonical(checks)
        fields = dict(project=project, code=code, why=why, expect=expect, checks=tuple(checks), setup=setup,
                      data_scope=data_scope, actor=actor or Actor(), created=self.now())
        try:
            CellRequest(cid="c0000", **fields)  # validate before an id is spent
        except ValidationError as exc:
            raise BenchError("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())) from exc
        _validate_checks(checks)
        return CellRequest(cid=journal.allocate("c"), **fields)

    def wait(self, ref: str, wait_s: float | None = None, for_jobs: bool = True) -> CellResult:
        """The cell's result once it is final and (for_jobs) its Slurm jobs have reported, or at the deadline."""
        project, cid = self._cell_ref(ref)
        journal = self.journal(project)
        deadline = self.monotonic() + (self.settings.bench.run_wait_s if wait_s is None else wait_s)
        queue_checked, jobs_open = -JOB_POLL_S, True
        while True:
            entry = journal.cell(cid)
            if entry is not None and entry.final:
                if not for_jobs or not _unreported(entry):
                    break
                if self.monotonic() - queue_checked >= JOB_POLL_S:  # squeue only every few seconds
                    queue_checked, jobs_open = self.monotonic(), bool(_open(self._live_jobs(entry)))
                if not jobs_open:
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
        return cell_result(self._live_jobs(entry), previous, self._where() if not entry.final else "",
                           self._setup_refs(journal))

    def _live_jobs(self, entry: CellEntry) -> CellEntry:
        """Current Slurm states of the cell's jobs that have not reported yet (not stored)."""
        open_jobs = [j.job_id for j in entry.jobs if j.state not in FINAL_JOB_STATES]
        if not open_jobs:
            return entry
        try:
            states = self.slurm.states(open_jobs)
        except SlurmError:
            return entry
        jobs = tuple(j.model_copy(update={"state": states.get(j.job_id, "ENDED (no result yet)")})
                     if j.job_id in open_jobs else j for j in entry.jobs)
        return entry.model_copy(update={"jobs": jobs})

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
        journal = self.journal(project)
        try:
            missing = unresolved(text, journal.evidence(because)) if kind in NUMBERED_KINDS else ()
            return journal.add_note(kind, text, because, reverses_if, verdict, audience, actor, missing)
        except (JournalError, ValidationError) as exc:
            raise BenchError(str(exc)) from exc

    def handoff(self, project: str, text: str, disposition: str, next_action: str = "",
                waiting_jobs: Sequence[str] = (), actor: Actor | None = None) -> Checkpoint:
        journal = self.journal(project)
        if actor is not None and actor.role == "writer":
            raise BenchError("the report writer does not change the hand-over: the research stays complete. Publish "
                             "the report with report(project, spec, publish=true)")
        store = CheckpointStore(self.projects.path_of(project), now=self.now)
        try:
            check_handoff(text)  # both or neither: a bad text must not leave a new checkpoint behind
            checkpoint = store.write(disposition, next_action, [WaitingJob(job_id=j) for j in waiting_jobs],
                                     actor=actor)
            store.write_handoff(text)
        except (CheckpointError, ValidationError) as exc:
            raise BenchError(str(exc)) from exc
        first = text.strip().splitlines()[0][:300]
        journal.add_note("handoff", f"{disposition}: {first}", actor=actor)
        return checkpoint

    def report(self, project: str, spec: ReportSpec | None = None, publish: bool = False,
               actor: Actor | None = None) -> ReportAnswer:
        """Build a report from `spec` (a draft, or published), or list the published ones."""
        journal = self.journal(project)
        store = ReportStore(self.projects.path_of(project))
        if spec is None:
            return ReportAnswer(status="listed", reports=tuple(store.published()),
                                hint="Write one with report(project, spec); skills('report_writing') explains how.")
        question = self.projects.meta(project).question
        try:
            built = build_report(journal, project, question, spec, actor or Actor(), self.now())
        except ReportError as exc:
            raise BenchError(f"the report was not built: {exc}") from exc
        return (store.publish if publish else store.draft)(built, spec, actor or Actor(), self.now())

    # ---- the workbench ------------------------------------------------------------------

    def status(self) -> str:
        return self._where()

    def stop_workbench(self) -> str:
        cancelled = self.workbench.stop()
        if not cancelled:
            return self.workbench.state().summary()
        return (f"Stopped the workbench (job {', '.join(cancelled)}): its kernel variables are gone, files remain. "
                "The next cell starts a new one.")

    def stop(self, target: str) -> str:
        """A cell ref (interrupt it) or "workbench" (free its slot now)."""
        target = target.strip()
        if target == "workbench":
            return self.stop_workbench()
        if "#" in target:
            return self.interrupt(target)
        if target.isdigit():
            return self.cancel_job(target)
        raise BenchError("stop takes a cell reference like 'project#c0007', a job id, or 'workbench'")

    def cancel_job(self, job_id: str) -> str:
        """Only jobs the bench submitted (a %%slurm cell) can be cancelled here."""
        record = lookup(self.settings, job_id)
        if record is None:
            raise BenchError(f"job {job_id} was not sent by the bench; sc-hub only cancels its own jobs")
        self.slurm.cancel([job_id])
        return f"cancelled job {job_id} of {record.ref}"


def _validate_checks(checks: Sequence[CheckSpec]) -> None:
    """A misspelt check fails now, not after a job of several hours."""
    from .checks import registry

    known = registry()
    for spec in checks:
        check = known.get(spec.name)
        if check is None:  # agents guess names like file_exists: answer with the right one at once
            close = difflib.get_close_matches(spec.name, known, n=1, cutoff=0.4)
            hint = f" Did you mean {close[0]!r}?" if close else ""
            raise BenchError(f"no check {spec.name!r}.{hint} Checks: {', '.join(sorted(known))} "
                             "(skills('checks') has their parameters)")
        try:
            check.params.model_validate(spec.params)
        except ValidationError as exc:
            error = exc.errors()[0]
            raise BenchError(f"check {spec.name}: {'.'.join(map(str, error['loc']))}: {error['msg']}") from exc


def _previous_epoch(journal: Journal, entry: CellEntry) -> str:
    """The kernel epoch of the last cell before `entry` that ran (to detect a restart)."""
    earlier = [e for e in journal.entries(kinds=("cell",))
               if isinstance(e, CellEntry) and e.kernel_epoch and e.created < entry.created]
    return earlier[-1].kernel_epoch if earlier else ""
