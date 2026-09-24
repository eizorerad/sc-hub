from __future__ import annotations

from pathlib import Path

import pytest

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
