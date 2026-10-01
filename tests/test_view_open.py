"""The dashboard's launcher opens its page in the browser, and when that fails it says what to open, in words that
are no command to paste (zsh would read a leading "open" and the address's characters as shell)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import schub_view as view  # noqa: E402

URL = "http://127.0.0.1:27182/go?to=%2F"


def test_a_mac_whose_webbrowser_gave_up_is_opened_with_open() -> None:
    ran: list[list[str]] = []
    done = view.open_in_browser(URL, opener=lambda url: False, platform="darwin",
                                runner=lambda args, **kwargs: ran.append(args) or SimpleNamespace(returncode=0))
    assert done is True and ran == [["open", URL]]
    assert view.open_in_browser(URL, opener=lambda url: True, platform="darwin", runner=None) is True  # (no runner needed)


def test_nothing_that_can_be_tried_is_a_no_not_a_crash() -> None:
    def raising(url: str) -> bool:
        raise OSError("no display")

    def missing(args, **kwargs):
        raise FileNotFoundError(args[0])

    assert view.open_in_browser(URL, opener=raising, runner=missing, platform="darwin") is False
    assert view.open_in_browser(URL, opener=lambda url: False, runner=missing, platform="win32") is False


def test_when_it_cannot_open_the_page_it_gives_the_address_not_a_command() -> None:
    text = view.not_opened_message("http://sc-hub.localhost:27182")
    assert text.endswith("http://sc-hub.localhost:27182") and not text.lower().startswith("open ")
    assert "!" not in text and "in your browser" in text
