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
ALIAS = "mbzuai-schub"  # the key that only opens sc-hub (the assistants' way in)
IDE_ALIAS = "mbzuai-schub-ide"  # VS Code's Remote-SSH into the workbench job (the job's own sshd, through sc-hub's key)
JOB_ALIAS = "schub"  # the student's terminal: their own key, then `schub shell` on the login node (srun --pty into the job)
LOGIN_ALIAS = "mbzuai-login"  # the student's own key: the login node, no password
BEGIN, END = "# >>> sc-hub >>>", "# <<< sc-hub <<<"
LOGIN = re.compile(r"^[a-z][a-z0-9._-]{1,63}$")
ASKPASS_MIN = (8, 4)  # OpenSSH that honours SSH_ASKPASS_REQUIRE=force; older ones may still use askpass (tried)
# A password login gets no console on Windows: an ssh that ignores askpass then fails at once instead of
# asking in the helper's own window, where nobody looks.
NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
OWN_CONNECTION = ("-o", "ControlMaster=no", "-o", "ControlPath=none")  # a connection of its own, never a shared one
# What the cluster says when a login with sc-hub's key fails in its gate: the gate refused, or the login shell could not
# start it (the error names it: the cluster's /l is not mounted, the folder moved). Either way that key's line is the limited one.
GATE_SIGNS = (b"schub-gate", b"only opens sc-hub")
GATE_MISSING = "the gate script is not there on the cluster (is /l mounted?): the key was not written"
# sc-hub's folder on the cluster goes into later commands between single quotes, and into ~/.ssh/config: a plain path only.
# Logins look like firstname.lastname, so the default /l/users/<login>/schub has a dot in it.
REMOTE_ROOT = re.compile(r"/[\w./-]+")
# Rewrites a key's line in authorized_keys to "$OPTS <key>" (the same program as the shell installer's KEY_LINE_REMOTE;
# a test keeps them equal): commented lines are left alone, a copy goes to .schub-backup first. The key comes on stdin and
# must be one whole public key line (an empty or cut-short one would match other lines and collapse the file); a line is
# found by its first key type and the key after it, as whole fields (a trailing carriage return does not count; a comment
# that quotes another key does not make the line that key), never as a piece of the text. Limiting a key needs the gate script there:
# without it nothing is written, so a key meant to be limited is never left plain. awk reads its values from the
# environment, where a backslash in a comment means nothing (`awk -v` would turn `\n` into a new line), and the line is
# split by command substitution, which every shell splits (zsh does not split a plain `$line`). `>|` because a login shell
# may have noclobber on (set -C), which refuses `>` onto the file mktemp made and onto authorized_keys (PR #13); `unset IFS`
# because a .bashrc may set IFS too, which would split the key line its own way.
KEY_LINE_REMOTE = (
    "unset IFS; set -e -f; umask 077; mkdir -p ~/.ssh; f=~/.ssh/authorized_keys; touch \"$f\"; "
    "if [ -n \"$OPTS\" ]; then test -x \"$R/bin/schub-gate\" || "
    "{ echo \"sc-hub: the gate $R/bin/schub-gate is not there\" >&2; exit 3; }; fi; "
    "line=$(tr -d '\\r' | head -n 1); set -- $(printf '%s\\n' \"$line\"); ok=0; "
    "case \"${1:-}\" in ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521) ok=1;; esac; "
    "case \"${2:-}\" in \"\"|*[!A-Za-z0-9+/=]*) ok=0;; esac; [ \"${#2}\" -ge 40 ] || ok=0; "
    "[ \"$ok\" = 1 ] || { echo 'sc-hub: that is not a public key line' >&2; exit 2; }; "
    "new=\"${OPTS:+$OPTS }$line\"; cp \"$f\" \"$f.schub-backup\"; tmp=$(mktemp); "
    "TYPE=\"$1\" BLOB=\"$2\" NEW=\"$new\" awk 'function hit(a, n, i, l) { l = $0; sub(/\\r$/, \"\", l); n = split(l, a, /[ \\t]+/); "
    "for (i = 1; i < n; i++) if (a[i] ~ /^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521))$/) "
    "return a[i] == ENVIRON[\"TYPE\"] && a[i + 1] == ENVIRON[\"BLOB\"]; return 0 } "
    "!/^[[:space:]]*#/ && hit() { if (!done) print ENVIRON[\"NEW\"]; done = 1; next } { print } "
    "END { if (!done) print ENVIRON[\"NEW\"] }' \"$f\" >| \"$tmp\"; cat \"$tmp\" >| \"$f\"; rm -f \"$tmp\""
)
# One public key line: a type, the key, a comment of plain words. What goes to the cluster is only ever this.
PUBLIC_KEY = re.compile(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/=]{40,}(?: [\w.@+-]+)*")


class SshError(RuntimeError):
    pass


class GateMissing(SshError):
    """Limiting a key needs sc-hub's gate script on the cluster, and it is not there (the program wrote nothing)."""


class SshUnreachable(SshError):
    """The cluster did not answer (off campus, the VPN, a slow network): worth trying again, not a refusal."""


class AskpassUnsupported(SshError):
    """This ssh did not take the password from the page (an older OpenSSH, e.g. Windows 10's 8.1)."""


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
    def login_key(self) -> Path:
        """The student's own key: a normal one (sc-hub's key is limited to sc-hub), for logging in without a password.
        A name of its own, so no key the student made themselves is ever taken for it."""
        return self.ssh_dir / "mbzuai_schub_login_ed25519"

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
    def __init__(self, paths: Paths, login: str, host: str = HOST, ssh: str = "ssh", alias: str = ALIAS) -> None:
        self.paths, self.login, self.host, self.ssh, self.alias = paths, login, host, ssh, alias
        self.password: str | None = None  # a re-run after the key was limited: setup logs in with the password

    def base(self) -> list[str]:
        args = [self.ssh]
        if self.paths.custom:
            args += ["-F", str(self.paths.ssh_config), "-o", f"UserKnownHostsFile={self.paths.ssh_dir / 'known_hosts'}"]
        return args

    def prepared(self, command: str, password: str | None = None, tty: bool = False, extra: Sequence[str] = ()
                 ) -> tuple[list[str], dict[str, str], Path | None]:
        """(argv, environment, askpass program to delete afterwards) for `command` on the login node: with the
        key, or with the password (askpass) when one is given or set for this run. `extra`: more ssh options
        (a port forward). Never over a connection the student opened (a `Host *` with ControlMaster auto shares one per
        user@host:port): a look at a key must use that key, not a login made with the password or another key."""
        args = self.base() + (["-t"] if tty else ["-T"]) + ["-o", "ConnectTimeout=20", *OWN_CONNECTION, *extra]
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
        return args + [self.alias, command], env, askpass

    def run(self, command: str, stdin: bytes | None = None, password: str | None = None, timeout: int = 900,
            tty: bool = False) -> subprocess.CompletedProcess[bytes]:
        """`command` on the login node: with the key (batch), or with the password (askpass)."""
        args, env, askpass = self.prepared(command, password, tty)
        try:
            done = subprocess.run(args, input=stdin, capture_output=True, timeout=timeout, env=env,
                                  stdin=None if stdin is not None else subprocess.DEVNULL,
                                  **(NO_CONSOLE if askpass is not None else {}))
            if askpass is not None and done.returncode == 255 and not (askpass.parent / "called").exists() and \
                    any(word in done.stderr for word in (b"ermission denied", b"passphrase", b"askpass", b"tty")):
                raise AskpassUnsupported("this computer's ssh does not take the password from this page")
            return done
        except subprocess.TimeoutExpired as exc:
            raise SshUnreachable(f"the cluster did not answer within {timeout} s") from exc
        except OSError as exc:
            raise SshError(f"ssh could not start: {exc}") from exc
        finally:
            if askpass is not None:
                shutil.rmtree(askpass.parent, ignore_errors=True)

    def key_works(self) -> bool:
        return self.run("true", timeout=60).returncode == 0

    def shell_works(self) -> bool:
        """Whether this way in gives a real shell: sc-hub's gate lets `true` through (key_works), but not an `echo`. A network
        that is down is no answer either way: SshUnreachable."""
        done = self.run("echo sc-hub-own", timeout=60)
        if done.returncode == 255:
            failure = explain(done.stderr)
            if isinstance(failure, SshUnreachable):
                raise failure
        return done.returncode == 0 and b"sc-hub-own" in done.stdout

    def install_key_console(self, timeout: int = 600, remote_root: str = "", options: str = "") -> None:
        """For an ssh that ignores askpass (Windows 10): ssh asks for the password itself, in a console window of
        its own; the public key goes in the command, so the window needs nothing but the password."""
        public = public_key(self.paths.key)  # (one plain key line: it goes into the command between single quotes)
        command = f"R='{remote_root}'; OPTS='{options}'; printf '%s\\n' '{public}' | {{ {KEY_LINE_REMOTE}; }}"
        args = self.base() + ["-T", "-o", "ConnectTimeout=20", "-o", "PubkeyAuthentication=no",
                              "-o", "PreferredAuthentications=password,keyboard-interactive", ALIAS, command]
        console = {"creationflags": getattr(subprocess, "CREATE_NEW_CONSOLE", 0)} if os.name == "nt" else \
            {"stdin": subprocess.DEVNULL}  # (elsewhere only in tests: the fake ssh "types" the password)
        try:
            done = subprocess.run(args, timeout=timeout, **console)  # type: ignore[call-overload]
        except subprocess.TimeoutExpired as exc:
            raise SshError("the password window was open for too long; Retry") from exc
        except OSError as exc:
            raise SshError(f"ssh could not start: {exc}") from exc
        if done.returncode == 3:  # the program's own: limiting a key needs the gate script, and it is not there
            raise GateMissing(GATE_MISSING)
        if done.returncode != 0:
            raise SshError(f"ssh ended with exit code {done.returncode} in the password window")

    def install_key(self, password: str, remote_root: str = "", options: str = "") -> None:
        """Write the key's line into authorized_keys (with `options`, e.g. the sc-hub gate), logging in with the
        password once."""
        self.authorize(self.paths.key, remote_root, options, password)

    def authorize(self, key: Path, remote_root: str = "", options: str = "", password: str | None = None) -> None:
        """Write the public key of `key` into authorized_keys on the login node (a line already there is rewritten in
        place, never doubled), logging in the way this object does: with its password, or with a key that can still
        run it. `password` logs in with that one, for this call only. SshError if the key is not one plain key line."""
        public = public_key(key)
        command = f"R='{remote_root}'; OPTS='{options}'; {KEY_LINE_REMOTE}"
        done = self.run(command, stdin=(public + "\n").encode(), password=password, timeout=120)
        if done.returncode == 3 and options:  # the program's own: limiting a key needs the gate script, and it is not there
            raise GateMissing(GATE_MISSING)
        if done.returncode != 0:
            detail = done.stderr.decode(errors="replace").strip().splitlines()
            raise _login_failure(detail[-1] if detail else f"ssh exited with {done.returncode}")


UNREACHABLE = ("could not resolve", "timed out", "connection refused", "no route to host", "network is unreachable",
               "connection reset", "connection closed", "kex_exchange_identification")


def _login_failure(detail: str) -> SshError:
    if "Permission denied" in detail:
        return SshError("the cluster refused the login: check the login and the password (caps lock?)")
    if any(word in detail.lower() for word in UNREACHABLE):
        return SshUnreachable(f"the cluster cannot be reached from this computer ({detail}); on campus Wi-Fi or the VPN?")
    return SshError(detail)


def gated(stderr: bytes) -> bool:
    """Whether a failed login with sc-hub's key failed in its gate (GATE_SIGNS): its line on the cluster is the limited one."""
    return any(sign in stderr for sign in GATE_SIGNS)


def explain(stderr: bytes | str) -> SshError:
    """What ssh's own error text (stderr, its last line) says went wrong: SshUnreachable for a network that is down, else a
    refusal or the text itself."""
    text = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
    lines = text.strip().splitlines()
    return _login_failure(lines[-1] if lines else "")


def public_key(key: Path) -> str:
    """The one-line public key of the private key `key`. Its .pub is made again from the private key when it is missing or
    is not exactly one key (a key generation that stopped halfway, a file copied by hand). SshError if the private key has
    a passphrase or is not a key: a key that needs a passphrase cannot log in without a password."""
    pub = key.with_suffix(".pub")
    try:
        text = pub.read_text().strip()
    except (OSError, UnicodeDecodeError):
        text = ""
    if PUBLIC_KEY.fullmatch(text):
        return text
    try:
        done = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(key)], capture_output=True, text=True, timeout=30,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SshError(f"ssh-keygen could not read {key.name}: {exc}") from exc
    derived = done.stdout.strip()
    if done.returncode != 0 or not PUBLIC_KEY.fullmatch(derived):
        raise SshError(f"{key.name} is not a key without a passphrase (remove {key.name} and run this again)")
    try:
        pub.write_text(derived + "\n")
    except OSError:
        pass  # (the key itself is fine; only the copy beside it could not be written)
    return derived


def _askpass_script() -> Path:
    """A private folder with a program that prints $SCHUB_ONBOARD_SECRET (ssh runs it instead of asking)."""
    folder = Path(tempfile.mkdtemp(prefix="schub-askpass-"))
    os.chmod(folder, 0o700)
    script = folder / "askpass.py"
    script.write_text("import os, sys\n"  # "called": this ssh does use askpass (older ones may not)
                      "open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'called'), 'w').close()\n"
                      "sys.stdout.write(os.environ.get('SCHUB_ONBOARD_SECRET', '') + '\\n')\n")
    if os.name == "nt":
        program = folder / "askpass.cmd"
        program.write_text(f'@"{sys.executable}" "{script}"\r\n')
    else:
        program = folder / "askpass"
        program.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{script}'\n")
        program.chmod(stat.S_IRWXU)
    return program


def create_key(paths: Paths, login: str, key: Path | None = None, kind: str = "sc-hub") -> bool:
    """A key without a passphrase: sc-hub's own by default (the assistants connect on their own), or `key` (the
    student's, `kind` names it in the key's comment); False if it existed."""
    key = key or paths.key
    paths.ssh_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(paths.ssh_dir, 0o700)
    if key.exists():
        return False
    comment = f"{kind} {login}@{os.uname().nodename if hasattr(os, 'uname') else os.environ.get('COMPUTERNAME', 'pc')}"
    done = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key), "-C", comment],
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise SshError(f"ssh-keygen failed: {done.stderr.strip()}")
    return True


def _plain(name: str, value: str) -> str:
    """A value that goes into ~/.ssh/config: nothing that could start a line of its own."""
    if re.search(r"[\x00-\x1f\x7f]", value):
        raise SshError(f"unexpected characters in {name}")
    return value


def alias_block(paths: Paths, login: str, host: str = HOST, ide_proxy: str = "", login_key: bool = False,
                shell_root: str = "") -> str:
    """The ways into the cluster, in the order they came:
    - sc-hub's key, which the cluster limits to sc-hub (`Host mbzuai-schub`): never shared with another connection, or a
      connection made with the student's own key (a normal shell) would carry the assistants' commands past the gate;
    - VS Code's Remote-SSH into the workbench job, once it is set up (`ide_proxy`: how to reach the job's sshd);
    - the student's own key, once it exists: the login node (`mbzuai-login`) and, when sc-hub's folder on the cluster is
      known (`shell_root`), the terminal (`schub`): the same login, running `schub shell` there, like a workstation command."""
    login, host, ide_proxy = _plain("the login", login), _plain("the host", host), _plain("the proxy command", ide_proxy)
    if shell_root and not REMOTE_ROOT.fullmatch(shell_root):
        raise SshError("unexpected characters in sc-hub's folder on the cluster")
    key = paths.key.as_posix()  # forward slashes: Windows' ssh reads them, and no backslash is taken as an escape
    lines = [BEGIN, f"Host {ALIAS}", f"    HostName {host}", f"    User {login}", f'    IdentityFile "{key}"',
             "    IdentitiesOnly yes", "    ControlMaster no", "    ControlPath none", "    StrictHostKeyChecking accept-new",
             "    ServerAliveInterval 60", "    ServerAliveCountMax 3"]
    if ide_proxy:  # VS Code: through the login node (forwarding only) into the workbench job's own sshd
        lines += [f"Host {IDE_ALIAS}", f"    User {login}", f'    IdentityFile "{key}"', "    IdentitiesOnly yes",
                  f"    ProxyCommand {ide_proxy}", "    StrictHostKeyChecking accept-new",
                  f'    UserKnownHostsFile "{(paths.ssh_dir / "schub_ide_known_hosts").as_posix()}"',
                  "    ServerAliveInterval 30"]
    if login_key:  # a normal key, so a normal shell: it is the student's own way in, never the assistants'
        own = ["    IdentitiesOnly yes", "    StrictHostKeyChecking accept-new", "    ServerAliveInterval 60",
               "    ServerAliveCountMax 3"]
        lines += [f"Host {LOGIN_ALIAS}", f"    HostName {host}", f"    User {login}",
                  f'    IdentityFile "{paths.login_key.as_posix()}"', *own]
        if shell_root:  # `ssh schub` = `schub shell` on the login node, on a terminal (no scp through this one)
            lines += [f"Host {JOB_ALIAS}", f"    HostName {host}", f"    User {login}",
                      f'    IdentityFile "{paths.login_key.as_posix()}"', "    RequestTTY force",
                      f"    RemoteCommand {shell_root}/bin/schub shell", *own]
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
