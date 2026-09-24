"""Commands for the assistant that runs the setup with the student (see onboard/AGENT_GUIDE.md).

    python -m sc_hub_onboard status [--home DIR] [--json]   each step: status, detail, hint, last log lines
    python -m sc_hub_onboard open [--home DIR]              the running page in the default browser (no link printed)
    python -m sc_hub_onboard retry [STEP] [--home DIR]      the running page retries STEP (default: the failed ones)
    python -m sc_hub_onboard stop [--home DIR]              the running page stops (start it again to reload the helper)
    python -m sc_hub_onboard check                          the helper's own tests
    python -m sc_hub_onboard review-prompt "FAILURE"        the reviewer's task: REVIEW.md, the failure, the checkout

None of them prints a password, a sign-in code or the page's token: a form waiting for the student shows
only its title and the names of its fields.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .sshkit import Paths

REPO = Path(__file__).resolve().parents[2]
COMMANDS = ("status", "open", "retry", "stop", "check", "review-prompt")
LOG_LINES = 12
# The page's end-to-end tests drive a fake `ssh` script through PATH, which Windows cannot run.
PORTABLE_TESTS = "config_block or twice or colours or error_messages"
# The helper's tests need only pytest; the repository's conftest.py loads the analysis stack.
PYTEST_ARGS = ("-p", "no:cacheprovider", "--noconftest")  # (pyproject's addopts already make it quiet)


def page_file(paths: Paths) -> Path:
    """Where the running page leaves its address for these commands (private, removed when it stops)."""
    return paths.state.parent / "page.json"


def write_page_file(paths: Paths, port: int, token: str) -> Path:
    target = page_file(paths)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"port": port, "token": token, "pid": os.getpid()}, handle)
    temp.replace(target)
    return target


def _call(paths: Paths, path: str, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The running page's API, or None when no page is running for this home."""
    try:
        page = json.loads(page_file(paths).read_text())
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(page['port'])}{path}", method="GET" if body is None else "POST",
            data=None if body is None else json.dumps(body).encode(),
            headers={"X-Onboard-Token": str(page["token"]), "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read() or b"{}")
    except (OSError, ValueError, KeyError, urllib.error.URLError):
        return None


def _saved(paths: Paths) -> dict[str, Any] | None:
    try:
        saved = json.loads(paths.state.read_text())
    except (OSError, ValueError):
        return None
    steps = list(saved.get("steps", {}).values())
    return {"steps": steps, "finished": bool(steps) and all(s.get("status") in ("done", "skipped") for s in steps)}


def _form_outline(ask: dict[str, Any] | None) -> dict[str, Any] | None:
    if not ask:
        return None
    return {"title": ask.get("title", ""), "fields": [f.get("label", f.get("name", "")) for f in ask.get("fields", [])],
            "choices": [c.get("label", "") for c in ask.get("choices", [])], "waiting": bool(ask.get("wait"))}


def _checkout() -> str:
    git = shutil.which("git")
    if git is None or not (REPO / ".git").exists():
        return "not a git checkout"
    def run(*args: str) -> str:
        done = subprocess.run([git, "-C", str(REPO), *args], capture_output=True, text=True, timeout=30)
        return done.stdout.strip()
    changed = run("status", "--porcelain")
    return f"{run('rev-parse', '--short', 'HEAD')} on {run('rev-parse', '--abbrev-ref', 'HEAD')}" + \
        (" (with local changes)" if changed else "")


def status(paths: Paths, as_json: bool = False) -> int:
    live = _call(paths, "/api/state")
    state = live or _saved(paths)
    if state is None:
        print("No setup has run for this home yet (no page, no saved state).")
        return 1
    steps = [{"id": s.get("id"), "title": s.get("title"), "status": s.get("status"), "detail": s.get("detail", ""),
              "hint": s.get("hint", ""), "log": list(s.get("log", []))[-LOG_LINES:], "form": _form_outline(s.get("ask"))}
             for s in state.get("steps", [])]
    report = {"page": "running" if live else "not running (saved state)", "finished": bool(state.get("finished")),
              "progress": state.get("progress"), "checkout": _checkout(), "steps": steps}
    if as_json:
        print(json.dumps(report, indent=1))
        return 0
    print(f"page: {report['page']}; finished: {report['finished']}; sc-hub checkout: {report['checkout']}")
    for step in steps:
        print(f"\n[{step['status']}] {step['id']}: {step['title']}")
        if step["detail"]:
            print(f"  detail: {step['detail']}")
        if step["hint"]:
            print(f"  hint:   {step['hint']}")
        if step["form"]:
            form = step["form"]
            print(f"  waiting for the student on the page: {form['title']}"
                  + (f" (fields: {', '.join(form['fields'])})" if form["fields"] else ""))
        if step["status"] in ("failed", "running", "asking") and step["log"]:
            print("  last log lines:\n    " + "\n    ".join(step["log"]))
    return 0


def open_page(paths: Paths) -> int:
    """The running page in the student's browser, without showing its link (the token) to anyone."""
    import webbrowser

    try:
        page = json.loads(page_file(paths).read_text())
        url = f"http://127.0.0.1:{int(page['port'])}/?t={page['token']}"
    except (OSError, ValueError, KeyError):
        print("The page is not running. Start it (onboard/start.sh or onboard\\start.cmd).")
        return 1
    if _call(paths, "/api/state") is None:
        print("The page is not running. Start it again: it resumes where it stopped.")
        return 1
    print("Opened in the default browser." if webbrowser.open(url) else "No browser could be opened on this computer.")
    return 0


def retry(paths: Paths, step: str) -> int:
    answer = _call(paths, "/api/retry", {"step": step})
    if answer is None:
        print("The page is not running. Start it again (onboard/start.sh or onboard\\start.cmd): it resumes where it stopped.")
        return 1
    print(f"The page retries {step or 'the failed steps'}; follow it with the status command.")
    return 0


def stop(paths: Paths) -> int:
    if _call(paths, "/api/quit", {}) is None:
        print("The page is not running.")
        return 1
    print("The page stops. Start it again (onboard/start.sh or onboard\\start.cmd): the steps resume, and the page opens "
          "in the student's browser again.")
    return 0


def check() -> int:
    tests = REPO / "tests" / "test_onboard.py"
    if not tests.exists():
        print("The helper's tests are not in this copy of sc-hub (tests/test_onboard.py).")
        return 1
    select = ["-k", PORTABLE_TESTS] if os.name == "nt" else []
    if os.name == "nt":
        print("Windows: running the portable tests only; the page's end-to-end tests run on macOS, Linux or WSL "
              "(and on every pull request).")  # (tests/onboard_fakes/ssh is a Python script PATH cannot run here)
    try:
        import pytest  # noqa: F401
        command = [sys.executable, "-m", "pytest", *PYTEST_ARGS, str(tests), *select]
    except ImportError:
        uv = shutil.which("uv")
        if uv is None:
            print("pytest is not installed. Install it (python -m pip install --user pytest) or uv "
                  "(https://docs.astral.sh/uv/), then run this again.")
            return 2
        command = [uv, "run", "--no-project", "--with", "pytest", "python", "-m", "pytest", *PYTEST_ARGS, str(tests),
                   *select]
    code = subprocess.run(command, cwd=REPO).returncode
    print("check: passed" if code == 0 else f"check: FAILED (pytest exit code {code})")
    return code


def review_prompt(failure: str) -> int:
    """REVIEW.md with what the reviewer needs, to pipe into a second assistant (works the same on Windows)."""
    checklist = (REPO / "onboard" / "REVIEW.md").read_text()
    print(checklist.rstrip() + "\n\n## This review\n\n"
          f"- The checkout: `{REPO}` (run `git diff` and `git status` there).\n"
          f"- It was at: {_checkout()}.\n"
          f"- The failure the change fixes: {failure or '(not given: judge from the code and say so)'}\n"
          "- If you can run commands, run `sh onboard/start.sh check` there (Windows: `onboard\\start.cmd check`); "
          "if you cannot, say so.\n"
          "- The last line of your answer is `APPROVE` or `CHANGES` with its reasons, as above.")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sc_hub_onboard", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("step", nargs="?", default="",
                        help="retry: the step id (default: every failed step); review-prompt: the failure")
    parser.add_argument("--home", type=Path, default=None, help="the --home the page was started with")
    parser.add_argument("--json", action="store_true", help="status: machine-readable")
    args = parser.parse_args(argv)
    paths = Paths(home=args.home.resolve()) if args.home else Paths()
    if args.command == "status":
        return status(paths, args.json)
    if args.command == "retry":
        return retry(paths, args.step)
    if args.command == "stop":
        return stop(paths)
    if args.command == "open":
        return open_page(paths)
    if args.command == "review-prompt":
        return review_prompt(args.step)
    return check()
