"""sc-hub onto the cluster: the source as one archive over ssh, then the bootstrap in a Slurm job."""

from __future__ import annotations

import base64
import gzip
import io
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Callable

from .sshkit import Ssh, SshError

REPO = Path(__file__).resolve().parents[2]
INCLUDE = ("pyproject.toml", "README.md", "LICENSE", "NOTICE", "src", "scripts", "templates")
SKIP_PARTS = {"__pycache__", ".DS_Store", ".pytest_cache"}
UPLOAD = """set -e
R="${SCHUB_ROOT:-%(root)s}"
mkdir -p "$R/src"; rm -rf "$R/src/sc-hub.new"; mkdir "$R/src/sc-hub.new"
base64 -di | tar -xzf - -C "$R/src/sc-hub.new"
rm -rf "$R/src/sc-hub"; mv "$R/src/sc-hub.new" "$R/src/sc-hub"
printf "%%s" "$R"
"""


def bundle(repo: Path = REPO) -> bytes:
    """The sc-hub source as base64 of a tar.gz (what the shell installer embeds)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in INCLUDE:
            path = repo / name
            files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
            for item in files:
                if SKIP_PARTS & set(item.parts) or item.name.endswith(".egg-info"):
                    continue
                info = tar.gettarinfo(str(item), arcname=str(item.relative_to(repo)))
                info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
                with item.open("rb") as handle:
                    tar.addfile(info, handle)
    return base64.encodebytes(gzip.compress(raw.getvalue(), mtime=0))


def upload(ssh: Ssh, root: str = "/l/users/$USER/schub") -> str:
    done = ssh.run(UPLOAD % {"root": root}, stdin=bundle(), timeout=600)
    remote = done.stdout.decode(errors="replace").strip()
    if done.returncode != 0 or not remote.startswith("/"):
        raise SshError(f"could not copy sc-hub to the cluster: {done.stderr.decode(errors='replace').strip()[-300:]}")
    return remote


def stream(ssh: Ssh, command: str, on_line: Callable[[str], None], timeout: int = 3600) -> int:
    """Run `command` on the login node with the key and hand every output line to `on_line`."""
    args, env, askpass = ssh.prepared(command)
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                               env=env)
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.decode(errors="replace").rstrip()
        if line:
            on_line(line)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        raise SshError(f"{command.split()[0]} did not finish within {timeout // 60} minutes") from None
    finally:
        if askpass is not None:
            shutil.rmtree(askpass.parent, ignore_errors=True)


def remote(ssh: Ssh, command: str, timeout: int = 600) -> str:
    """stdout of `command` on the login node (with the key); SshError with its stderr if it fails."""
    done = ssh.run(command, timeout=timeout)
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).decode(errors="replace").strip()
        raise SshError(detail[-400:] or f"{command.split()[0]} exited with {done.returncode}")
    return done.stdout.decode(errors="replace")
