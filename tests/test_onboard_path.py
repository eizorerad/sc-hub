"""Codex and Claude Code on the student's own PATH on the cluster, and `schub` on the PATH of this computer: the
blocks the setup writes into the shell's files.

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

from sc_hub_onboard.assistants import LAPTOP_BEGIN, LAPTOP_END, LAPTOP_PATH  # noqa: E402
from sc_hub_onboard.cluster_agents import INSTALL, REGISTER_PATH, login_path  # noqa: E402

SKEL_BASHRC = "# ~/.bashrc\ncase $- in\n    *i*) ;;\n      *) return;;\nesac\nHISTSIZE=1000\n"
SKEL_PROFILE = 'if [ -n "$BASH_VERSION" ]; then\n    if [ -f "$HOME/.bashrc" ]; then\n\t. "$HOME/.bashrc"\n    fi\nfi\n'
BEGIN, END = "# >>> sc-hub >>>", "# <<< sc-hub <<<"


def register(home: Path, shell: str = "/bin/bash", before: str = "", program: str = REGISTER_PATH,
             **env: str) -> list[str]:
    done = subprocess.run([sys.executable, "-"], input=before + program, capture_output=True, text=True,
                          timeout=60, env={"HOME": str(home), "SHELL": shell, "PATH": os.environ.get("PATH", ""), **env})
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


def test_its_own_block_in_the_middle_is_moved_to_the_end(home: Path) -> None:
    register(home)
    block = (home / ".bashrc").read_text()[len(SKEL_BASHRC):].strip("\n")
    (home / ".bashrc").write_text(f"a=1\n{block}\nb=2\n")
    register(home)
    text = (home / ".bashrc").read_text()
    assert text.count(BEGIN) == 1 and text.startswith("a=1\nb=2\n\n" + BEGIN) and text.rstrip().endswith(END)


def test_lines_the_student_put_inside_the_block_are_not_deleted(home: Path) -> None:
    """Found in the second security review: any BEGIN ... END pair used to lose up to 8 lines between its markers."""
    register(home)
    text = (home / ".bashrc").read_text().replace(END, "alias ll='ls -l'\n" + END)
    (home / ".bashrc").write_text(text)
    assert register(home)[0].startswith("PATH: left ~/.bashrc alone") and (home / ".bashrc").read_text() == text


def test_markers_that_happen_to_sit_in_a_heredoc_are_not_a_block(home: Path) -> None:
    """BEGIN in a heredoc with an END a few lines later: deleting between them would eat the heredoc's terminator
    and the student's lines, and bash would then read the rest of the file as the heredoc."""
    text = (f"{SKEL_BASHRC}cat > ~/notes <<'EOF'\n{BEGIN}\nsome notes\nEOF\nexport MINE=1\nalias a=b\n{END}\n")
    (home / ".bashrc").write_text(text)
    assert register(home)[0].startswith("PATH: left ~/.bashrc alone") and (home / ".bashrc").read_text() == text


def test_a_lost_end_marker_leaves_the_file_alone_instead_of_eating_lines(home: Path) -> None:
    """Found in review: with the end marker edited away, the next run would have removed everything from the old
    begin marker down to the new block's end, the student's own lines included."""
    text = f"{SKEL_BASHRC}{BEGIN}\nexport PATH=$HOME/.local/bin:$PATH\nalias ll='ls -l'\nexport MINE=1\n"
    (home / ".bashrc").write_text(text)
    lines = register(home)
    assert lines[0] == "PATH: left ~/.bashrc alone (its sc-hub lines were edited; remove them and run this again)"
    assert (home / ".bashrc").read_text() == text and register(home)[0] == lines[0]


def test_the_block_is_always_lf_even_in_a_file_of_crlf_lines(home: Path) -> None:
    """bash and zsh cannot read a CRLF line (`$'\\r': command not found`, a `case` that never ends): a block of CRLF lines would
    never set the PATH, whatever the file's other lines are. The lines the student has are left as they are."""
    (home / ".bashrc").write_bytes(b"alias a=b\r\nexport X=1\r\n")
    register(home)
    raw = (home / ".bashrc").read_bytes()
    assert raw.startswith(b"alias a=b\r\nexport X=1\r\n\n# >>> sc-hub >>>\n") and raw.endswith(b"# <<< sc-hub <<<\n")
    assert raw.count(b"\r\n") == 2  # (only the student's own two lines)
    before = raw
    assert register(home)[0] == "PATH: ~/.bashrc already has it" and (home / ".bashrc").read_bytes() == before


def test_a_block_an_earlier_helper_wrote_in_crlf_is_replaced_by_an_lf_one(home: Path) -> None:
    body = "\r\n".join([BEGIN, *OLD_BODY, END]) + "\r\n"
    (home / ".bashrc").write_bytes(b"alias a=b\r\n" + body.encode())
    assert register(home)[0] == "PATH: added to ~/.bashrc"
    raw = (home / ".bashrc").read_bytes()
    assert raw.count(BEGIN.encode()) == 1 and raw.count(b"\r\n") == 1 and raw.endswith(b"# <<< sc-hub <<<\n")


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


OLD_BODY = [  # what the setup wrote before the block also kept Codex's files apart: a block of its own, not the student's
    "# Codex and Claude Code, installed by the sc-hub setup, live in ~/.local/bin.",
    'case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac',
]


def test_a_block_an_earlier_setup_wrote_is_replaced_and_not_taken_for_the_students_own(home: Path) -> None:
    (home / ".bashrc").write_text(SKEL_BASHRC + "\n".join([BEGIN, *OLD_BODY, END]) + "\n")
    assert register(home)[0] == "PATH: added to ~/.bashrc"
    text = (home / ".bashrc").read_text()
    assert text.count(BEGIN) == 1 and text.startswith(SKEL_BASHRC) and "CODEX_SQLITE_HOME" in text
    assert register(home)[0] == "PATH: ~/.bashrc already has it"


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("hostname") is None, reason="needs bash")
def test_the_students_own_codex_keeps_its_sqlite_files_per_host_like_the_lab_agent(tmp_path: Path) -> None:
    """/home is NFS, shared by every node: Codex's SQLite files from two nodes at once corrupt or lock up. The lab
    agent already keeps one folder per host; the student's own shell now does the same, on every node."""
    bare = tmp_path / "bare"  # (the cluster's own ~/.bashrc returns early for a shell that is not interactive)
    bare.mkdir()
    register(bare)
    block = (bare / ".bashrc").read_text().split(BEGIN)[1]
    assert "echo" not in block  # a non-interactive login must stay silent
    env = {"HOME": str(bare), "PATH": os.environ.get("PATH", "")}
    done = subprocess.run(["bash", "-c", '. "$HOME/.bashrc"; printf "%s" "$CODEX_SQLITE_HOME"'], capture_output=True,
                          text=True, timeout=60, env=env)
    short = subprocess.run(["hostname", "-s"], capture_output=True, text=True, timeout=30).stdout.strip()
    assert done.returncode == 0 and done.stdout == f"{bare}/.codex-sqlite/{short}" and done.stderr == ""
    assert (bare / ".codex-sqlite").stat().st_mode & 0o777 == 0o700
    assert Path(done.stdout).is_dir() and Path(done.stdout).stat().st_mode & 0o777 == 0o700
    again = subprocess.run(["bash", "-c", 'export CODEX_SQLITE_HOME=/elsewhere; . "$HOME/.bashrc"; '
                                          'printf "%s" "$CODEX_SQLITE_HOME"'],
                           capture_output=True, text=True, timeout=60, env=env)
    assert again.stdout == "/elsewhere" and again.stderr == ""  # a student's own setting wins
    # srun and sbatch pass the environment on: a shell in a job starts with the login node's folder, and takes its own host's
    inherited = subprocess.run(["bash", "-c", f'export CODEX_SQLITE_HOME="{bare}/.codex-sqlite/lo-02"; . "$HOME/.bashrc"; '
                                              'printf "%s" "$CODEX_SQLITE_HOME"'],
                               capture_output=True, text=True, timeout=60, env=env)
    assert inherited.stdout == f"{bare}/.codex-sqlite/{short}" and inherited.stderr == ""


@pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs bash and a user that cannot write everywhere")
def test_the_block_never_ends_a_job_script_that_runs_with_set_e(tmp_path: Path) -> None:
    """A `#!/bin/bash -e` script that sources ~/.bashrc must not die because a folder could not be made (over quota, or
    two jobs making the same one at once)."""
    bare = tmp_path / "bare"
    bare.mkdir()
    register(bare)
    bare.chmod(0o555)  # nothing can be made in the home
    try:
        done = subprocess.run(["bash", "-e", "-c", '. "$HOME/.bashrc"; echo survived'], capture_output=True, text=True,
                              timeout=60, env={"HOME": str(bare), "PATH": os.environ.get("PATH", "")})
    finally:
        bare.chmod(0o755)
    assert done.stdout == "survived\n" and done.stderr == ""  # and it says nothing either


def test_the_setups_own_commands_keep_the_same_rule() -> None:
    from sc_hub_onboard.cluster_agents import PRELUDE

    assert 'case "${CODEX_SQLITE_HOME:-}" in ""|"$HOME/.codex-sqlite/"*)' in PRELUDE and "|| :" in PRELUDE


# ---- this computer: ~/.sc-hub/bin, where `schub` lives ---------------------------------------------------------------

def test_on_this_computer_zsh_gets_its_zshrc_and_nothing_else(tmp_path: Path) -> None:
    """macOS: zsh reads ~/.zshrc in every terminal window; ~/.bashrc and ~/.profile are not its files."""
    assert register(tmp_path, shell="/bin/zsh", program=LAPTOP_PATH) == ["PATH: added to ~/.zshrc"]
    text = (tmp_path / ".zshrc").read_text()
    assert text.startswith(LAPTOP_BEGIN) and text.rstrip().endswith(LAPTOP_END) and "$HOME/.sc-hub/bin" in text
    assert 'export PATH="$PATH:$HOME/.sc-hub/bin"' in text  # last: it never shadows a command the student has
    assert not (tmp_path / ".bashrc").exists() and not (tmp_path / ".profile").exists()
    assert register(tmp_path, shell="/bin/zsh", program=LAPTOP_PATH) == ["PATH: ~/.zshrc already has it"]


def test_on_this_computer_bash_gets_its_bashrc_and_the_login_file(tmp_path: Path) -> None:
    (tmp_path / ".bash_profile").write_text("export EDITOR=vim\n")  # macOS's bash reads this one in a new window
    assert register(tmp_path, shell="/bin/bash", program=LAPTOP_PATH) == ["PATH: added to ~/.bashrc",
                                                                         "PATH: added to ~/.bash_profile"]
    assert not (tmp_path / ".zshrc").exists()


def test_on_this_computer_another_shell_is_told_to_add_the_folder_itself(tmp_path: Path) -> None:
    assert register(tmp_path, shell="/usr/bin/fish", program=LAPTOP_PATH) == [
        "PATH: your shell (fish) is not one the setup edits; add ~/.sc-hub/bin to your PATH yourself"]
    assert list(tmp_path.iterdir()) == []


def test_on_this_computer_a_dotfile_the_student_keeps_in_a_dotfiles_folder_is_edited_there(tmp_path: Path) -> None:
    (tmp_path / "dotfiles").mkdir()
    (tmp_path / "dotfiles" / "zshrc").write_text("alias ll='ls -l'\n")
    (tmp_path / ".zshrc").symlink_to("dotfiles/zshrc")
    register(tmp_path, shell="/bin/zsh", program=LAPTOP_PATH)
    assert (tmp_path / ".zshrc").is_symlink() and LAPTOP_BEGIN in (tmp_path / "dotfiles" / "zshrc").read_text()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_a_new_terminal_then_finds_schub_by_its_name(tmp_path: Path) -> None:
    launcher = tmp_path / ".sc-hub" / "bin" / "schub"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\necho launched\n")
    launcher.chmod(0o755)
    register(tmp_path, shell="/bin/bash", program=LAPTOP_PATH)
    done = subprocess.run(["bash", "-c", '. "$HOME/.bashrc"; schub'], capture_output=True, text=True, timeout=60,
                          env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert done.stdout == "launched\n", (done.stdout, done.stderr)


def test_the_laptops_block_and_the_clusters_can_share_a_dotfile(tmp_path: Path) -> None:
    """A dotfiles repository synced between the laptop and the cluster account carries both: each program knows its own
    block (another marker) and does not call the other's edited, as it would if both had the same markers."""
    assert LAPTOP_BEGIN != BEGIN and LAPTOP_END != END
    register(tmp_path, shell="/bin/zsh")  # the cluster's, which writes ~/.zshrc for zsh too
    assert register(tmp_path, shell="/bin/zsh", program=LAPTOP_PATH) == ["PATH: added to ~/.zshrc"]
    assert register(tmp_path, shell="/bin/zsh", program=LAPTOP_PATH) == ["PATH: ~/.zshrc already has it"]
    again = register(tmp_path, shell="/bin/zsh")  # its own block goes behind the laptop's (the end, as always)
    assert not any("left" in line or "edited" in line for line in again)
    text = (tmp_path / ".zshrc").read_text()
    assert text.count(BEGIN) == 1 and text.count(LAPTOP_BEGIN) == 1 and text.count(END) == 1


def test_zsh_reads_the_folder_zdotdir_names_when_there_is_one(tmp_path: Path) -> None:
    zdot = tmp_path / "zdot"
    zdot.mkdir()
    assert register(tmp_path, shell="/bin/zsh", program=LAPTOP_PATH, ZDOTDIR=str(zdot)) == [
        f"PATH: added to {zdot}/.zshrc"]
    assert LAPTOP_BEGIN in (zdot / ".zshrc").read_text() and not (tmp_path / ".zshrc").exists()
    gone = tmp_path / "no-such-folder"  # a ZDOTDIR that is not a folder: zsh reads nothing there, the home it is
    assert register(tmp_path, shell="/bin/zsh", program=LAPTOP_PATH, ZDOTDIR=str(gone)) == ["PATH: added to ~/.zshrc"]


def test_without_a_shell_variable_the_accounts_own_login_shell_decides(tmp_path: Path) -> None:
    """Started by an assistant, the helper may have no $SHELL: the program asks the account (macOS: zsh)."""
    import pwd

    lines = register(tmp_path, shell="", program=LAPTOP_PATH)
    shell = os.path.basename(pwd.getpwuid(os.getuid()).pw_shell)
    if shell == "zsh":
        assert lines == ["PATH: added to ~/.zshrc"]
    elif shell in ("bash", "sh"):
        assert lines[0] == "PATH: added to ~/.bashrc"
    else:
        assert lines == [f"PATH: your shell ({shell}) is not one the setup edits; add ~/.sc-hub/bin to your PATH yourself"]


def test_one_stray_crlf_does_not_turn_the_whole_block_into_crlf(home: Path) -> None:
    """bash cannot read CRLF lines: a file with a single one (a pasted comment, ignored by bash) must stay readable."""
    (home / ".bashrc").write_bytes(b"# pasted\r\nexport X=1\nexport Y=2\n")
    register(home)
    raw = (home / ".bashrc").read_bytes()
    assert raw.count(b"\r\n") == 1 and raw.endswith(b"# <<< sc-hub <<<\n")
    assert register(home)[0] == "PATH: ~/.bashrc already has it"


def test_a_trial_home_never_writes_the_real_zdotdir(tmp_path: Path, monkeypatch) -> None:
    """VS Code's terminal exports ZDOTDIR: a `--home` trial, or the tests, run from there must change their own home's
    ~/.zshrc, not the student's real one."""
    from sc_hub_onboard import assistants
    from sc_hub_onboard.sshkit import Paths

    real = tmp_path / "real-zdotdir"
    real.mkdir()
    monkeypatch.setenv("ZDOTDIR", str(real))
    monkeypatch.setenv("SHELL", "/bin/zsh")
    trial = Paths(home=tmp_path / "trial")
    trial.home.mkdir()
    assert assistants.register_path(trial) == ["PATH: added to ~/.zshrc"]
    assert LAPTOP_BEGIN in (trial.home / ".zshrc").read_text() and list(real.iterdir()) == []
