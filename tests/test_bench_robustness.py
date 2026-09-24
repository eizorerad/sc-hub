"""Regressions from the 2026-09-24 bug hunt: each test names the failure it guards against."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from schub.bench.checkpoint import CheckpointStore
from schub.bench.models import OutputItem
from schub.config import Settings
from schub.locking import LockTimeout, exclusive
from schub.projects import ProjectStore
from schub.slurm import JobSpec, Slurm, render_script
from tests.conftest import FakeCluster


# ---- locks that hold across nodes (Lustre is mounted with localflock) --------------------------


def test_a_held_lock_with_a_heartbeat_is_never_broken_as_stale(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    with exclusive(path, stale_after_s=0.5, heartbeat_s=0.1):
        time.sleep(1.0)  # longer than stale_after_s: only the heartbeat keeps it
        with pytest.raises(LockTimeout):
            with exclusive(path, wait_s=0.3, stale_after_s=0.5):
                pass
    with exclusive(path, wait_s=0.3):
        pass


def test_a_dead_holders_lock_is_broken(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    path.write_text("node 1 0\n")
    os.utime(path, (time.time() - 600, time.time() - 600))
    with exclusive(path, wait_s=1, stale_after_s=120):
        assert path.exists()


def test_a_lock_is_only_removed_by_its_own_holder(tmp_path: Path) -> None:
    """A holder whose lock was broken (it looked dead) must not delete the lock its successor holds."""
    path = tmp_path / "x.lock"
    with exclusive(path, stale_after_s=0.2):
        time.sleep(0.4)  # no heartbeat: looks dead
        with exclusive(path, wait_s=1, stale_after_s=0.2):  # breaks it and holds it
            successor = path.read_text()
        path.write_text(successor)  # (as if the successor still held it)
    assert path.exists() and path.read_text() == successor  # the first holder left it alone


def test_the_heartbeat_survives_a_passing_file_server_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "x.lock"
    real_utime, failures = os.utime, iter([OSError(116, "Stale file handle")])

    def flaky(target, *args, **kwargs):
        error = next(failures, None)
        if error is not None:
            raise error
        return real_utime(target, *args, **kwargs)

    monkeypatch.setattr("schub.locking.os.utime", flaky)
    with exclusive(path, stale_after_s=0.5, heartbeat_s=0.1):
        time.sleep(1.0)
        with pytest.raises(LockTimeout):
            with exclusive(path, wait_s=0.3, stale_after_s=0.5):
                pass


def test_a_stale_lock_is_broken_by_one_waiter_only(tmp_path: Path) -> None:
    import threading

    path = tmp_path / "x.lock"
    path.write_text("dead 1 x\n")
    os.utime(path, (time.time() - 600, time.time() - 600))
    inside, overlaps, lock = [], [], threading.Lock()

    def worker() -> None:
        with exclusive(path, wait_s=5, stale_after_s=120):
            with lock:
                inside.append(1)
                if len(inside) > 1:
                    overlaps.append(1)
            time.sleep(0.05)
            with lock:
                inside.pop()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlaps and not path.exists()


# ---- the workbench -------------------------------------------------------------------------------


def test_the_workbench_job_is_never_requeued_under_its_id(settings: Settings) -> None:
    from schub.bench.workbench import Workbench

    cluster = FakeCluster()
    Workbench(settings, Slurm(cluster)).ensure()
    script = next(s for s in cluster.scripts.values() if "schub.bench.runner" in s)
    assert "#SBATCH --no-requeue" in script
    assert "--no-requeue" not in render_script(JobSpec(
        name="x", partition="gpu", resources=__import__("schub.bricks", fromlist=["Resources"]).Resources(
            cpus=1, mem_gb=1, time_min=5), log_path=settings.root / "l", workdir=settings.root, command=("true",)))


def test_parallel_ensures_submit_one_workbench(settings: Settings) -> None:
    from schub.bench.workbench import Workbench

    cluster = FakeCluster()
    threads = [threading.Thread(target=Workbench(settings, Slurm(cluster)).ensure) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum("schub.bench.runner" in s for s in cluster.scripts.values()) == 1


def test_a_second_runner_waits_for_the_lock_and_leaves(settings: Settings) -> None:
    from schub.bench.runner import EXIT_BUSY, Runner

    settings.bench_dir.mkdir(parents=True, exist_ok=True)
    with exclusive(settings.bench_dir / "runner.lock", heartbeat_s=0.1):
        assert Runner(settings, job_id="2", wait_for_other_s=0.3).run() == EXIT_BUSY


def test_idle_kernels_are_evicted_beyond_the_cap(settings: Settings, monkeypatch) -> None:
    from schub.bench import runner as runner_module
    from schub.bench.runner import Runner
    from schub.bench.worker import EVICT

    class Fake:
        def __init__(self, used: float) -> None:
            self.kernel, self.closing, self.last_used, self.asked = object(), False, used, []
            self.queue_items = []

        def idle(self) -> bool:
            return True

        def evict(self) -> None:
            self.asked.append(EVICT)

    monkeypatch.setattr(runner_module, "MAX_KERNELS", 2)
    run = Runner(settings, job_id="1")
    now = time.monotonic()
    run.workers = {f"p{i}": Fake(now - 100 * i) for i in range(4)}
    run._evict_kernels()
    assert [bool(w.asked) for w in run.workers.values()] == [False, False, True, True]  # the two oldest


# ---- outputs, results, numbers ---------------------------------------------------------------------


def test_the_traceback_survives_hundreds_of_outputs(tmp_path: Path) -> None:
    from schub.bench.outputs import MAX_ITEMS, OutputCollector

    collector = OutputCollector(tmp_path, "c0001", 2000)
    for i in range(MAX_ITEMS + 50):
        collector.add("stream", {"name": "stdout" if i % 2 else "stderr", "text": f"line {i}\n"})
    collector.add("error", {"ename": "KeyError", "evalue": "'gene'", "traceback": ["KeyError: 'gene'"]})
    kinds = [o.kind for o in collector.snapshot()]
    assert "error" in kinds and collector.error() == ("KeyError", "'gene'")


def test_many_small_outputs_fit_the_client(tmp_path: Path) -> None:
    from schub.bench.results import CellResult, trimmed

    outputs = tuple(OutputItem(kind="stream", name="stdout" if i % 2 else "stderr", text="x" * 400)
                    for i in range(200)) + (OutputItem(kind="error", text="Traceback: boom", ename="E"),)
    result = trimmed(CellResult(ref="p#c0001", status="error", outputs=outputs), 8000)
    assert sum(len(o.text) for o in result.outputs) <= 8200
    assert result.outputs[-1].kind == "error" and any("left out" in o.text for o in result.outputs)


def test_numbers_in_a_printed_csv_row_are_traced() -> None:
    from schub.bench.numbers import unresolved

    assert unresolved("GATA1 has 120 cells and a knockdown of 0.873", ["GATA1,120,0.873"]) == ()
    assert unresolved("12,345 cells passed", ["n_cells = 12345"]) == ()


# ---- files and notes ---------------------------------------------------------------------------------


def test_files_hides_the_journal_and_tokens_but_not_a_logs_folder(settings: Settings) -> None:
    from schub.bench.files import FilesError, view

    ProjectStore(settings).create("demo", question="q")
    folder = settings.projects_dir / "demo"
    (folder / "work" / "logs").mkdir(parents=True)
    (folder / "work" / "logs" / "metrics.csv").write_text("loss\n1.0\n")
    (folder / "journal" / "notes").mkdir(parents=True, exist_ok=True)
    (folder / "journal" / "notes" / "n0001.json").write_text('{"text": "for the student only"}')
    assert "loss" in view(settings, "demo", "work/logs/metrics.csv").text
    for hidden in ("journal/notes/n0001.json", "journal"):
        with pytest.raises(FilesError):
            view(settings, "demo", hidden)
    with pytest.raises(FilesError):
        view(settings, None, "demo/journal/notes/n0001.json")
    listing = [e.name for e in view(settings, "demo", ".").entries]
    assert "work" in listing and "journal" not in listing


def test_min_cells_ignores_categories_without_cells(tmp_path: Path) -> None:
    import anndata as ad

    from schub.bench.checks.h5ad import MinCellsParams, check_min_cells

    obs = pd.DataFrame({"g": pd.Categorical(["a"] * 30 + ["b"] * 30, categories=["a", "b", "c"])},
                       index=[str(i) for i in range(60)])
    ad.AnnData(X=np.zeros((60, 2), dtype=np.float32), obs=obs).write_h5ad(tmp_path / "x.h5ad")
    result = check_min_cells(lambda p: tmp_path / p, MinCellsParams(path="x.h5ad", groupby="g", min_cells=20))
    assert result.status == "pass", result.message


# ---- twins -------------------------------------------------------------------------------------------


def test_a_twin_keeps_raw_counts_and_obsm_frames_and_groups_missing_labels(tmp_path: Path) -> None:
    import anndata as ad
    from scipy import sparse

    from schub.bench.h5rows import subset
    from schub.bench.twins import _labels

    adata = ad.AnnData(X=sparse.random(50, 8, density=0.3, format="csr", dtype=np.float32),
                       obs=pd.DataFrame({"g": ["a", None] * 25}, index=[f"c{i}" for i in range(50)]),
                       var=pd.DataFrame(index=[f"g{i}" for i in range(8)]))
    adata.obsm["covariates"] = pd.DataFrame({"batch": ["x"] * 50}, index=adata.obs_names)
    adata.raw = adata.copy()
    adata.write_h5ad(tmp_path / "x.h5ad")
    small, dropped = subset(tmp_path / "x.h5ad", np.array([0, 3, 7]))
    assert small.raw is not None and small.raw.shape == (3, 8) and small.obsm["covariates"].shape == (3, 1)
    assert set(_labels(adata.obs["g"])) == {"a", "nan"}


# ---- %%slurm jobs --------------------------------------------------------------------------------------


def test_a_job_ended_by_slurm_is_reported_as_such(settings: Settings, monkeypatch) -> None:
    from schub.bench import jobrun
    from schub.bench.journal import Journal
    from schub.bench.models import CellEntry

    ProjectStore(settings).create("demo", question="q")
    journal = Journal(settings.projects_dir / "demo", "demo")
    cid = journal.allocate("c")
    journal.write_cell(CellEntry(ref=f"demo#{cid}", project="demo", cid=cid, why="w", expect="e", code="x",
                                 created=journal.now(), status="ok"))
    job_dir = settings.root / "jobs" / "x"
    job_dir.mkdir(parents=True)
    result = jobrun.report(job_dir, {"job_id": "9"}, 143, (), [], (), "t", ended_by="TIMEOUT")
    jobrun.to_journal(settings, {"project": "demo", "cid": cid}, job_dir, result)
    assert journal.cell(cid).jobs[0].state == "TIMEOUT"


def test_gpu_visible_probes_the_jobs_own_python(monkeypatch, tmp_path: Path) -> None:
    from schub.bench.checks.training import NoParams, check_gpu

    fake = tmp_path / "python"
    fake.write_text(f"#!{sys.executable}\nimport json\nprint(json.dumps({{'version': '2.1.2', 'cuda': '12.1', "
                    "'count': 1, 'name': 'RTX 5000 Ada'}))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("SCHUB_CHECK_PYTHON", str(fake))
    result = check_gpu(lambda p: tmp_path / p, NoParams())
    assert result.status == "pass" and "RTX 5000 Ada" in result.message and str(fake) in result.message


def test_checkpoint_info_takes_numpy_numbers_and_refuses_before_writing(tmp_path: Path) -> None:
    sys.path.insert(0, str(Path(__file__).parents[1] / "src" / "schub" / "bench" / "portable"))
    from schub_ckpt import CheckpointError, Run

    with Run(tmp_path / "run", {"lr": 1e-3}) as run:
        run.start()
        record = run.save(1, lambda d: (d / "w.bin").write_bytes(b"w"), loss=np.float32(0.25), acc=np.array([1, 2]))
        assert record["info"] == {"acc": [1, 2], "loss": 0.25}
        with pytest.raises(CheckpointError):
            run.save(2, lambda d: (d / "w.bin").write_bytes(b"w"), model=object())
        assert len(list((tmp_path / "run" / "checkpoints").iterdir())) == 1  # nothing half-written
        with pytest.raises(CheckpointError, match="another process trains"):
            Run(tmp_path / "run", {"lr": 1e-3}).start()
    Run(tmp_path / "run", {"lr": 1e-3}).start()  # closed: free again


# ---- the lab agent ---------------------------------------------------------------------------------


def test_the_lab_agent_keeps_waiting_when_slurm_does_not_answer(settings: Settings) -> None:
    from schub.bench.goal_agent import Slice
    from schub.bench.models import Checkpoint, WaitingJob

    ProjectStore(settings).create("p", question="q")
    cluster = FakeCluster()
    cluster.slurm_down = cluster.sacct_down = True
    slice_ = Slice(settings, Slurm(cluster), "p", "1")
    checkpoint = Checkpoint(disposition="waiting", next_action="x", waiting_jobs=(WaitingJob(job_id="42"),))
    assert slice_._open_waiting_jobs(checkpoint) == ["42"]


def test_two_handoffs_at_once_never_mix(settings: Settings) -> None:
    ProjectStore(settings).create("p", question="q")
    store = CheckpointStore(settings.projects_dir / "p")
    texts = [f"hand-over {i}\n" + "x" * 5000 for i in range(8)]
    threads = [threading.Thread(target=store.write_handoff, args=(t,)) for t in texts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.read_handoff() in {t.strip() + "\n" for t in texts}
    assert not [p for p in store.folder.iterdir() if p.name.endswith(".tmp")]


def test_a_parent_project_does_not_record_its_variants_files(settings: Settings) -> None:
    from schub.bench.filesnap import scan

    ProjectStore(settings).create("a", question="q")
    ProjectStore(settings).create("a/b", question="q")
    (settings.projects_dir / "a" / "work" / "mine.csv").write_text("x")
    (settings.projects_dir / "a" / "b" / "work" / "theirs.csv").write_text("y")
    files = scan(settings.projects_dir / "a", 1000).files
    assert "work/mine.csv" in files and not any("theirs" in f for f in files)


def test_a_cell_that_arrives_while_the_runner_idles_out_gets_a_successor(settings: Settings) -> None:
    from schub.bench.clock import stamp
    from schub.bench.inbox import Inbox
    from schub.bench.models import CellRequest
    from schub.bench.runner import Runner

    ProjectStore(settings).create("demo", question="q")
    Inbox(settings.bench_dir).submit(CellRequest(project="demo", cid="c0001", code="1", why="w", expect="e",
                                                 created=stamp()))
    cluster = FakeCluster()
    runner = Runner(settings, job_id="77", slurm=Slurm(cluster))
    runner._shutdown("idle-stopped")
    assert runner.successor and any("schub.bench.runner" in s for s in cluster.scripts.values())
    assert json.loads((settings.bench_dir / "workbench.json").read_text())["successor"] == runner.successor
