"""The setup page's address and how it reaches the student's browser. On macOS zsh reads the `?` of the address as a
wildcard (`zsh: no matches found`) and a "!" as a history lookup, so the message never asks the student to paste
`open <address>`: the address is shown bare (a terminal makes it a link), and a command without it is given."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ONBOARD = Path(__file__).resolve().parents[1] / "onboard"
sys.path.insert(0, str(ONBOARD))

from sc_hub_onboard import browser  # noqa: E402

URL = "http://127.0.0.1:8791/?t=fake-token_for-tests-only-0123456789"
START = Path("/Users/t/sc-hub/onboard/start.sh")


def test_the_address_is_shown_bare_and_a_command_without_it_is_given() -> None:
    lines = browser.startup_lines(URL, opened=True, start=START, windows=False)
    assert URL in [line.strip() for line in lines]  # alone on its line: a terminal turns it into a link
    assert "sh /Users/t/sc-hub/onboard/start.sh open" in [line.strip() for line in lines]  # nothing in it to expand
    assert not any(line.lstrip().startswith("open ") for line in lines)  # "open <address>" is what zsh refuses
    assert not any(URL in line and line.strip() != URL for line in lines)  # the address is in no other line
    assert "!" not in "\n".join(lines)


def test_a_folder_with_a_space_is_quoted_for_the_shell() -> None:
    lines = browser.startup_lines(URL, opened=True, start=Path("/Users/t/My Folder/onboard/start.sh"), windows=False)
    assert "sh '/Users/t/My Folder/onboard/start.sh' open" in [line.strip() for line in lines]


def test_windows_gets_a_command_both_cmd_and_powershell_run() -> None:
    """`"C:\\...\\start.cmd" open` is a parse error in PowerShell (Windows Terminal's default): a path with a separator in it,
    relative to the sc-hub folder where the student ran the setup, runs in both."""
    lines = browser.startup_lines(URL, opened=True, start=Path(r"C:\Users\t\sc-hub\onboard\start.cmd"), windows=True)
    assert r"onboard\start.cmd open" in [line.strip() for line in lines]
    assert not any(line.strip().startswith(('"', "&")) for line in lines)


def test_a_page_started_for_another_home_says_so_in_the_command() -> None:
    """`start.sh open` reads the page that this home's helper wrote: a trial's helper needs the same --home."""
    home = Path("/tmp/schub trial")
    lines = browser.startup_lines(URL, opened=True, start=START, windows=False, home=home)
    assert "sh /Users/t/sc-hub/onboard/start.sh open --home '/tmp/schub trial'" in [line.strip() for line in lines]
    windows = browser.startup_lines(URL, opened=True, start=START, windows=True, home=Path(r"C:\trial home"))
    assert r'onboard\start.cmd open --home "C:\trial home"' in [line.strip() for line in windows]


def test_the_message_says_so_when_the_browser_did_not_open() -> None:
    opened = " ".join(browser.startup_lines(URL, opened=True, start=START, windows=False))
    failed = " ".join(browser.startup_lines(URL, opened=False, start=START, windows=False))
    assert "opened in your browser" in opened.lower() and "did not open" not in opened
    assert "did not open by itself" in failed and "opened in your browser" not in failed.lower()


def test_pythons_own_way_of_opening_a_page_is_tried_first() -> None:
    seen: list[str] = []

    def boom(*args, **kwargs):
        raise AssertionError("the system opener must not run when webbrowser worked")

    assert browser.open_in_browser(URL, opener=lambda url: seen.append(url) or True, runner=boom, platform="darwin")
    assert seen == [URL]


def test_a_mac_whose_webbrowser_gave_up_is_opened_with_open() -> None:
    ran: list[list[str]] = []
    done = browser.open_in_browser(URL, opener=lambda url: False, platform="darwin",
                                   runner=lambda args, **kwargs: ran.append(args) or SimpleNamespace(returncode=0))
    assert done is True and ran == [["open", URL]]  # a list, never a shell line: the `?` means nothing to it


def test_linux_falls_back_to_xdg_open_and_windows_has_nothing_else_to_try() -> None:
    ran: list[list[str]] = []
    runner = lambda args, **kwargs: ran.append(args) or SimpleNamespace(returncode=0)  # noqa: E731
    assert browser.open_in_browser(URL, opener=lambda url: False, platform="linux", runner=runner) is True
    assert ran == [["xdg-open", URL]]
    ran.clear()
    assert browser.open_in_browser(URL, opener=lambda url: False, platform="win32", runner=runner) is False and ran == []


def test_an_opener_that_fails_or_is_missing_is_a_no_not_a_crash() -> None:
    def raising(url: str) -> bool:
        raise OSError("no display")

    def missing(args, **kwargs):
        raise FileNotFoundError(args[0])

    assert browser.open_in_browser(URL, opener=raising, runner=missing, platform="darwin") is False
    failing = lambda args, **kwargs: SimpleNamespace(returncode=1)  # noqa: E731
    assert browser.open_in_browser(URL, opener=lambda url: False, runner=failing, platform="darwin") is False


# ---- through main(): what the terminal really shows ----------------------------------------------------------------------

def run_main(monkeypatch, tmp_path, capsys, *argv: str, opened: bool | None = None) -> str:
    """main(): serve, say how to open the page, leave at the first look (what the terminal shows meanwhile)."""
    import re
    import time

    from sc_hub_onboard import __main__ as entry

    real_sleep = time.sleep
    monkeypatch.setattr(entry, "detach", lambda: None)  # (under pytest it would call setsid on the test process)
    monkeypatch.setattr(entry, "leave", lambda engine, idle: True)
    monkeypatch.setattr(entry.time, "sleep", lambda seconds: real_sleep(0.01))
    if opened is not None:
        monkeypatch.setattr(entry.browser, "open_in_browser", lambda url, *args, **kwargs: opened)
    assert entry.main(["--home", str(tmp_path / "home"), "--port", "0", *argv]) == 0
    out = capsys.readouterr().out
    assert re.search(r"^  http://127\.0\.0\.1:\d+/\?t=[\w-]+$", out, re.M)  # the address, bare, on its own line
    return out


def test_the_terminal_shows_the_address_bare_and_a_command_without_it(monkeypatch, tmp_path, capsys) -> None:
    import re

    out = run_main(monkeypatch, tmp_path, capsys, opened=True)
    assert "Its page is opened in your browser." in out
    link = re.search(r"^  (http://127\.0\.0\.1:\d+/\?t=[\w-]+)$", out, re.M)
    assert out.count(link[1]) == 1 and "!" not in out  # the address is nowhere else
    assert re.search(r"^  sh '?[^\n]*onboard/start\.sh'? open --home '?[^\n]*home'?$", out, re.M)  # a trial's helper: its home
    assert not re.search(r"^\s*(sc-hub setup: )?open ", out, re.M)  # no `open <address>` for zsh to refuse


def test_it_says_so_when_no_browser_took_it_and_when_none_was_asked(monkeypatch, tmp_path, capsys) -> None:
    assert "did not open by itself" in run_main(monkeypatch, tmp_path, capsys, opened=False)
    out = run_main(monkeypatch, tmp_path, capsys, "--no-browser")
    assert "Open this link in your browser:" in out and "opened in your browser" not in out
