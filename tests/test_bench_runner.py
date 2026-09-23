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
