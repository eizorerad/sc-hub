"""The quick setup and its background part (scripts/bootstrap_cluster.sh, scripts/setup_steps.sh), run with
fake Slurm and uv: the student waits only for the analysis tools and the starter datasets; the deep-learning
stack comes in a background job that starts later, says where it stands, is started again only when needed,
and never installs into the environment at the same time as another part."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

FAKE_UV = f"""#!/bin/bash
echo "uv $*" >> "$FAKE_LOG"
case "$1" in
  --version) echo "uv 0.12.17 (fake)";;
  venv) env="${{@: -1}}"; mkdir -p "$env/bin"; cp "$FAKE_PYTHON" "$env/bin/python";;
  pip) if [ "$2" = compile ]; then out=""; prev=""
         for a in "$@"; do [ "$prev" = -o ] && out="$a"; prev="$a"; done
         [ -s "$out" ] && echo "uv compile keeps $(head -n 1 "$out")" >> "$FAKE_LOG"
         printf 'scanpy==1.12.4\\ntorch==2.11.0+cu128\\n' > "$out"
       fi
       [ "$2" = freeze ] && echo "scanpy==1.11.0"
       if [ "$2" = install ]; then
         [ -d "$SCHUB_ROOT/.env-install.lock" ] || echo "uv installed without the lock" >> "$FAKE_LOG"
         sleep "${{FAKE_UV_SLEEP:-0}}"
         [ -n "${{FAKE_UV_FAIL:-}}" ] && exit 1
       fi
       exit 0;;
esac
"""
# The environment's python: sc-hub's own code, except for downloads and the GPU check (logged)
FAKE_PYTHON = f"""#!/bin/bash
case "${{1:-}} ${{2:-}} ${{3:-}}" in
  "-m schub.cli fetch") echo "schub ${{*:3}}" >> "$FAKE_LOG"; [ -z "${{FAKE_FETCH_FAIL:-}}" ]; exit;;
  "-m schub.cli gpu-check") echo "schub gpu-check" >> "$FAKE_LOG"; [ -z "${{FAKE_NO_GPU:-}}" ]; exit;;
esac
PYTHONPATH={REPO / 'src'} exec {sys.executable} "$@"
"""
FAKE_SRUN = """#!/bin/bash
echo "srun $*" >> "$FAKE_LOG"
while [ "${1:-}" != bash ]; do shift; done
exec "$@"
"""
FAKE_SBATCH = """#!/bin/bash
echo "sbatch $*" >> "$FAKE_LOG"
if [ -n "${FAKE_SBATCH_FAIL:-}" ]; then echo 'sbatch: error: Batch job submission failed: "QOS" limit' >&2; exit 1; fi
echo "sbatch: warning: a warning on stderr" >&2
echo "4242;cluster"
"""
FAKE_SQUEUE = """#!/bin/bash
echo "squeue $*" >> "$FAKE_LOG"
if [ -n "${FAKE_SLURM_DOWN:-}" ]; then echo "squeue: error: Socket timed out on send/recv operation" >&2; exit 1; fi
# FAKE_QUEUED: the background job's state (PENDING, RUNNING); without it the job is gone
if [ -n "${FAKE_QUEUED:-}" ]; then echo "$FAKE_QUEUED"; exit 0; fi
echo "slurm_load_jobs error: Invalid job id specified" >&2
exit 1
"""
FAKE_SCHUB = """#!/bin/bash
echo "schub $*" >> "$FAKE_LOG"
[ "$1" = fetch ] && [ -n "${FAKE_FETCH_FAIL:-}" ] && exit 1
[ "$1" = assets ] && [ -n "${FAKE_FETCH_FAIL:-}" ] && echo "pbmc3k"  # what is still missing
# a run of the setup that rewrites the lock right after the install (it waited for the lock)
[ "$1" = gpu-check ] && [ -n "${FAKE_LOCK_CHANGES:-}" ] && echo "numpy==3.0" >> "$SCHUB_ROOT/env-lock.txt"
[ "$1" = gpu-check ] && [ -n "${FAKE_NO_GPU:-}" ] && exit 1
exit 0
"""


def fake(folder: Path, name: str, text: str) -> None:
    path = folder / name
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def cluster(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    for name, text in (("srun", FAKE_SRUN), ("sbatch", FAKE_SBATCH), ("squeue", FAKE_SQUEUE)):
        fake(bin_dir, name, text)
    root = tmp_path / "root"
    (root / "bin").mkdir(parents=True)
    fake(root / "bin", "uv", FAKE_UV)  # the right version: bootstrap does not download one
    fake(tmp_path, "python", FAKE_PYTHON)
    log = tmp_path / "calls.log"
    log.touch()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SCHUB_ROOT": str(root), "HOME": str(tmp_path),
           "FAKE_LOG": str(log), "FAKE_PYTHON": str(tmp_path / "python"), "SCHUB_LIBRARY": "",
           "SCHUB_LOCK_POLL_S": "1"}
    env.pop("SLURM_JOB_ID", None)
    env.pop("SCHUB_PYTHON", None)
    return {"root": root, "log": log, "env": env, "home": tmp_path}


def bootstrap(cluster: dict, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(REPO / "scripts" / "bootstrap_cluster.sh")], capture_output=True, text=True,
                          env={**cluster["env"], **extra}, timeout=300)


def calls(cluster: dict) -> list[str]:
    return cluster["log"].read_text().splitlines()


def count(cluster: dict, command: str) -> int:
    return sum(c.startswith(command) for c in calls(cluster))


def status_of(cluster: dict) -> dict:
    return json.loads((cluster["root"] / "setup-extras.json").read_text())


def test_the_student_waits_only_for_the_analysis_tools_and_the_datasets(cluster: dict) -> None:
    done = bootstrap(cluster)
    assert done.returncode == 0, done.stdout + done.stderr
    log = calls(cluster)
    [srun] = [c for c in log if c.startswith("srun")]
    assert "--gres" not in srun  # the quick part needs no GPU
    install = [c for c in log if c.startswith("uv pip install")]
    assert len(install) == 1 and "[analysis]" in install[0] and "-c" in install[0] and "ml]" not in install[0]
    assert any(c.startswith("uv pip compile") and "--extra ml" in c for c in log)  # the whole set, resolved once
    assert log.index(install[0]) < log.index("schub fetch -- pbmc3k kang2018 celltypist")  # in the quick part
    [sbatch] = [c for c in log if c.startswith("sbatch")]
    assert "--begin=now+10minutes" in sbatch and "--gres=gpu:1" in sbatch and sbatch.endswith("setup_steps.sh extras")
    assert "--job-name=schub-" in sbatch and "-setup-extras " in sbatch  # this folder's own job names
    status = status_of(cluster)
    assert status["state"] == "queued" and status["job"] == "4242"  # (sbatch's warning and ";cluster" left out)
    assert "you can start working now" in done.stdout and "about 5 minutes" in done.stdout
    assert "uv installed without the lock" not in log and not (cluster["root"] / ".env-install.lock").exists()
    assert (cluster["root"] / "AGENTS.md").exists()


def test_a_second_run_queues_the_background_part_only_when_needed(cluster: dict) -> None:
    assert bootstrap(cluster).returncode == 0
    for state in ("PENDING", "RUNNING"):  # still on its way: not queued twice (the quick part waits for its lock)
        again = bootstrap(cluster, FAKE_QUEUED=state)
        assert again.returncode == 0 and "is still on its way" in again.stdout
    assert count(cluster, "sbatch") == 1
    lock_sum = subprocess.run(["sha256sum", str(cluster["root"] / "env-lock.txt")], capture_output=True,
                              text=True).stdout[:16]
    status = cluster["root"] / "setup-extras.json"
    status.write_text(json.dumps({"state": "done", "job": "4242", "lock": lock_sum}))
    again = bootstrap(cluster)
    assert "are installed" in again.stdout and count(cluster, "sbatch") == 1
    status.write_text(json.dumps({"state": "failed", "job": "4242", "step": "the GPU check"}))
    assert bootstrap(cluster).returncode == 0 and count(cluster, "sbatch") == 2
    status.write_text(json.dumps({"state": "done", "job": "4242", "lock": "an-older-lock"}))  # new versions since
    assert bootstrap(cluster).returncode == 0 and count(cluster, "sbatch") == 3
    status.write_text(json.dumps({"state": "running", "job": "4242"}))  # its job ended without a word
    assert bootstrap(cluster).returncode == 0 and count(cluster, "sbatch") == 4


def test_when_slurm_does_not_answer_nothing_is_queued_twice(cluster: dict) -> None:
    assert bootstrap(cluster).returncode == 0
    again = bootstrap(cluster, FAKE_SLURM_DOWN="1")
    assert again.returncode == 0 and "Slurm did not answer" in again.stdout
    assert count(cluster, "sbatch") == 1 and status_of(cluster)["state"] == "queued"


def test_a_refused_background_job_does_not_undo_the_setup(cluster: dict) -> None:
    done = bootstrap(cluster, FAKE_SBATCH_FAIL="1")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "could not queue the background install" in done.stdout and (cluster["root"] / "AGENTS.md").exists()
    status = status_of(cluster)
    assert status["state"] == "failed" and "QOS" in status["error"] and '"' not in status["error"]
    assert bootstrap(cluster).returncode == 0 and count(cluster, "sbatch") == 2  # the next run queues it


def test_without_jobs_the_background_part_runs_right_there(cluster: dict) -> None:
    done = bootstrap(cluster, SCHUB_INSTALL_MODE="login", SLURM_JOB_ID="777")  # e.g. from inside the workbench
    assert done.returncode == 0, done.stdout + done.stderr
    assert count(cluster, "srun") == 0 and count(cluster, "sbatch") == 0
    assert any("[analysis,ml]" in c for c in calls(cluster))
    status = status_of(cluster)
    assert status["state"] == "done" and status["job"] == ""  # not the enclosing job's id
    local = (cluster["root"] / "logs" / "setup-extras-local.log").read_text()
    assert "no GPU check here" in local and "the deep-learning tools are installed" in local


def steps(cluster: dict, part: str, **extra: str) -> subprocess.CompletedProcess:
    fake(cluster["root"] / "bin", "schub", FAKE_SCHUB)
    env = {**cluster["env"], "SCHUB_SRC": str(REPO), "SCHUB_ASSETS": "pbmc3k kang2018", "SLURM_JOB_ID": "4242",
           **extra}
    (cluster["root"] / "logs").mkdir(exist_ok=True)
    return subprocess.run(["bash", str(REPO / "scripts" / "setup_steps.sh"), part], capture_output=True, text=True,
                          env=env, timeout=120)


def test_running_the_setup_again_keeps_the_versions_already_there(cluster: dict) -> None:
    assert steps(cluster, "quick").returncode == 0
    assert not any(c.startswith("uv compile keeps") for c in calls(cluster))  # the first time: a fresh resolution
    assert steps(cluster, "quick").returncode == 0
    assert "uv compile keeps scanpy==1.12.4" in calls(cluster)  # the lock's own pins
    (cluster["root"] / "env-lock.txt").unlink()  # an environment set up before the lock existed
    assert steps(cluster, "quick").returncode == 0
    assert "uv compile keeps scanpy==1.11.0" in calls(cluster)  # what it has installed
    assert not (cluster["root"] / "env-lock.txt.new").exists()


def test_a_failed_download_does_not_stop_the_quick_part_and_is_noted(cluster: dict) -> None:
    done = steps(cluster, "quick", FAKE_FETCH_FAIL="1")
    assert done.returncode == 0 and "could not download: pbmc3k" in done.stdout
    assert (cluster["root"] / "setup-datasets-missing.txt").read_text().split() == ["pbmc3k"]
    assert steps(cluster, "quick").returncode == 0  # the next run gets it
    assert not (cluster["root"] / "setup-datasets-missing.txt").exists()


def test_the_background_part_installs_the_rest_and_says_where_it_stands(cluster: dict) -> None:
    assert steps(cluster, "quick").returncode == 0
    done = steps(cluster, "extras")
    assert done.returncode == 0, done.stdout + done.stderr
    log = calls(cluster)
    fetch = len(log) - 1 - log[::-1].index("schub fetch -- pbmc3k kang2018")  # another try at a missing one
    install = next(i for i, c in enumerate(log) if c.startswith("uv pip install") and "[analysis,ml]" in c)
    assert fetch < install < log.index("schub gpu-check")
    assert "-c" in log[install].split()  # the versions of the one resolution: nothing installed changes
    assert "uv installed without the lock" not in log
    status = status_of(cluster)
    assert status["state"] == "done" and status["job"] == "4242" and status["started"] and status["lock"]


def test_a_failed_download_does_not_fail_the_background_part(cluster: dict) -> None:
    steps(cluster, "quick")
    done = steps(cluster, "extras", FAKE_FETCH_FAIL="1")
    assert done.returncode == 0 and status_of(cluster)["state"] == "done"
    assert (cluster["root"] / "setup-datasets-missing.txt").read_text().split() == ["pbmc3k"]


def test_done_names_the_versions_it_installed(cluster: dict) -> None:
    steps(cluster, "quick")
    lock = cluster["root"] / "env-lock.txt"
    installed = subprocess.run(["sha256sum", str(lock)], capture_output=True, text=True).stdout[:16]
    assert steps(cluster, "extras", FAKE_LOCK_CHANGES="1").returncode == 0
    now = subprocess.run(["sha256sum", str(lock)], capture_output=True, text=True).stdout[:16]
    assert status_of(cluster)["lock"] == installed != now  # so the next run installs the new versions


def test_a_lock_that_cannot_be_made_fails_instead_of_waiting(cluster: dict) -> None:
    steps(cluster, "quick")
    cluster["root"].chmod(0o500)  # (a full quota says the same to mkdir)
    try:
        done = steps(cluster, "quick")
    finally:
        cluster["root"].chmod(0o700)
    assert done.returncode != 0 and "waiting for the other install" not in done.stdout


def test_a_failed_install_or_gpu_check_says_which_step_stopped(cluster: dict) -> None:
    steps(cluster, "quick")
    done = steps(cluster, "extras", FAKE_UV_FAIL="1")
    status = status_of(cluster)
    assert done.returncode != 0 and status["state"] == "failed"
    assert status["step"] == "the deep-learning tools (torch, scvi-tools)" and "log:" in status["error"]
    assert not (cluster["root"] / ".env-install.lock").exists()  # released on the way out
    done = steps(cluster, "extras", FAKE_NO_GPU="1")
    status = status_of(cluster)
    assert done.returncode == 1 and status["state"] == "failed" and status["step"] == "the GPU check"
    assert "saw no GPU" in status["error"]


def test_slurm_stopping_the_job_is_written_down(cluster: dict) -> None:
    steps(cluster, "quick")
    fake(cluster["root"] / "bin", "schub", FAKE_SCHUB)
    env = {**cluster["env"], "SCHUB_SRC": str(REPO), "SCHUB_ASSETS": "pbmc3k", "SLURM_JOB_ID": "4242",
           "FAKE_UV_SLEEP": "30"}
    job = subprocess.Popen(["bash", str(REPO / "scripts" / "setup_steps.sh"), "extras"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 20
    status = cluster["root"] / "setup-extras.json"
    while not (status.exists() and "deep-learning" in status.read_text()) and time.monotonic() < deadline:
        time.sleep(0.2)  # (the status names the deep-learning step: the install is under way)
    time.sleep(0.5)
    os.killpg(job.pid, signal.SIGTERM)  # what Slurm does at the time limit or on scancel: the whole job
    assert job.wait(timeout=20) == 143
    status = status_of(cluster)
    assert status["state"] == "failed" and "stopped by Slurm" in status["error"]
    assert not (cluster["root"] / ".env-install.lock").exists()


def test_one_install_at_a_time_and_a_lock_left_by_a_killed_job_is_taken_over(cluster: dict) -> None:
    busy = cluster["root"] / ".env-install.lock"
    busy.mkdir()
    (busy / "owner").write_text("99.ws-l1-001.4321\njob 99 on ws-l1-001 since 10:00\n")  # (its mark, then who)
    fake(cluster["root"] / "bin", "schub", FAKE_SCHUB)
    waiting = subprocess.Popen(["bash", str(REPO / "scripts" / "setup_steps.sh"), "quick"],
                               env={**cluster["env"], "SCHUB_SRC": str(REPO), "SCHUB_ASSETS": "pbmc3k"},
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(3)
    assert waiting.poll() is None and count(cluster, "uv pip install") == 0  # it waits for the other one
    shutil.rmtree(busy)  # the other install is done
    out, _ = waiting.communicate(timeout=60)
    assert waiting.returncode == 0 and "waiting for the other install" in out and "job 99" in out
    busy.mkdir()
    old = time.time() - 20 * 60  # not refreshed for 20 minutes: its holder was killed
    os.utime(busy, (old, old))
    done = steps(cluster, "quick")
    assert done.returncode == 0 and not busy.exists()
