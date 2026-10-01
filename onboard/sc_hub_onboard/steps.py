"""The onboarding steps, in order. Each is safe to run again and skips what is already done."""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import assistants, browser, cluster, cluster_agents
from .engine import Context, Skip, Step, StepFailed
from .sshkit import ALIAS, HOST, IDE_ALIAS, JOB_ALIAS, LOGIN_ALIAS, REMOTE_ROOT, AskpassUnsupported, GateMissing, Paths, Ssh, \
    OWN_CONNECTION, SshError, SshUnreachable, alias_block, check_login, create_key, explain, gated, public_key, quote_cmd, \
    ssh_version, write_block

GATE_OPTIONS = 'restrict,port-forwarding,command="{root}/bin/schub-gate"'
HELLO = "hello"


def own_login(windows: bool = os.name == "nt") -> str:
    """What the student types to log in with their own key: `schub login`, or on Windows (no `schub` there) the ssh host."""
    return f"ssh {LOGIN_ALIAS}" if windows else "schub login"


# The password form of a re-run after the key was limited, and the one of the step that installs the student's own key: that
# one says what it installs (a key that is not limited) and can be put off.
PASSWORD_FORM = {"title": "Your cluster password, for this run", "text": [
    "Your sc-hub key only opens sc-hub now, so this step needs your password once. "
    "It stays in memory until this page closes and is never stored."],
    "fields": [{"name": "password", "label": "Password", "type": "password", "required": True}]}
OWN_KEY_CONSENT = {"title": "Install your own key on the cluster", "text": [
    "This installs a second key of your own on the cluster, so that you can log in from a terminal without a password "
    f"(`{own_login()}`). Unlike sc-hub's key, which only opens sc-hub, it is a normal key, for you alone. It has no passphrase: "
    "anything that runs as you on this computer can use it, so keep the disk encrypted and the screen locked."],
    "submit": "Install my key", "choices": [{"name": "skip", "label": "Not now"}]}
OWN_KEY_FORM = {"title": "Install your own key on the cluster", "text": [
    "This installs a second key of your own on the cluster, so that you can log in from a terminal without a password "
    f"(`{own_login()}`). Unlike sc-hub's key, which only opens sc-hub, it is a normal key, for you alone. It has no passphrase: "
    "anything that runs as you on this computer can use it, so keep the disk encrypted and the screen locked.",
    "It needs your cluster password once. The password stays in memory until this page closes and is never stored."],
    "fields": PASSWORD_FORM["fields"], "submit": "Install my key", "choices": [{"name": "skip", "label": "Not now"}]}


VPN_HINT = "Join the campus Wi-Fi or the VPN, then Retry."
# sc-hub's key is limited on the account but its gate is not there: the page never writes a plain key in its place.
GATE_HINT = ("This is on the cluster's side: if its /l is not available right now, Retry in a while. If sc-hub's folder "
             "there was moved or deleted, tell the pilot owner.")
# What `schub shell --no-start` exits with when it only looked (src/schub/bench/ide.py has the same numbers: the helper
# cannot import the cluster's code, and a test keeps the two equal).
SHELL_NOT_RUNNING, SHELL_STOPPED = 75, 76


class Declined(Exception):
    """The student chose "Not now" on a form that offered it."""


class Unreachable(StepFailed):
    """The cluster did not answer (the VPN, the Wi-Fi): a step to retry, never a refusal and never a skip."""


class Setup:
    """What the steps share: where to write on this computer, and the cluster account."""

    def __init__(self, paths: Paths, host: str = HOST, remote_root: str = "", repo: Path = cluster.REPO,
                 open_dashboard: bool = True, open_url: Callable[[str], Any] = browser.open_in_browser) -> None:
        self.paths, self.host, self.repo, self.open_dashboard = paths, host, repo, open_dashboard
        self.open_url = open_url  # the sign-in pages, in the student's default browser
        self.remote_root_wanted = remote_root  # "" = the default /l/users/<login>/schub

    def ssh(self, ctx: Context, form: dict[str, Any] = PASSWORD_FORM) -> Ssh:
        login = ctx.values.get("login")
        if not login:
            raise StepFailed("the cluster login is not known yet", "Retry the sign-in step first.")
        ssh = Ssh(self.paths, login, self.host)
        if ctx.values.get("limited") and "password" not in ctx.values:
            # a re-run after the key was limited: setup commands need the student's own login, once per run (the page
            # asks: that is the human in the loop, whatever other keys there are)
            answer = ctx.ask(form)
            if answer.get("choice") == "skip":
                raise Declined()
            ctx.values["password"] = str(answer.pop("password", ""))  # "password" is never saved to disk
        ssh.password = ctx.values.get("password")
        if ssh.password is not None:
            try:
                done = ssh.run("true", timeout=60)
            except AskpassUnsupported:
                raise StepFailed("this computer's ssh cannot take your password from this page, and the sc-hub key "
                                 "only opens sc-hub now", "Update OpenSSH (Windows: Settings → System → Optional "
                                 "features → OpenSSH Client, or: winget install Microsoft.OpenSSH.Preview), then "
                                 "Retry.") from None
            except SshUnreachable as exc:
                raise Unreachable(str(exc), VPN_HINT) from None
            if done.returncode != 0:
                ctx.values.pop("password", None)
                failure = explain(done.stderr)
                if isinstance(failure, SshUnreachable):  # (a network that is down is no refusal of what was typed)
                    raise Unreachable(str(failure), VPN_HINT)
                raise StepFailed("the cluster refused the password", "Retry and type it again.")
        return ssh

    def root(self, ctx: Context) -> str:
        """sc-hub's folder on the cluster, checked each time it goes into a command: it is read from the saved state, and a
        quote in it would end the single quotes that every command puts it in (a password session's included)."""
        root = str(ctx.values.get("remote_root", ""))
        if not REMOTE_ROOT.fullmatch(root):
            raise StepFailed("sc-hub's folder on the cluster is not known or is not a plain path",
                             "Retry the step that copies sc-hub to the cluster (`retry cluster`).")
        return root

    def write_config(self, ctx: Context, login: str | None = None) -> None:
        """The sc-hub block of ~/.ssh/config from what is there by now: the key that only opens sc-hub; VS Code's host once
        it is set up; the student's own key once it exists, and with it the terminal (`schub`) once it was seen to work (no
        half-working alias). Every value is checked: it comes from the saved state, and a line break would start a new line."""
        login = check_login(login or str(ctx.values["login"]))
        root = str(ctx.values.get("remote_root", ""))
        write_block(self.paths.ssh_config, alias_block(
            self.paths, login, self.host, str(ctx.values.get("ide_proxy", "")),
            login_key=self.paths.login_key.exists(),
            shell_root=root if ctx.values.get("terminal_command") and REMOTE_ROOT.fullmatch(root) else ""))

    # ---- 1. this computer ---------------------------------------------------------------------

    def check_computer(self, ctx: Context) -> str:
        found = {name: bool(shutil.which(name)) for name in ("ssh", "ssh-keygen", "codex", "claude", "code")}
        if not (found["ssh"] and found["ssh-keygen"]):
            hint = ("Windows: Settings → System → Optional features → add \"OpenSSH Client\", then run this again."
                    if os.name == "nt" else "Install the OpenSSH client, then run this again.")
            raise StepFailed("ssh is not installed on this computer", hint)
        version = ssh_version()
        ctx.values["ssh_version"] = list(version)
        ctx.values["local"] = found
        vscode = found["code"] or _vscode_app(self.paths)
        ctx.values["vscode"] = bool(vscode)
        tools = [n for n, label in (("codex", "Codex"), ("claude", "Claude Code")) if found[n]]
        parts = [platform.system().replace("Darwin", "macOS"), f"OpenSSH {version[0]}.{version[1]}",
                 ", ".join(label for n, label in (("codex", "Codex"), ("claude", "Claude Code")) if n in tools)
                 or "no terminal assistant yet", "VS Code" if vscode else "no VS Code"]
        return " · ".join(p for p in parts if p)

    # ---- 2. the browser for the sign-ins ------------------------------------------------------------

    def browser(self, ctx: Context) -> str:
        ctx.ask({"title": "Your browser for the sign-ins", "cancel": False, "text": [
            "Later, the agents on the cluster (Codex and Claude Code) need your permission to use your account. "
            "Their sign-in pages open in your default browser.",
            "Use the browser (or browser profile) where your MBZUAI student ChatGPT and Claude accounts are signed "
            "in, not a personal one: make it your default browser now, or copy the links into it when they appear.",
        ], "fields": [{"name": "ok", "type": "checkbox", "required": True,
                       "label": "My student accounts are in my default browser, or I will copy the links into it"}],
            "submit": "Continue"})
        return "student accounts for the sign-ins"

    # ---- 3. the cluster account and the key -------------------------------------------------------------

    def sign_in(self, ctx: Context) -> str:
        login, error = ctx.values.get("login", ""), ""
        create = None
        for _ in range(3):
            if login:
                self.write_config(ctx, login)
                create = create_key(self.paths, login)
                if self.key_logs_in(ctx, login):
                    ctx.values["login"] = login
                    self.settle_limit(ctx)
                    return f"{login}@{self.host}: key login works" + (" (a new key)" if create else "")
            answer = ctx.ask({"title": "Sign in to the MBZUAI cluster", "text": [
                "Your cluster login and password, once. The password goes straight to ssh on this computer to "
                "install a key; it is not stored and never goes to the assistant.", *([error] if error else [])],
                "fields": [{"name": "login", "label": "Cluster login", "type": "text", "required": True,
                            "placeholder": "firstname.lastname", "value": login},
                           {"name": "password", "label": "Password", "type": "password", "required": True}],
                "submit": "Sign in"})
            try:
                login = check_login(str(answer.get("login", "")))
            except SshError as exc:
                error, login = str(exc), ""
                continue
            self.write_config(ctx, login)
            create_key(self.paths, login)
            if self.key_logs_in(ctx, login):  # (the saved state was lost, not the key: nothing to write, and
                ctx.values["login"] = login  # above all no plain line over the limited one the assistants already use)
                self.settle_limit(ctx)
                return f"{login}@{self.host}: key login works"
            ctx.say(f"installing a key for {login} on {self.host}")
            try:
                self._install(ctx, Ssh(self.paths, login, self.host), str(answer.get("password", "")))
            except GateMissing as exc:  # (not the password: typing it again changes nothing)
                raise StepFailed(str(exc), GATE_HINT) from None
            except SshError as exc:
                error = str(exc)
                continue
            finally:
                answer.clear()  # the password is not kept anywhere
            ctx.values["login"] = login
            self.settle_limit(ctx)
            return f"{login}@{self.host}: key installed; no password from now on"
        raise StepFailed(error or "the sign-in did not work", "Check the login and the password, then Retry.")

    def key_logs_in(self, ctx: Context, login: str) -> bool:
        """Whether sc-hub's key logs in. A login that failed in the gate (it refused, or the login shell could not start it:
        the cluster's /l is not mounted, the folder moved) says the key's line is the limited one, whatever the saved state
        says: what sign-in installs is then limited too, never a plain line over it."""
        done = Ssh(self.paths, login, self.host).run("true", timeout=60)
        if done.returncode != 0 and gated(done.stderr):
            ctx.values["limited"] = True
        return done.returncode == 0

    def _install(self, ctx: Context, ssh: Ssh, password: str) -> None:
        """Put sc-hub's key on the cluster. On an account that is limited already (the laptop lost its key, a new laptop) it
        goes in limited from the start: never a plain line for a moment, and, with the gate not there, no line at all."""
        options, root = "", ""
        if ctx.values.get("limited"):
            root = str(ctx.values.get("remote_root", ""))
            if not REMOTE_ROOT.fullmatch(root):  # (the saved state is gone: there is no gate to point a limited line at)
                raise StepFailed("sc-hub's key only opens sc-hub on your account, and this computer no longer knows "
                                 "sc-hub's folder on the cluster: the key is not written (never as a plain line)", GATE_HINT)
            options = GATE_OPTIONS.format(root=root)
        try:
            ssh.install_key(password, remote_root=root, options=options)
        except AskpassUnsupported:
            if not console_available():
                raise SshError("this computer's OpenSSH is too old to take the password from this page; update "
                               "OpenSSH, then run this again") from None
            ctx.show({"title": "Type your password in the new window", "cancel": False, "text": [
                "This computer's ssh asks for the password itself: a black window opened. Type your cluster password "
                "there (it does not show while you type) and press Enter. The window closes by itself."],
                "wait_text": "Waiting for the password window…"})
            try:
                ssh.install_key_console(remote_root=root, options=options)
            finally:
                ctx.clear()
        if not ssh.key_works():
            raise SshError("the key was installed but the cluster does not accept it yet")
        for gone in ("vscode_link", "ide_proxy"):  # the job's own sshd holds the old public key: VS Code's host is set up
            ctx.values.pop(gone, None)  # again for this one, by the terminal step, which runs again
        ctx.reopen("terminal")

    def settle_limit(self, ctx: Context) -> None:
        """Make `limited` say what sc-hub's key is on the cluster, by looking at the key itself. A limited line the state does
        not know of (the helper stopped before it saved, a blip at the probe) is taken up: the page then asks the password for
        what takes a shell, instead of sending a rewrite through a key that refuses it for ever. A plain line on an account the
        state calls limited, or after the limit step is done (written some other way, a state that forgot), sends the limit step
        round again. A network that is down decides nothing."""
        try:
            probe = Ssh(self.paths, ctx.values["login"], self.host).run("echo sc-hub-probe", timeout=60)
        except SshError:
            return
        if gated(probe.stderr):
            ctx.values["limited"] = True
        elif probe.returncode == 0 and b"sc-hub-probe" in probe.stdout:
            if ctx.values.pop("limited", None) or ctx.status("limit") == "done":
                ctx.reopen("limit")

    # ---- 3b. the student's own key: a normal shell, no password -------------------------------------------------------------

    def login_key(self, ctx: Context) -> str:
        """A second key, the student's own, so they log in from a terminal without a password (`schub login`, `schub`). sc-hub's
        key is limited to sc-hub at the end of the setup and this one is not: a normal key, for the student alone (the
        assistants' instructions say so). It goes in with sc-hub's key while that still can; once it is limited the page asks
        for the password, says what it installs and lets the student put it off (a password the page already has from another
        step never stands in for that form). It is a convenience: if it does not work out, the setup goes on and says how to
        try again; only an unreachable cluster stops the page, to be retried."""
        login, key = ctx.values["login"], self.paths.login_key
        own = Ssh(self.paths, login, self.host, alias=LOGIN_ALIAS)
        ctx.values["login_key"] = False
        try:
            self.settle_limit(ctx)  # (a limit the saved state does not know of: the password, not a refusal of the gate)
            if key.exists():
                self.write_config(ctx)  # (the alias is there to try the key with)
            if not (key.exists() and own.shell_works()):
                ctx.values.pop("password", None)  # (the form with what this installs, and "Not now", comes every time)
                if not ctx.values.get("limited") and ctx.ask(OWN_KEY_CONSENT).get("choice") == "skip":
                    raise Declined()  # (the first run: nothing to type yet, but what it installs is said and can be put off)
                admin = self.ssh(ctx, OWN_KEY_FORM)  # the password, if sc-hub's key is limited by now
                ctx.say("installing your own key on the cluster: a normal key without a passphrase, for you alone "
                        "(sc-hub's own key only opens sc-hub)")
                create_key(self.paths, login, key=key, kind="sc-hub-login")
                if public_key(key).split()[:2] == public_key(self.paths.key).split()[:2]:  # (a copy of sc-hub's key at this
                    raise SshError(f"{key.name} is the same key as sc-hub's own: remove it, then run this again")  # place)
                self.write_config(ctx)
                admin.authorize(key)
                self.settle_limit(ctx)  # (whatever happened to sc-hub's line meanwhile, the limit is looked at again)
                if not own.shell_works():
                    raise SshError("the cluster does not accept it yet")
        except Unreachable:
            raise
        except SshUnreachable as exc:
            raise Unreachable(str(exc), VPN_HINT) from None
        except Declined:
            raise Skip("not now: run `retry login-key` when you want your own key (it needs your password once)") from None
        except StepFailed as exc:  # the password was refused, Stop was pressed, or this ssh cannot take a password from the page
            raise Skip(f"your own key was not installed ({exc}); the rest of the setup does not need it. "
                       "Run `retry login-key` to try again") from None
        except (SshError, OSError) as exc:
            raise Skip(f"could not set up your own key ({exc}); the rest of the setup does not need it. "
                       "Run `retry login-key` to try again") from None
        ctx.values["login_key"] = True
        for later in ("terminal", "dashboard"):  # they say what this key makes possible
            ctx.reopen(later)
        return f"your own key is in place: type ssh {LOGIN_ALIAS} to log in without a password"

    # ---- 4. sc-hub on the cluster ------------------------------------------------------------------------

    def install_cluster(self, ctx: Context) -> str:
        ssh = self.ssh(ctx)
        noise = ssh.run("true", timeout=60).stdout
        if noise:
            raise StepFailed(f"your cluster shell prints {len(noise)} bytes on non-interactive logins",
                             "In ~/.bashrc on the cluster, put this line before any echo: [[ $- == *i* ]] || return")
        ctx.say("copying sc-hub to the cluster")
        root = cluster.upload(ssh, self.remote_root_wanted or "/l/users/$USER/schub")
        if not REMOTE_ROOT.fullmatch(root):
            raise StepFailed(f"unexpected sc-hub folder on the cluster: {root[:80]}", "Use a plain --remote-root path.")
        ctx.values["remote_root"] = root
        ctx.say("setting up your workspace in a Slurm job: the analysis tools and the starter datasets (the first "
               "time about 5 minutes); the deep-learning tools follow in the background")
        code = cluster.stream(ssh, f"SCHUB_ROOT='{root}' bash '{root}/src/sc-hub/scripts/bootstrap_cluster.sh'", ctx.log)
        if code != 0:
            raise StepFailed("the setup on the cluster failed (see the details)", "Retry; the setup continues where "
                             "it stopped. If it fails again, send the details to the pilot owner.")
        if ctx.values.get("login_key") and not ctx.values.get("terminal_command") and \
                ctx.status("terminal") in ("done", "skipped"):  # (sc-hub there may have been older than `schub shell`; with
            ctx.reopen("terminal")  # VS Code set up, the step was done all the same)
        return f"sc-hub in {root}; the deep-learning tools (torch, scvi-tools) install in the background"

    # ---- 5. a first run -------------------------------------------------------------------------------------

    def hello(self, ctx: Context) -> str:
        root = self.root(ctx)  # (checked before the password is asked for)
        ssh = self.ssh(ctx)
        schub = f"'{root}/bin/schub'"
        cluster.remote(ssh, f"{schub} projects >/dev/null && ({schub} project-new {HELLO} --question "
                            f"'Does sc-hub work for me?' >/dev/null 2>&1 || true)")
        ctx.say("a first cell in your kernel (the workbench job starts; a minute or two)")
        first = self._run(ssh, schub, "import platform, os\nprint(platform.node(), os.cpu_count(), 'CPUs')",
                          "a first cell", "the node name and its CPUs", wait=240)
        first = self._final(ssh, schub, first)
        if first.get("status") != "ok":
            raise StepFailed(f"the first cell ended as {first.get('status')}: {first.get('message', '')}",
                             "Retry in a minute: the workbench may still be starting.")
        ctx.log(" ".join(o.get("text", "") for o in first.get("outputs", [])).strip())
        ctx.say("a small Slurm job with a check")
        code = ("%%slurm --cpus 1 --mem 1G --time 5m\nimport pandas as pd\n"
                "pd.DataFrame({'x': [1, 2, 3]}).to_csv('hello.csv', index=False)\nprint('hello from a job')")
        checks = [{"name": "table_columns", "params": {"path": "work/hello.csv", "columns": ["x"]}}]
        job = self._run(ssh, schub, code, "a first Slurm job", "a table with 3 rows", checks=checks, wait=60)
        job = self._final(ssh, schub, job, rounds=40)
        states = [j.get("state") for j in job.get("jobs", [])]
        checks_ok = [c.get("status") for c in job.get("checks", [])]
        if states != ["COMPLETED"] or checks_ok != ["pass"]:
            raise StepFailed(f"the job ended as {states or 'unknown'} with checks {checks_ok}",
                             "Retry; if it fails again, send the details to the pilot owner.")
        cluster.remote(ssh, f"{schub} dashboard >/dev/null")
        return "a kernel cell and a Slurm job ran; their check passed"

    def _run(self, ssh: Ssh, schub: str, code: str, why: str, expect: str, checks: list | None = None,
             wait: int = 60) -> dict:
        """A cell through the CLI: its code goes through stdin into a file, so no quoting can break it."""
        command = (f'f=$(mktemp) && cat >| "$f" && {schub} bench-run {HELLO} --code "$f" --why {_q(why)} '
                   f"--expect {_q(expect)} --checks {_q(json.dumps(checks or []))} --wait {wait}; "
                   'code=$?; rm -f "$f"; exit $code')
        done = ssh.run(command, stdin=code.encode(), timeout=wait + 300)
        if done.returncode != 0:
            raise StepFailed(f"the cell was refused: {done.stderr.decode(errors='replace').strip()[-300:]}", "Retry.")
        return _json(done.stdout.decode(errors="replace"))

    def _final(self, ssh: Ssh, schub: str, result: dict, rounds: int = 12) -> dict:
        for _ in range(rounds):
            open_jobs = [j for j in result.get("jobs", []) if j.get("state") not in ("COMPLETED", "FAILED",
                                                                                      "CANCELLED", "TIMEOUT")]
            if result.get("status") not in ("queued", "running") and not open_jobs:
                break
            result = _json(cluster.remote(ssh, f"{schub} bench-wait '{result['ref']}' --wait 45", timeout=120))
        return result

    # ---- 6. the assistants on this computer ------------------------------------------------------------------

    def connect_assistants(self, ctx: Context) -> str:
        root = self.root(ctx)
        folder = assistants.workspace(self.paths, self.repo)  # first: Codex and Claude Code turn sc-hub on there
        ctx.values["workspace"] = str(folder)
        done = [line for line in (assistants.codex(self.paths, root, folder, self.repo),
                                  assistants.claude_code(self.paths, root, folder, self.repo),
                                  assistants.claude_desktop(self.paths, root)) if line]
        for line in done:
            ctx.log(line)
        connected = [line.split(":")[0] for line in done if "connected" in line]
        ctx.values["assistants"] = connected  # for the welcome page
        return (", ".join(connected) + " connected" if connected else "no assistant found to connect") + \
            f"; workspace {folder}"

    # ---- 7. a terminal and VS Code in the workbench job ---------------------------------------------------------------

    def job_shell(self, ctx: Context) -> str:
        """The student's terminal: `schub` (or `ssh schub`) lands in their workbench job, over their own key and the login
        node's `schub shell` (srun --pty into the job), like a workstation command. Where there is VS Code, its Remote-SSH
        host into the job too (through sc-hub's key, as before). The two do not wait for each other, and neither is essential:
        a problem is told in the step's line with how to try again, and the step is skipped only if nothing came out of it,
        so it can never hold back the key limit."""
        root = self.root(ctx)
        works: list[str] = []
        problems: list[str] = []
        before = ctx.values.get("terminal_command")
        ok = keep = False
        try:
            command = self.terminal_command(ctx)  # the launcher and the PATH: all on this computer
            self.write_config(ctx)  # (the own key's host, to look with; `schub` stays as it was while the look runs)
            if ctx.values.get("login_key") and self.paths.login_key.exists():
                try:
                    where = self.look_at_job(ctx, root)
                except SshUnreachable:
                    keep = True  # a network that is down decides nothing: a `schub` that worked stays
                    raise
                ctx.values["terminal_command"], ok = command, True
                works.append(f"type {command} in a new terminal window: a shell inside {where}")
            else:
                problems.append("the terminal needs your own key (`retry login-key`)")
        except (SshError, StepFailed, subprocess.SubprocessError, OSError) as exc:
            problems.append(f"the terminal: {exc}")
        if not (ok or keep):
            ctx.values.pop("terminal_command", None)  # nothing promises a `schub` that does not work
        try:
            self.write_config(ctx)  # the host `schub` exactly when there is a terminal command
        except (SshError, OSError) as exc:
            problems.append(f"~/.ssh/config: {exc}")
        if ctx.values.get("terminal_command") != before:
            ctx.reopen("dashboard")  # (its welcome says what to type)
        if ctx.values.get("vscode"):
            try:
                works.append(f"VS Code ({self.vscode_host(ctx, root)})")
            except (SshError, StepFailed, subprocess.SubprocessError, OSError) as exc:  # (and Stop, a refused password)
                problems.append(f"VS Code was not set up ({exc})")
        for line in problems:
            ctx.log(line)
        if not works:
            raise Skip(f"{'; '.join(problems)}; run `retry terminal` to try again")
        return ". ".join(works) + (f". {'; '.join(problems)}: run `retry terminal` to try again" if problems else "")

    def look_at_job(self, ctx: Context, root: str) -> str:
        """Over the student's own key: does `schub shell` work on the cluster, and is the workbench running (`--no-start`: it
        starts nothing)? What to say about the job; an error says what is wrong."""
        ctx.say("looking at your workbench job")
        done = Ssh(self.paths, ctx.values["login"], self.host, alias=LOGIN_ALIAS).run(
            f"'{root}/bin/schub' shell --no-start -c 'echo sc-hub-job $SLURM_JOB_ID $(hostname)'", timeout=180)
        tail = done.stderr.decode(errors="replace").strip()[-160:]
        if done.returncode == 0:  # (the job's login shell may print a greeting first: the answer is the marked line)
            answer = next((line.split()[1:] for line in reversed(done.stdout.decode(errors="replace").splitlines())
                           if line.startswith("sc-hub-job ")), [])
            return f"job {answer[0]} on {answer[1]}" if len(answer) == 2 else "your workbench job"
        if done.returncode == SHELL_NOT_RUNNING:  # nothing is started by the setup: `schub` starts it
            return "your workbench job (it starts when you type schub)"
        if done.returncode == SHELL_STOPPED:
            raise SshError("your bench is stopped (bench/STOP exists on the cluster): remove it, then run `retry terminal`")
        if done.returncode == 255:  # ssh's own: the network, or a refusal
            raise explain(done.stderr)
        raise SshError(f"sc-hub on the cluster could not open a shell in your job ({tail or 'exit ' + str(done.returncode)}); "
                       "if it is older than this setup, run `retry cluster` first")

    def vscode_host(self, ctx: Context, root: str) -> str:
        """VS Code's Remote-SSH host into the workbench job, through sc-hub's key and the gate's ide-proxy: its name for the
        page. Starts the job if it is not running, so it is only tried where the student has VS Code, and only once: what
        was set up before is left as it is (no password, no job started by a rerun)."""
        if ctx.values.get("vscode_link") and ctx.values.get("ide_proxy"):
            return f"Remote-SSH → {IDE_ALIAS}"
        ssh = self.ssh(ctx)
        done = ssh.run(f"'{root}/bin/schub' ide-setup", stdin=public_key(self.paths.key).encode() + b"\n", timeout=120)
        if done.returncode != 0:
            raise StepFailed(f"could not prepare VS Code on the cluster: {done.stderr.decode(errors='replace')[-300:]}",
                             "Retry this step.")
        proxy = quote_cmd([shutil.which("ssh") or "ssh", *(["-F", str(self.paths.ssh_config)] if self.paths.custom else []),
                           "-T", "-o", "BatchMode=yes", ALIAS, f"{root}/bin/schub ide-proxy"])
        ctx.values["ide_proxy"] = proxy
        self.write_config(ctx)
        ctx.say("connecting VS Code into your workbench job (it starts if it is not running; up to a few minutes)")
        probe = subprocess.run(Ssh(self.paths, ctx.values["login"], self.host).base() +
                               ["-o", "BatchMode=yes", *OWN_CONNECTION, IDE_ALIAS, "echo $SLURM_JOB_ID $(hostname)"],
                               capture_output=True, text=True, timeout=1200, stdin=subprocess.DEVNULL)
        if probe.returncode != 0:
            ctx.values.pop("ide_proxy", None)  # (set up again, from the start, by the next try)
            raise StepFailed(f"VS Code's connection did not open: {probe.stderr.strip()[-300:]}",
                             "Retry in a few minutes (the workbench job may be waiting for a free slot).")
        ctx.values["vscode_link"] = f"vscode://vscode-remote/ssh-remote+{IDE_ALIAS}{root}/projects"
        code = shutil.which("code")
        if code and "ms-vscode-remote.remote-ssh" not in _run([code, "--list-extensions"]):
            ctx.log(_run([code, "--install-extension", "ms-vscode-remote.remote-ssh"]).strip()[-200:])
        return f"Remote-SSH → {IDE_ALIAS}"

    def terminal_command(self, ctx: Context) -> str:
        """What to type to land in the job. The launcher and ~/.sc-hub/bin on the PATH of new terminal windows are put in
        first: `schub` where that took (macOS, Linux), else the launcher's full path; on Windows `ssh schub`. A trial (`--home`)
        changed only its own home, so it gets the full path and the ssh config to use."""
        launcher = assistants.install_tools(self.paths, self.repo).parent / "schub"
        if os.name == "nt":
            return f"ssh {JOB_ALIAS}"
        lines = assistants.register_path(self.paths)
        for line in lines:
            ctx.log(line)
        if self.paths.custom:
            return f"SCHUB_SSH_CONFIG={shlex.quote(str(self.paths.ssh_config))} {shlex.quote(str(launcher))}"
        if any("added to" in line or "already has it" in line for line in lines):
            return "schub"
        ctx.log(f"schub is not on your PATH: type {view_command(self.paths, launcher)} (or add that folder to it)")
        return view_command(self.paths, launcher)

    # ---- 8. Codex and Claude Code on the cluster, with the student's accounts ---------------------------------

    def cluster_agents(self, ctx: Context) -> str:
        ssh = self.ssh(ctx)
        ctx.say("installing Codex and Claude Code on the cluster (the first time: a few minutes)")
        at_login: list[str] = []

        def line(text: str) -> None:
            text = cluster_agents.clean(text)
            ctx.log(text)
            if text.startswith(cluster_agents.PATH_AT_LOGIN):  # what a new login finds on PATH
                at_login[:] = sorted({word.rsplit("/", 1)[-1] for word in text.split(":", 1)[1].split()})

        code = cluster.stream(ssh, cluster_agents.INSTALL, line, timeout=1800)
        if code != 0:
            raise StepFailed("could not install the agents on the cluster (see the details)",
                             "Retry; if it fails again, send the details to the pilot owner.")
        ctx.values["cluster_path"] = at_login
        if at_login != ["claude", "codex"]:
            ctx.log("not on PATH at login: " + ", ".join(sorted({"claude", "codex"} - set(at_login))) +
                    " (the shell's files may skip ~/.bashrc; the details above say which files were changed)")
        skipped: set[str] = set()
        again = list(cluster_agents.LABELS)
        for _ in range(3):
            state = cluster_agents.status(ssh)
            for name in again:
                if not state[name].get("signed_in") and name not in skipped:
                    ctx.say(f"signing in {cluster_agents.LABELS[name]} on the cluster")
                    if not cluster_agents.sign_in(name, ctx, ssh, self.open_url):
                        skipped.add(name)
            state = cluster_agents.status(ssh)
            lines = [cluster_agents.account(n, state[n]) for n in cluster_agents.LABELS if n not in skipped]
            signed = [n for n in cluster_agents.LABELS if state[n].get("signed_in")]
            if not signed:
                raise Skip("no agent signed in on the cluster; Retry this step to sign in")
            odd = [cluster_agents.LABELS[n] for n in signed if cluster_agents.not_student(state[n])]
            answer = ctx.ask({"title": "Are these your student accounts?", "cancel": False, "text": [
                "The agents on the cluster work with these accounts:", *lines,
                *([f"{' and '.join(odd)}: this is not an {cluster_agents.STUDENT_DOMAIN} address. If it is a "
                   "personal account, sign in again with the student one."] if odd else [])],
                "fields": [{"name": "ok", "type": "checkbox", "required": True,
                            "label": "Yes, these are the accounts the agents should use"}],
                "submit": "Continue", "choices": [{"name": n, "label": f"Sign in {cluster_agents.LABELS[n]} again"}
                                                  for n in signed]})
            choice = str(answer.get("choice", ""))
            if choice not in cluster_agents.LABELS:
                if answer.get("ok") is not True:  # (the engine only lets a ticked box or a choice through)
                    continue
                ctx.values["cluster_agents"] = {n: state[n].get("email", "") for n in signed}
                return " · ".join(lines)
            cluster_agents.sign_out(ssh, choice)
            again = [choice]
        raise StepFailed("the accounts were not confirmed", "Retry this step to sign in again.")

    # ---- 9. the key only opens sc-hub -------------------------------------------------------------------------

    def limit_key(self, ctx: Context) -> str:
        root = self.root(ctx)  # (checked before the password is asked for)
        self.settle_limit(ctx)  # (is the key limited already, whatever the state says?)
        ssh = self.ssh(ctx)
        try:
            ssh.authorize(self.paths.key, remote_root=root, options=GATE_OPTIONS.format(root=root))
        except SshError:
            raise StepFailed("could not limit the key (everything else is set up)", "Retry this step.") from None
        gate = Ssh(self.paths, ctx.values["login"], self.host)  # the probe must use sc-hub's key itself, nothing else
        probe = gate.run("echo sc-hub-probe", timeout=60)
        if b"only opens sc-hub" not in probe.stderr:
            if probe.returncode == 255 and isinstance(explain(probe.stderr), SshUnreachable):  # a blip, not a key
                raise Unreachable(str(explain(probe.stderr)), VPN_HINT)
            raise StepFailed("the key still opens a shell", "Retry; if it stays, tell the pilot owner.")
        if not gate.key_works():
            raise StepFailed("the key no longer opens sc-hub", "On the cluster, restore ~/.ssh/authorized_keys."
                             "schub-backup with your password, then Retry.")
        ctx.values["limited"] = True
        return "sc-hub's key opens sc-hub only (MCP server, dashboard, sessions); your own key and your password are unchanged"

    # ---- 10. the dashboard ------------------------------------------------------------------------------------

    def dashboard(self, ctx: Context) -> str:
        """The dashboard's own small server on this computer, in the background, installed in ~/.sc-hub/bin (out of
        the folders the assistants write in); the page then opens its welcome: what the setup installed and how to
        work with it. It runs again at every start of this page, so an update or a restart brings the dashboard back."""
        for stale in ("dashboard", "next"):  # an earlier run's address may be gone (a restart of the computer)
            ctx.values.pop(stale, None)
        launcher = assistants.install_tools(self.paths, self.repo)
        command = ctx.values["view_command"] = view_command(self.paths, launcher)
        write_welcome(self.paths, self.host, ctx.values)
        ctx.values["summary"] = {"lines": summary_lines(ctx.values, self.host)}
        if not self.open_dashboard:
            raise Skip(f"start it yourself: {command}")
        ctx.say("starting your dashboard on this computer")
        start = [sys.executable, str(launcher.parent / "schub_view.py"), "start", "--no-open", "--json", "--alias", ALIAS,
                 "--remote", self.root(ctx), *(["--home", str(self.paths.home)] if self.paths.custom else [])]
        done = None
        try:
            done = subprocess.run(start, capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)
            info = json.loads(done.stdout.strip().splitlines()[-1])
            url, port = str(info["url"]), int(info["port"])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError, KeyError, TypeError):
            reason = ((done.stderr or done.stdout).strip()[-200:] if done is not None else "") or "it did not answer"
            raise Skip(f"the dashboard did not start ({reason}); start it yourself: {command}") from None
        ctx.values["dashboard"] = url
        ctx.values["summary"] = {"lines": summary_lines(ctx.values, self.host)}
        ctx.values["next"] = f"http://127.0.0.1:{port}/go?to=%2Fwelcome"  # the page goes there when it is done
        return f"{url}, in the background; after a restart of this computer: {command}"


def terminal_commands(values: dict[str, Any]) -> tuple[str, str]:
    """(what to type to land in the job, what to type for the login node) as the page and the welcome say them. Both need the
    student's own key, so neither is promised without it. Worked out when asked, so a later `retry login-key` shows in both."""
    terminal = str(values.get("terminal_command") or "")
    if not terminal or not values.get("login_key"):
        return "", ""
    return terminal, (f"ssh {LOGIN_ALIAS}" if terminal.startswith("ssh ") else f"{terminal} login")


def view_command(paths: Paths, launcher: Path) -> str:
    """How the student starts the dashboard again, as they would type it."""
    if paths.custom:
        return str(launcher)
    return "~" + ("\\" if os.name == "nt" else "/") + launcher.relative_to(paths.home).as_posix().replace(
        "/", "\\" if os.name == "nt" else "/")


def write_welcome(paths: Paths, host: str, values: dict[str, Any]) -> None:
    """What the setup installed, for the dashboard's welcome page (~/.sc-hub/welcome.json, this account only)."""
    facts = {"login": values.get("login", ""), "host": host, "remote_root": values.get("remote_root", ""),
             "workspace": values.get("workspace", ""), "assistants": list(values.get("assistants") or []),
             "vscode_host": IDE_ALIAS if values.get("vscode_link") else "",
             "cluster_agents": dict(values.get("cluster_agents") or {}),
             "cluster_path": list(values.get("cluster_path") or []), "windows": os.name == "nt",
             "terminal_command": terminal_commands(values)[0], "login_command": terminal_commands(values)[1],
             "view_command": values.get("view_command", ""), "set_up": time.strftime("%Y-%m-%d %H:%M")}
    target = paths.state.parent / "welcome.json"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(json.dumps(facts, indent=1))
    temp.replace(target)


def summary_lines(values: dict[str, Any], host: str) -> list[str]:
    """The setup page's last card (its welcome page says the same at more length)."""
    folder = values.get("workspace", "")
    command = values.get("view_command", "")
    lines = [f"Your workspace: {folder}. Open it in Codex or Claude Code (or restart Claude Desktop), type $schub "
             "(Codex) or /schub (Claude Code) and ask your question."]
    if values.get("dashboard"):
        lines.append(f"Your dashboard: {values['dashboard']}. It runs in the background; after a restart of this "
                     f"computer, {command} brings it back.")
    else:
        lines.append(f"Start your dashboard with {command}; its Journal shows the project '{HELLO}' with the first "
                     "run.")
    terminal, login = terminal_commands(values)
    if terminal:
        lines.append(f"Your terminal: type {terminal} in a new terminal window to land in your workbench job on the cluster"
                     + (f"; {login} opens the login node. Neither asks for a password." if login else
                        ". It asks for no password."))
    if values.get("vscode_link"):
        lines.append(f"VS Code: Remote-SSH → {IDE_ALIAS} opens your projects inside your workbench job "
                     f"({values['vscode_link']}).")
    if values.get("cluster_agents"):
        lines.append(f"The lab agent on the cluster uses {_agents_line(values['cluster_agents'])}.")
    on_path = values.get("cluster_path") or []
    if on_path:
        lines.append(f"On the cluster, {' and '.join(on_path)} work after you log in "
                     f"(ssh {values.get('login', 'LOGIN')}@{host}).")
    return lines


def _agents_line(signed: dict[str, str]) -> str:
    return " and ".join(f"{cluster_agents.LABELS.get(name, name)} ({email or 'signed in'})" for name, email in signed.items())


def console_available() -> bool:
    """Windows can give ssh a console window of its own for the password."""
    return os.name == "nt"


def _run(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=300).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _q(text: str) -> str:
    """A single-quoted word for the cluster's shell."""
    return "'" + text.replace("'", "'\\''") + "'"


def _json(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise StepFailed(f"unexpected answer from the cluster: {text.strip()[:200]}", "Retry.") from exc
    return data if isinstance(data, dict) else {}


def _vscode_app(paths: Paths) -> bool:
    if paths.custom:
        return False
    candidates = [Path("/Applications/Visual Studio Code.app")]
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Microsoft VS Code" / "Code.exe")
    return any(c.exists() for c in candidates)


def build(setup: Setup) -> list[Step]:
    return [
        Step("computer", "Check this computer", setup.check_computer, 0.3),
        Step("browser", "Your browser for the sign-ins", setup.browser, 0.2),
        Step("sign-in", "Sign in to the cluster, once", setup.sign_in, 1.0),
        Step("login-key", "Your own key for the cluster", setup.login_key, 0.4),
        Step("cluster", "sc-hub on the cluster", setup.install_cluster, 4.0),
        Step("hello", "A first run: a cell and a Slurm job", setup.hello, 3.0),
        Step("terminal", "A terminal and VS Code in your workbench job", setup.job_shell, 1.0),
        Step("agents", "Codex and Claude Code on the cluster", setup.cluster_agents, 1.5),
        Step("limit", "Limit the key to sc-hub", setup.limit_key, 0.3),
        # (after the limit: an assistant connected sooner would hold a key that is still a full shell if the run stopped
        # in between, at a sign-in the student pressed Stop on)
        Step("assistants", "Connect your assistants", setup.connect_assistants, 0.5),
        Step("dashboard", "Open the dashboard", setup.dashboard, 0.3, again=True),
    ]


def python() -> str:
    return sys.executable
