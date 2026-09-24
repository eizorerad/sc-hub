"""Codex and Claude Code on the cluster, signed in with the student's own accounts.

The CLIs are installed with their official installers and run on the login node (it has
internet). Both sign-ins finish in the laptop's browser with nothing listening on the
cluster: Codex with a one-time device code, Claude Code with a code the student pastes
back into the page. When a ChatGPT workspace has device codes turned off, Codex signs in
the usual way and its redirect to localhost:1455 reaches the cluster through an ssh
tunnel that lasts as long as the sign-in.

Nothing here reads or shows a token: the account shown to the student is the e-mail
address and plan, from the status commands and the id token's public claims.
"""

from __future__ import annotations

import codecs
import json
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Sequence

from .engine import Context, StepFailed
from .sshkit import Ssh

LABELS = {"codex": "Codex", "claude": "Claude Code"}
STUDENT_DOMAIN = "@mbzuai.ac.ae"
CODEX_PORT = 1455  # Codex's sign-in redirect (http://localhost:1455/auth/callback)
START_S = 90
DEVICE_S = 16 * 60  # the device code expires after 15 minutes

# Every command starts with this: the CLIs where their installers put them, Codex's SQLite files per host
# (the home folder is on NFS, shared by every node), and a helper that stops a sign-in when ssh goes away.
PRELUDE = r"""export PATH="$HOME/.local/bin:$PATH" CODEX_NON_INTERACTIVE=1
export CODEX_SQLITE_HOME="${CODEX_SQLITE_HOME:-$HOME/.codex-sqlite/$(hostname -s)}"
[ -d "$HOME/.codex-sqlite" ] || mkdir -m 700 "$HOME/.codex-sqlite"; [ -d "$CODEX_SQLITE_HOME" ] || mkdir -p -m 700 "$CODEX_SQLITE_HOME"
codex_bin() { for c in "$HOME/.codex/packages/standalone/current/bin/codex" "$HOME/.local/bin/codex" "$(command -v codex)"; do
  [ -n "$c" ] && [ -x "$c" ] && { printf '%s' "$c"; return 0; }; done; return 1; }
claude_bin() { for c in "$HOME/.local/bin/claude" "$(command -v claude)"; do
  [ -n "$c" ] && [ -x "$c" ] && { printf '%s' "$c"; return 0; }; done; return 1; }
# (a background job reads /dev/null unless told otherwise, so the watcher gets the real stdin as fd 3)
until_hangup() { exec 3<&0; "$@" </dev/null 3<&- & p=$!; ( cat <&3 >/dev/null; kill "$p" 2>/dev/null ) >/dev/null 2>&1 &
  exec 3<&-; wait "$p"; }
"""
INSTALL = PRELUDE + r"""set -o pipefail
if ! codex_bin >/dev/null; then echo "installing Codex"; curl -fsSL https://chatgpt.com/codex/install.sh | sh || exit 1; fi
if ! claude_bin >/dev/null; then echo "installing Claude Code"; curl -fsSL https://claude.ai/install.sh | bash || exit 1; fi
echo "Codex: $("$(codex_bin)" --version 2>/dev/null)"; echo "Claude Code: $("$(claude_bin)" --version 2>/dev/null)"
"""
CODEX_DEVICE = PRELUDE + 'until_hangup "$(codex_bin)" login --device-auth'
# The usual sign-in listens on 127.0.0.1:1455 of a login node that other users share: stop if that port is taken
# (Codex would otherwise cancel whatever holds it: another student's sign-in).
CODEX_BROWSER = PRELUDE + (
    'if python3 -c "import socket; raise SystemExit(socket.socket().connect_ex((\'127.0.0.1\', 1455)) != 0)"; then '
    'echo "port 1455 is taken on the login node (another sign-in); try again in a few minutes"; exit 3; fi; '
    'until_hangup "$(codex_bin)" login')
CLAUDE_LOGIN = PRELUDE + 'exec "$(claude_bin)" auth login'  # reads the pasted code; stdin's end stops it
SIGN_OUT = {"codex": PRELUDE + '"$(codex_bin)" logout', "claude": PRELUDE + '"$(claude_bin)" auth logout'}
# Run on the login node with python3 (stdin): which CLIs are there and which account each one uses.
STATUS = r'''
import base64, json, os, re, shutil, subprocess
home = os.path.expanduser("~")
def find(paths, name):
    for path in paths + [shutil.which(name) or ""]:
        if path and os.access(path, os.X_OK):
            return path
    return ""
def version(text):
    return next((line for line in text.splitlines() if re.search(r"\d+\.\d+", line) and "WARNING" not in line), "")[:60]
def run(args):
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout + done.stderr
    except (OSError, subprocess.SubprocessError):
        return -1, ""
out = {}
codex = find([home + "/.codex/packages/standalone/current/bin/codex", home + "/.local/bin/codex"], "codex")
info = {"installed": bool(codex)}
if codex:
    info["version"] = version(run([codex, "--version"])[1])
    code, text = run([codex, "login", "status"])
    info["signed_in"] = code == 0 and "Logged in" in text
    info["method"] = "ChatGPT" if "ChatGPT" in text else ("API key" if "API key" in text else "")
    try:
        with open(os.path.join(os.environ.get("CODEX_HOME") or home + "/.codex", "auth.json")) as handle:
            part = ((json.load(handle).get("tokens") or {}).get("id_token") or "").split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        info["email"] = str(claims.get("email", ""))
        info["plan"] = str((claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type", ""))
    except (OSError, ValueError, IndexError, AttributeError, TypeError):
        pass
out["codex"] = info
claude = find([home + "/.local/bin/claude"], "claude")
info = {"installed": bool(claude)}
if claude:
    info["version"] = version(run([claude, "--version"])[1])
    try:
        status = json.loads(run([claude, "auth", "status", "--json"])[1])
    except ValueError:
        status = {}
    info["signed_in"] = bool(status.get("loggedIn"))
    info.update({key: str(status.get(key) or "") for key in ("email", "orgName", "subscriptionType", "authMethod")})
out["claude"] = info
print(json.dumps(out))
'''

ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]|\r")
DEVICE = re.compile(r"(?P<url>https://auth\.openai\.com/codex/device\S*)[\s\S]*?\b(?P<code>[A-Z0-9]{4}-[A-Z0-9]{4,6})\b")
CODEX_URL = re.compile(r"https://auth\.openai\.com/oauth/authorize\?\S+")
CLAUDE_URL = re.compile(r"https://claude\.(?:com|ai)/\S*oauth/authorize\?\S+")
SIGN_IN_LINK = re.compile(r"(https?://[^/\s]+)\S*")
ONE_TIME_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")


def clean(text: str) -> str:
    return ANSI.sub("", text)


class Remote:
    """A command on the login node: its output read as it comes, its stdin kept open for an answer."""

    def __init__(self, ssh: Ssh, command: str, extra: Sequence[str] = ()) -> None:
        args, env, self._askpass = ssh.prepared(command, extra=extra)
        try:
            self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, env=env)
        except OSError as exc:
            self._drop_askpass()
            raise StepFailed(f"ssh could not start: {exc}", "Retry this step.") from exc
        self._chunks: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        stream = self.process.stdout
        assert stream is not None
        while True:
            chunk = stream.read1(4096)
            if not chunk:
                return
            self._chunks.append(decoder.decode(chunk))

    @property
    def text(self) -> str:
        return clean("".join(self._chunks))

    def expect(self, pattern: re.Pattern[str], timeout: float = START_S) -> re.Match[str] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = pattern.search(self.text)
            if match:
                return match
            if self.process.poll() is not None and not self._reader.is_alive():
                return pattern.search(self.text)
            time.sleep(0.2)
        return None

    def send(self, line: str) -> None:
        try:
            assert self.process.stdin is not None
            self.process.stdin.write((line + "\n").encode())
            self.process.stdin.flush()
        except (OSError, ValueError):
            pass

    def wait(self, timeout: float) -> int | None:
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        self._reader.join(timeout=5)
        return code

    def tail(self, hide: Sequence[str] = ()) -> str:
        """The last output, for an error message (saved with the step): no links, codes or `hide` in it."""
        text = SIGN_IN_LINK.sub(lambda m: m[1] + "…", self.text)
        text = ONE_TIME_CODE.sub("XXXX-XXXX", text)
        for secret in hide:
            if secret:
                text = text.replace(secret, "…")
        return " ".join(text.split())[-300:]

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                assert self.process.stdin is not None
                self.process.stdin.close()  # the cluster side stops the sign-in when its stdin ends
            except OSError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        self._drop_askpass()

    def _drop_askpass(self) -> None:
        if self._askpass is not None:
            shutil.rmtree(self._askpass.parent, ignore_errors=True)
            self._askpass = None


def status(ssh: Ssh) -> dict[str, dict[str, Any]]:
    done = ssh.run(PRELUDE + "python3 -", stdin=STATUS.encode(), timeout=180)
    try:
        data = json.loads(done.stdout.decode(errors="replace").strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise StepFailed(f"could not read the agents' state on the cluster: "
                         f"{done.stderr.decode(errors='replace').strip()[-200:]}", "Retry this step.") from exc
    return {name: data.get(name) or {} for name in LABELS}


def account(name: str, info: dict[str, Any]) -> str:
    """One line for the page: whose account the agent uses on the cluster."""
    if not info.get("signed_in"):
        return f"{LABELS[name]}: not signed in"
    email = info.get("email") or "signed in (account not shown)"
    extra = [info.get("plan", "") and f"ChatGPT {info['plan']}", info.get("orgName", ""),
             info.get("subscriptionType", ""), info.get("method") == "API key" and "an API key"]
    details = ", ".join(str(e) for e in extra if e)
    return f"{LABELS[name]}: {email}" + (f" ({details})" if details else "")


def not_student(info: dict[str, Any]) -> bool:
    email = str(info.get("email", "")).lower()
    return bool(info.get("signed_in") and email and not email.endswith(STUDENT_DOMAIN))


def sign_out(ssh: Ssh, name: str) -> None:
    ssh.run(SIGN_OUT[name], timeout=120)


def sign_in(name: str, ctx: Context, ssh: Ssh, open_url: Callable[[str], Any]) -> bool:
    """False if the student chose to skip this agent."""
    return (_codex if name == "codex" else _claude)(ctx, ssh, open_url)


def _codex(ctx: Context, ssh: Ssh, open_url: Callable[[str], Any]) -> bool:
    job = Remote(ssh, CODEX_DEVICE)
    try:
        match = job.expect(DEVICE)
        if not match:
            raise StepFailed(f"Codex's sign-in did not start: {job.tail()}", "Retry this step.")
        open_url(match["url"])
        choice = _wait(ctx, job, {
            "title": "Sign in to Codex with your student ChatGPT account",
            "text": ["A page opened in your browser (or open the link below). Sign in with your MBZUAI student "
                     "ChatGPT account and enter this code. Codex on the cluster then works with that account.",
                     "If the page says device codes are turned off for your account, use the other way below."],
            "links": [{"label": "Open the Codex sign-in page", "url": match["url"]}], "code": match["code"],
            "wait_text": "Waiting for you to approve in the browser…",
            "choices": [{"name": "browser", "label": "Device codes are off: sign in another way"},
                        {"name": "skip", "label": "Skip Codex"}]}, DEVICE_S)
    finally:
        job.stop()
        ctx.clear()
    if choice == "browser":
        return _codex_browser(ctx, ssh, open_url)
    return choice != "skip"


def _codex_browser(ctx: Context, ssh: Ssh, open_url: Callable[[str], Any]) -> bool:
    if not port_free(CODEX_PORT):
        raise StepFailed(f"port {CODEX_PORT} on this computer is busy (another Codex sign-in is open?)",
                         "Close it, then Retry this step.")
    # 127.0.0.1, where Codex listens: "localhost" could reach someone else's listener on [::1] first
    job = Remote(ssh, CODEX_BROWSER, extra=["-L", f"{CODEX_PORT}:127.0.0.1:{CODEX_PORT}",
                                            "-o", "ExitOnForwardFailure=yes"])
    try:
        match = job.expect(CODEX_URL)
        if not match:
            raise StepFailed(f"Codex's sign-in did not start: {job.tail()}", "Retry this step.")
        open_url(match[0])
        choice = _wait(ctx, job, {
            "title": "Sign in to Codex with your student ChatGPT account",
            "text": ["A page opened in your browser (or open the link below). Sign in with your MBZUAI student "
                     f"ChatGPT account. When you approve, the browser returns to localhost:{CODEX_PORT} on this "
                     "computer, and a tunnel carries that to Codex on the cluster. Keep this page open."],
            "links": [{"label": "Open the Codex sign-in page", "url": match[0]}],
            "wait_text": "Waiting for you to approve in the browser…",
            "choices": [{"name": "skip", "label": "Skip Codex"}]}, DEVICE_S)
    finally:
        job.stop()
        ctx.clear()
    return choice != "skip"


def _claude(ctx: Context, ssh: Ssh, open_url: Callable[[str], Any]) -> bool:
    error = ""
    for _ in range(3):
        job = Remote(ssh, CLAUDE_LOGIN)
        code = ""
        try:
            match = job.expect(CLAUDE_URL)
            if not match:
                raise StepFailed(f"Claude Code's sign-in did not start: {job.tail()}", "Retry this step.")
            open_url(match[0])
            answer = ctx.ask({
                "title": "Sign in to Claude Code with your student Claude account",
                "text": ["A page opened in your browser (or open the link below). Sign in with your MBZUAI student "
                         "Claude account and press Authorize. The page then shows a code: copy it here.",
                         *([error] if error else [])],
                "links": [{"label": "Open the Claude sign-in page", "url": match[0]}],
                "fields": [{"name": "code", "label": "The code from the Claude page", "type": "text",
                            "required": True, "placeholder": "paste the code"}],
                "submit": "Sign in", "choices": [{"name": "skip", "label": "Skip Claude Code"}]})
            if answer.get("choice") == "skip":
                return False
            code = str(answer.get("code", "")).strip()
            job.send(code)
            job.wait(120)
            if status(ssh)["claude"].get("signed_in"):
                return True
            hide = [code, *code.split("#")]
            error = f"That code did not work ({job.tail(hide=hide)[-160:] or 'no answer'}); try again with the new page."
        finally:
            job.stop()
    raise StepFailed("Claude Code's sign-in did not work three times", "Retry this step.")


def _wait(ctx: Context, job: Remote, form: dict[str, Any], timeout: float) -> str:
    """Until the sign-in on the cluster ends (""), or the student presses one of the form's choices."""
    ctx.show(form)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answer = ctx.poll()
        if answer is not None:
            if answer.get("cancel"):
                raise StepFailed("stopped on the page", "Press Retry to try this step again.")
            if answer.get("choice"):
                return str(answer["choice"])
            ctx.show(form)  # an answer the form did not ask for: show it again
        code = job.wait(0.5)
        if code is not None:
            if code != 0:
                raise StepFailed(f"the sign-in ended with an error: {job.tail()}", "Retry this step.")
            return ""
    raise StepFailed("the sign-in code expired", "Retry this step for a new code.")


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True
