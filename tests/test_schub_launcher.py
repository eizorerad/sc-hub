"""`schub` on the student's computer (scripts/schub, installed in ~/.sc-hub/bin): typed in a terminal, it lands in the
workbench job on the cluster, like the pilot owner's own `mbzuai` command. Run here against a fake `ssh`."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "schub"
pytestmark = pytest.mark.skipif(os.name == "nt", reason="a shell script for macOS and Linux (Windows uses `ssh schub`)")


@pytest.fixture
def launcher(tmp_path: Path):
    """(run, log): run("login") runs schub with a fake ssh first on PATH; log() is what that ssh was called with."""
    folder = tmp_path / "sc-hub-bin"
    fakes = tmp_path / "fakes"
    folder.mkdir()
    fakes.mkdir()
    shutil.copy(SCRIPT, folder / "schub")
    for name, answer in (("schub-view", "view ran"), ("schub-lab", "lab ran")):  # the launchers next to it
        (folder / name).write_text(f'#!/bin/sh\necho "{answer} $*"\n')
        (folder / name).chmod(0o755)
    ssh = fakes / "ssh"
    ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" | tr "\\n" "\\001" >> "$SSH_LOG"; echo >> "$SSH_LOG"\n'
                   '[ -z "${SSH_SAYS:-}" ] || echo "$SSH_SAYS"\nexit "${SSH_EXIT:-0}"\n')  # (one argument per field)
    ssh.chmod(0o755)
    log = tmp_path / "ssh.log"

    def run(*args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(folder / "schub"), *args], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, env={"PATH": f"{fakes}:/usr/bin:/bin", "SSH_LOG": str(log),
                                                              "HOME": str(tmp_path), **env})

    def calls() -> list[list[str]]:
        """Each ssh call as its list of arguments (not one joined line: a path with a space must stay one word)."""
        return [line.rstrip("\x01").split("\x01") for line in log.read_text().splitlines()] if log.exists() else []

    return run, calls


def test_schub_alone_lands_in_the_workbench_job(launcher) -> None:
    run, calls = launcher
    done = run()
    assert done.returncode == 0 and calls() == [["-t", "schub"]]  # the host that runs `schub shell` on the login node
    assert "workbench job" in done.stdout  # it says where it goes before the wait


def test_login_opens_the_login_node_with_the_own_key(launcher) -> None:
    run, calls = launcher
    assert run("login").returncode == 0 and calls() == [["-t", "mbzuai-login"]]


def test_status_shows_the_students_jobs_through_the_own_key(launcher) -> None:
    run, calls = launcher
    done = run("status", SSH_SAYS="the queue")
    (call,) = calls()
    assert done.returncode == 0 and "the queue" in done.stdout
    assert call[:3] == ["-o", "BatchMode=yes", "mbzuai-login"] and call[3].startswith("squeue --me ") and len(call) == 4


def test_another_ssh_config_is_passed_on_for_trials(launcher, tmp_path: Path) -> None:
    run, calls = launcher
    run(SCHUB_SSH_CONFIG=str(tmp_path / "trial config"))
    assert calls() == [["-F", str(tmp_path / "trial config"), "-t", "schub"]]  # (a path with a space: still one argument)


def test_the_hosts_can_be_overridden(launcher) -> None:
    run, calls = launcher
    run("login", SCHUB_LOGIN_HOST="other-login")
    run(SCHUB_JOB_HOST="other-job")
    assert calls() == [["-t", "other-login"], ["-t", "other-job"]]


def test_view_and_lab_are_the_launchers_next_to_it(launcher) -> None:
    run, calls = launcher
    assert run("view").stdout == "view ran \n" and run("lab", "cellxgene").stdout == "lab ran cellxgene\n"
    assert calls() == []  # no ssh of its own: they have theirs


def test_help_lists_what_it_can_do_and_says_nothing_a_shell_would_mistake(launcher) -> None:
    run, _ = launcher
    for ask in ("help", "-h", "--help"):
        done = run(ask)
        assert done.returncode == 0
        for word in ("schub login", "schub status", "schub view", "schub lab"):
            assert word in done.stdout
        assert "!" not in done.stdout  # pasted into zsh, a "!" starts a history lookup
    unknown = run("frobnicate")
    assert unknown.returncode == 2 and "schub help" in unknown.stderr


def test_a_connection_that_fails_says_what_to_do(launcher) -> None:
    run, _ = launcher
    job = run(SSH_EXIT="255")
    assert job.returncode == 255 and "campus Wi-Fi or the VPN" in job.stderr and "retry terminal" in job.stderr
    own = run("login", SSH_EXIT="255")
    assert own.returncode == 255 and "retry login-key" in own.stderr
    assert run("login", SSH_EXIT="3").returncode == 3  # what the remote shell ended with is passed on, quietly
    assert run("login", SSH_EXIT="3").stderr == ""


def test_an_exported_cdpath_does_not_confuse_it(launcher, tmp_path: Path) -> None:
    """With CDPATH exported, a `cd` in a command substitution prints the folder it went to: the launcher then looked for
    its siblings in a garbage path (found in review, with a relative call)."""
    run, _ = launcher
    folder = tmp_path / "sc-hub-bin"
    done = subprocess.run(["bash", "schub", "view"], cwd=folder, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                          env={"PATH": "/usr/bin:/bin", "CDPATH": f".:{tmp_path}", "HOME": str(tmp_path)})
    assert done.returncode == 0 and done.stdout == "view ran \n", (done.stdout, done.stderr)


def test_it_finds_its_siblings_through_a_link_in_another_folder_of_the_path(launcher, tmp_path: Path) -> None:
    run, _ = launcher
    elsewhere = tmp_path / "usr-local-bin"
    elsewhere.mkdir()
    (elsewhere / "schub").symlink_to(tmp_path / "sc-hub-bin" / "schub")
    done = subprocess.run([str(elsewhere / "schub"), "lab", "cellxgene"], capture_output=True, text=True, timeout=60,
                          stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert done.stdout == "lab ran cellxgene\n", (done.stdout, done.stderr)
