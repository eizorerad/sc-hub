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


def test_a_syntax_error_stops_before_anything_is_queued(settings: Settings, cluster: FakeCluster, project: Path) -> None:
    with pytest.raises(SlurmCellError, match="does not parse"):
        submit_cell(settings, Slurm(cluster), "demo", project, "demo#c0001", "def (", parse_line("", "ws-ia"), [])
    assert cluster.jobs == {}
    assert not (project / "jobs").exists() or not any((project / "jobs").iterdir())


def test_an_unknown_outcome_is_said_plainly(settings: Settings, project: Path) -> None:
    class Blind(FakeCluster):
        def _sbatch(self, args):
            return subprocess.CompletedProcess(args, 1, "", "sbatch: error: Socket timed out")

        def _squeue(self, args):
            return subprocess.CompletedProcess(args, 1, "", "squeue: error: Unable to contact controller")

    with pytest.raises(SlurmCellError, match="may exist"):
        submit_cell(settings, Slurm(Blind()), "demo", project, "demo#c0001", "x = 1", parse_line("", "ws-ia"), [])


def test_another_interpreter_runs_the_body(settings: Settings, cluster: FakeCluster, project: Path, monkeypatch,
                                           tmp_path: Path) -> None:
    import sys

    fake = tmp_path / "paper-python"
    fake.write_text(f"#!/bin/sh\necho ran-by-the-paper-env > {project / 'work' / 'marker.txt'}\n")
    fake.chmod(0o755)
    spec = parse_line(f"--python {fake}", "ws-ia")
    submitted = submit_cell(settings, Slurm(cluster), "demo", project, "demo#c0001", "print('x')", spec, [])
    assert json.loads((Path(submitted.job_dir) / "job.json").read_text())["python"] == sys.executable
    monkeypatch.setenv("SCHUB_ROOT", str(settings.root))
    monkeypatch.setenv("SLURM_JOB_ID", submitted.job.job_id)
    monkeypatch.chdir(project)
    assert jobrun.main(["--job-dir", submitted.job_dir]) == 0
    assert (project / "work" / "marker.txt").read_text().strip() == "ran-by-the-paper-env"


def test_the_job_has_bench_like_the_kernel(settings: Settings, cluster: FakeCluster, project: Path,
                                           monkeypatch) -> None:
    code = "bench.work_dir().joinpath('where.txt').write_text(f'{bench.project_dir()} {bench.current_cell()}')"
    submitted, code_ = _run_job(settings, cluster, project, code, monkeypatch)
    assert submitted.warnings == () and code_ == 0
    assert (project / "work" / "where.txt").read_text() == f"{project.resolve()} demo#c0001"
    foreign = submit_cell(settings, Slurm(cluster), "demo", project, "demo#c0002", code,
                          parse_line("--python /usr/bin/python3", "ws-ia"), [])
    assert foreign.warnings == ("the job will not have these kernel names: bench",)


def test_jobs_are_warned_before_their_time_limit(settings: Settings, cluster: FakeCluster, project: Path) -> None:
    hour = submit_cell(settings, Slurm(cluster), "demo", project, "demo#c0001", "x = 1", parse_line("--time 1h", "gpu"),
                       [])
    assert "#SBATCH --signal=B:USR1@360" in (Path(hour.job_dir) / "job.sbatch").read_text()
    long = submit_cell(settings, Slurm(cluster), "demo", project, "demo#c0002", "x = 1", parse_line("--time 8h", "gpu"),
                       [])
    assert "#SBATCH --signal=B:USR1@600" in (Path(long.job_dir) / "job.sbatch").read_text()


def test_a_time_warning_does_not_kill_the_job_and_checkpoints_import(settings: Settings, cluster: FakeCluster,
                                                                      project: Path, monkeypatch) -> None:
    import signal as signals

    before = signals.getsignal(signals.SIGUSR1)
    code = ("import os, signal\nfrom schub_ckpt import Run\nos.kill(os.getpid(), signal.SIGUSR1)\n"
            "open('after.txt', 'w').write(Run.__name__)")
    submitted, code_ = _run_job(settings, cluster, project, code, monkeypatch)
    assert code_ == 0 and (project / "work" / "after.txt").read_text() == "Run"
    assert signals.getsignal(signals.SIGUSR1) == before


def test_a_foreign_interpreter_gets_the_warning_and_the_portable_helpers(settings: Settings, cluster: FakeCluster,
                                                                         project: Path, monkeypatch,
                                                                         tmp_path: Path) -> None:
    import os
    import signal as signals
    import threading
    import time

    marks = project / "work"
    fake = tmp_path / "paper-python"
    fake.write_text(f"""#!/bin/sh
echo "$PYTHONPATH" > {marks / 'pythonpath.txt'}
trap 'echo usr1 > {marks / 'usr1.txt'}; exit 0' USR1
touch {marks / 'started.txt'}
i=0; while [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done
exit 3
""")
    fake.chmod(0o755)
    submitted = submit_cell(settings, Slurm(cluster), "demo", project, "demo#c0001", "print('x')",
                            parse_line(f"--python {fake}", "ws-ia"), [])
    monkeypatch.setenv("SCHUB_ROOT", str(settings.root))
    monkeypatch.setenv("SLURM_JOB_ID", submitted.job.job_id)
    monkeypatch.chdir(project)

    def warn() -> None:
        for _ in range(100):
            if (marks / "started.txt").exists():
                os.kill(os.getpid(), signals.SIGUSR1)
                return
            time.sleep(0.05)

    threading.Thread(target=warn, daemon=True).start()
    assert jobrun.main(["--job-dir", submitted.job_dir]) == 0
    assert (marks / "usr1.txt").read_text().strip() == "usr1"
    assert (marks / "pythonpath.txt").read_text().strip().endswith("schub/bench/portable")
