"""The ways into the cluster that the setup writes into ~/.ssh/config (sc-hub's key, VS Code, the student's own key and the
terminal `schub`), what real OpenSSH makes of them, and the program that writes a key into authorized_keys."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ONBOARD = Path(__file__).resolve().parents[1] / "onboard"
REPO = ONBOARD.parent
sys.path.insert(0, str(ONBOARD))

from sc_hub_onboard.sshkit import (  # noqa: E402
    ALIAS, IDE_ALIAS, JOB_ALIAS, KEY_LINE_REMOTE, LOGIN_ALIAS, Paths, Ssh, SshError, alias_block, create_key, public_key)

PROXY = "ssh -T -o BatchMode=yes mbzuai-schub /l/users/test.user/schub/bin/schub ide-proxy"
ROOT = "/l/users/test.user/schub"
REAL_SSH = shutil.which("ssh")


def hosts(block: str) -> dict[str, list[str]]:
    """{"Host line": [its options]} of an ssh config text."""
    found: dict[str, list[str]] = {}
    current = None
    for line in block.splitlines():
        if line.startswith("Host "):
            current = line[5:]
            found[current] = []
        elif current is not None and line.startswith("    "):
            found[current].append(line.strip())
    return found


def everything(paths: Paths) -> str:
    return alias_block(paths, "test.user", ide_proxy=PROXY, login_key=True, shell_root=ROOT)


def effective(config: Path, alias: str) -> dict[str, list[str]]:
    """What real OpenSSH makes of `alias` in `config` (ssh -G prints it without connecting): {option: [values]}."""
    done = subprocess.run([REAL_SSH, "-F", str(config), "-G", alias], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    found: dict[str, list[str]] = {}
    for line in done.stdout.splitlines():
        key, _, value = line.partition(" ")
        found.setdefault(key, []).append(value)
    return found


def test_the_block_has_one_host_for_each_way_in(tmp_path: Path) -> None:
    paths = Paths(home=tmp_path)
    parsed = hosts(everything(paths))
    assert list(parsed) == [ALIAS, IDE_ALIAS, LOGIN_ALIAS, JOB_ALIAS, "*"]
    assert (ALIAS, IDE_ALIAS, LOGIN_ALIAS, JOB_ALIAS) == ("mbzuai-schub", "mbzuai-schub-ide", "mbzuai-login", "schub")
    gate, editor, login, terminal, _ = parsed.values()
    assert f'IdentityFile "{paths.key.as_posix()}"' in gate and f'IdentityFile "{paths.key.as_posix()}"' in editor
    assert f"ProxyCommand {PROXY}" in editor  # VS Code: the job's own sshd, through sc-hub's key and the gate
    for own in (login, terminal):  # the student's own key, never sc-hub's: a normal shell, never the assistants'
        assert f'IdentityFile "{paths.login_key.as_posix()}"' in own and "IdentitiesOnly yes" in own
    assert paths.key != paths.login_key and paths.login_key.name == "mbzuai_schub_login_ed25519"
    assert "RemoteCommand" not in " ".join(login)  # the login node is a plain shell: scp and rsync work through it
    assert f"RemoteCommand {ROOT}/bin/schub shell" in terminal and "RequestTTY force" in terminal


def test_nothing_is_defined_before_it_exists(tmp_path: Path) -> None:
    """No editor host before VS Code is set up, no own-key hosts before the key exists, no terminal before sc-hub's folder
    on the cluster is known."""
    paths = Paths(home=tmp_path)
    assert list(hosts(alias_block(paths, "test.user"))) == [ALIAS, "*"]
    assert list(hosts(alias_block(paths, "test.user", ide_proxy=PROXY))) == [ALIAS, IDE_ALIAS, "*"]
    assert list(hosts(alias_block(paths, "test.user", login_key=True))) == [ALIAS, LOGIN_ALIAS, "*"]
    assert list(hosts(alias_block(paths, "test.user", shell_root=ROOT))) == [ALIAS, "*"]  # (no key: no terminal)


def test_hostile_values_cannot_start_a_line_of_their_own(tmp_path: Path) -> None:
    """They come from the saved state of the helper: a line break would write a new option (a ProxyCommand that runs at the
    next `ssh mbzuai-schub`, which is the assistants' MCP start)."""
    paths = Paths(home=tmp_path)
    for bad in ("x\n    ProxyCommand sh -c evil", "x\r\nHost y", "x\x00"):
        for field in ("login", "proxy"):
            with pytest.raises(SshError):
                alias_block(paths, bad if field == "login" else "test.user", ide_proxy=bad if field == "proxy" else "")
    for bad in ("/l/users/x y/schub", "/l/users/x\n/schub", "relative/path", "/l/users/$HOME/schub", "/l/x'y"):
        with pytest.raises(SshError):
            alias_block(paths, "test.user", login_key=True, shell_root=bad)
    assert alias_block(paths, "test.user", login_key=True, shell_root="/l/users/a-b_c.d/schub-2")


@pytest.mark.skipif(REAL_SSH is None, reason="needs OpenSSH")
def test_real_openssh_reads_the_terminal_as_the_own_key_running_schub_shell(tmp_path: Path) -> None:
    paths = Paths(home=tmp_path)
    config = tmp_path / "config"
    config.write_text(everything(paths))
    terminal = effective(config, "schub")
    assert terminal["remotecommand"] == [f"{ROOT}/bin/schub shell"] and terminal["requesttty"] == ["force"]
    assert terminal["identityfile"] == [str(paths.login_key)] and terminal["user"] == ["test.user"]
    assert terminal["hostname"] == ["login-student-lab.mbzu.ae"] and "proxycommand" not in terminal
    login = effective(config, "mbzuai-login")
    assert login["identityfile"] == [str(paths.login_key)] and "remotecommand" not in login
    gate = effective(config, "mbzuai-schub")
    assert gate["identityfile"] == [str(paths.key)] and "remotecommand" not in gate
    editor = effective(config, "mbzuai-schub-ide")
    assert editor["proxycommand"] == [PROXY] and editor["identityfile"] == [str(paths.key)]
    assert "remotecommand" not in editor  # (it must stay a host that scp and VS Code can use)


@pytest.mark.skipif(REAL_SSH is None, reason="needs OpenSSH")
def test_sc_hubs_key_never_rides_a_connection_the_students_own_key_made(tmp_path: Path) -> None:
    """Many HPC users set `ControlMaster auto` under `Host *`. Connections to the same host and user then share a socket
    whatever the alias, and the assistants' alias would ride the student's own, unlimited one: the gate would never run."""
    paths = Paths(home=tmp_path)
    config = tmp_path / "config"
    config.write_text(everything(paths) + "Host *\n    ControlMaster auto\n    ControlPath ~/.ssh/cm-%C\n    ControlPersist 10m\n")
    gate = effective(config, "mbzuai-schub")
    assert gate["controlmaster"] == ["false"] and "controlpath" not in gate  # (ssh -G leaves out a path that is `none`)
    own = effective(config, "mbzuai-login")
    assert own["controlmaster"] == ["auto"] and own["controlpath"]  # (the student's own settings stay theirs)


def test_ssh_goes_through_the_alias_it_is_given(tmp_path: Path) -> None:
    paths = Paths(home=tmp_path)
    assert Ssh(paths, "test.user").prepared("true")[0][-2:] == [ALIAS, "true"]
    own = Ssh(paths, "test.user", alias=LOGIN_ALIAS)
    assert own.prepared("true")[0][-2:] == [LOGIN_ALIAS, "true"] and "BatchMode=yes" in own.prepared("true")[0]
    assert own.alias == LOGIN_ALIAS


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs OpenSSH")
def test_the_own_key_is_a_key_file_of_its_own_without_a_passphrase(tmp_path: Path) -> None:
    paths = Paths(home=tmp_path)
    assert create_key(paths, "test.user", key=paths.login_key, kind="sc-hub-login") is True
    assert create_key(paths, "test.user", key=paths.login_key, kind="sc-hub-login") is False  # it is kept
    public = paths.login_key.with_suffix(".pub").read_text()
    assert public.startswith("ssh-ed25519 ") and " ".join(public.split()[2:]).startswith("sc-hub-login test.user@")
    assert not paths.key.exists()  # the key that sc-hub limits is a different one
    if sys.platform != "win32":
        assert paths.login_key.stat().st_mode & 0o077 == 0
    assert public_key(paths.login_key) == public.strip()  # (derived the same way a passphrase-less key must be)


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs OpenSSH")
def test_a_key_file_that_is_not_what_it_seems_is_made_right_or_refused(tmp_path: Path) -> None:
    paths = Paths(home=tmp_path)
    create_key(paths, "test.user", key=paths.login_key, kind="sc-hub-login")
    pub = paths.login_key.with_suffix(".pub")
    good = pub.read_text().strip()
    for broken in ("", "\n", "not a key\n", good + "\n" + good + "\n", "ssh-ed25519 AAAA short\n",
                   good.split()[0] + " " + good.split()[1] + " a\\nfrom=\"x\" b\n"):  # a key generation that stopped halfway ...
        pub.write_text(broken)
        assert public_key(paths.login_key).split()[:2] == good.split()[:2]  # ... is made again from the private key
        assert pub.read_text().split()[:2] == good.split()[:2] and len(pub.read_text().splitlines()) == 1
    pub.unlink()
    assert public_key(paths.login_key).startswith("ssh-ed25519 ") and pub.exists()  # no .pub at all: the same
    other = tmp_path / "id_with_a_passphrase"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "secret", "-f", str(other)], check=True, timeout=60)
    other.with_suffix(".pub").unlink()
    with pytest.raises(SshError, match="passphrase"):
        public_key(other)
    (tmp_path / "notakey").write_text("hello\n")
    with pytest.raises(SshError, match="not a key"):
        public_key(tmp_path / "notakey")


def test_the_pages_own_logins_never_ride_a_connection_the_student_opened(tmp_path: Path) -> None:
    """A `Host *` with ControlMaster auto shares one connection per user@host:port: the page's look at the student's own key
    went over a connection the student had opened with the password, and said the key works when it was not even there
    (found in review). Every login of the page makes its own connection."""
    paths = Paths(home=tmp_path)
    for alias in (ALIAS, LOGIN_ALIAS):
        for password in (None, "secret"):
            args, _, askpass = Ssh(paths, "test.user", alias=alias).prepared("echo x", password)
            if askpass is not None:
                shutil.rmtree(askpass.parent, ignore_errors=True)
            before = args[:args.index(alias)]
            assert "ControlPath=none" in before and before[before.index("ControlPath=none") - 1] == "-o"
            assert "ControlMaster=no" in before and before[before.index("ControlMaster=no") - 1] == "-o"


# ---- the program that writes a key's line into authorized_keys (run here with bash and awk, as on the cluster) -------------

KEY_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA sc-hub a@laptop"
KEY_B = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB sc-hub-login a@laptop"
GATE = 'restrict,port-forwarding,command="{root}/bin/schub-gate"'


@pytest.fixture(params=["bash", "sh", "dash", "zsh", "bash -C", "sh -C", "dash -C", "zsh -C"])
def remote(tmp_path: Path, request):
    """(write, lines, home): run the program with a key on stdin as `$OPTS` says, in the account's login shell (the cluster's is
    bash, but a student may have another), also with noclobber on (`set -C` in a student's .bashrc refused `>` onto the
    file mktemp made: PR #13); the account's authorized_keys lines."""
    shell, *flags = request.param.split()
    if shutil.which(shell) is None:
        pytest.skip(f"no {shell} here")
    home = tmp_path / "cluster-home"
    (home / "schub" / "bin").mkdir(parents=True)
    (home / "schub" / "bin" / "schub-gate").write_text("#!/bin/sh\n")
    (home / "schub" / "bin" / "schub-gate").chmod(0o755)
    (home / ".ssh").mkdir()

    def write(stdin: str, opts: str = "", rc: str = "") -> subprocess.CompletedProcess:
        """`rc`: what the account's startup file did to the shell before the command (sshd runs it after ~/.bashrc)."""
        command = f"{rc}R='{home / 'schub'}'; OPTS='{opts.format(root=home / 'schub')}'; {KEY_LINE_REMOTE}"
        return subprocess.run([shell, *flags, "-c", command], input=stdin, capture_output=True, text=True, timeout=60,
                              env={"HOME": str(home), "PATH": os.environ.get("PATH", "")})

    def lines() -> list[str]:
        path = home / ".ssh" / "authorized_keys"
        return path.read_text().splitlines() if path.exists() else []

    return write, lines, home


def test_two_keys_each_change_only_their_own_line(remote) -> None:
    write, lines, home = remote
    other = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC0000000000000000000000000000000000000000 laptop-of-the-student"
    (home / ".ssh" / "authorized_keys").write_text(f"{other}\n# {KEY_A}\n")
    assert write(KEY_A + "\n").returncode == 0 and write(KEY_B + "\n").returncode == 0
    assert write(KEY_A + "\n", GATE).returncode == 0  # the limit step: sc-hub's key only
    assert write(KEY_B + "\n").returncode == 0 and write(KEY_A + "\n", GATE).returncode == 0  # re-runs change nothing
    got = lines()
    assert got[0] == other and got[1] == f"# {KEY_A}"  # the student's own line and a comment: untouched
    assert got[2] == f'restrict,port-forwarding,command="{home / "schub"}/bin/schub-gate" {KEY_A}' and got[3] == KEY_B
    assert len(got) == 4


@pytest.mark.parametrize("bad", ["", "\n", "   \n", "hello\n", "ssh-ed25519\n", "command=\"x\" " + KEY_A + "\n",
                                 "# " + KEY_A + "\n",
                                 "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5\n",  # the start every ed25519 key shares: a prefix
                                 "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\"; rm -rf ~\n",
                                 "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAA*AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA x\n"])
def test_something_that_is_not_a_public_key_line_changes_nothing(remote, bad: str) -> None:
    """An empty line made the match a single space: every key line collapsed into one, the account's own keys included, and
    the backup was overwritten by the damage on the next run."""
    write, lines, home = remote
    original = f"{KEY_A}\nssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC0000000000000000000000000000000000000000 mine\n"
    (home / ".ssh" / "authorized_keys").write_text(original)
    done = write(bad)
    assert done.returncode == 2 and "not a public key line" in done.stderr
    assert (home / ".ssh" / "authorized_keys").read_text() == original and not (home / ".ssh" / "authorized_keys.schub-backup").exists()


def test_a_backslash_or_a_star_in_a_comment_stays_one_harmless_line(remote) -> None:
    write, lines, home = remote
    key = KEY_B.rsplit(" ", 2)[0] + ' pwn\\nrestrict,command="x" * [a-z]?'
    (home / ".ssh" / "authorized_keys").write_text(KEY_A + "\n")
    assert write(key + "\n").returncode == 0
    got = lines()
    assert got[0] == KEY_A and len(got) == 2 and got[1].startswith(KEY_B.rsplit(" ", 2)[0]) and "\\n" in got[1]


@pytest.mark.parametrize("ifs", [":", "\n"])
def test_a_login_shell_with_its_own_ifs_still_reads_the_key(remote, ifs: str) -> None:
    """A .bashrc that sets IFS split the key line its own way: every install failed as "not a public key line" (found in
    review; a setting of the student's login shell, like noclobber in PR #13)."""
    write, lines, home = remote
    done = write(KEY_A + "\n", rc=f"IFS='{ifs}'; ")
    assert done.returncode == 0, done.stderr
    assert lines() == [KEY_A]


def test_the_shell_installers_copy_of_the_program_is_the_same_one() -> None:
    """The shell installer (installer/install.sh.in) keeps its own copy, in single quotes: a change to one that misses the
    other would leave a way in that the page closed."""
    installer = (REPO / "installer" / "install.sh.in").read_text()
    assignment = next(line for line in installer.splitlines() if line.startswith("KEY_LINE_REMOTE="))
    done = subprocess.run(["bash", "-c", assignment + '\nprintf "%s" "$KEY_LINE_REMOTE"'], capture_output=True, text=True,
                          timeout=30)
    assert done.returncode == 0 and done.stdout == KEY_LINE_REMOTE


def test_a_blob_that_is_the_start_of_another_keys_never_rewrites_that_keys_line(remote) -> None:
    """The match is on whole fields, not on a piece of the line: a key whose text is the start of another's (a truncated
    copy, a prefix) used to replace the longer key's line, and the account lost a key."""
    write, lines, home = remote
    longer = KEY_A.split()[0] + " " + KEY_A.split()[1] + "TheRestOfThisLongerKeyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA comment"
    (home / ".ssh" / "authorized_keys").write_text(longer + "\n")
    assert write(KEY_A + "\n").returncode == 0  # a valid key: whose blob is the start of the longer one's
    assert lines() == [longer, KEY_A]  # a line of its own; the other one untouched


def test_a_missing_gate_fails_closed_and_says_so(remote) -> None:
    """Limiting a key needs the gate script: without it (the cluster's /l is down, the root moved) nothing is written, so there
    is never a plain line for a key that was meant to be limited."""
    write, lines, home = remote
    (home / "schub" / "bin" / "schub-gate").unlink()
    (home / ".ssh" / "authorized_keys").write_text(KEY_B + "\n")
    done = write(KEY_A + "\n", GATE)
    assert done.returncode == 3 and "schub-gate is not there" in done.stderr
    assert lines() == [KEY_B]


def test_the_page_checks_a_key_the_same_way_before_it_sends_it(tmp_path: Path) -> None:
    from sc_hub_onboard.sshkit import PUBLIC_KEY

    assert PUBLIC_KEY.fullmatch(KEY_A) and PUBLIC_KEY.fullmatch(KEY_B)
    assert not PUBLIC_KEY.fullmatch("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5") and not PUBLIC_KEY.fullmatch(KEY_A + "\n" + KEY_A)


def test_a_line_that_ends_with_a_carriage_return_is_still_the_same_key(remote) -> None:
    """A plain line with CRLF and no comment (edited on Windows) is accepted by sshd; matching it as another key left it first
    in the file, and sshd uses the first line: the gated line after it never took effect."""
    write, lines, home = remote
    blob = KEY_A.split()[1]
    (home / ".ssh" / "authorized_keys").write_bytes(f"ssh-ed25519 {blob}\r\n".encode())
    assert write(KEY_A + "\n", GATE).returncode == 0
    got = lines()
    assert len(got) == 1 and got[0].startswith("restrict,") and blob in got[0]


def test_a_comment_that_quotes_another_key_does_not_make_the_line_that_key(remote) -> None:
    """Only the first key type on a line starts its key: a comment that contains sc-hub's type and key (a note, a pasted
    line) must not get the whole line replaced, and with it the other key deleted."""
    write, lines, home = remote
    other = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherKeyOfTheStudentXXXXXXXXXXXXXXXXXXXXXXX note "
             + KEY_A.split()[0] + " " + KEY_A.split()[1])
    (home / ".ssh" / "authorized_keys").write_text(other + "\n")
    assert write(KEY_A + "\n").returncode == 0
    assert lines() == [other, KEY_A]
