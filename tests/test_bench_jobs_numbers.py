from __future__ import annotations

import errno
import json
import os
import time
from pathlib import Path

import pytest

from schub.bench import jobs as jobs_module
from schub.bench import journal as journal_module
from schub.bench.jobs import JobRecord, lookup, open_records, reap, register
from schub.bench.journal import Journal
from schub.bench.models import CellEntry, JobRef, OutputItem
from schub.bench.numbers import claimed_numbers, unresolved
from schub.bench.service import BenchError, BenchService
from schub.config import Settings
from schub.projects import ProjectStore
from schub.slurm import Slurm
from tests.conftest import FakeCluster


def test_numbers_that_need_tracing() -> None:
    assert claimed_numbers("24,673 cells in 2018, 8 donors, median 519 genes, 87% hemoglobin, p=0.012") == \
        ["24,673", "519", "87%", "0.012"]
    evidence = ["n_cells 24673\nmedian genes 519.0\nhb share 0.8712\np 0.0121"]
    assert unresolved("24,673 cells; median 519; 87% of counts; p = 0.012", evidence) == ()
    assert unresolved("median 530 genes and 12,000 cells", evidence) == ("530", "12,000")
    assert claimed_numbers("p = 1e-5 and 5e-3") == ["1e-5", "5e-3"]
    small = ["padj 1.2e-05\nlfc 3.20\ncount 1.2e+03"]
    assert unresolved("padj 1.2e-5, lfc 3.2, 1,200 genes", small) == ()
    assert unresolved("padj 3.2e-12", ["padj 3.2e-08"]) == ("3.2e-12",)  # the mantissa alone is not a match


@pytest.fixture
def journal(settings: Settings) -> Journal:
    ProjectStore(settings).create("demo")
    journal = Journal(settings.projects_dir / "demo", "demo")
    journal.allocate("c")
    journal.write_cell(CellEntry(ref="demo#c0001", project="demo", cid="c0001", why="w", expect="e", code="x",
                                 created=journal.now(), status="ok",
                                 outputs=(OutputItem(kind="stream", text="cells 24673\nmedian 519\n"),),
                                 jobs=(JobRef(job_id="700", state="PENDING"),)))
    return journal


def test_findings_record_untraceable_numbers(settings: Settings, cluster: FakeCluster, journal: Journal) -> None:
    bench = BenchService(settings, Slurm(cluster))
    good = bench.note("demo", "finding", "24,673 cells, median 519 genes", because=["demo#c0001"])
    assert good.unresolved_numbers == ()
    odd = bench.note("demo", "finding", "about 25,000 cells", because=["demo#c0001"])
    assert odd.unresolved_numbers == ("25,000",)
    assert bench.note("demo", "note", "remember 12345").unresolved_numbers == ()  # plain notes are not traced


def test_jobs_that_end_without_a_result_are_recorded(settings: Settings, cluster: FakeCluster, journal: Journal,
                                                     tmp_path: Path) -> None:
    job_dir = tmp_path / "jobdir"
    job_dir.mkdir()
    register(settings, JobRecord(job_id="700", project="demo", ref="demo#c0001", job_dir=str(job_dir)))
    cluster.jobs["700"], cluster.names["700"] = "RUNNING", "schub-cell-demo-c0001"
    assert reap(settings, Slurm(cluster)) == []  # still running
    cluster.jobs.pop("700")
    cluster.recently_finished["700"] = "OUT_OF_MEMORY"
    assert reap(settings, Slurm(cluster)) == ["700"]
    assert [(j.job_id, j.state) for j in journal.cell("c0001").jobs] == [("700", "OUT_OF_MEMORY")]
    assert open_records(settings) == [] and lookup(settings, "700") is not None  # kept for ownership


def test_the_end_is_read_from_the_log_when_slurm_forgot(settings: Settings, cluster: FakeCluster, journal: Journal,
                                                        tmp_path: Path) -> None:
    job_dir = tmp_path / "jobdir"
    job_dir.mkdir()
    (job_dir / "slurm-700.log").write_text("step 1\n[2026-09-24T02:04:40] error: *** JOB 700 ON gpu-03 CANCELLED "
                                           "AT 2026-09-24T02:04:39 DUE TO TIME LIMIT ***\n")
    register(settings, JobRecord(job_id="700", project="demo", ref="demo#c0001", job_dir=str(job_dir)))
    assert reap(settings, Slurm(cluster)) == ["700"]
    assert journal.cell("c0001").jobs[0].state == "TIMEOUT"


def test_this_clusters_out_of_memory_wording_is_recognised(settings: Settings, cluster: FakeCluster,
                                                           journal: Journal, tmp_path: Path) -> None:
    job_dir = tmp_path / "jobdir"
    job_dir.mkdir()
    (job_dir / "slurm-700.log").write_text("loading\nslurmstepd: error: Detected 1 oom_kill event in StepId=700.batch."
                                           " Some of the step tasks have been OOM Killed.\n")
    register(settings, JobRecord(job_id="700", project="demo", ref="demo#c0001", job_dir=str(job_dir)))
    assert reap(settings, Slurm(cluster)) == ["700"]
    assert journal.cell("c0001").jobs[0].state == "OUT_OF_MEMORY"


def test_a_gpu_out_of_memory_is_not_slurms_memory_limit(settings: Settings, cluster: FakeCluster, journal: Journal,
                                                        tmp_path: Path) -> None:
    job_dir = tmp_path / "jobdir"
    job_dir.mkdir()
    (job_dir / "slurm-700.log").write_text("torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2 GiB\n")
    register(settings, JobRecord(job_id="700", project="demo", ref="demo#c0001", job_dir=str(job_dir)))
    assert reap(settings, Slurm(cluster)) == ["700"]
    assert journal.cell("c0001").jobs[0].state == "ENDED"


def test_a_job_cancelled_while_queued_says_it_never_started(settings: Settings, cluster: FakeCluster,
                                                            journal: Journal, tmp_path: Path) -> None:
    register(settings, JobRecord(job_id="700", project="demo", ref="demo#c0001", job_dir=str(tmp_path / "none")))
    assert reap(settings, Slurm(cluster)) == ["700"]
    [job] = journal.cell("c0001").jobs
    assert job.state == "NOT_STARTED" and job.log == ""


def test_an_end_the_journal_could_not_take_is_recorded_on_a_later_pass(settings: Settings, cluster: FakeCluster,
                                                                       journal: Journal, tmp_path: Path,
                                                                       monkeypatch) -> None:
    """Lustre sometimes answers a write with EIO: the job stays open until its end is in the journal."""
    import errno

    job_dir = tmp_path / "jobdir"
    job_dir.mkdir()
    (job_dir / "slurm-700.log").write_text("error: *** JOB 700 ON gpu-03 CANCELLED AT 2026-09-24T02:04:39 "
                                           "DUE TO TIME LIMIT ***\n")
    register(settings, JobRecord(job_id="700", project="demo", ref="demo#c0001", job_dir=str(job_dir)))
    add = Journal.add_addendum

    def eio(*args, **kwargs):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(Journal, "add_addendum", eio)
    assert reap(settings, Slurm(cluster)) == [] and [r.job_id for r in open_records(settings)] == ["700"]
    monkeypatch.setattr(Journal, "add_addendum", add)
    assert journal.cell("c0001").jobs[0].state == "PENDING"
    assert reap(settings, Slurm(cluster)) == ["700"] and open_records(settings) == []
    assert journal.cell("c0001").jobs[0].state == "TIMEOUT"


def test_nothing_is_decided_when_slurm_does_not_answer(settings: Settings, journal: Journal, tmp_path: Path) -> None:
    import subprocess

    def down(args):
        return subprocess.CompletedProcess(list(args), 1, "", "slurm_load_jobs error: Unable to contact controller")

    register(settings, JobRecord(job_id="700", project="demo", ref="demo#c0001", job_dir=str(tmp_path)))
    assert reap(settings, Slurm(runner=down)) == []
    assert [r.job_id for r in open_records(settings)] == ["700"]


def test_reported_jobs_are_simply_closed(settings: Settings, cluster: FakeCluster, journal: Journal,
                                         tmp_path: Path) -> None:
    job_dir = tmp_path / "jobdir"
    job_dir.mkdir()
    (job_dir / "result.json").write_text("{}")
    register(settings, JobRecord(job_id="701", project="demo", ref="demo#c0001", job_dir=str(job_dir)))
    assert reap(settings, Slurm(cluster)) == [] and open_records(settings) == []


def test_only_bench_jobs_can_be_cancelled_and_states_are_live(settings: Settings, cluster: FakeCluster,
                                                              journal: Journal, tmp_path: Path) -> None:
    bench = BenchService(settings, Slurm(cluster))
    cluster.jobs["700"], cluster.jobs["999"] = "RUNNING", "RUNNING"
    cluster.names["700"], cluster.names["999"] = "schub-cell-demo-c0001", "personal-ws"
    register(settings, JobRecord(job_id="700", project="demo", ref="demo#c0001", job_dir=str(tmp_path)))
    assert bench.wait("demo#c0001", 0).jobs[0].state == "RUNNING"  # live, not the stored PENDING
    with pytest.raises(BenchError, match="only cancels its own"):
        bench.stop("999")
    assert "cancelled job 700" in bench.stop("700")
    assert cluster.jobs["700"] == "CANCELLED" and cluster.jobs["999"] == "RUNNING"


def report(job_dir: Path, job_id: str = "700") -> None:
    """What a finished cell job saved before adding its parts to the journal (jobrun writes result.json first)."""
    job_dir.mkdir(exist_ok=True)
    (job_dir / "result.json").write_text(json.dumps({
        "job_id": job_id, "exit_code": 0, "finished": "2026-09-24T02:05:00.000+00:00", "files": [], "downloads": [],
        "checks": []}))


def refuse_numbers(monkeypatch, on: dict) -> None:
    """The file server refuses the journal's change numbers while on['on'] is true (the records themselves are
    written)."""
    real = journal_module.create_json_exclusive

    def create(path, payload, *args):
        if on["on"] and path.parent.name == "changes":
            raise OSError(errno.EIO, "Input/output error")
        return real(path, payload, *args)

    monkeypatch.setattr(journal_module, "create_json_exclusive", create)
    monkeypatch.setattr(journal_module, "NUMBERING_PAUSE_S", 0.0)


def test_a_report_the_journal_could_not_number_is_numbered_before_its_job_is_closed(
        settings: Settings, cluster: FakeCluster, journal: Journal, tmp_path: Path, monkeypatch) -> None:
    """Found in review: the addendum was written but its number was not; the watchdog's retry got 'already exists'
    from add_addendum, never numbered it, and closed the job: a reader going on from a number never saw COMPLETED."""
    job_dir = tmp_path / "jobdir"
    report(job_dir)
    register(settings, JobRecord(job_id="700", project="demo", ref="demo#c0001", job_dir=str(job_dir)))
    monkeypatch.setattr(journal_module, "LEGACY_S", -1.0)  # only the numbers show what is new
    _, place = journal.page(since=None)
    refusing = {"on": True}
    refuse_numbers(monkeypatch, refusing)
    assert reap(settings, Slurm(cluster)) == [] and [r.job_id for r in open_records(settings)] == ["700"]
    assert journal.cell("c0001").jobs[0].state == "COMPLETED"  # the addendum is there; its number is not
    assert journal.page(since=place)[0] == []
    refusing["on"] = False
    assert reap(settings, Slurm(cluster)) == [] and open_records(settings) == []
    assert [e.jobs[0].state for _, e in journal.page(since=place)[0]] == ["COMPLETED"]


def test_a_job_end_the_journal_could_not_number_is_numbered_too(
        settings: Settings, cluster: FakeCluster, journal: Journal, tmp_path: Path, monkeypatch) -> None:
    job_dir = tmp_path / "jobdir"
    job_dir.mkdir()
    (job_dir / "slurm-700.log").write_text("error: *** JOB 700 ON gpu-03 CANCELLED AT 2026-09-24T02:04:39 "
                                           "DUE TO TIME LIMIT ***\n")
    register(settings, JobRecord(job_id="700", project="demo", ref="demo#c0001", job_dir=str(job_dir)))
    monkeypatch.setattr(journal_module, "LEGACY_S", -1.0)
    _, place = journal.page(since=None)
    refusing = {"on": True}
    refuse_numbers(monkeypatch, refusing)
    assert reap(settings, Slurm(cluster)) == [] and [r.job_id for r in open_records(settings)] == ["700"]
    refusing["on"] = False
    assert reap(settings, Slurm(cluster)) == ["700"] and open_records(settings) == []
    assert [e.jobs[0].state for _, e in journal.page(since=place)[0]] == ["TIMEOUT"]


@pytest.mark.parametrize("failure", [OSError(errno.EACCES, "Permission denied"), RuntimeError("a bug")])
def test_a_job_the_journal_never_takes_stays_open_for_days_and_then_is_given_up(
        settings: Settings, cluster: FakeCluster, journal: Journal, tmp_path: Path, monkeypatch, failure) -> None:
    """Found in review: a failure that never passes (permissions, quota, a bug) kept the job open for ever, and open
    jobs keep the watchdog re-arming itself. An unexpected error is not 'not a report': it must not close the job
    at once either."""
    job_dir = tmp_path / "jobdir"
    report(job_dir)
    register(settings, JobRecord(job_id="700", project="demo", ref="demo#c0001", job_dir=str(job_dir)))

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(jobs_module, "to_journal", fail)
    assert reap(settings, Slurm(cluster)) == [] and [r.job_id for r in open_records(settings)] == ["700"]
    old = time.time() - 2 * jobs_module.GIVE_UP_S
    os.utime(job_dir / "result.json", (old, old))
    reap(settings, Slurm(cluster))
    assert open_records(settings) == []  # given up: result.json stays on disk, the job stays known for ownership
    assert lookup(settings, "700") is not None


def test_a_file_that_is_not_a_report_closes_the_job(settings: Settings, cluster: FakeCluster, journal: Journal,
                                                    tmp_path: Path) -> None:
    job_dir = tmp_path / "jobdir"
    job_dir.mkdir()
    for text in ("not json", "[1, 2]", '{"job_id": "700"}'):
        (job_dir / "result.json").write_text(text)
        register(settings, JobRecord(job_id="700", project="demo", ref="demo#c0001", job_dir=str(job_dir)))
        reap(settings, Slurm(cluster))
        assert open_records(settings) == [], text


def test_an_end_the_journal_never_takes_is_given_up_after_days(settings: Settings, cluster: FakeCluster,
                                                               journal: Journal, tmp_path: Path, monkeypatch) -> None:
    job_dir = tmp_path / "jobdir"
    job_dir.mkdir()
    log = job_dir / "slurm-700.log"
    log.write_text("error: *** JOB 700 ON gpu-03 CANCELLED AT 2026-09-24T02:04:39 DUE TO TIME LIMIT ***\n")
    register(settings, JobRecord(job_id="700", project="demo", ref="demo#c0001", job_dir=str(job_dir)))

    def refuse(*args, **kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(Journal, "add_addendum", refuse)
    assert reap(settings, Slurm(cluster)) == [] and [r.job_id for r in open_records(settings)] == ["700"]
    old = time.time() - 2 * jobs_module.GIVE_UP_S
    os.utime(log, (old, old))
    assert reap(settings, Slurm(cluster)) == [] and open_records(settings) == []
