"""ssh for the onboarding helper: the key, the alias, a password used once, commands on the cluster.

Standard library only: the helper runs on a student's laptop before anything is installed.
The password never goes through the assistant's chat: the student types it on the local page,
it reaches `ssh` through SSH_ASKPASS for the one login that installs the key, and it is dropped.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

HOST = "login-student-lab.mbzu.ae"
ALIAS = "mbzuai-schub"
IDE_ALIAS = "mbzuai-schub-ide"
BEGIN, END = "# >>> sc-hub >>>", "# <<< sc-hub <<<"
LOGIN = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
ASKPASS_MIN = (8, 4)  # OpenSSH that honours SSH_ASKPASS_REQUIRE=force
# Rewrites the sc-hub key's line in authorized_keys to "$OPTS <key>" (the same program as the shell
# installer's KEY_LINE_REMOTE): commented lines are left alone, a copy goes to .schub-backup first.
KEY_LINE_REMOTE = (
    "set -e; umask 077; mkdir -p ~/.ssh; f=~/.ssh/authorized_keys; touch \"$f\"; "
    "if [ -n \"$OPTS\" ]; then test -x \"$R/bin/schub-gate\"; fi; line=$(tr -d '\\r' | head -n 1); "
    "set -- $line; blob=\"$1 $2\"; new=\"${OPTS:+$OPTS }$line\"; cp \"$f\" \"$f.schub-backup\"; tmp=$(mktemp); "
    "awk -v blob=\"$blob\" -v new=\"$new\" '!/^[[:space:]]*#/ && index($0, blob) { if (!done) print new; "
    "done = 1; next } { print } END { if (!done) print new }' \"$f\" > \"$tmp\"; cat \"$tmp\" > \"$f\"; rm -f \"$tmp\""
)


class SshError(RuntimeError):
    pass


@dataclass(frozen=True)
class Paths:
    """Where the helper writes on the laptop. `home` other than the real one is for tests and trials: then
    every ssh call gets -F and a known_hosts file under it, so the real ~/.ssh is never touched."""

    home: Path = field(default_factory=Path.home)

    @property
    def custom(self) -> bool:
        return self.home.resolve() != Path.home().resolve()

    @property
    def ssh_dir(self) -> Path:
        return self.home / ".ssh"

    @property
    def ssh_config(self) -> Path:
        return self.ssh_dir / "config"

    @property
    def key(self) -> Path:
        return self.ssh_dir / "mbzuai_schub_ed25519"

    @property
    def state(self) -> Path:
        return self.home / ".sc-hub" / "onboard.json"

    @property
    def workspace(self) -> Path:
        return self.home / "sc-hub-workspace"


def ssh_version(ssh: str = "ssh") -> tuple[int, int]:
    """(major, minor) of the OpenSSH client, e.g. (9, 6); (0, 0) if unknown."""
    try:
        text = subprocess.run([ssh, "-V"], capture_output=True, text=True, timeout=20).stderr
    except (OSError, subprocess.SubprocessError):
        return (0, 0)
    match = re.search(r"OpenSSH(?:_for_Windows)?_(\d+)\.(\d+)", text)
    return (int(match[1]), int(match[2])) if match else (0, 0)


def check_login(login: str) -> str:
    login = login.strip().lower()
    if not LOGIN.fullmatch(login):
        raise SshError("the cluster login looks like firstname.lastname (letters, digits, dots, dashes)")
    return login


class Ssh:
    def __init__(self, paths: Paths, login: str, host: str = HOST, ssh: str = "ssh") -> None:
        self.paths, self.login, self.host, self.ssh = paths, login, host, ssh
        self.password: str | None = None  # a re-run after the key was limited: setup logs in with the password

    def base(self) -> list[str]:
        args = [self.ssh]
        if self.paths.custom:
            args += ["-F", str(self.paths.ssh_config), "-o", f"UserKnownHostsFile={self.paths.ssh_dir / 'known_hosts'}"]
        return args

    def prepared(self, command: str, password: str | None = None, tty: bool = False
                 ) -> tuple[list[str], dict[str, str], Path | None]:
        """(argv, environment, askpass program to delete afterwards) for `command` on the login node: with the
        key, or with the password (askpass) when one is given or set for this run."""
        args = self.base() + (["-t"] if tty else ["-T"]) + ["-o", "ConnectTimeout=20"]
        env = dict(os.environ)
        askpass = None
        password = password if password is not None else self.password
        if password is not None:
            askpass = _askpass_script()
            args += ["-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
                     "-o", "NumberOfPasswordPrompts=1"]
            env.update(SSH_ASKPASS=str(askpass), SSH_ASKPASS_REQUIRE="force", DISPLAY=env.get("DISPLAY", ":0"),
                       SCHUB_ONBOARD_SECRET=password)
        else:
            args += ["-o", "BatchMode=yes"]
        return args + [ALIAS, command], env, askpass

    def run(self, command: str, stdin: bytes | None = None, password: str | None = None, timeout: int = 900,
            tty: bool = False) -> subprocess.CompletedProcess[bytes]:
        """`command` on the login node: with the key (batch), or with the password (askpass)."""
        args, env, askpass = self.prepared(command, password, tty)
        try:
            return subprocess.run(args, input=stdin, capture_output=True, timeout=timeout, env=env,
                                  stdin=None if stdin is not None else subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise SshError(f"the cluster did not answer within {timeout} s") from exc
        except OSError as exc:
            raise SshError(f"ssh could not start: {exc}") from exc
        finally:
            if askpass is not None:
                shutil.rmtree(askpass.parent, ignore_errors=True)

    def key_works(self) -> bool:
        return self.run("true", timeout=60).returncode == 0

    def install_key(self, password: str, remote_root: str = "", options: str = "") -> None:
        """Write the key's line into authorized_keys (with `options`, e.g. the sc-hub gate), logging in with the
        password once."""
        public = self.paths.key.with_suffix(".pub").read_bytes()
        command = f"R='{remote_root}'; OPTS='{options}'; {KEY_LINE_REMOTE}"
        done = self.run(command, stdin=public, password=password, timeout=120)
        if done.returncode != 0:
            detail = done.stderr.decode(errors="replace").strip().splitlines()
            raise SshError(_login_failure(detail[-1] if detail else f"ssh exited with {done.returncode}"))


def _login_failure(detail: str) -> str:
    if "Permission denied" in detail:
        return "the cluster refused the login: check the login and the password (caps lock?)"
    if "Could not resolve" in detail or "timed out" in detail.lower():
        return f"the cluster cannot be reached from this computer ({detail}); on campus Wi-Fi or the VPN?"
    return detail


def _askpass_script() -> Path:
    """A private folder with a program that prints $SCHUB_ONBOARD_SECRET (ssh runs it instead of asking)."""
    folder = Path(tempfile.mkdtemp(prefix="schub-askpass-"))
    os.chmod(folder, 0o700)
    script = folder / "askpass.py"
    script.write_text("import os, sys\nsys.stdout.write(os.environ.get('SCHUB_ONBOARD_SECRET', '') + '\\n')\n")
    if os.name == "nt":
        program = folder / "askpass.cmd"
        program.write_text(f'@"{sys.executable}" "{script}"\r\n')
    else:
        program = folder / "askpass"
        program.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{script}'\n")
        program.chmod(stat.S_IRWXU)
    return program


def create_key(paths: Paths, login: str) -> bool:
    """The dedicated key, without a passphrase (assistants connect on their own); False if it existed."""
    paths.ssh_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(paths.ssh_dir, 0o700)
    if paths.key.exists():
        return False
    comment = f"sc-hub {login}@{os.uname().nodename if hasattr(os, 'uname') else os.environ.get('COMPUTERNAME', 'pc')}"
    done = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(paths.key), "-C", comment],
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise SshError(f"ssh-keygen failed: {done.stderr.strip()}")
    return True


def alias_block(paths: Paths, login: str, host: str = HOST, ide_proxy: str = "") -> str:
    lines = [BEGIN, f"Host {ALIAS}", f"    HostName {host}", f"    User {login}", f'    IdentityFile "{paths.key}"',
             "    IdentitiesOnly yes", "    StrictHostKeyChecking accept-new", "    ServerAliveInterval 60",
             "    ServerAliveCountMax 3"]
    if ide_proxy:  # VS Code: through the login node (forwarding only) into the workbench job's own sshd
        lines += [f"Host {IDE_ALIAS}", f"    User {login}", f'    IdentityFile "{paths.key}"', "    IdentitiesOnly yes",
                  f"    ProxyCommand {ide_proxy}", "    StrictHostKeyChecking accept-new",
                  f'    UserKnownHostsFile "{paths.ssh_dir / "schub_ide_known_hosts"}"', "    ServerAliveInterval 30"]
    lines += ["Host *", END]  # settings that were at the top of the file keep applying to every host
    return "\n".join(lines) + "\n"


def write_block(path: Path, block: str) -> None:
    """Our block first (ssh uses the first matching value), replacing an earlier one; the rest kept as it was."""
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text() if path.exists() else ""
    rest = strip_block(old)
    temp = path.with_name(f".{path.name}.schub-tmp")
    temp.write_text(block + rest)
    if os.name != "nt":
        os.chmod(temp, 0o600)
    temp.replace(path)


def strip_block(text: str) -> str:
    return re.sub(rf"{re.escape(BEGIN)}.*?{re.escape(END)}\n?", "", text, flags=re.S)


def quote_cmd(args: Sequence[str]) -> str:
    """A command line for ssh_config's ProxyCommand on this OS."""
    if os.name == "nt":
        return subprocess.list2cmdline(list(args))
    import shlex

    return " ".join(shlex.quote(a) for a in args)
