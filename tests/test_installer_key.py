"""The installer limits the sc-hub key to sc-hub, detects a limited key on re-runs
(so update steps use the password) and can undo the limit. Run against a fake
cluster: ssh is replaced by a function that applies authorized_keys the way sshd
does (a forced command runs the real schub-gate with SSH_ORIGINAL_COMMAND)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTestsOnly0000000000000000000000 sc-hub test"
OTHER = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOwnKeyOfTheStudent000000000000000000000 laptop"

FAKE_SSH = r'''
sshx() {  # sshx [-o opt]... ALIAS COMMAND: the fake cluster's sshd
  local password=0 args=()
  while [ "$1" = "-o" ]; do [ "$2" = "PubkeyAuthentication=no" ] && password=1; shift 2; done
  shift  # the alias
  local command="$*" line
  if [ "$password" = 1 ]; then echo password >> "$CALLS"; HOME="$CLUSTER_HOME" bash -c "$command"; return; fi
  line="$(grep -F "${KEY_BLOB}" "$CLUSTER_HOME/.ssh/authorized_keys" | grep -v '^[[:space:]]*#' | head -n 1)"
  [ -n "$line" ] || { echo "Permission denied (publickey)" >&2; return 255; }
  case "$line" in
    *command=*) HOME="$CLUSTER_HOME" SSH_ORIGINAL_COMMAND="$command" bash "$CLUSTER_HOME/schub/bin/schub-gate" ;;
    *) HOME="$CLUSTER_HOME" bash -c "$command" ;;
  esac
}
'''


@pytest.fixture
def cluster(tmp_path):
    home = tmp_path / "cluster-home"
    (home / ".ssh").mkdir(parents=True)
    (home / "schub" / "bin").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "schub-gate", home / "schub" / "bin" / "schub-gate")
    (home / "schub" / "bin" / "schub-gate").chmod(0o755)
    (home / ".ssh" / "authorized_keys").write_text(f"{OTHER}\n# {KEY}\n{KEY}\n")
    laptop = tmp_path / "laptop"
    laptop.mkdir()
    (laptop / "key.pub").write_text(KEY + "\n")
    return home, laptop


def run(cluster, body: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    home, laptop = cluster
    installer = (REPO / "installer" / "install.sh.in").read_text()
    functions = installer[: installer.index("\nmain() {")]
    script = (f"{functions}\n{FAKE_SSH}\nALIAS=mbzuai-schub KEY='{laptop / 'key'}' REMOTE_ROOT='{home / 'schub'}'\n"
              f"CLUSTER_HOME='{home}' KEY_BLOB='{' '.join(KEY.split()[:2])}' CALLS='{laptop / 'calls'}'\n{body}\n")
    return subprocess.run(["bash", "-c", script, "install", "student"], capture_output=True, text=True, timeout=60,
                          env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(laptop), **(env or {})})


def keys(cluster) -> list[str]:
    return (cluster[0] / ".ssh" / "authorized_keys").read_text().splitlines()


def test_first_install_limits_the_key_and_leaves_other_lines_alone(cluster):
    result = run(cluster, "set_key_access; key_is_limited && echo LIMITED")
    assert result.returncode == 0, result.stderr
    assert "LIMITED" in result.stdout and "WARNING" not in result.stderr
    other, commented, limited = keys(cluster)
    assert other == OTHER and commented == f"# {KEY}"  # untouched, the comment too
    assert limited == f'restrict,port-forwarding,command="{cluster[0] / "schub"}/bin/schub-gate" {KEY}'
    assert (cluster[0] / ".ssh" / "authorized_keys.schub-backup").exists()


def test_a_rerun_detects_the_limited_key_and_uses_the_password(cluster):
    run(cluster, "set_key_access")
    result = run(cluster, 'choose_admin_access; echo "LIMITED=$KEY_LIMITED"; set_key_access')
    assert "LIMITED=1" in result.stdout and "type your cluster password" in result.stdout
    assert (cluster[1] / "calls").read_text().splitlines() == ["password"]  # the update went by password
    assert sum(KEY.split()[1] in line and not line.startswith("#") for line in keys(cluster)) == 1


def test_the_limit_can_be_removed_again(cluster):
    run(cluster, "set_key_access")
    result = run(cluster, "choose_admin_access; set_key_access; key_is_limited || echo NORMAL",
                 env={"SCHUB_KEY_UNRESTRICTED": "1"})
    assert "removing the limit" in result.stdout and "NORMAL" in result.stdout
    assert keys(cluster)[-1] == KEY


def test_a_failed_change_warns_instead_of_stopping_the_installer(cluster):
    (cluster[0] / "schub" / "bin" / "schub-gate").unlink()  # bootstrap did not install the gate
    result = run(cluster, "set_key_access; echo STILL-RUNNING")
    assert "STILL-RUNNING" in result.stdout and "could not change the key's access" in result.stderr
    assert keys(cluster)[-1] == KEY  # left as it was


def test_a_limited_key_whose_gate_is_gone_is_repaired_not_duplicated(cluster):
    run(cluster, "set_key_access")
    (cluster[0] / "schub" / "bin" / "schub-gate").unlink()  # the workspace was deleted
    result = run(cluster, "install_key; echo KEY-WORKS")
    assert "KEY-WORKS" in result.stdout, result.stderr
    live = [line for line in keys(cluster) if KEY.split()[1] in line and not line.startswith("#")]
    assert live == [KEY]  # replaced in place: sshd would use a first, broken line
    assert (cluster[1] / "calls").read_text().splitlines() == ["password"]
