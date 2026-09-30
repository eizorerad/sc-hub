"""Codex and Claude Code on the student's own PATH on the cluster: the block the setup writes into the shell's files.

The official installers skip this when ~/.local/bin is already on PATH, which it is in the setup's own commands,
so a student who logged in to the cluster found no `codex` and no `claude`. The program runs on the login node
with python3; here it runs with this Python in a folder that stands in for the home.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ONBOARD = Path(__file__).resolve().parents[1] / "onboard"
sys.path.insert(0, str(ONBOARD))

from sc_hub_onboard.cluster_agents import INSTALL, REGISTER_PATH, login_path  # noqa: E402

SKEL_BASHRC = "# ~/.bashrc\ncase $- in\n    *i*) ;;\n      *) return;;\nesac\nHISTSIZE=1000\n"
SKEL_PROFILE = 'if [ -n "$BASH_VERSION" ]; then\n    if [ -f "$HOME/.bashrc" ]; then\n\t. "$HOME/.bashrc"\n    fi\nfi\n'
BEGIN, END = "# >>> sc-hub >>>", "# <<< sc-hub <<<"


def register(home: Path, shell: str = "/bin/bash", before: str = "") -> list[str]:
    done = subprocess.run([sys.executable, "-"], input=before + REGISTER_PATH, capture_output=True, text=True,
                          timeout=60, env={"HOME": str(home), "SHELL": shell, "PATH": os.environ.get("PATH", "")})
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    folder = tmp_path / "home"
    folder.mkdir()
    (folder / ".bashrc").write_text(SKEL_BASHRC)
    (folder / ".profile").write_text(SKEL_PROFILE)
    return folder


def test_a_new_account_gets_the_block_in_bashrc_and_profile(home: Path) -> None:
    lines = register(home)
    assert lines == ["PATH: added to ~/.bashrc", "PATH: added to ~/.profile"]
    for name, before in ((".bashrc", SKEL_BASHRC), (".profile", SKEL_PROFILE)):
        text = (home / name).read_text()
        assert text.startswith(before) and text.count(BEGIN) == 1 and text.rstrip().endswith(END)
        assert 'export PATH="$HOME/.local/bin:$PATH"' in text and "echo" not in text.split(BEGIN)[1]


def test_running_it_again_changes_nothing(home: Path) -> None:
    register(home)
    first = (home / ".bashrc").read_text(), (home / ".profile").read_text()
    assert register(home) == ["PATH: ~/.bashrc already has it", "PATH: ~/.profile already has it"]
    assert ((home / ".bashrc").read_text(), (home / ".profile").read_text()) == first


def test_an_older_block_in_the_middle_is_replaced_by_one_at_the_end(home: Path) -> None:
    (home / ".bashrc").write_text(f"a=1\n{BEGIN}\nexport PATH=/old:$PATH\n{END}\nb=2\n")
    register(home)
    text = (home / ".bashrc").read_text()
    assert text.count(BEGIN) == 1 and "/old" not in text and text.startswith("a=1\nb=2\n\n" + BEGIN)


def test_a_lost_end_marker_leaves_the_file_alone_instead_of_eating_lines(home: Path) -> None:
    """Found in review: with the end marker edited away, the next run would have removed everything from the old
    begin marker down to the new block's end, the student's own lines included."""
    text = f"{SKEL_BASHRC}{BEGIN}\nexport PATH=$HOME/.local/bin:$PATH\nalias ll='ls -l'\nexport MINE=1\n"
    (home / ".bashrc").write_text(text)
    lines = register(home)
    assert lines[0] == "PATH: left ~/.bashrc alone (its sc-hub lines were edited; remove them and run this again)"
    assert (home / ".bashrc").read_text() == text and register(home)[0] == lines[0]


def test_line_endings_stay_as_they_were(home: Path) -> None:
    (home / ".bashrc").write_bytes(b"alias a=b\r\nexport X=1\r\n")
    register(home)
    raw = (home / ".bashrc").read_bytes()
    assert raw.startswith(b"alias a=b\r\nexport X=1\r\n\r\n# >>> sc-hub >>>\r\n") and b"\n" not in raw.replace(b"\r\n", b"")
    before = raw
    assert register(home)[0] == "PATH: ~/.bashrc already has it" and (home / ".bashrc").read_bytes() == before


def test_a_file_another_account_owns_is_left_alone(home: Path) -> None:
    """A dotfile linked to a shared lab file: replacing it would make it the student's and change it for everyone."""
    lines = register(home, before="import os\nos.getuid = lambda: 4242\n")  # every file here is someone else's now
    assert lines == ["PATH: left ~/.bashrc alone (another account owns it)",
                     "PATH: left ~/.profile alone (another account owns it)"]
    assert (home / ".bashrc").read_text() == SKEL_BASHRC


def test_a_dotfile_that_is_a_link_is_edited_where_it_lives(home: Path) -> None:
    dotfiles = home / ".bash"
    dotfiles.mkdir()
    (dotfiles / "bashrc").write_text(SKEL_BASHRC)
    (home / ".bashrc").unlink()
    (home / ".bashrc").symlink_to(".bash/bashrc")
    register(home)
    assert (home / ".bashrc").is_symlink() and BEGIN in (dotfiles / "bashrc").read_text()


def test_a_login_shell_reads_bash_profile_first_so_the_block_goes_there(home: Path) -> None:
    (home / ".bash_profile").write_text("export EDITOR=vim\n")
    assert register(home) == ["PATH: added to ~/.bashrc", "PATH: added to ~/.bash_profile"]
    assert BEGIN not in (home / ".profile").read_text() and BEGIN in (home / ".bash_profile").read_text()


def test_missing_files_are_created_and_zsh_gets_its_own(tmp_path: Path) -> None:
    empty = tmp_path / "empty-home"
    empty.mkdir()
    assert register(empty, shell="/usr/bin/zsh") == ["PATH: added to ~/.bashrc", "PATH: added to ~/.profile",
                                                     "PATH: added to ~/.zshrc"]
    assert (empty / ".bashrc").read_text().startswith(BEGIN)


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), reason="POSIX file modes")
def test_a_file_it_cannot_write_is_reported_and_the_rest_still_done(home: Path) -> None:
    (home / ".profile").chmod(0o444)
    home_mode = home.stat().st_mode
    home.chmod(0o555)  # nor a new file next to it
    try:
        lines = register(home)
    finally:
        home.chmod(home_mode)
    assert lines[1].startswith("PATH: could not write ~/.profile") and lines[0].startswith("PATH: could not write ~/.bashrc")
    assert not list(home.glob(".schub-*"))  # no temporary file left behind
    (home / ".profile").chmod(0o644)
    assert BEGIN not in (home / ".profile").read_text()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_login_on_the_cluster_then_finds_codex_and_claude(home: Path) -> None:
    """What a student gets on their next ssh login (bash -l reads .profile, which reads .bashrc)."""
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("codex", "claude"):
        (bin_dir / name).write_text("#!/bin/sh\necho ok\n")
        (bin_dir / name).chmod(0o755)
    register(home)
    done = subprocess.run(["bash", "-c", login_path + '\necho "PATH at login: $(login_path)"'], capture_output=True,
                          text=True, timeout=60, env={"HOME": str(home), "SHELL": "/bin/bash", "PATH": "/usr/bin:/bin"})
    assert f"{bin_dir}/codex" in done.stdout and f"{bin_dir}/claude" in done.stdout, (done.stdout, done.stderr)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_login_check_speaks_only_for_the_shells_whose_files_it_wrote(home: Path) -> None:
    for shell in ("/usr/bin/fish", "/bin/tcsh"):
        done = subprocess.run(["bash", "-c", login_path + '\necho "PATH at login: [$(login_path)]"'], capture_output=True,
                              text=True, timeout=60, env={"HOME": str(home), "SHELL": shell, "PATH": "/usr/bin:/bin"})
        assert done.stdout.strip() == "PATH at login: []", shell


def test_the_install_registers_the_path_and_checks_a_login() -> None:
    assert "python3 - <<'SCHUB_PATH'" in INSTALL and REGISTER_PATH.strip() in INSTALL
    assert 'echo "PATH at login: $(login_path)"' in INSTALL
    # the installers still run first, and a failed PATH update does not fail the install
    assert INSTALL.index("chatgpt.com/codex/install.sh") < INSTALL.index("SCHUB_PATH")
    assert "|| echo \"PATH: could not update" in INSTALL
