"""python -m sc_hub_onboard [--home DIR] [--remote-root PATH] [--no-browser]
python -m sc_hub_onboard status | open | retry [STEP] | stop | check | review-prompt   (see onboard/AGENT_GUIDE.md)

Starts the local page (127.0.0.1) and opens it in the browser; the steps run from the page.
--home writes everything (ssh config, key, assistants' configs, workspace) under DIR instead of
the real home: for trials that must not touch this computer's own settings.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import agent_cli, browser
from .engine import Engine
from .server import OnboardServer
from .sshkit import HOST, Paths
from .steps import Setup, build

IDLE_EXIT_S = 15 * 60  # finished and nobody looked at the page for this long: the helper leaves
# Not finished, nothing running (a form waits) and nobody looked for this long: the helper leaves too, and a password
# typed on the page goes with it. Starting it again resumes where it stopped.
IDLE_UNFINISHED_S = 2 * 3600


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = next((a for a in argv if a in agent_cli.COMMANDS), None)
    if command is not None:  # `status --home DIR` and `--home DIR status` alike
        rest = list(argv)
        rest.remove(command)
        return agent_cli.main([command, *rest])
    parser = argparse.ArgumentParser(prog="sc_hub_onboard", description="Set up sc-hub for a student, in one go.")
    parser.add_argument("--home", type=Path, default=None, help="write under this folder instead of the real home")
    parser.add_argument("--remote-root", default="", help="sc-hub's folder on the cluster (default /l/users/<login>/schub)")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 9):
        print("sc-hub onboarding needs Python 3.9 or newer", file=sys.stderr)
        return 2
    paths = Paths(home=args.home.resolve()) if args.home else Paths()
    open_url = (lambda url: None) if args.no_browser else browser.open_in_browser  # the page shows every link anyway
    detach()
    # --no-browser: the dashboard is not started either (a trial's --home gets a dashboard of its own)
    engine = Engine(build(Setup(paths, args.host, args.remote_root, open_url=open_url,
                                open_dashboard=not args.no_browser)), paths.state)
    server = OnboardServer(engine, args.port)
    page = agent_cli.write_page_file(paths, server.port, server.token)  # for `status`, `retry` and `stop`
    engine.start()  # the steps run whether or not the page is open yet; forms wait for the student
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opened = None if args.no_browser else browser.open_in_browser(server.url)
    print("\n".join(browser.startup_lines(server.url, opened, browser.start_file(), home=paths.home if args.home else None)),
          flush=True)
    try:
        while thread.is_alive():
            time.sleep(2)
            if leave(engine, time.monotonic() - server.last_seen):
                server.shutdown()
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        page.unlink(missing_ok=True)
        if args.home:
            stop_trial_dashboard(paths)
    return 0


def leave(engine: Engine, idle: float) -> bool:
    """Nobody looked at the page for `idle` seconds: the helper leaves when it is done, or when nothing runs and it
    only waits for a student who went away (a password typed on the page goes with it)."""
    if engine.finished:
        return idle > IDLE_EXIT_S
    return not engine.busy() and idle > IDLE_UNFINISHED_S


def stop_trial_dashboard(paths: Paths) -> None:
    """A trial's dashboard (its own home, its own port) goes when the trial's helper does."""
    program = paths.state.parent / "bin" / "schub_view.py"
    if program.exists():
        try:
            subprocess.run([sys.executable, str(program), "stop", "--home", str(paths.home)], capture_output=True,
                           timeout=60, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass


def detach() -> None:
    """Started in the background by an assistant (no terminal on either end): a session of its own, so the end of
    the assistant's command, which may signal its whole process group, does not stop the page halfway. From a
    terminal (even with its output piped to a log) it stays where it is, and Ctrl-C stops it."""
    if not hasattr(os, "setsid") or any(s is None or s.isatty() for s in (sys.stdin, sys.stdout)):
        return
    try:
        os.setsid()
    except OSError:
        pass  # already a process group leader: nothing above it to leave


if __name__ == "__main__":
    sys.exit(main())
