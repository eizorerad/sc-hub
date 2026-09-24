from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from schub.bench.fsio import write_json_atomic
from schub.bench.runner import Runner
from schub.bench.workbench import WATCHDOG, BenchStopped, Workbench
from schub.config import Settings
from schub.slurm import Slurm
from tests.conftest import FakeCluster

NOW = "2026-09-24T12:00:00.000+00:00"


class PickyCluster(FakeCluster):
    """Refuses jobs on the gpu partition (a cluster that closed the CPU-only loophole)."""

    def _sbatch(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if "--partition=gpu" in Path(args[-1]).read_text():
            return subprocess.CompletedProcess(args, 1, "", "sbatch: error: Batch job submission failed: Invalid qos")
        return super()._sbatch(args)


def bench(settings: Settings, cluster: FakeCluster) -> Workbench:
    return Workbench(settings, Slurm(runner=cluster), now=lambda: NOW)


def names(cluster: FakeCluster) -> list[str]:
    return [cluster.names[j] for j in cluster.jobs]


def test_ensure_starts_one_workbench_and_its_watchdog(settings: Settings, cluster: FakeCluster, monkeypatch) -> None:
    monkeypatch.setenv("SCHUB_BENCH_CPUS", "8")
    monkeypatch.setenv("SOME_SECRET_TOKEN", "x")
    wb = bench(settings, cluster)
    state = wb.ensure()
    assert state.started_now and state.slurm_state == "PENDING"
    assert names(cluster) == ["schub-bench-workbench", "schub-bench-watchdog"]
    script = cluster.scripts[state.job_id]
    assert "#SBATCH --partition=ws-ia" in script and "--gres" not in script
    assert "#SBATCH --signal=B:USR1@1800" in script and "-m schub.bench.runner" in script
    assert "export SCHUB_BENCH_CPUS=8" in script and "SECRET" not in script
    watchdog = cluster.scripts[[j for j in cluster.jobs if cluster.names[j].endswith(WATCHDOG)][0]]
    assert "#SBATCH --partition=gpu" in watchdog and "#SBATCH --begin=now+30minutes" in watchdog
    assert "#SBATCH --cpus-per-task=1" in watchdog
    wb.ensure()
    assert len(cluster.jobs) == 2  # never a second workbench or watchdog


def test_stop_file_is_the_off_switch(settings: Settings, cluster: FakeCluster) -> None:
    (settings.bench_dir).mkdir(parents=True)
    (settings.bench_dir / "STOP").write_text("")
    wb = bench(settings, cluster)
    with pytest.raises(BenchStopped):
        wb.ensure()
    assert wb.ensure_watchdog() is None and wb.arm_successor("1") is None and cluster.jobs == {}
    assert "stopped" in wb.state().summary()


def test_state_reports_node_and_pending_reason(settings: Settings, cluster: FakeCluster) -> None:
    wb = bench(settings, cluster)
    job = wb.ensure().job_id
    assert "waits in the Slurm queue" in wb.state().summary()
    cluster.jobs[job] = "RUNNING"
    write_json_atomic(settings.bench_dir / "workbench.json", {"job_id": job, "node": "ws-l1-007", "state": "running",
                                                               "heartbeat": NOW})
    state = wb.state()
    assert state.node == "ws-l1-007" and "runs on ws-l1-007" in state.summary()


def test_successor_waits_on_the_current_job_and_is_never_doubled(settings: Settings, cluster: FakeCluster) -> None:
    wb = bench(settings, cluster)
    own = wb.submit_workbench()
    cluster.jobs[own] = "RUNNING"
    successor = wb.arm_successor(own)  # its own job does not count as a successor (A4)
    assert successor is not None and cluster.dependencies(successor) == f"afterany:{own}"
    assert wb.arm_successor(own) is None  # one successor already waits


def test_stop_cancels_the_workbench(settings: Settings, cluster: FakeCluster) -> None:
    wb = bench(settings, cluster)
    job = wb.ensure().job_id
    assert wb.stop() == (job,)
    assert cluster.jobs[job] == "CANCELLED" and wb.stop() == ()


def test_watchdog_falls_back_when_gpu_refuses_and_excludes_itself(settings: Settings) -> None:
    cluster = PickyCluster()
    wb = bench(settings, cluster)
    first = wb.ensure_watchdog()
    assert first is not None and "#SBATCH --partition=ws-ia" in cluster.scripts[first]
    assert wb.ensure_watchdog() is None  # one is enough
    cluster.jobs[first] = "RUNNING"
    rearmed = wb.ensure_watchdog(own_job_id=first)  # the running watchdog arms the next one
    assert rearmed is not None and rearmed != first


def test_dormant_without_recent_heartbeat(settings: Settings, cluster: FakeCluster) -> None:
    wb = bench(settings, cluster)
    assert wb.dormant()
    write_json_atomic(settings.bench_dir / "workbench.json", {"heartbeat": "2026-09-24T11:00:00.000+00:00"})
    assert not wb.dormant()
    write_json_atomic(settings.bench_dir / "workbench.json", {"heartbeat": "2026-09-23T11:00:00.000+00:00"})
    assert wb.dormant()


def test_a_retiring_runner_arms_its_successor_only_when_in_use(settings: Settings, cluster: FakeCluster) -> None:
    slurm = Slurm(runner=cluster)
    runner = Runner(settings, job_id="4242", slurm=slurm, idle_stop_s=600)
    runner.retire("close to the time limit", successor=True)
    runner.touch()
    runner._shutdown("retired")
    assert runner.successor is not None and cluster.dependencies(runner.successor) == "afterany:4242"
    idle = Runner(settings, job_id="4343", slurm=Slurm(runner=FakeCluster()), idle_stop_s=0)
    idle.retire("close to the time limit", successor=True)
    idle._shutdown("retired")
    assert idle.successor is None  # nobody was working: let the slot go


def test_a_workbench_that_is_stopping_does_not_block_a_new_one(settings: Settings, cluster: FakeCluster) -> None:
    """Found on the cluster: stop the workbench, then run a cell at once; the cancelled job was still listed
    as COMPLETING, ensure() took it for a live workbench, and the cell waited for the watchdog."""
    wb = bench(settings, cluster)
    old = wb.ensure().job_id
    cluster.jobs[old] = "COMPLETING"
    fresh = wb.ensure()
    assert fresh.started_now and fresh.job_id != old
