"""The onboarding steps, in order. Each is safe to run again and skips what is already done."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from . import assistants, cluster
from .engine import Context, Skip, Step, StepFailed
from .sshkit import ALIAS, ASKPASS_MIN, HOST, IDE_ALIAS, Paths, Ssh, SshError, alias_block, check_login, create_key, \
    quote_cmd, ssh_version, write_block

GATE_OPTIONS = 'restrict,port-forwarding,command="{root}/bin/schub-gate"'
HELLO = "hello"


class Setup:
    """What the steps share: where to write on this computer, and the cluster account."""

    def __init__(self, paths: Paths, host: str = HOST, remote_root: str = "", repo: Path = cluster.REPO,
                 open_dashboard: bool = True) -> None:
        self.paths, self.host, self.repo, self.open_dashboard = paths, host, repo, open_dashboard
        self.remote_root_wanted = remote_root  # "" = the default /l/users/<login>/schub

    def ssh(self, ctx: Context) -> Ssh:
        login = ctx.values.get("login")
        if not login:
            raise StepFailed("the cluster login is not known yet", "Retry the sign-in step first.")
        ssh = Ssh(self.paths, login, self.host)
        if ctx.values.get("limited") and "password" not in ctx.values:
            # a re-run after the key was limited: setup commands need the student's own login, once per run
            answer = ctx.ask({"title": "Your cluster password, for this run", "text": [
                "Your sc-hub key only opens sc-hub now, so updating the cluster side needs your password once. "
                "It stays in memory until this page closes and is never stored."],
                "fields": [{"name": "password", "label": "Password", "type": "password", "required": True}]})
            ctx.values["password"] = str(answer.pop("password", ""))  # "password" is never saved to disk
        ssh.password = ctx.values.get("password")
        return ssh

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
                write_block(self.paths.ssh_config, alias_block(self.paths, login, self.host, ctx.values.get("ide_proxy", "")))
                create = create_key(self.paths, login)
                if Ssh(self.paths, login, self.host).key_works():
                    ctx.values["login"] = login
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
            write_block(self.paths.ssh_config, alias_block(self.paths, login, self.host, ctx.values.get("ide_proxy", "")))
            create_key(self.paths, login)
            ctx.say(f"installing a key for {login} on {self.host}")
            try:
                self._install(ctx, Ssh(self.paths, login, self.host), str(answer.get("password", "")))
            except SshError as exc:
                error = str(exc)
                continue
            finally:
                answer.clear()  # the password is not kept anywhere
            ctx.values["login"] = login
            return f"{login}@{self.host}: key installed; no password from now on"
        raise StepFailed(error or "the sign-in did not work", "Check the login and the password, then Retry.")

    def _install(self, ctx: Context, ssh: Ssh, password: str) -> None:
        if tuple(ctx.values.get("ssh_version", (0, 0))) < ASKPASS_MIN:
            raise SshError("this computer's OpenSSH is too old to take the password from this page; update it "
                           "(Windows: Settings → Optional features → OpenSSH Client) and run this again")
        ssh.install_key(password)
        if not ssh.key_works():
            raise SshError("the key was installed but the cluster does not accept it yet")

    # ---- 4. sc-hub on the cluster ------------------------------------------------------------------------

    def install_cluster(self, ctx: Context) -> str:
        ssh = self.ssh(ctx)
        noise = ssh.run("true", timeout=60).stdout
        if noise:
            raise StepFailed(f"your cluster shell prints {len(noise)} bytes on non-interactive logins",
                             "In ~/.bashrc on the cluster, put this line before any echo: [[ $- == *i* ]] || return")
        ctx.say("copying sc-hub to the cluster")
        root = cluster.upload(ssh, self.remote_root_wanted or "/l/users/$USER/schub")
        ctx.values["remote_root"] = root
        ctx.say("setting up your workspace in a Slurm job (a few minutes)")
        code = cluster.stream(ssh, f"bash '{root}/src/sc-hub/scripts/bootstrap_cluster.sh'", ctx.log)
        if code != 0:
            raise StepFailed("the setup on the cluster failed (see the details)", "Retry; the setup continues where "
                             "it stopped. If it fails again, send the details to the pilot owner.")
        return f"sc-hub in {root}"

    # ---- 5. a first run -------------------------------------------------------------------------------------

    def hello(self, ctx: Context) -> str:
        ssh, root = self.ssh(ctx), ctx.values["remote_root"]
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
        command = (f'f=$(mktemp) && cat > "$f" && {schub} bench-run {HELLO} --code "$f" --why {_q(why)} '
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
        root = ctx.values["remote_root"]
        done = [line for line in (assistants.codex(self.paths, root), assistants.claude_code(self.paths, root),
                                  assistants.claude_desktop(self.paths, root)) if line]
        for line in done:
            ctx.log(line)
        folder = assistants.workspace(self.paths, self.repo)
        ctx.values["workspace"] = str(folder)
        connected = [line.split(":")[0] for line in done if "connected" in line]
        return (", ".join(connected) + " connected" if connected else "no assistant found to connect") + \
            f"; workspace {folder}"

    # ---- 7. VS Code in the workbench job --------------------------------------------------------------------------

    def vscode(self, ctx: Context) -> str:
        """VS Code (Remote-SSH) into the student's own workbench job, through the gate: set up once, then opening
        VS Code connects to the running job or starts it."""
        ssh, root = self.ssh(ctx), ctx.values["remote_root"]
        done = ssh.run(f"'{root}/bin/schub' ide-setup", stdin=self.paths.key.with_suffix(".pub").read_bytes(),
                       timeout=120)
        if done.returncode != 0:
            raise StepFailed(f"could not prepare VS Code on the cluster: {done.stderr.decode(errors='replace')[-300:]}",
                             "Retry this step.")
        proxy = quote_cmd([shutil.which("ssh") or "ssh", *(["-F", str(self.paths.ssh_config)] if self.paths.custom else []),
                           "-T", "-o", "BatchMode=yes", ALIAS, f"{root}/bin/schub ide-proxy"])
        ctx.values["ide_proxy"] = proxy
        write_block(self.paths.ssh_config, alias_block(self.paths, ctx.values["login"], self.host, proxy))
        ctx.say("connecting into your workbench job (it starts if it is not running; up to a few minutes)")
        probe = subprocess.run(Ssh(self.paths, ctx.values["login"], self.host).base() +
                               ["-o", "BatchMode=yes", IDE_ALIAS, "echo $SLURM_JOB_ID $(hostname)"],
                               capture_output=True, text=True, timeout=1200, stdin=subprocess.DEVNULL)
        if probe.returncode != 0:
            raise StepFailed(f"VS Code's connection did not open: {probe.stderr.strip()[-300:]}",
                             "Retry in a few minutes (the workbench job may be waiting for a free slot).")
        job, _, node = probe.stdout.strip().partition(" ")
        folder = f"{root}/projects"
        ctx.values["vscode_link"] = f"vscode://vscode-remote/ssh-remote+{IDE_ALIAS}{folder}"
        code = shutil.which("code")
        if code and "ms-vscode-remote.remote-ssh" not in _run([code, "--list-extensions"]):
            ctx.log(_run([code, "--install-extension", "ms-vscode-remote.remote-ssh"]).strip()[-200:])
        where = f"job {job} on {node}" if job else "your workbench job"
        if not ctx.values.get("vscode"):
            raise Skip(f"ready for VS Code ({where}); install VS Code, then Remote-SSH → {IDE_ALIAS}")
        return f"VS Code opens a shell and files in {where}: Remote-SSH → {IDE_ALIAS}"

    # ---- 8. the key only opens sc-hub -------------------------------------------------------------------------

    def limit_key(self, ctx: Context) -> str:
        ssh, root = self.ssh(ctx), ctx.values["remote_root"]
        public = self.paths.key.with_suffix(".pub").read_bytes()
        from .sshkit import KEY_LINE_REMOTE

        options = GATE_OPTIONS.format(root=root)
        done = ssh.run(f"R='{root}'; OPTS='{options}'; {KEY_LINE_REMOTE}", stdin=public, timeout=120)
        if done.returncode != 0:
            raise StepFailed("could not limit the key (everything else is set up)", "Retry this step.")
        ssh.password = None  # the probe must use the key itself
        probe = ssh.run("echo sc-hub-probe", timeout=60)
        if b"only opens sc-hub" not in probe.stderr:
            raise StepFailed("the key still opens a shell", "Retry; if it stays, tell the pilot owner.")
        if not ssh.key_works():
            raise StepFailed("the key no longer opens sc-hub", "On the cluster, restore ~/.ssh/authorized_keys."
                             "schub-backup with your password, then Retry.")
        ctx.values["limited"] = True
        return "the key opens sc-hub only (MCP server, dashboard, sessions); your password login is unchanged"

    # ---- 8. the dashboard -------------------------------------------------------------------------------------

    def dashboard(self, ctx: Context) -> str:
        folder = Path(ctx.values["workspace"])
        view = folder / ("schub-view.cmd" if os.name == "nt" else "schub-view")
        if not view.exists() or not self.open_dashboard:
            raise Skip("start it yourself: ./schub-view in the workspace")
        env = {**os.environ, "SCHUB_ALIAS": "mbzuai-schub"}
        kwargs = {"creationflags": subprocess.CREATE_NEW_CONSOLE} if os.name == "nt" else {"start_new_session": True}
        subprocess.Popen([str(view)], cwd=folder, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         **kwargs)  # type: ignore[arg-type]
        ctx.values["summary"] = {"lines": [
            f"Your workspace: {folder}. Open it in Codex or Claude Code (or restart Claude Desktop) and ask, for "
            "example: \"What datasets are in sc-hub? Create a project for my question.\"",
            "The dashboard mirror runs in the background and opens in your browser; its Journal shows the "
            f"project '{HELLO}' with the first run.",
            *([f"VS Code: Remote-SSH → {IDE_ALIAS} opens your projects inside your workbench job "
               f"({ctx.values['vscode_link']})."] if ctx.values.get("vscode_link") else []),
        ]}
        return "the dashboard mirror is running"


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
        Step("cluster", "sc-hub on the cluster", setup.install_cluster, 4.0),
        Step("hello", "A first run: a cell and a Slurm job", setup.hello, 3.0),
        Step("assistants", "Connect your assistants", setup.connect_assistants, 0.5),
        Step("vscode", "VS Code in your workbench job", setup.vscode, 1.0),
        Step("limit", "Limit the key to sc-hub", setup.limit_key, 0.3),
        Step("dashboard", "Open the dashboard", setup.dashboard, 0.3),
    ]


def python() -> str:
    return sys.executable
