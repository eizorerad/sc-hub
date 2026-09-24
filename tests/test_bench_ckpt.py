"""The checkpoint transaction: a checkpoint counts only when complete, resume keeps the
registration, a signal stops training at a checkpoint instead of killing it."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from schub.bench.portable.schub_ckpt import CheckpointError, Run

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
