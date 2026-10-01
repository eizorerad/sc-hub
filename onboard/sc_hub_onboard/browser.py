"""The setup page in the student's browser, and what the terminal says about it.

On macOS zsh reads the `?` of the page's address as a wildcard (`zsh: no matches found`) and a "!" as a history lookup,
so nothing here asks the student to paste `open <address>`: the address is shown bare (a terminal turns it into a link),
and the command given for the case that it did not open has no address in it (it reads it from the running page).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any, Callable

SYSTEM_OPENERS = {"darwin": "open", "linux": "xdg-open"}  # what Python's webbrowser may fail to reach (no GUI session)


def open_in_browser(url: str, opener: Callable[[str], Any] | None = None, runner: Callable[..., Any] = subprocess.run,
                    platform: str = sys.platform) -> bool:
    """`url` in the default browser: Python's webbrowser first, then the system's own opener (`open` on macOS,
    `xdg-open` on Linux; the program gets the address as an argument, never through a shell). Never raises: False says
    nothing opened it. (The address carries the page's token, and a program's arguments can be read by other accounts of a
    shared computer while it runs: only the fallback does this, and for the moment `open` takes.)"""
    try:
        if (opener or webbrowser.open)(url):
            return True
    except (OSError, webbrowser.Error):
        pass
    command = SYSTEM_OPENERS.get("linux" if platform.startswith("linux") else platform)
    if command is None:
        return False
    try:
        return runner([command, url], capture_output=True, timeout=20, stdin=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def start_file(windows: bool | None = None) -> Path:
    """onboard/start.sh (Windows: start.cmd): what the student ran, and what runs the `open` command."""
    windows = os.name == "nt" if windows is None else windows
    return Path(__file__).resolve().parents[1] / ("start.cmd" if windows else "start.sh")


def open_command(start: Path, windows: bool, home: Path | None = None) -> str:
    """The command that opens the running page again: no address in it, so no shell can misread one. On Windows it is the
    relative `onboard\\start.cmd` (the student ran the setup in the sc-hub folder, which is also where an assistant starts
    it), the one form that both cmd and PowerShell run. A helper started for another home (a trial) needs that home too."""
    more = ""
    if home is not None:
        more = f" --home {_quote_windows(str(home)) if windows else shlex.quote(str(home))}"
    return f"onboard\\start.cmd open{more}" if windows else f"sh {shlex.quote(str(start))} open{more}"


def _quote_windows(text: str) -> str:
    return f'"{text}"' if " " in text else text


def startup_lines(url: str, opened: bool | None, start: Path, windows: bool | None = None,
                  home: Path | None = None) -> list[str]:
    """What the terminal says when the page is up. `opened`: whether the browser took the address (None: nobody tried)."""
    windows = os.name == "nt" if windows is None else windows
    if opened is None:
        head = ["sc-hub setup is running.", "Open this link in your browser:"]
    elif opened:
        head = ["sc-hub setup is running. Its page is opened in your browser.", "If you do not see it, open this link:"]
    else:
        head = ["sc-hub setup is running. Its page did not open by itself.", "Open this link in your browser:"]
    return [*head, f"  {url}", "or, in another terminal window" + (" (in the sc-hub folder):" if windows else ":"),
            f"  {open_command(start, windows, home)}"]
