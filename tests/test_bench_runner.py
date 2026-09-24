from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path

import pytest

from schub.bench.clock import stamp
from schub.bench.config import BenchConfig
from schub.bench.fsio import read_json, write_json_atomic
from schub.bench.inbox import Inbox
from schub.bench.journal import Journal
from schub.bench.models import Actor, CellEntry, CellRequest
from schub.bench.runner import EXIT_BUSY, Runner
from schub.config import Settings
from schub.projects import ProjectStore
from schub.slurm import Slurm
from tests.conftest import FakeCluster

pytestmark = pytest.mark.kernel


@pytest.fixture
def bench(settings: Settings) -> Settings:
    fast = dataclasses.replace(settings, bench=BenchConfig(poll_s=0.05, output_chars=2000))
    ProjectStore(fast).create("demo", question="does the bench work?")
    return fast


def journal_of(settings: Settings, project: str = "demo") -> Journal:
    return Journal(settings.projects_dir / project, project)


def submit(settings: Settings, code: str, project: str = "demo", setup: bool = False) -> str:
    journal = journal_of(settings, project) if project == "demo" else Journal(settings.projects_dir / project, project)
    cid = journal.allocate("c")
    Inbox(settings.bench_dir).submit(CellRequest(
        project=project, cid=cid, code=code, why="test", expect="it works", setup=setup,
        actor=Actor(kind="chat", client="claude-code"), created=stamp()))
    return cid


def wait_final(settings: Settings, cid: str, timeout_s: float = 60) -> CellEntry:
    journal = journal_of(settings)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        entry = journal.cell(cid)
        if entry is not None and entry.final:
            return entry
        time.sleep(0.05)
    raise AssertionError(f"{cid} did not finish: {journal.cell(cid)}")


def wait_running(settings: Settings, cid: str, timeout_s: float = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        entry = journal_of(settings).cell(cid)
        if entry is not None and entry.status == "running" and entry.kernel_epoch:
            return
        time.sleep(0.05)
    raise AssertionError(f"{cid} never started")


def start(runner: Runner) -> threading.Thread:
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    return thread


def stop(settings: Settings, thread: threading.Thread) -> None:
    (settings.bench_dir / "STOP").write_text("")
    thread.join(timeout=60)
    assert not thread.is_alive()


def test_cells_run_in_order_and_are_recorded(bench: Settings) -> None:
    thread = start(Runner(bench, job_id="900"))
    first = submit(bench, "x = 2\nopen('out.txt', 'w').write('hi')")
    second = submit(bench, "print(x * 21)")
    one, two = wait_final(bench, first), wait_final(bench, second)
    stop(bench, thread)
    assert (one.status, two.status) == ("ok", "ok")
    assert two.outputs[0].text.strip() == "42"
    assert [(f.path, f.change) for f in one.files] == [("work/out.txt", "created")]
    assert one.kernel_epoch == two.kernel_epoch == "900.1"
    assert one.actor.client == "claude-code" and one.why == "test" and one.duration_s is not None
    state = read_json(bench.bench_dir / "workbench.json")
    assert state["state"] == "stopped" and state["job_id"] == "900"


def test_errors_interrupts_and_downloads(bench: Settings) -> None:
    thread = start(Runner(bench, job_id="901"))
    failing = submit(bench, "{}['gene']")
    assert wait_final(bench, failing).status == "error"
    sleeping = submit(bench, "import time\ntime.sleep(60)")
    wait_running(bench, sleeping)
    Inbox(bench.bench_dir).control("demo", sleeping, "interrupt")
    assert wait_final(bench, sleeping).status == "interrupted"
    recorded = submit(bench, "from schub.bench import ledger\n"
                             "ledger.record('download', url='https://x/y.h5ad', path='data/y.h5ad', size=3, sha256='ab')")
    entry = wait_final(bench, recorded)
    stop(bench, thread)
    assert entry.status == "ok" and [d.url for d in entry.downloads] == ["https://x/y.h5ad"]


def test_retiring_hands_over_to_a_successor(bench: Settings) -> None:
    runner = Runner(bench, job_id="902")
    thread = start(runner)
    setup = submit(bench, "import os", setup=True)
    wait_final(bench, setup)
    busy = submit(bench, "import time\ntime.sleep(60)")
    wait_running(bench, busy)
    waiting = submit(bench, "print('later')")
    time.sleep(0.5)
    runner.retire("the workbench job is close to its time limit; a successor takes over")
    thread.join(timeout=60)
    entry = journal_of(bench).cell(busy)
    assert entry.status == "retired" and "successor" in entry.message
    assert [r.cid for r in Inbox(bench.bench_dir).pending()] == [waiting]  # never started: handed over
    notes = journal_of(bench).entries(kinds=("incident",))
    assert len(notes) == 1 and setup in notes[0].text and notes[0].actor.kind == "system"
    assert read_json(bench.bench_dir / "workbench.json")["state"] == "retired"


def test_idle_stop_frees_the_slot(bench: Settings) -> None:
    thread = start(Runner(bench, job_id="903", idle_stop_s=1.0))
    wait_final(bench, submit(bench, "1 + 1"))
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert read_json(bench.bench_dir / "workbench.json")["state"] == "idle-stopped"
    assert "nothing ran" in journal_of(bench).entries(kinds=("incident",))[0].text


def test_project_stop_file_blocks_cells(bench: Settings) -> None:
    (bench.projects_dir / "demo" / "STOP").write_text("")
    thread = start(Runner(bench, job_id="904"))
    entry = wait_final(bench, submit(bench, "print('no')"))
    stop(bench, thread)
    assert entry.status == "interrupted" and "STOP" in entry.message


def test_a_refused_final_write_keeps_the_kernel_and_is_recorded_later(bench: Settings, monkeypatch) -> None:
    """The quota fills after the code ran: its variables survive, the entry does not stay "running"."""
    from schub.bench import runner as runner_module
    from schub.bench.journal import Journal as JournalClass

    monkeypatch.setattr(runner_module, "HEARTBEAT_S", 0.2)
    real_write, full = JournalClass.write_cell, {"on": True}

    def write_cell(self, entry, *args, **kwargs):
        if full["on"] and entry.final and entry.cid == "c0001":
            raise OSError(122, "Disk quota exceeded")
        return real_write(self, entry, *args, **kwargs)

    monkeypatch.setattr(JournalClass, "write_cell", write_cell)
    thread = start(Runner(bench, job_id="906"))
    first = submit(bench, "x = 41")
    inbox = Inbox(bench.bench_dir)
    deadline = time.monotonic() + 60
    while inbox.rejected_reason("demo", first) is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "Disk quota exceeded" in inbox.rejected_reason("demo", first)
    assert journal_of(bench).cell(first).status == "running"  # the record could not be written
    second = submit(bench, "print(x + 1)")
    assert "42" in wait_final(bench, second).outputs[0].text  # the kernel and its variables were kept
    full["on"] = False  # room again: the runner closes the entry at its next beat
    entry = wait_final(bench, first, timeout_s=30)
    stop(bench, thread)
    assert entry.status == "error" and "Disk quota exceeded" in entry.message


def test_unknown_project_is_rejected(bench: Settings) -> None:
    thread = start(Runner(bench, job_id="905"))
    inbox = Inbox(bench.bench_dir)
    inbox.submit(CellRequest(project="nope", cid="c0001", code="1", why="w", expect="e", created=stamp()))
    deadline = time.monotonic() + 30
    while inbox.rejected_reason("nope", "c0001") is None and time.monotonic() < deadline:
        time.sleep(0.05)
    stop(bench, thread)
    assert "does not exist" in inbox.rejected_reason("nope", "c0001")


def test_sweep_marks_lost_and_requeues_unstarted(bench: Settings) -> None:
    inbox = Inbox(bench.bench_dir)
    started = submit(bench, "print(1)")
    unstarted = submit(bench, "print(2)")
    claimed = {c.request.cid: c for c in inbox.claim("777")}
    journal = journal_of(bench)
    request = claimed[started].request
    journal.write_cell(CellEntry(ref=f"demo#{started}", project="demo", cid=started, why=request.why,
                                 expect=request.expect, code=request.code, created=request.created,
                                 status="running", kernel_epoch="777.1"))
    assert Runner(bench, job_id="906", slurm=Slurm(runner=FakeCluster())).sweep() == 2
    lost = journal.cell(started)
    assert lost.status == "lost" and "job 777" in lost.message
    assert [r.cid for r in inbox.pending()] == [unstarted]
    assert inbox.claimed() == []


def test_a_second_runner_refuses_to_start(bench: Settings) -> None:
    bench.bench_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(bench.bench_dir / "workbench.json", {"state": "retiring", "job_id": "1", "heartbeat": stamp()})
    assert Runner(bench, job_id="2", wait_for_other_s=0.3).run() == EXIT_BUSY
    write_json_atomic(bench.bench_dir / "workbench.json",
                      {"state": "running", "job_id": "1", "heartbeat": "2020-01-01T00:00:00.000+00:00"})
    assert not Runner(bench, job_id="2").other_runner_alive()


def test_kernels_have_bench_and_slurm_cells_submit_jobs(bench: Settings, monkeypatch) -> None:
    fake_bin = Path(__file__).parent / "fake_bin"
    monkeypatch.setenv("PATH", f"{fake_bin}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("SCHUB_ROOT", str(bench.root))
    thread = start(Runner(bench, job_id="910"))
    helper = wait_final(bench, submit(bench, "print(bench.work_dir().name)"))
    journal = journal_of(bench)
    cid = journal.allocate("c")
    Inbox(bench.bench_dir).submit(CellRequest(
        project="demo", cid=cid, code="%%slurm --time 10m\nprint(adata)", why="send a job", expect="a job id",
        checks=({"name": "file", "params": {"path": "work/never.txt"}},), created=stamp()))
    sent = wait_final(bench, cid)
    stop(bench, thread)
    assert helper.status == "ok" and helper.outputs[0].text.strip() == "work"
    assert sent.status == "ok" and [(j.job_id, j.state) for j in sent.jobs] == [("4242", "PENDING")]
    assert "Submitted Slurm job 4242" in sent.outputs[0].text and "kernel names: adata" in sent.outputs[0].text
    assert sent.check_results == ()  # the job runs the checks when it ends
    job_dirs = list((bench.projects_dir / "demo" / "jobs").iterdir())
    assert len(job_dirs) == 1 and (job_dirs[0] / "cell.py").read_text().strip() == "print(adata)"
    assert sent.files == ()  # the snapshot is bench bookkeeping, not an output


def test_cell_checks_run_after_the_cell(bench: Settings) -> None:
    thread = start(Runner(bench, job_id="911"))
    journal = journal_of(bench)
    cid = journal.allocate("c")
    Inbox(bench.bench_dir).submit(CellRequest(
        project="demo", cid=cid, code="open('t.csv', 'w').write('gene,lfc\\nA,1\\n')", why="w", expect="a table",
        checks=({"name": "table_columns", "params": {"path": "work/t.csv", "columns": ["gene", "pvalue"]}},),
        created=stamp()))
    entry = wait_final(bench, cid)
    stop(bench, thread)
    assert entry.status == "ok" and [(c.name, c.status) for c in entry.check_results] == [("table_columns", "fail")]


def test_slurm_not_answering_never_buries_a_live_runner(bench: Settings) -> None:
    from schub.slurm import SlurmError

    inbox = Inbox(bench.bench_dir)
    started = submit(bench, "print(1)")
    [claimed] = inbox.claim("777")
    request = claimed.request
    journal_of(bench).write_cell(CellEntry(ref=f"demo#{started}", project="demo", cid=started, why=request.why,
                                           expect=request.expect, code=request.code, created=request.created,
                                           status="running", kernel_epoch="777.1"))
    cluster = FakeCluster()
    cluster.jobs["777"] = "RUNNING"
    cluster.slurm_down = cluster.sacct_down = True  # the login node never reaches sacct anyway
    with pytest.raises(SlurmError, match="did not answer"):
        Slurm(runner=cluster).states(["777"])
    assert Runner(bench, job_id="906", slurm=Slurm(runner=cluster)).sweep() == 0
    assert journal_of(bench).cell(started).status == "running"
    cluster.slurm_down = False
    del cluster.jobs["777"]  # now really gone: squeue says "Invalid job id"
    assert Runner(bench, job_id="906", slurm=Slurm(runner=cluster)).sweep() == 1
    assert journal_of(bench).cell(started).status == "lost"


def test_a_fresh_heartbeat_keeps_the_claims_whatever_slurm_says(bench: Settings) -> None:
    inbox = Inbox(bench.bench_dir)
    submit(bench, "print(1)")
    inbox.claim("777")
    write_json_atomic(bench.bench_dir / "workbench.json", {"state": "running", "job_id": "777", "heartbeat": stamp()})
    assert Runner(bench, job_id="906", slurm=Slurm(runner=FakeCluster())).sweep() == 0


def test_two_sweepers_never_handle_one_claim(bench: Settings) -> None:
    inbox = Inbox(bench.bench_dir)
    submit(bench, "print(1)")
    [item] = inbox.claim("777")
    assert inbox.adopt(item, "906") is not None and inbox.adopt(item, "907") is None
