"""The checkpoint transaction: a checkpoint counts only when complete, resume keeps the
registration, a signal stops training at a checkpoint instead of killing it."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from schub.bench.portable.schub_ckpt import LOCK_STALE_S, CheckpointError, Run

REG = {"config": {"lr": 1e-3, "layers": [2, 2]}, "data": "sha:abc", "code": "0123abcd"}


def write_model(text: str):
    def write(folder: Path) -> None:
        (folder / "model.bin").write_text(text)
        (folder / "optim").mkdir()
        (folder / "optim" / "state.bin").write_text(text + "-optim")
    return write


def test_save_then_resume_from_the_latest(tmp_path: Path) -> None:
    run = Run(tmp_path / "run", REG)
    assert run.start() is None
    run.save(100, write_model("a"))
    run.save(200, write_model("b"), loss=0.5)
    run.close()
    again = Run(tmp_path / "run", json.loads(json.dumps(REG)))  # the same registration after a JSON round trip
    latest = again.start()
    assert latest["step"] == 200 and latest["status"] == "running" and latest["info"] == {"loss": 0.5}
    assert (latest["dir"] / "model.bin").read_text() == "b"


def test_an_interrupted_save_leaves_the_previous_checkpoint(tmp_path: Path) -> None:
    run = Run(tmp_path / "run", REG)
    run.start()
    run.save(100, write_model("a"))

    def crash(folder: Path) -> None:
        (folder / "model.bin").write_text("half")
        raise OSError("node died")

    with pytest.raises(OSError):
        run.save(200, crash)
    assert run.latest()["step"] == 100


def test_a_changed_checkpoint_or_registration_is_refused(tmp_path: Path) -> None:
    run = Run(tmp_path / "run", REG)
    run.start()
    saved = run.save(100, write_model("a"))
    run.close()
    with pytest.raises(CheckpointError, match="registration differs"):
        Run(tmp_path / "run", {**REG, "config": {"lr": 1e-2, "layers": [2, 2]}}).start()
    (saved["dir"] / "model.bin").write_text("tampered")
    with pytest.raises(CheckpointError, match="changed"):
        Run(tmp_path / "run", REG).start()


def test_one_process_trains_a_run(tmp_path: Path) -> None:
    first = Run(tmp_path / "run", REG)
    first.start()
    with pytest.raises(CheckpointError, match="another process"):
        Run(tmp_path / "run", REG).start()
    first.close()
    Run(tmp_path / "run", REG).start()


def test_old_checkpoints_are_pruned(tmp_path: Path) -> None:
    run = Run(tmp_path / "run", REG, keep=2)
    run.start()
    for step in (1, 2, 3, 4):
        run.save(step, write_model(str(step)))
    kept = sorted(p.name.split("-")[0] for p in (tmp_path / "run" / "checkpoints").iterdir())
    assert kept == ["000000003", "000000004"]


def test_signals_ask_to_stop_instead_of_killing(tmp_path: Path) -> None:
    run = Run(tmp_path / "run", REG)
    run.start()
    before = signal.getsignal(signal.SIGUSR1)
    with run.signals():
        os.kill(os.getpid(), signal.SIGUSR1)
        assert run.stop_requested and run.stop_signal == "SIGUSR1"
        run.save(7, write_model("x"), status="paused")
    assert signal.getsignal(signal.SIGUSR1) == before
    assert run.latest()["status"] == "paused"
    assert not run.segment_over(seconds=3600) and run.segment_over(seconds=0)
    with pytest.raises(ValueError):
        run.segment_over()


def test_nothing_written_is_not_a_checkpoint(tmp_path: Path) -> None:
    run = Run(tmp_path / "run", REG)
    run.start()
    with pytest.raises(CheckpointError, match="wrote nothing"):
        run.save(1, lambda folder: None)
    with pytest.raises(CheckpointError, match="start"):
        Run(tmp_path / "other", REG).save(1, write_model("a"))


def test_the_jobs_time_limit_file_asks_to_stop(tmp_path: Path, monkeypatch) -> None:
    stop = tmp_path / "time-limit-near"
    monkeypatch.setenv("SCHUB_STOP_FILE", str(stop))
    run = Run(tmp_path / "run", REG)
    run.start()
    assert not run.stop_requested
    stop.touch()
    assert run.stop_requested and run.stop_signal == "time limit near"


def test_a_registration_with_a_set_resumes_and_odd_values_are_refused(tmp_path: Path) -> None:
    import subprocess
    import sys

    code = ("import sys; sys.path.insert(0, sys.argv[1]); from schub_ckpt import Run; "
            "r = Run(sys.argv[2], {'genes': {'MYC', 'GATA1', 'TP53', 'KLF1'}}); print(r.registration_sha256)")
    portable = str(Path(__file__).parents[1] / "src" / "schub" / "bench" / "portable")
    hashes = {subprocess.run([sys.executable, "-c", code, portable, str(tmp_path / "r")], capture_output=True,
                             text=True, env={"PYTHONHASHSEED": str(seed)}).stdout for seed in range(4)}
    assert len(hashes) == 1  # the same registration in every process
    with pytest.raises(CheckpointError, match="JSON values"):
        Run(tmp_path / "odd", {"model": object()})


def test_a_refused_resume_releases_the_run(tmp_path: Path) -> None:
    with Run(tmp_path / "run", REG) as run:
        run.start()
        saved = run.save(1, write_model("a"))
    (saved["dir"] / "model.bin").write_text("tampered")
    with pytest.raises(CheckpointError, match="changed"):
        Run(tmp_path / "run", REG).start()
    with pytest.raises(CheckpointError, match="changed"):
        Run(tmp_path / "run", REG).start()  # not "another process": the first refusal let go of the lock


def dead_owner(folder: Path) -> Path:
    """The owner file of a process that died without closing: untouched for longer than LOCK_STALE_S."""
    folder.mkdir(parents=True, exist_ok=True)
    owner = folder / ".owner"
    owner.write_text("node0 pid 1 job 7\n")
    os.utime(owner, (time.time() - LOCK_STALE_S - 30,) * 2)
    return owner


def test_two_takers_of_a_dead_owners_run_do_not_both_get_it(tmp_path: Path, monkeypatch) -> None:
    """The audit's order: A and B both read the dead owner's old mtime; A removes that file and takes the run,
    and only then B acts on what it read. B removed A's new owner file and took the run too."""
    owner = dead_owner(tmp_path / "run")
    b_read, a_done = threading.Event(), threading.Event()
    real_stat, first = Path.stat, set()

    def stat(path: Path, *args, **kwargs):
        name = threading.current_thread().name
        if path != owner or name not in ("A", "B") or name in first:
            return real_stat(path, *args, **kwargs)
        first.add(name)
        if name == "A":
            assert b_read.wait(10)
            return real_stat(path, *args, **kwargs)
        read = real_stat(path, *args, **kwargs)
        b_read.set()
        assert a_done.wait(10)
        return read

    runs, outcome = {"A": Run(owner.parent, REG), "B": Run(owner.parent, REG)}, {}

    def take(name: str) -> None:
        try:
            runs[name].start()
            outcome[name] = "took"
        except Exception as exc:  # noqa: BLE001 - reported below
            outcome[name] = f"{type(exc).__name__}: {exc}"
        finally:
            if name == "A":
                a_done.set()

    monkeypatch.setattr(Path, "stat", stat)
    threads = [threading.Thread(target=take, args=(name,), name=name) for name in runs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    monkeypatch.undo()
    assert outcome["A"] == "took" and outcome["B"].startswith("CheckpointError: another process trains"), outcome
    runs["A"].close()
    assert not owner.exists()


def test_a_run_taken_over_is_not_released_by_its_first_owner(tmp_path: Path) -> None:
    """The first owner stopped touching its file (it looked dead) and a second took the run over: the first
    one's close() removed the second's owner file, so a third process trained next to the second."""
    first = Run(tmp_path / "run", REG)
    first.start()
    os.utime(tmp_path / "run" / ".owner", (time.time() - LOCK_STALE_S - 30,) * 2)
    second = Run(tmp_path / "run", REG)
    second.start()
    first.close()
    with pytest.raises(CheckpointError, match="another process"):
        Run(tmp_path / "run", REG).start()
    second.close()
    Run(tmp_path / "run", REG).start()


def test_the_heartbeat_touches_only_its_own_owner_file(tmp_path: Path, monkeypatch) -> None:
    """Once another process holds the owner file, touching it would keep its lock fresh after that process
    died too, and nobody could resume the run."""
    monkeypatch.setattr("schub.bench.portable.schub_ckpt.LOCK_BEAT_S", 0.1)
    run = Run(tmp_path / "run", REG)
    run.start()
    owner, old = tmp_path / "run" / ".owner", time.time() - LOCK_STALE_S - 30
    taken = "node9 pid 99 job 12 token 0123abcd\n"
    try:
        os.utime(owner, (old, old))
        deadline = time.monotonic() + 10
        while owner.stat().st_mtime < old + 60 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert owner.stat().st_mtime > old + 60  # its own file is kept fresh (it just touched it)
        other = tmp_path / "run" / "other"
        other.write_text(taken)
        os.utime(other, (old, old))
        os.replace(other, owner)  # meanwhile a process on another node took the run over
        time.sleep(0.6)
        assert time.time() - owner.stat().st_mtime > LOCK_STALE_S
    finally:
        run.close()
    assert owner.read_text() == taken  # nor removed by close()


def test_a_breaker_that_died_does_not_lock_the_run_forever(tmp_path: Path) -> None:
    owner = dead_owner(tmp_path / "run")
    breaker = owner.with_name(".owner.break")
    breaker.write_text("")
    os.utime(breaker, (time.time() - LOCK_STALE_S,) * 2)  # it died while breaking the dead owner's file
    run = Run(owner.parent, REG)
    assert run.start() is None
    assert owner.exists() and not breaker.exists()
    run.close()
    assert not owner.exists()


STRESS = """
import os, pathlib, random, sys, time
sys.path.insert(0, sys.argv[1])
from schub_ckpt import CheckpointError, Run

def slow(real):
    def call(self, *args, **kwargs):
        time.sleep(random.random() * 0.002)  # a busy file server: the moments between look and act grow
        return real(self, *args, **kwargs)
    return call

pathlib.Path.stat, pathlib.Path.unlink = slow(pathlib.Path.stat), slow(pathlib.Path.unlink)
folder, rounds = sys.argv[2], int(sys.argv[3])
inside = folder + ".inside"
took = overlaps = 0
for r in range(rounds):
    while not os.path.exists(f"{folder}.go{r}"):
        time.sleep(0.001)
    run = Run(folder, {"stress": 1})
    try:
        run.start()
    except CheckpointError:
        pass
    else:
        took += 1
        try:
            os.close(os.open(inside, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            overlaps += 1  # another process trains the run at the same time
        else:
            time.sleep(0.02)
            os.unlink(inside)
        run.close()
    open(f"{folder}.done{r}.{os.getpid()}", "w").close()
print(took, overlaps)
"""


def test_processes_racing_for_a_dead_owners_run_take_it_one_at_a_time(tmp_path: Path) -> None:
    """Eight processes, released together on a run whose owner died, ten times over, on a slow file server
    (the unfixed takeover let two of them train at once in most rounds)."""
    import subprocess
    import sys

    portable = str(Path(__file__).parents[1] / "src" / "schub" / "bench" / "portable")
    folder, workers, rounds = tmp_path / "run", 8, 10
    processes = [subprocess.Popen([sys.executable, "-c", STRESS, portable, str(folder), str(rounds)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(workers)]
    try:
        for r in range(rounds):
            owner = dead_owner(folder)
            (tmp_path / f"run.go{r}").touch()
            deadline = time.monotonic() + 60
            while len(list(tmp_path.glob(f"run.done{r}.*"))) < workers:
                assert time.monotonic() < deadline, [p.poll() for p in processes]
                time.sleep(0.01)
            assert not owner.exists() and not owner.with_name(".owner.break").exists()
        outputs = [p.communicate(timeout=60) for p in processes]
    finally:
        for process in processes:
            process.kill()
    counts = [out.split() for out, _ in outputs]
    assert all(len(c) == 2 for c in counts), outputs
    assert sum(int(overlaps) for _, overlaps in counts) == 0
    assert sum(int(took) for took, _ in counts) >= rounds
