"""The failure paths the review asked for: nothing may leave a cell running forever,
kill the runner silently, or run a cell twice."""

from __future__ import annotations

import dataclasses
import threading
import time

import pytest

from schub.bench import runner as runner_module
from schub.bench import worker as worker_module
from schub.bench.clock import stamp
from schub.bench.config import BenchConfig
from schub.bench.fsio import read_json
from schub.bench.inbox import Inbox
from schub.bench.journal import Journal
from schub.bench.models import CellEntry, CellRequest
from schub.bench.runner import EXIT_CRASHED, Runner
from schub.config import Settings
from schub.projects import ProjectStore


@pytest.fixture
def bench(settings: Settings) -> Settings:
    fast = dataclasses.replace(settings, bench=BenchConfig(poll_s=0.05, output_chars=2000))
    ProjectStore(fast).create("demo")
    return fast


def submit(settings: Settings, code: str) -> str:
    journal = Journal(settings.projects_dir / "demo", "demo")
    cid = journal.allocate("c")
    Inbox(settings.bench_dir).submit(CellRequest(project="demo", cid=cid, code=code, why="w", expect="e",
                                                 created=stamp()))
    return cid


def wait_final(settings: Settings, cid: str, timeout_s: float = 60) -> CellEntry:
    journal = Journal(settings.projects_dir / "demo", "demo")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        entry = journal.cell(cid)
        if entry is not None and entry.final:
            return entry
        time.sleep(0.05)
    raise AssertionError(f"{cid} did not finish: {journal.cell(cid)}")


def run_in_thread(runner: Runner) -> threading.Thread:
    thread = threading.Thread(target=runner.run, daemon=True)
    thread.start()
    return thread


def stop(settings: Settings, thread: threading.Thread) -> None:
    (settings.bench_dir / "STOP").write_text("")
    thread.join(timeout=60)
    assert not thread.is_alive()


@pytest.mark.kernel
def test_failing_progress_writes_do_not_lose_the_cell(bench: Settings, monkeypatch) -> None:
    real = Journal.write_cell

    def flaky(self: Journal, entry: CellEntry) -> None:
        if entry.status == "running" and entry.outputs:
            raise OSError(122, "Disk quota exceeded")
        real(self, entry)

    monkeypatch.setattr(Journal, "write_cell", flaky)
    monkeypatch.setattr(worker_module, "PROGRESS_EVERY_S", 0.0)
    thread = run_in_thread(Runner(bench, job_id="1"))
    cid = submit(bench, "import time\nfor i in range(3):\n    print(i, flush=True)\n    time.sleep(0.3)")
    entry = wait_final(bench, cid)
    stop(bench, thread)
    assert entry.status == "ok" and entry.outputs[0].text == "0\n1\n2\n"


@pytest.mark.kernel
def test_a_crash_after_the_code_started_stops_the_kernel(bench: Settings, monkeypatch) -> None:
    calls = []

    def broken_diff(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("scan exploded")

    monkeypatch.setattr(worker_module, "diff", broken_diff)
    runner = Runner(bench, job_id="2")
    thread = run_in_thread(runner)
    first = wait_final(bench, submit(bench, "x = 1"))
    monkeypatch.setattr(worker_module, "diff", lambda *a, **k: ())
    second = wait_final(bench, submit(bench, "print(x)"))  # a fresh kernel: x is gone
    stop(bench, thread)
    assert first.status == "error" and "scan exploded" in first.message and calls
    assert second.status == "error" and second.kernel_epoch == "2.2"


def test_a_dead_worker_is_replaced_and_keeps_its_queue(bench: Settings, monkeypatch) -> None:
    runner = Runner(bench, job_id="3")
    dead = worker_module.ProjectWorker(runner, "demo")
    runner.workers["demo"] = dead  # never started: its thread is not alive
    submit(bench, "print(1)")
    item = Inbox(bench.bench_dir).claim("3")[0]
    dead.submit(item)
    started: list[str] = []
    monkeypatch.setattr(worker_module.ProjectWorker, "start", lambda self: started.append(self.project))
    runner._replace_dead_workers()
    replacement = runner.workers["demo"]
    assert replacement is not dead and started == ["demo"]
    assert replacement.queue.get_nowait() is item


def test_shutdown_force_retires_a_stuck_worker(bench: Settings, monkeypatch) -> None:
    runner = Runner(bench, job_id="4")
    events: list[str] = []

    class Stuck:
        closing = False
        kernel = None
        busy = "c0001"

        def close(self) -> None:
            events.append("close")

        def join(self, timeout_s: float) -> bool:
            return False

        def force_retire(self, reason: str) -> None:
            events.append(f"force: {reason}")

        def retire_note(self, reason: str) -> None:
            events.append("note")

    runner.workers["demo"] = Stuck()  # type: ignore[assignment]
    monkeypatch.setattr(runner_module, "SHUTDOWN_S", 0.1)
    runner._shutdown("stopped")
    assert events == ["close", "force: the bench was stopped (bench/STOP)", "note"]
    assert read_json(bench.bench_dir / "workbench.json")["state"] == "stopped"


def test_file_system_failures_end_as_crashed_with_a_clean_shutdown(bench: Settings, monkeypatch) -> None:
    runner = Runner(bench, job_id="5")
    monkeypatch.setattr(runner_module, "MAX_FAILURES", 1)
    monkeypatch.setattr(runner.inbox, "claim", lambda owner: (_ for _ in ()).throw(OSError(116, "Stale file handle")))
    assert runner.run() == EXIT_CRASHED
    state = read_json(bench.bench_dir / "workbench.json")
    assert state["state"] == "crashed" and "Stale file handle" in state["reason"]


def test_a_bug_in_the_loop_still_shuts_down(bench: Settings, monkeypatch) -> None:
    runner = Runner(bench, job_id="6")
    monkeypatch.setattr(runner.inbox, "claim", lambda owner: (_ for _ in ()).throw(RuntimeError("bug")))
    with pytest.raises(RuntimeError):
        runner.run()
    assert read_json(bench.bench_dir / "workbench.json")["state"] == "crashed"


def test_main_reexecutes_with_a_clean_environment(monkeypatch) -> None:
    seen = {}

    def fake_execve(path: str, argv: list[str], env: dict[str, str]) -> None:
        seen.update(path=path, argv=argv, env=env)
        raise SystemExit(0)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.delenv("SCHUB_RUNNER_CLEAN", raising=False)
    monkeypatch.setattr(runner_module.os, "execve", fake_execve)
    with pytest.raises(SystemExit):
        runner_module.main()
    assert seen["argv"][1:] == ["-m", "schub.bench.runner"]
    assert seen["env"]["SCHUB_RUNNER_CLEAN"] == "1" and "OPENAI_API_KEY" not in seen["env"]
