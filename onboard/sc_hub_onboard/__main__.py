"""python -m sc_hub_onboard [--home DIR] [--remote-root PATH] [--no-browser]
python -m sc_hub_onboard status | open | retry [STEP] | stop | check | review-prompt   (see onboard/AGENT_GUIDE.md)

Starts the local page (127.0.0.1) and opens it in the browser; the steps run from the page.
--home writes everything (ssh config, key, assistants' configs, workspace) under DIR instead of
the real home: for trials that must not touch this computer's own settings.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import agent_cli
from .engine import Engine
from .server import OnboardServer
from .sshkit import HOST, Paths
from .steps import Setup, build

IDLE_EXIT_S = 15 * 60  # finished and nobody looked at the page for this long: the helper leaves


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
    open_url = (lambda url: None) if args.no_browser else webbrowser.open  # the page shows every link anyway
    # --home (a trial) or --no-browser: the dashboard mirror is not started (it would use the real home)
    engine = Engine(build(Setup(paths, args.host, args.remote_root, open_url=open_url,
                                open_dashboard=not (args.no_browser or args.home))), paths.state)
    server = OnboardServer(engine, args.port)
    page = agent_cli.write_page_file(paths, server.port, server.token)  # for `status`, `retry` and `stop`
    engine.start()  # the steps run whether or not the page is open yet; forms wait for the student
    print(f"sc-hub setup: open {server.url}", flush=True)
    if not args.no_browser:
        webbrowser.open(server.url)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            time.sleep(2)
            if engine.finished and time.time() - server.last_seen > IDLE_EXIT_S:
                server.shutdown()
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        page.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
