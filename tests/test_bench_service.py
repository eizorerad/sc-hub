from __future__ import annotations

import dataclasses
import threading
import time

import pytest

from schub.bench.clock import stamp
from schub.bench.config import BenchConfig
from schub.bench.inbox import Inbox
from schub.bench.journal import Journal
from schub.bench.models import Actor, CellEntry, OutputItem
from schub.bench.runner import Runner
from schub.bench.worker import new_entry
from schub.bench.service import BenchError, BenchService
from schub.config import Settings
from schub.projects import ProjectStore
from schub.slurm import Slurm
from tests.conftest import FakeCluster


@pytest.fixture
def bench(settings: Settings) -> Settings:
    fast = dataclasses.replace(settings, bench=BenchConfig(poll_s=0.05, run_wait_s=20))
    ProjectStore(fast).create("demo")
    return fast


def service(settings: Settings, cluster: FakeCluster) -> BenchService:
    return BenchService(settings, Slurm(runner=cluster))


def finish(settings: Settings, cid: str, epoch: str, status: str = "ok", setup: bool = False, text: str = "") -> None:
    journal = Journal(settings.projects_dir / "demo", "demo")
    journal.write_cell(CellEntry(ref=f"demo#{cid}", project="demo", cid=cid, why="w", expect="e", code="x",
                                 created=journal.now(), status=status, kernel_epoch=epoch, setup=setup,
                                 outputs=(OutputItem(kind="stream", name="stdout", text=text),)))


def test_run_queues_the_cell_and_starts_the_workbench(bench: Settings, cluster: FakeCluster) -> None:
    result = service(bench, cluster).run("demo", "print(1)", "check", "1", wait_s=0,
                                         actor=Actor(kind="chat", client="codex"))
    assert result.ref == "demo#c0001" and result.status == "queued"
    assert "waits in the Slurm queue" in result.workbench and "Slots:" in result.workbench
    assert [r.actor.client for r in Inbox(bench.bench_dir).pending()] == ["codex"]
    assert sorted(cluster.names.values()) == ["schub-bench-watchdog", "schub-bench-workbench"]


def test_bad_requests_spend_no_id(bench: Settings, cluster: FakeCluster) -> None:
    svc = service(bench, cluster)
    with pytest.raises(BenchError, match="why"):
        svc.run("demo", "print(1)", "  ", "1", wait_s=0)
    with pytest.raises(BenchError, match="does not exist"):
        svc.run("nope", "print(1)", "w", "e", wait_s=0)
    assert svc.run("demo", "print(1)", "w", "e", wait_s=0).ref == "demo#c0001"


def test_stop_files_refuse_new_cells(bench: Settings, cluster: FakeCluster) -> None:
    svc = service(bench, cluster)
    (bench.projects_dir / "demo" / "STOP").write_text("")
    with pytest.raises(BenchError, match="STOP"):
        svc.run("demo", "1", "w", "e", wait_s=0)
    (bench.projects_dir / "demo" / "STOP").unlink()
    bench.bench_dir.mkdir(parents=True, exist_ok=True)
    (bench.bench_dir / "STOP").write_text("")
    with pytest.raises(BenchError, match="bench is stopped"):
        svc.run("demo", "1", "w", "e", wait_s=0)


def test_wait_reports_results_and_kernel_restarts(bench: Settings, cluster: FakeCluster) -> None:
    svc = service(bench, cluster)
    first = svc.run("demo", "import os", "setup", "nothing", setup=True, wait_s=0).ref
    finish(bench, first.split("#")[1], "100.1", setup=True)
    second = svc.run("demo", "print(2)", "w", "e", wait_s=0).ref
    finish(bench, second.split("#")[1], "101.1", text="2\n")
    result = svc.wait(second, wait_s=0)
    assert result.status == "ok" and result.outputs[0].text == "2\n"
    assert result.kernel_restarted and first in result.hint and result.workbench == ""
    with pytest.raises(BenchError, match="does not exist"):
        svc.wait("demo#c0099", wait_s=0)
    with pytest.raises(BenchError, match="note"):
        svc.wait("demo#n0001", wait_s=0)


def test_interrupt_leaves_a_control_for_the_runner(bench: Settings, cluster: FakeCluster) -> None:
    svc = service(bench, cluster)
    ref = svc.run("demo", "import time; time.sleep(9)", "w", "e", wait_s=0).ref
    Inbox(bench.bench_dir).claim("1")  # a runner holds it
    assert "interrupt" in svc.interrupt(ref)
    assert Inbox(bench.bench_dir).take_controls() == [("demo", "c0001", "interrupt")]


def test_interrupting_a_queued_cell_takes_it_out_of_the_queue(bench: Settings, cluster: FakeCluster) -> None:
    """No workbench took it yet: it never runs, and wait() says why at once (not "queued" forever)."""
    svc = service(bench, cluster)
    ref = svc.run("demo", "import time; time.sleep(9)", "w", "e", wait_s=0).ref
    assert "taken out of the queue" in svc.interrupt(ref)
    inbox = Inbox(bench.bench_dir)
    assert inbox.pending() == [] and inbox.take_controls() == []
    started = time.monotonic()
    result = svc.wait(ref, wait_s=5)
    assert result.status == "rejected" and "interrupted before it started" in result.message
    assert time.monotonic() - started < 2  # no waiting for a cell that will never run


def test_rejected_requests_say_why(bench: Settings, cluster: FakeCluster) -> None:
    svc = service(bench, cluster)
    ref = svc.run("demo", "1", "w", "e", wait_s=0).ref
    inbox = Inbox(bench.bench_dir)
    inbox.reject(inbox.claim("1")[0], "the project was removed")
    result = svc.wait(ref, wait_s=0)
    assert result.status == "rejected" and "removed" in result.message


def test_a_started_cell_whose_record_failed_answers_the_reason(bench: Settings, cluster: FakeCluster) -> None:
    svc = service(bench, cluster)
    ref = svc.run("demo", "1", "w", "e", wait_s=0).ref
    inbox = Inbox(bench.bench_dir)
    item = inbox.claim("1")[0]
    journal = Journal(bench.projects_dir / "demo", "demo")
    journal.write_cell(new_entry(item.request, journal, status="running", started=stamp()))
    inbox.reject(item, "sc-hub could not run this cell: OSError: Disk quota exceeded")
    result = svc.wait(ref, wait_s=0)
    assert result.status == "error" and "Disk quota exceeded" in result.message  # not "still running"


@pytest.mark.kernel
def test_end_to_end_with_a_local_runner(bench: Settings, cluster: FakeCluster) -> None:
    runner = Runner(bench, job_id="7000", slurm=Slurm(runner=cluster))
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    try:
        svc = service(bench, cluster)
        result = svc.run("demo", "import socket\nprint('node', socket.gethostname() != '')", "where am I", "node True")
        assert result.status == "ok" and result.outputs[0].text.strip() == "node True"
        assert result.kernel_epoch == "7000.1" and not result.kernel_restarted
    finally:
        (bench.bench_dir / "STOP").write_text("")
        thread.join(timeout=60)


def test_cli_runs_and_reads_the_journal(bench: Settings, cluster: FakeCluster, monkeypatch, capsys) -> None:
    import json

    from schub import cli
    from schub.service import Hub

    monkeypatch.setattr(cli, "load_settings", lambda: bench)
    monkeypatch.setattr(cli, "Hub", lambda settings: Hub(settings, slurm=Slurm(runner=cluster)))
    assert cli.main(["bench-run", "demo", "--code", "print(1)", "--why", "w", "--expect", "e", "--wait", "0"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ref"] == "demo#c0001" and out["status"] == "queued"
    assert cli.main(["bench-journal", "demo"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli.main(["bench-wait", "demo#c0042", "--wait", "0"]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_wait_also_waits_for_the_cells_slurm_jobs(bench: Settings, cluster: FakeCluster) -> None:
    from schub.bench.models import JobRef

    svc = service(bench, cluster)
    ref = svc.run("demo", "%%slurm\nx = 1", "a job", "a job id", wait_s=0).ref
    job_id = str(cluster.next_id)
    cluster.jobs[job_id], cluster.names[job_id] = "PENDING", "schub-cell-demo-c0001"
    journal = Journal(bench.projects_dir / "demo", "demo")
    journal.write_cell(CellEntry(ref=ref, project="demo", cid="c0001", why="a job", expect="a job id", code="x",
                                 created=journal.now(), status="ok", jobs=(JobRef(job_id=job_id, state="PENDING"),)))
    clock = {"t": 0.0}
    timed = BenchService(bench, Slurm(runner=cluster), sleep=lambda s: clock.update(t=clock["t"] + s),
                         monotonic=lambda: clock["t"])
    assert timed.run("demo", "1", "w", "e", wait_s=0).status == "queued"  # run itself never waits for jobs
    result = timed.wait(ref, wait_s=30)
    assert result.status == "ok" and clock["t"] >= 30 and f"job {job_id} is still PENDING" in result.hint
    cluster.slurm_down = cluster.sacct_down = True  # a controller hiccup is not "the job ended"
    clock["t"] = 0.0
    silent = timed.wait(ref, wait_s=30)
    assert clock["t"] >= 30 and silent.jobs[0].state == "PENDING"
    cluster.slurm_down = cluster.sacct_down = False
    clock["t"] = 0.0
    journal.add_addendum("c0001", f"job-{job_id}", {"kind": "job", "job": {
        "job_id": job_id, "state": "COMPLETED", "exit_code": 0, "finished": journal.now(), "log": "x"}})
    cluster.jobs[job_id] = "COMPLETED"
    done = timed.wait(ref, wait_s=30)
    assert clock["t"] < 1 and done.jobs[0].state == "COMPLETED" and "still" not in done.hint
    assert svc.wait(ref, wait_s=0, for_jobs=False).status == "ok"


def test_a_guessed_check_name_gets_the_right_one(bench: Settings, cluster: FakeCluster) -> None:
    from schub.bench.models import CheckSpec

    with pytest.raises(BenchError, match="Did you mean 'file'") as info:
        service(bench, cluster).run("demo", "x = 1", "w", "e", wait_s=0,
                                    checks=[CheckSpec(name="fille", params={"path": "work/x"})])
    assert "table_columns" in str(info.value)
    queued = service(bench, cluster).run("demo", "x = 1", "w", "e", wait_s=0,
                                         checks=[CheckSpec(name="file_exists", params={"path": "work/x"})])
    [request] = Inbox(bench.bench_dir).pending()
    assert queued.status == "queued" and request.checks[0].name == "file"  # the name agents reach for first
