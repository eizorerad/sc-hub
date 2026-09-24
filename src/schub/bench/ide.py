"""VS Code on the student's laptop, inside the student's own workbench job.

On the laptop, ~/.ssh/config has a host `mbzuai-schub-ide` whose ProxyCommand is
`ssh mbzuai-schub <root>/bin/schub ide-proxy`. The sc-hub gate lets that one command through
the limited key. `ide-proxy` makes sure the workbench job runs, then starts a user-mode sshd
in inetd mode inside that job's allocation (`srun --overlap --unbuffered sshd -i`); its stdin
and stdout are the ssh stream. That sshd accepts the sc-hub key from <root>/ide/authorized_keys,
so VS Code gets a shell and files on the compute node, in the same job where the assistant's
kernels run, never a shell on the login node. No port is opened and nothing runs between
connections (the pilot owner's ~/.ssh/sshd_job/proxy.sh, made part of sc-hub).

While a connection lasts, <root>/ide/active is touched: the workbench does not stop for being
idle, and it arms a successor at its time limit.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO

from ..config import Settings
from ..slurm import Slurm, SlurmError
from .workbench import BenchStopped, Workbench

SSHD = "/usr/sbin/sshd"
BEAT_S = 30
ACTIVE_FOR_S = 180
WAIT_S = 900
PUBLIC_KEY = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)) [A-Za-z0-9+/=]{40,}( [^\n\r]{0,200})?$")
CONFIG = """# sshd for VS Code in the sc-hub workbench job (started per connection by `schub ide-proxy`).
HostKey {host_key}
AuthorizedKeysFile {authorized}
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PidFile none
StrictModes yes
AllowUsers {user}
X11Forwarding no
AllowTcpForwarding yes
PermitTunnel no
ClientAliveInterval 30
ClientAliveCountMax 4
Subsystem sftp internal-sftp
"""


class IdeError(ValueError):
    pass


def folder(settings: Settings) -> Path:
    return settings.root / "ide"


def setup(settings: Settings, public_key: str, user: str | None = None) -> dict:
    """<root>/ide: the host key (once), the student's key, the sshd config."""
    key = public_key.strip()
    if not PUBLIC_KEY.fullmatch(key):
        raise IdeError("expected one public key line (ssh-ed25519 AAAA... comment), without options")
    home = folder(settings)
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    host_key = home / "host_ed25519"
    if not host_key.exists():
        done = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key), "-C",
                               "sc-hub workbench"], capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise IdeError(f"ssh-keygen failed: {done.stderr.strip()}")
    authorized = home / "authorized_keys"
    authorized.write_text(key + "\n")
    authorized.chmod(0o600)
    config = home / "sshd_config"
    config.write_text(CONFIG.format(host_key=host_key, authorized=authorized, user=user or os.environ.get("USER", "")))
    config.chmod(0o600)
    return {"ide": str(home), "host_key": (host_key.with_suffix(".pub")).read_text().strip()}


def active(settings: Settings) -> bool:
    """A VS Code connection is open (or closed less than ACTIVE_FOR_S ago)."""
    try:
        return time.time() - (folder(settings) / "active").stat().st_mtime < ACTIVE_FOR_S
    except OSError:
        return False


def _touch(settings: Settings) -> None:
    try:
        (folder(settings) / "active").touch()
    except OSError:
        pass


def _running_job(bench: Workbench, deadline: float, err: TextIO) -> str:
    said = ""
    while True:
        state = bench.state()
        if state.job_id and state.slurm_state == "RUNNING":
            return state.job_id
        line = f"sc-hub: waiting for your workbench job ({state.slurm_state or 'starting'}{', ' + state.reason if state.reason else ''})"
        if line != said:
            err.write(line + "\n")
            err.flush()
            said = line
        if time.monotonic() > deadline:
            raise IdeError("the workbench job did not start in time; try again in a few minutes")
        time.sleep(5)


def proxy(settings: Settings, slurm: Slurm, wait_s: float = WAIT_S, err: TextIO = sys.stderr) -> int:
    """The ProxyCommand's far end: stdout must carry the ssh stream only, so everything else goes to stderr."""
    config = folder(settings) / "sshd_config"
    if not config.exists():
        err.write("sc-hub: VS Code is not set up for this account; run the sc-hub setup again\n")
        return 2
    _touch(settings)  # counts from the first second: a starting workbench does not idle-stop under the student
    bench = Workbench(settings, slurm)
    try:
        bench.ensure()
        job = _running_job(bench, time.monotonic() + wait_s, err)
        node, partition = slurm.node_of(job)
    except (BenchStopped, SlurmError, IdeError) as exc:
        err.write(f"sc-hub: {exc}\n")
        return 1
    env = (f"SetEnv=SLURM_JOB_ID={job} SLURM_JOBID={job} SLURM_JOB_NODELIST={node} SLURM_JOB_PARTITION={partition} "
           f"SCHUB_ROOT={settings.root} TMPDIR=/tmp")
    process = subprocess.Popen(["srun", f"--jobid={job}", "--overlap", "--quiet", "--unbuffered", SSHD, "-i",
                                "-f", str(config), "-o", env])  # inherits stdin/stdout: the ssh stream
    while True:
        try:
            return process.wait(timeout=BEAT_S)
        except subprocess.TimeoutExpired:
            _touch(settings)
