"""The SSH key gate: what the passphrase-less sc-hub key may run on the cluster."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parents[1] / "scripts" / "schub-gate"


@pytest.fixture
def cluster_home(tmp_path):
    """A home with ~/schub -> the workspace, like on the cluster, and fake programs."""
    root = tmp_path / "lustre" / "schub"
    (root / "bin").mkdir(parents=True)
    (root / "view").mkdir()
    shutil.copy(GATE, root / "bin" / "schub-gate")
    for name, text in (("schub", 'echo "schub $*"'), ("schub-mcp", "echo mcp-server")):
        (root / "bin" / name).write_text(f"#!/usr/bin/env bash\n{text}\n")
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    (fakes / "rsync").write_text('#!/usr/bin/env bash\necho "rsync $*"\n')
    for path in [*(root / "bin").iterdir(), fakes / "rsync"]:
        path.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    (home / "schub").symlink_to(root)
    return home, root, fakes


def gate(cluster_home, command: str | None) -> subprocess.CompletedProcess[str]:
    home, root, fakes = cluster_home
    env = {"HOME": str(home), "PATH": f"{fakes}:{os.environ['PATH']}"}
    if command is not None:
        env["SSH_ORIGINAL_COMMAND"] = command
    return subprocess.run(["bash", str(root / "bin" / "schub-gate")], cwd=home, env=env,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("command, output", [
    ("true", ""),
    ("schub/bin/schub-mcp", "mcp-server"),  # relative to home, through the ~/schub link
    ("{root}/bin/schub-mcp", "mcp-server"),  # absolute, as the installer registers it
    ("schub/bin/schub dashboard >/dev/null", ""),  # schub-view; output discarded
    ("schub/bin/schub session-info jupyter", "schub session-info jupyter"),  # schub-lab
    ("{root}/bin/schub ide-proxy", "schub ide-proxy"),  # VS Code: sshd inside the workbench job
    ("schub/bin/schub view-sum", "schub view-sum"),  # the Windows mirror
    ("schub/bin/schub view-pack full", "schub view-pack full"),
    ("schub/bin/schub view-pack light", "schub view-pack light"),
    ("rsync --server --sender -logDtpre.iLsfxCIvu --safe-links --include /index.html --exclude * . schub/view/",
     "rsync --server --sender -logDtpre.iLsfxCIvu --safe-links --include /index.html --exclude * . schub/view/"),
    ("rsync --server --sender -logDtpre.iLsfxCIvu --safe-links . schub/view/",
     "rsync --server --sender -logDtpre.iLsfxCIvu --safe-links . schub/view/"),  # GNU rsync
    ("rsync --server --sender -g -l -o -p -D -r -t --delete-before --dirs --safe-links --exclude .schub-view . schub/view/",
     "rsync --server --sender -g -l -o -p -D -r -t --delete-before --dirs --safe-links --exclude .schub-view . schub/view/"),
])
def test_sc_hub_commands_pass(cluster_home, command, output):
    root = cluster_home[1].resolve()
    result = gate(cluster_home, command.format(root=root))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == output
    assert "allowed" in (root / "logs" / "gate.log").read_text()


@pytest.mark.parametrize("command", [
    None,  # an interactive login
    "bash",
    "lfs quota -u me -h /l",
    "schub/bin/schub submit abc",  # everything the assistant does goes through the MCP server
    "schub/bin/schub dashboard; rm -rf ~",
    "schub/bin/schub-mcp --debug",
    "schub/bin/schub ide-proxy --debug",
    "schub/bin/schub ide-setup",  # the key's own line is set by the setup with the student's login, not by the key
    "schub/bin/schub view-pack",
    "schub/bin/schub view-pack full /etc",
    "schub/bin/schub view-pack ../x",
    "true && cat ~/.ssh/id_rsa",
    "/tmp/schub/bin/schub-mcp",  # another program of the same name
    "rsync --server --sender -logDtpre.iLsfxCIvu . /etc/",
    "rsync --server --sender --remove-source-files -e.iLsfxCIvu . schub/view/",
    "rsync --server -logDtpre.iLsfxCIvu . schub/view/",  # receiving: would write into the cluster
    "rsync --server --sender -e.iLsfxCIvu . schub/view/../../",
    "scp -f schub/view/index.html",
    "rsync --server --sender --log-file=/l/users/me/x -r . schub/view/",  # would write on the cluster
    "rsync --server --sender --exclude-from=/etc/passwd -r . schub/view/",  # would read another file
    "rsync --server --sender --files-from=- -r . schub/view/",
    "rsync --server --sender -rf merge_/etc/passwd . schub/view/",  # a filter rule: reads another file
    "rsync --server --sender -rL . schub/view/",  # follow symlinks out of the folder
    "rsync --server --sender -rT /tmp . schub/view/",  # an option that takes the next word
    "rsync --server --sender --copy-links -r . schub/view/",
])
def test_everything_else_is_refused(cluster_home, command):
    result = gate(cluster_home, command)
    assert result.returncode == 126 and result.stdout == ""
    assert "this key only opens sc-hub" in result.stderr
    assert "refused" in (cluster_home[1] / "logs" / "gate.log").read_text()


def test_an_exported_cdpath_does_not_confuse_the_gate(cluster_home):
    home, root, fakes = cluster_home
    env = {"HOME": str(home), "PATH": f"{fakes}:{os.environ['PATH']}", "CDPATH": f".:{home}",
           "SSH_ORIGINAL_COMMAND": "schub/bin/schub session-info jupyter"}
    result = subprocess.run(["bash", str(root / "bin" / "schub-gate")], cwd=home, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and result.stdout.strip() == "schub session-info jupyter"
