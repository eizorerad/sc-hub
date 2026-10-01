"""VS Code into the workbench job: the in-job sshd's files, and the ProxyCommand's far end."""

from __future__ import annotations

import io
import os
import subprocess
import time

import pytest

from schub.bench import ide
from schub.bench.workbench import Workbench
from schub.config import Settings
from schub.slurm import Slurm
from tests.conftest import FakeCluster

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIleIF5aaKxLcstRlYLVdUhDmILSsEV+ysjGVW3vh8uv sc-hub test"


def test_setup_writes_a_private_sshd_config_and_keeps_its_host_key(settings: Settings) -> None:
    first = ide.setup(settings, KEY + "\n", user="student")
    home = ide.folder(settings)
    config = (home / "sshd_config").read_text()
    assert f"AuthorizedKeysFile {home / 'authorized_keys'}" in config and "AllowUsers student" in config
    assert "PasswordAuthentication no" in config and "StrictModes yes" in config
    assert (home / "authorized_keys").read_text() == KEY + "\n"
    for name in ("sshd_config", "authorized_keys", "host_ed25519"):
        assert oct((home / name).stat().st_mode & 0o777) == "0o600", name
    assert oct(home.stat().st_mode & 0o777) == "0o700"
    assert ide.setup(settings, KEY, user="student")["host_key"] == first["host_key"]  # VS Code's known host stays


@pytest.mark.parametrize("bad", ["", "not a key", 'command="sh" ' + KEY, KEY + "\n" + KEY,
                                 "ssh-ed25519 AAAA short"])
def test_setup_takes_one_plain_public_key(settings: Settings, bad: str) -> None:
    with pytest.raises(ide.IdeError):
        ide.setup(settings, bad)


def test_the_proxy_starts_sshd_inside_the_running_workbench(settings: Settings, cluster: FakeCluster,
                                                            monkeypatch) -> None:
    ide.setup(settings, KEY, user="student")
    Workbench(settings, Slurm(cluster)).ensure()
    job = next(j for j, name in cluster.names.items() if name.endswith("workbench"))
    cluster.jobs[job] = "RUNNING"
    started = []

    class Child:
        def __init__(self, args, **_):
            started.append(args)
            self.rounds = 0

        def wait(self, timeout=None):
            self.rounds += 1
            if self.rounds == 1:
                raise subprocess.TimeoutExpired("srun", timeout)
            return 0

    monkeypatch.setattr(ide.subprocess, "Popen", Child)
    monkeypatch.setattr(ide, "BEAT_S", 0.01)
    assert ide.proxy(settings, Slurm(cluster), err=io.StringIO()) == 0
    [args] = started
    assert args[:5] == ["srun", f"--jobid={job}", "--overlap", "--quiet", "--unbuffered"]
    assert args[5:9] == [ide.SSHD, "-i", "-f", str(ide.folder(settings) / "sshd_config")]
    assert f"SLURM_JOB_ID={job}" in args[-1] and f"SLURM_JOB_NODELIST=node-{job}" in args[-1]
    assert ide.active(settings)  # the connection counts as activity: no idle stop under VS Code


def test_the_proxy_without_setup_says_so_on_stderr(settings: Settings, cluster: FakeCluster) -> None:
    err = io.StringIO()
    assert ide.proxy(settings, Slurm(cluster), err=err) == 2 and "run the sc-hub setup again" in err.getvalue()


def test_an_old_heartbeat_is_not_activity(settings: Settings) -> None:
    ide.setup(settings, KEY)
    beat = ide.folder(settings) / "active"
    beat.touch()
    assert ide.active(settings)
    os.utime(beat, (time.time() - 600, time.time() - 600))
    assert not ide.active(settings)


# ---- `schub shell`: the student's own terminal in the workbench job (the setup's `schub` command) -------------------------

def running_workbench(settings: Settings, cluster: FakeCluster) -> str:
    Workbench(settings, Slurm(cluster)).ensure()
    job = next(j for j, name in cluster.names.items() if name.endswith("workbench"))
    cluster.jobs[job] = "RUNNING"
    return job


def fake_srun(monkeypatch, started: list) -> None:
    """srun, as a child that records how it was started and ends after one beat of the heartbeat."""

    class Child:
        def __init__(self, args, **_):
            started.append(args)
            self.rounds = 0

        def wait(self, timeout=None):
            self.rounds += 1
            if self.rounds == 1:
                raise subprocess.TimeoutExpired("srun", timeout)
            return 7

    monkeypatch.setattr(ide.subprocess, "Popen", Child)
    monkeypatch.setattr(ide, "BEAT_S", 0.01)


def test_the_shell_is_a_login_shell_in_the_running_workbench(settings: Settings, cluster: FakeCluster,
                                                             monkeypatch) -> None:
    job = running_workbench(settings, cluster)
    started: list = []
    fake_srun(monkeypatch, started)
    assert not ide.folder(settings).exists()  # no VS Code here: nothing of the editor's is set up
    assert ide.shell(settings, Slurm(cluster), err=io.StringIO()) == 7  # what the shell ended with
    assert started == [["srun", f"--jobid={job}", "--overlap", "--pty", "bash", "-l"]]  # like a workstation command
    assert ide.active(settings)  # while it lasts the workbench does not stop for being idle under the student
    assert oct(ide.folder(settings).stat().st_mode & 0o777) == "0o700"


def test_a_command_runs_in_the_job_without_a_terminal(settings: Settings, cluster: FakeCluster, monkeypatch) -> None:
    job = running_workbench(settings, cluster)
    started: list = []
    fake_srun(monkeypatch, started)
    ide.shell(settings, Slurm(cluster), command="echo $SLURM_JOB_ID $(hostname)", err=io.StringIO())
    assert started == [["srun", f"--jobid={job}", "--overlap", "bash", "-lc", "echo $SLURM_JOB_ID $(hostname)"]]


def test_without_start_it_only_looks_and_submits_nothing(settings: Settings, cluster: FakeCluster, monkeypatch) -> None:
    err = io.StringIO()
    started: list = []
    fake_srun(monkeypatch, started)
    assert ide.shell(settings, Slurm(cluster), start=False, command="true", err=err) == ide.NOT_RUNNING == 75
    assert "not running" in err.getvalue() and "schub" in err.getvalue() and not cluster.names and not started


def test_it_starts_a_missing_workbench_and_says_while_it_waits(settings: Settings, cluster: FakeCluster) -> None:
    err = io.StringIO()  # the fake cluster's job stays pending, and there is no time to wait for it
    assert ide.shell(settings, Slurm(cluster), wait_s=0, err=err) == 1
    assert "waiting for your workbench job" in err.getvalue() and "did not start in time" in err.getvalue()
    assert any(name.endswith("workbench") for name in cluster.names.values())  # it was submitted, as VS Code's proxy does


def test_a_stopped_bench_is_not_started_by_a_terminal(settings: Settings, cluster: FakeCluster) -> None:
    from schub.bench.workbench import stop_path

    stop_path(settings).parent.mkdir(parents=True, exist_ok=True)
    stop_path(settings).write_text("")
    err = io.StringIO()
    assert ide.shell(settings, Slurm(cluster), err=err) == 1 and "stopped" in err.getvalue() and not cluster.names


def test_a_stopped_bench_has_its_own_exit_code_and_words_when_only_looked_at(settings: Settings, cluster: FakeCluster) -> None:
    """3 is what the cluster's bootstrap exits with when sc-hub's Python is missing; "not running" and "stopped" are told
    apart too: the first starts when the student types schub, the second does not."""
    from schub.bench.workbench import stop_path

    stop_path(settings).parent.mkdir(parents=True, exist_ok=True)
    stop_path(settings).write_text("")
    err = io.StringIO()
    assert ide.shell(settings, Slurm(cluster), start=False, command="true", err=err) == ide.STOPPED == 76
    assert "stopped" in err.getvalue() and "bench/STOP" in err.getvalue() and "type schub" not in err.getvalue()
    assert ide.NOT_RUNNING != 3 and ide.STOPPED != 3


def test_a_look_that_starts_nothing_is_not_activity(settings: Settings, cluster: FakeCluster, monkeypatch) -> None:
    """The setup's look (`--no-start -c ...`) is not the student at work: it must not keep the workbench from its idle stop,
    or make it go on into a next job near its time limit (found in review)."""
    running_workbench(settings, cluster)

    class Quick:
        def __init__(self, args, **_):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(ide.subprocess, "Popen", Quick)
    assert ide.shell(settings, Slurm(cluster), start=False, command="echo x", err=io.StringIO()) == 0
    assert not ide.active(settings)


def test_ctrl_c_while_it_waits_for_the_job_says_one_line(settings: Settings, cluster: FakeCluster, monkeypatch) -> None:
    """The wait for a starting workbench can last minutes: a Ctrl-C then printed a Python traceback (found in review)."""
    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(ide, "_running_job", interrupted)
    err = io.StringIO()
    assert ide.shell(settings, Slurm(cluster), err=err) == 130
    assert "stopped waiting" in err.getvalue() and "Traceback" not in err.getvalue()


def test_the_setup_helper_reads_the_same_exit_codes() -> None:
    """onboard/ cannot import the cluster's code: its copy of the two numbers must be the real ones."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "onboard"))
    from sc_hub_onboard import steps

    assert (steps.SHELL_NOT_RUNNING, steps.SHELL_STOPPED) == (ide.NOT_RUNNING, ide.STOPPED)
