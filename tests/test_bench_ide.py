"""VS Code into the workbench job: the in-job sshd's files, and the ProxyCommand's far end."""

from __future__ import annotations

import io
import os
import subprocess
import time
from pathlib import Path

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
