from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from schub.bench import jobrun, ledger
from schub.bench.journal import Journal
from schub.bench.models import CellEntry
from schub.bench.slurm_cell import SlurmCellError, kernel_names, parse_line, submit_cell
from schub.config import Settings
from schub.projects import ProjectStore
from schub.slurm import Slurm
from tests.conftest import FakeCluster


@pytest.fixture
def project(settings: Settings) -> Path:
    ProjectStore(settings).create("demo")
    journal = Journal(settings.projects_dir / "demo", "demo")
    journal.allocate("c")
    journal.write_cell(CellEntry(ref="demo#c0001", project="demo", cid="c0001", why="w", expect="e", code="%%slurm",
                                 created=journal.now(), status="ok"))
    return settings.projects_dir / "demo"


def test_parse_line_and_limits() -> None:
    spec = parse_line("--gpus 1 --cpus 8 --mem 64G --time 6h", "ws-ia")
    assert (spec.gpus, spec.cpus, spec.mem_gb, spec.minutes, spec.partition) == (1, 8, 64, 360, "ws-ia")
    assert parse_line("--time 02:30:00 --mem 2048M", "ws-ia").minutes == 150
    assert parse_line("", "ws-ia").minutes == 60
    for bad in ("--gpus 2", "--partition gpu --time 9h", "--partition gpu --cpus 20", "--mem 200G",
                "--time forever", "--wat 1", "--name 'a b'", "--partition 'x;y'"):
        with pytest.raises(SlurmCellError):
            parse_line(bad, "ws-ia")


def test_kernel_names_are_found() -> None:
    code = "import numpy as np\nfrom x import y\nz = np.log(adata.X)\ndef f(a):\n    return a + b\nprint(len(z), y)"
    assert kernel_names(code) == ["adata", "b"]
    with pytest.raises(SlurmCellError):
        kernel_names("def (")


def test_submit_snapshots_and_registers(settings: Settings, cluster: FakeCluster, project: Path) -> None:
    spec = parse_line("--gpus 1 --time 30m", settings.partition)
    submitted = submit_cell(settings, Slurm(cluster), "demo", project, "demo#c0001", "print(adata)", spec,
                            [{"name": "file", "params": {"path": "work/out.txt"}}])
    job_dir = Path(submitted.job_dir)
    meta = json.loads((job_dir / "job.json").read_text())
    assert meta["job_id"] == submitted.job.job_id and meta["checks"][0]["name"] == "file"
    assert (job_dir / "cell.py").read_text() == "print(adata)" and (job_dir / "intent.json").exists()
    script = cluster.scripts[submitted.job.job_id]
    assert "#SBATCH --gres=gpu:1" in script and f"#SBATCH --comment={meta['comment']}" in script
    assert "-m schub.bench.jobrun --job-dir" in script and "SECRET" not in script
    assert (settings.bench_dir / "jobs" / f"{submitted.job.job_id}.json").exists()
    assert submitted.warnings == ("the job will not have these kernel names: adata",)


def test_an_ambiguous_sbatch_failure_never_submits_twice(settings: Settings, project: Path) -> None:
    class Flaky(FakeCluster):
        def _sbatch(self, args):
            super()._sbatch(args)  # the job went in...
            return subprocess.CompletedProcess(args, 1, "", "sbatch: error: Socket timed out")  # ...but we hear "no"

    cluster = Flaky()
    submitted = submit_cell(settings, Slurm(cluster), "demo", project, "demo#c0001", "x = 1",
                            parse_line("", "ws-ia"), [])
    assert len(cluster.jobs) == 1 and submitted.job.job_id in cluster.jobs

    class Refusing(FakeCluster):
        def _sbatch(self, args):
            return subprocess.CompletedProcess(args, 1, "", "sbatch: error: QOSMaxSubmitJobPerUserLimit")

    with pytest.raises(SlurmCellError, match="QOSMax"):
        submit_cell(settings, Slurm(Refusing()), "demo", project, "demo#c0001", "x = 1", parse_line("", "ws-ia"), [])


def _run_job(settings: Settings, cluster: FakeCluster, project: Path, code: str, monkeypatch, checks=()) -> tuple:
    submitted = submit_cell(settings, Slurm(cluster), "demo", project, "demo#c0001", code, parse_line("", "ws-ia"),
                            list(checks))
    monkeypatch.setenv("SCHUB_ROOT", str(settings.root))
    monkeypatch.setenv("SLURM_JOB_ID", submitted.job.job_id)
    monkeypatch.chdir(project)
    ledger.drain()
    return submitted, jobrun.main(["--job-dir", submitted.job_dir])


def test_jobrun_reports_into_the_journal(settings: Settings, cluster: FakeCluster, project: Path, monkeypatch) -> None:
    code = ("from pathlib import Path\nPath('out.txt').write_text('done')\n"
            "from schub.bench import ledger\nledger.record('download', url='https://x/y', path='y', size=1, sha256='ab')")
    submitted, code_ = _run_job(settings, cluster, project, code, monkeypatch,
                                checks=[{"name": "file", "params": {"path": "work/out.txt"}}])
    assert code_ == 0
    entry = Journal(project, "demo").cell("c0001")
    job = next(j for j in entry.jobs if j.job_id == submitted.job.job_id)
    assert (job.state, job.exit_code) == ("COMPLETED", 0)
    assert ("work/out.txt", "created") in [(f.path, f.change) for f in entry.files]
    assert [d.url for d in entry.downloads] == ["https://x/y"]
    assert [(c.name, c.status) for c in entry.check_results] == [("file", "pass")]


def test_a_failing_job_is_recorded_as_failed(settings: Settings, cluster: FakeCluster, project: Path, monkeypatch) -> None:
    submitted, code_ = _run_job(settings, cluster, project, "raise ValueError('bad data')", monkeypatch)
    assert code_ == 1
    job = next(j for j in Journal(project, "demo").cell("c0001").jobs if j.job_id == submitted.job.job_id)
    assert job.state == "FAILED" and job.exit_code == 1


def test_an_edited_snapshot_is_refused(settings: Settings, cluster: FakeCluster, project: Path, monkeypatch) -> None:
    submitted = submit_cell(settings, Slurm(cluster), "demo", project, "demo#c0001", "print('original')",
                            parse_line("", "ws-ia"), [])
    (Path(submitted.job_dir) / "cell.py").write_text("print('edited while queued')")
    monkeypatch.setenv("SCHUB_ROOT", str(settings.root))
    monkeypatch.setenv("SLURM_JOB_ID", submitted.job.job_id)
    monkeypatch.chdir(project)
    assert jobrun.main(["--job-dir", submitted.job_dir]) == jobrun.EXIT_TAMPERED
    job = next(j for j in Journal(project, "demo").cell("c0001").jobs if j.job_id == submitted.job.job_id)
    assert job.state == "FAILED" and job.exit_code == 3
