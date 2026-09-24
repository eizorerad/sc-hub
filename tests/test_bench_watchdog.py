from __future__ import annotations

from schub.bench.clock import stamp
from schub.bench.fsio import read_json, write_json_atomic
from schub.bench.inbox import Inbox
from schub.bench.journal import Journal
from schub.bench.models import CellEntry, CellRequest
from schub.bench.watchdog import check
from schub.config import Settings
from schub.projects import ProjectStore
from schub.slurm import Slurm
from tests.conftest import FakeCluster


def request(settings: Settings, project: str = "demo") -> CellRequest:
    cid = Journal(settings.projects_dir / project, project).allocate("c")
    return CellRequest(project=project, cid=cid, code="print(1)", why="w", expect="e", created=stamp())


def names(cluster: FakeCluster) -> list[str]:
    return sorted(cluster.names[j] for j in cluster.jobs)


def test_stop_file_means_no_rearm(settings: Settings, cluster: FakeCluster) -> None:
    (settings.bench_dir).mkdir(parents=True)
    (settings.bench_dir / "STOP").write_text("")
    report = check(settings, Slurm(runner=cluster))
    assert report.rearmed is None and cluster.jobs == {}
    assert read_json(settings.bench_dir / "watchdog.json")["actions"] == ["bench/STOP exists: not re-arming"]


def test_waiting_cells_start_a_workbench(settings: Settings, cluster: FakeCluster) -> None:
    ProjectStore(settings).create("demo")
    Inbox(settings.bench_dir).submit(request(settings))
    report = check(settings, Slurm(runner=cluster), own_job_id="77")
    assert names(cluster) == ["schub-bench-watchdog", "schub-bench-workbench"]
    assert "1 waiting cell" in report.actions[0] and report.rearmed is not None


def test_dead_runner_leftovers_are_swept(settings: Settings, cluster: FakeCluster) -> None:
    ProjectStore(settings).create("demo")
    inbox = Inbox(settings.bench_dir)
    started, unstarted = request(settings), request(settings)
    inbox.submit(started)
    inbox.submit(unstarted)
    inbox.claim("555")
    journal = Journal(settings.projects_dir / "demo", "demo")
    journal.write_cell(CellEntry(ref=f"demo#{started.cid}", project="demo", cid=started.cid, why="w", expect="e",
                                 code="print(1)", created=started.created, status="running", kernel_epoch="555.1"))
    write_json_atomic(settings.bench_dir / "workbench.json",
                      {"state": "running", "job_id": "555", "heartbeat": "2026-01-01T00:00:00.000+00:00"})
    report = check(settings, Slurm(runner=cluster))
    assert journal.cell(started.cid).status == "lost"
    assert "swept 2 cell" in report.actions[0] and "started the workbench" in report.actions[1]
    assert [r.cid for r in inbox.pending()] == [unstarted.cid]


def test_a_live_runner_is_never_swept(settings: Settings, cluster: FakeCluster) -> None:
    ProjectStore(settings).create("demo")
    inbox = Inbox(settings.bench_dir)
    inbox.submit(request(settings))
    inbox.claim("9")
    cluster.jobs["9"], cluster.names["9"] = "RUNNING", "schub-bench-workbench"
    inbox.submit(request(settings))
    inbox.claim("local-5")  # a local runner with a fresh heartbeat
    write_json_atomic(settings.bench_dir / "workbench.json", {"state": "running", "job_id": "local-5", "heartbeat": stamp()})
    report = check(settings, Slurm(runner=cluster))
    assert not any("swept" in a for a in report.actions)
    assert len(inbox.claimed()) == 2


def test_dormant_bench_lets_the_watchdog_lapse(settings: Settings, cluster: FakeCluster) -> None:
    report = check(settings, Slurm(runner=cluster), own_job_id="5")
    assert report.rearmed is None and "dormant" in report.actions[-1] and cluster.jobs == {}
