from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from schub.bricks import Resources
from schub.slurm import JobSpec, Slurm, SlurmError, format_time, parse_job_id, render_script


def spec(**overrides) -> JobSpec:
    base = dict(
        name="schub-abc123-01-qc_filter",
        partition="ws-ia",
        resources=Resources(cpus=8, mem_gb=32, time_min=90),
        log_path=Path("/l/x/slurm-%j.log"),
        workdir=Path("/l/x dir"),
        command=("/env/bin/python", "-m", "schub.execute", "--step-dir", "/l/x dir"),
        env=(("MPLBACKEND", "Agg"),),
    )
    return JobSpec(**{**base, **overrides})


def test_render_cpu_job_without_dependency():
    script = render_script(spec())
    assert "#SBATCH --time=01:30:00" in script
    assert "--gres" not in script and "--dependency" not in script
    assert "export MPLBACKEND=Agg" in script
    assert "cd '/l/x dir'" in script
    assert script.rstrip().endswith("--step-dir '/l/x dir'")


def test_render_gpu_job_with_dependency_kills_on_invalid_dep():
    script = render_script(spec(resources=Resources(cpus=8, mem_gb=32, time_min=30, gpus=1), dependency=("41", "42")))
    assert "#SBATCH --gres=gpu:1" in script
    assert "#SBATCH --dependency=afterok:41:42" in script
    assert "#SBATCH --kill-on-invalid-dep=yes" in script


def test_rejects_unsafe_job_names():
    with pytest.raises(ValueError):
        render_script(spec(name="bad name; rm -rf"))


def test_format_time_and_parse_job_id():
    assert format_time(0) == "00:01:00"
    assert format_time(1440) == "24:00:00"
    assert parse_job_id("12345;cluster\n") == "12345"
    with pytest.raises(SlurmError):
        parse_job_id("Submitted batch job")


def test_states_fall_back_to_squeue_for_fresh_jobs():
    def runner(args):
        if args[0] == "sacct":
            return subprocess.CompletedProcess(args, 0, "1|COMPLETED\n2|CANCELLED by 3525\n", "")
        return subprocess.CompletedProcess(args, 0, "3|PENDING\n", "")

    assert Slurm(runner).states(["1", "2", "3"]) == {"1": "COMPLETED", "2": "CANCELLED", "3": "PENDING"}
    assert Slurm(runner).states([]) == {}


def test_failed_command_raises(cluster):
    cluster.fail_sbatch_at = 0
    with pytest.raises(SlurmError, match="QOSMaxSubmitJobPerUserLimit"):
        Slurm(cluster).submit(Path(__file__))


def test_partitions_and_active_jobs(cluster, tmp_path):
    slurm = Slurm(cluster)
    parts = slurm.partitions()
    assert parts[0].name == "ws-ia" and parts[0].idle == 94 and parts[1].total == 4
    script = tmp_path / "job.sbatch"
    script.write_text(render_script(spec()))
    job_id = slurm.submit(script)
    assert [j.job_id for j in slurm.active("schub")] == [job_id]
    assert slurm.active("other") == []
    slurm.cancel([job_id])
    assert slurm.active("schub") == []


def test_states_without_accounting_database(cluster, tmp_path):
    cluster.sacct_down = True
    slurm = Slurm(cluster)
    script = tmp_path / "job.sbatch"
    script.write_text(render_script(spec()))
    queued, finished, gone = slurm.submit(script), slurm.submit(script), slurm.submit(script)
    cluster.jobs[finished] = "COMPLETED"
    cluster.recently_finished[finished] = "COMPLETED"
    cluster.jobs[gone] = "COMPLETED"
    assert slurm.states([queued, finished, gone]) == {queued: "PENDING", finished: "COMPLETED"}


def test_signal_begin_and_comment_lines():
    script = render_script(spec(signal="B:USR1@1800", begin="now+30minutes", comment="demo-c0007-3f2a"))
    assert "#SBATCH --signal=B:USR1@1800" in script
    assert "#SBATCH --begin=now+30minutes" in script
    assert "#SBATCH --comment=demo-c0007-3f2a" in script
    assert "--signal" not in render_script(spec())


@pytest.mark.parametrize("field,value", [
    ("signal", "USR1@1800 --wrap=x"), ("signal", "KILL@10"), ("begin", "tomorrow; rm"), ("comment", "a b"),
    ("comment", "x" * 121),
])
def test_optional_lines_are_validated(field, value):
    with pytest.raises(ValueError):
        render_script(spec(**{field: value}))
