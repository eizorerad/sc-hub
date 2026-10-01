"""A marked block in a shell's startup files (~/.bashrc, ~/.zshrc, the file a login shell reads first).

The setup writes two: on the cluster, Codex and Claude Code go on the student's PATH (cluster_agents.py), and on this
computer ~/.sc-hub/bin does (assistants.py), where the `schub` command lives. The program is plain Python 3 that runs
where the files are: on the login node (stdin of `python3 -`) or here (this Python, with HOME and SHELL as the student's).

A marked block, rewritten on every run, goes at the end of each file. Only a block whose lines are exactly ones this
program wrote is ever replaced (`known` lists every body it shipped): markers that are the student's own, or with lines of
theirs between them, leave the file alone, and so does a file another account owns (a linked, shared dotfile). A linked
dotfile of the student's is edited where it lives, through a temporary file, so a full disk never leaves half a file; the
block's lines end with a plain newline, whatever the other lines of the file end with.
"""

from __future__ import annotations

import json
from typing import Sequence

# The files by the student's shell: bash gets ~/.bashrc and the file a login shell reads first, zsh its ~/.zshrc.
BASH_AND_ZSH = '[".bashrc", login] + ([".zshrc"] if shell == "zsh" else [])'  # the cluster: bash, whatever $SHELL says
ONE_SHELL = '[".zshrc"] if shell == "zsh" else ([".bashrc", login] if shell in ("bash", "sh") else [])'  # this computer

TEMPLATE = r"""
import os, re, shutil, tempfile
home = os.path.expanduser("~")
BEGIN, END = __MARKERS__
BODY = __BODY__
BLOCK = "\n".join([BEGIN, *BODY, END]) + "\n"
KNOWN = [BODY, *__OLD__]  # every body this program shipped (add the old one here when the text changes)
OURS = re.compile("^" + re.escape(BEGIN) + r"\r?\n(?:" + "|".join(
    "".join(re.escape(line) + r"[ \t]*\r?\n" for line in body) for body in KNOWN) + ")" +
    re.escape(END) + r"[ \t]*(?:\r?\n|\Z)", re.M)
try:
    import pwd
    account_shell = pwd.getpwuid(os.getuid()).pw_shell
except (ImportError, KeyError):
    account_shell = ""
shell = os.path.basename(os.environ.get("SHELL") or account_shell)  # (an assistant may start the helper without $SHELL)
zdot = os.environ.get("ZDOTDIR") or home  # zsh reads ~/.zshrc from here
if not os.path.isdir(zdot):
    zdot = home
login = next((n for n in (".bash_profile", ".bash_login", ".profile") if os.path.lexists(os.path.join(home, n))),
             ".profile")
names = __NAMES__
if not names:
    print("PATH: your shell (%s) is not one the setup edits; add __FOLDER__ to your PATH yourself" % (shell or "unknown"))
for name in names:
    base = zdot if name == ".zshrc" else home
    label = "~/" + name if base == home else os.path.join(base, name)
    real = os.path.realpath(os.path.join(base, name))
    try:
        if os.stat(real).st_uid != os.getuid():
            print("PATH: left %s alone (another account owns it)" % label)
            continue
        with open(real, newline="") as handle:
            old = handle.read()
    except FileNotFoundError:
        old = ""
    except (OSError, UnicodeDecodeError) as exc:
        print("PATH: could not read %s (%s)" % (label, getattr(exc, "strerror", None) or exc))
        continue
    rest, count = OURS.subn("", old)
    starts = len(re.findall("^" + re.escape(BEGIN), old, re.M))
    ends = len(re.findall("^" + re.escape(END), old, re.M))
    if starts != count or ends != count:
        print("PATH: left %s alone (its sc-hub lines were edited; remove them and run this again)" % label)
        continue
    # The block is always LF: bash and zsh cannot read a CRLF line, so a block of them would never set the PATH, whatever the
    # file's other lines are. The student's own lines are left exactly as they are, the last one's ending included.
    stripped = rest.rstrip("\r\n")
    ending = "\r\n" if rest[len(stripped):].startswith("\r\n") else "\n"
    new = (stripped + ending + "\n" if stripped.strip() else "") + BLOCK
    if new == old:
        print("PATH: %s already has it" % label)
        continue
    temp = None
    try:
        fd, temp = tempfile.mkstemp(dir=os.path.dirname(real), prefix=".schub-")
        with os.fdopen(fd, "w", newline="") as handle:
            handle.write(new)
        if os.path.exists(real):
            shutil.copymode(real, temp)
        os.replace(temp, real)
        print("PATH: added to %s" % label)
    except OSError as exc:
        if temp and os.path.exists(temp):
            os.remove(temp)
        print("PATH: could not write %s (%s)" % (label, exc.strerror or exc))
"""


CLUSTER_MARKERS = ("# >>> sc-hub >>>", "# <<< sc-hub <<<")


def program(body: Sequence[str], old: Sequence[Sequence[str]] = (), names: str = BASH_AND_ZSH,
            folder: str = "~/.local/bin", markers: Sequence[str] = CLUSTER_MARKERS) -> str:
    """The program that puts `body` (shell lines) in a block between `markers` in the files `names` (a Python expression
    over `shell` and `login`) says. `old` are the bodies earlier setups wrote: they are recognised as this program's own."""
    return (TEMPLATE.replace("__MARKERS__", json.dumps(list(markers))).replace("__BODY__", json.dumps(list(body)))
            .replace("__OLD__", json.dumps([list(o) for o in old])).replace("__NAMES__", names)
            .replace("__FOLDER__", folder))
