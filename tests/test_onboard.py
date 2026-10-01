"""The onboarding helper end to end against a fake cluster (tests/onboard_fakes/ssh), through its page's API."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ONBOARD = Path(__file__).resolve().parents[1] / "onboard"
FAKES = Path(__file__).resolve().parent / "onboard_fakes"
sys.path.insert(0, str(ONBOARD))

from sc_hub_onboard.cluster_agents import CLAUDE_URL, DEVICE, clean  # noqa: E402
from sc_hub_onboard.engine import Engine  # noqa: E402
from sc_hub_onboard.server import OnboardServer  # noqa: E402
from sc_hub_onboard.sshkit import Paths, SshError, check_login, strip_block, write_block  # noqa: E402
from sc_hub_onboard.steps import REMOTE_ROOT, Setup, build  # noqa: E402


@pytest.fixture
def helper(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKES}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_CLUSTER", str(tmp_path / "cluster"))
    monkeypatch.setenv("FAKE_PASSWORD", "right horse battery")
    monkeypatch.setenv("SHELL", "/bin/zsh")  # macOS: the PATH block of `schub` goes into the test home's ~/.zshrc
    monkeypatch.delenv("ZDOTDIR", raising=False)  # (a shell that exports it, VS Code's terminal, must not steer the test)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".ssh").mkdir()
    (home / ".ssh" / "config").write_text("Host *\n    ServerAliveInterval 30\n")
    paths = Paths(home=home)
    opened: list[str] = []  # the sign-in pages the helper opened in the browser
    engine = Engine(build(Setup(paths, open_dashboard=False, open_url=opened.append)), paths.state)
    server = OnboardServer(engine)
    server.opened = opened
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, paths, tmp_path / "cluster"
    server.shutdown()


def authorized(cluster: Path, public: Path) -> dict:
    """The cluster's authorized_keys entry of a public key file: {"key": its line, "options": what limits it}."""
    return json.loads((cluster / "authorized.json").read_text())[public.read_text().split()[1]]


def calls_to(cluster: Path) -> list[dict]:
    return [json.loads(line) for line in (cluster / "calls.jsonl").read_text().splitlines()]


def api(server: OnboardServer, path: str, body: dict | None = None, token: str | None = None, host: str | None = None):
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Onboard-Token": server.token if token is None else token,
                                              "Content-Type": "application/json",
                                              **({"Host": host} if host else {})})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read() or b"{}")


def until(server: OnboardServer, predicate, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = api(server, "/api/state")
        if predicate(state):
            return state
        time.sleep(0.05)
    raise AssertionError(f"timed out; last state: {json.dumps(state)[:600]}")


def reply(server: OnboardServer, values: dict) -> dict:
    """Answer the form on the page now, as the page does (with that form's id)."""
    return api(server, "/api/answer", {**values, "form_id": asking(api(server, "/api/state"))["ask"]["id"]})


def asking(state: dict) -> dict | None:
    return next((s for s in state["steps"] if s["status"] == "asking"), None)


def form(server: OnboardServer, title: str, timeout: float = 60) -> dict:
    """The form the page shows once it has `title` in it (a step waiting for the student)."""
    state = until(server, lambda s: bool(asking(s) and title in json.dumps(asking(s)["ask"])) or
                  any(x["status"] == "failed" for x in s["steps"]), timeout)
    assert asking(state), [x for x in state["steps"] if x["status"] == "failed"]
    return asking(state)["ask"]


def to_the_agents(server: OnboardServer, typo_first: bool = True, own_key: str = "install") -> None:
    """The browser form and the sign-in (a typo first, if asked), then the form of the own key: "install" answers it, "skip"
    presses Not now, "leave" leaves it to the test."""
    api(server, "/api/start", {})
    assert "student ChatGPT and Claude accounts" in json.dumps(form(server, "browser for the sign-ins"))
    reply(server, {"ok": True})
    form(server, "Sign in to the MBZUAI cluster")
    if typo_first:
        reply(server, {"login": "Test.User", "password": "wrong"})
        ask = form(server, "refused the login")
        assert ask["fields"][0]["value"] == "test.user"  # the login is kept, the password is not
    reply(server, {"login": "test.user", "password": "right horse battery"})
    if own_key != "leave":
        ask = form(server, "Install your own key on the cluster")  # the first run asks too, with nothing to type
        assert not ask.get("fields") and [c["name"] for c in ask["choices"]] == ["skip"]
        reply(server, {"choice": "skip"} if own_key == "skip" else {})


def test_the_whole_onboarding_from_the_page(helper) -> None:
    server, paths, cluster = helper
    to_the_agents(server)
    state = sign_in_both(server, cluster)
    failed = [x for x in state["steps"] if x["status"] == "failed"]
    assert not failed, failed
    assert state["progress"] == 100 and [x["status"] for x in state["steps"]][-1] == "skipped"
    assert state["next"] == ""  # no dashboard started here (the fixture's --no-browser): nothing to open
    by_id = {x["id"]: x for x in state["steps"]}
    assert by_id["login-key"]["status"] == "done" and "ssh mbzuai-login" in by_id["login-key"]["detail"]
    terminal = by_id["terminal"]  # no VS Code here: the terminal is set up all the same
    launcher = paths.home / ".sc-hub" / "bin" / "schub"
    assert terminal["status"] == "done" and "job 207131 on gpu-03" in terminal["detail"] and str(launcher) in terminal["detail"]
    # this computer: the alias first in ~/.ssh/config, the old settings kept, two keys, the assistants' configs
    config = (paths.ssh_config).read_text()
    assert config.startswith("# >>> sc-hub >>>\nHost mbzuai-schub\n") and "ServerAliveInterval 30" in config
    assert "User test.user" in config and paths.key.exists() and paths.login_key.exists()
    assert "Host mbzuai-schub-ide" not in config  # no editor: the assistants' key gets no way into the job
    assert "    ControlMaster no\n    ControlPath none\n" in config.split("Host mbzuai-login")[0]  # never a shared connection
    # the student's own key: the login node, and the terminal, which is `schub shell` there on a terminal of its own
    own_hosts = config.split("Host mbzuai-login\n")[1]
    assert f'IdentityFile "{paths.login_key.as_posix()}"' in own_hosts and "Host schub\n" in own_hosts
    assert "    RequestTTY force\n    RemoteCommand /l/users/test.user/schub/bin/schub shell\n" in own_hosts
    assert config.count("RemoteCommand") == 1 and config.count(f'IdentityFile "{paths.key.as_posix()}"') == 1
    codex = (paths.home / ".codex" / "config.toml").read_text()
    assert "[mcp_servers.schub]" in codex and "/l/users/test.user/schub/bin/schub-mcp" in codex and str(paths.ssh_config) in codex
    # sc-hub only where the student asks for it: off in general, on in the (trusted) workspace, a skill to start it
    assert "enabled = false" in codex and f'[projects.{json.dumps(str(paths.workspace))}]' in codex
    assert "enabled = true" in (paths.workspace / ".codex" / "config.toml").read_text()
    skill = (paths.home / ".codex" / "skills" / "schub" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: schub\n") and "$schub" in skill and str(paths.workspace) in skill
    assert "allow_implicit_invocation: false" in (paths.home / ".codex" / "skills" / "schub" / "agents" /
                                                  "openai.yaml").read_text()
    assert (paths.workspace / "AGENTS.md").exists()
    # what the student runs outside any sandbox (the dashboard, the session opener) lives out of the workspace the
    # assistants write in; the launcher tries this setup's Python first
    bin_dir = paths.home / ".sc-hub" / "bin"
    assert not any((paths.workspace / name).exists() for name in ("schub-view", "schub_view.py", "schub-lab", "schub"))
    assert all((bin_dir / name).exists() for name in ("schub_view.py", "schub_view_copy.py", "schub_view_pages.py",
                                                      "schub-lab", "schub"))
    assert f'PINNED="{sys.executable}"' in (bin_dir / "schub-view").read_text() and bin_dir.stat().st_mode & 0o077 == 0
    assert os.access(bin_dir / "schub", os.X_OK)  # the terminal command: typed in a new window, lands in the job
    rc = (paths.home / ".zshrc").read_text()  # (the fixture's shell is zsh) ~/.sc-hub/bin on the PATH, in a marked block
    assert rc.count("# >>> sc-hub (this computer) >>>") == 1 and '$HOME/.sc-hub/bin' in rc and \
        (paths.home / ".bashrc").exists() is False
    # what the welcome page will say: the assistants, the accounts, and codex and claude on the PATH at login
    welcome = json.loads((paths.home / ".sc-hub" / "welcome.json").read_text())
    assert welcome["login"] == "test.user" and welcome["cluster_path"] == ["claude", "codex"]
    assert welcome["cluster_agents"] == {"codex": "test.user@mbzuai.ac.ae", "claude": "test.user@mbzuai.ac.ae"}
    assert welcome["workspace"] == str(paths.workspace) and welcome["remote_root"] == "/l/users/test.user/schub"
    assert welcome["view_command"] == str(bin_dir / "schub-view")  # (a trial's home: its full path)
    # (a trial's home changed only its own shell files: the full path, and the ssh config to use; a real one says `schub`)
    command = f"SCHUB_SSH_CONFIG={paths.ssh_config} {launcher}"
    assert welcome["terminal_command"] == command and welcome["login_command"] == f"{command} login"
    assert f"Start your dashboard with {bin_dir / 'schub-view'}" in " ".join(state["summary"]["lines"])
    assert f"Your terminal: type {command} in a new terminal window to land in your workbench job on the cluster; " \
           f"{command} login opens the login node. Neither asks for a password." in state["summary"]["lines"]
    assert "next" not in json.loads(paths.state.read_text())["values"]  # an address of this run only
    assert "On the cluster, claude and codex work after you log in (ssh test.user@login-student-lab.mbzu.ae)." \
        in state["summary"]["lines"]
    agents = next(x for x in state["steps"] if x["id"] == "agents")
    assert any("PATH at login:" in line for line in agents["log"])
    # research there; the way back to fixing sc-hub names the folder the setup ran from
    assert f"(sc-hub setup folder: {ONBOARD.parent})" in (paths.workspace / "AGENTS.md").read_text()
    # the cluster: sc-hub's key limited to the gate at the end, the student's own key a normal one, sc-hub uploaded,
    # bootstrap and a first run
    gate = authorized(cluster, paths.key.with_suffix(".pub"))
    assert gate["options"] == 'restrict,port-forwarding,command="/l/users/test.user/schub/bin/schub-gate"'
    own = authorized(cluster, paths.login_key.with_suffix(".pub"))
    assert own["options"] == "" and own["key"].startswith("ssh-ed25519 ") and own["key"] != gate["key"]
    logged = calls_to(cluster)
    assert sum("PubkeyAuthentication=no" in c["args"] for c in logged) == 2  # the typo and the password: no other
    calls = [c["command"] for c in logged]  # password login, not even for the student's own key
    assert any("bootstrap_cluster.sh" in c for c in calls) and sum("bench-run" in c for c in calls) == 2
    assert (cluster / "bundle.b64").stat().st_size > 10_000
    saved = paths.state.read_text()
    assert "right horse battery" not in saved and "wrong" not in saved  # no password on disk, ever


def sign_in_both(server: OnboardServer, cluster: Path) -> dict:
    """From the agents' step to the end: Codex by its device code, Claude Code by a pasted code (a wrong one
    first), then the accounts confirmed."""
    # Codex on the cluster: the device page opens in the browser, the page shows the code and waits
    ask = form(server, "Sign in to Codex")
    assert ask["wait"] and ask["code"] == "FAKE-C0DE1" and ask["links"][0]["url"] == "https://auth.openai.com/codex/device"
    assert server.opened == ["https://auth.openai.com/codex/device"]
    (cluster / "codex_approved").write_text("test.user@mbzuai.ac.ae")  # the student approves in the browser
    # Claude Code: a wrong code first, then the right one
    ask = form(server, "Sign in to Claude Code")
    assert ask["links"][0]["url"] == "https://claude.com/cai/oauth/authorize?code=true&state=fake"
    reply(server, {"code": "nope"})
    ask = form(server, "That code did not work")
    assert "nope" not in json.dumps(ask)  # a code is never shown back
    reply(server, {"code": " good-code#fake "})
    ask = form(server, "Are these your student accounts?")
    assert "Codex: test.user@mbzuai.ac.ae" in ask["text"] and "Claude Code: test.user@mbzuai.ac.ae" in ask["text"]
    assert not any("not an @mbzuai" in line for line in ask["text"])
    reply(server, {"ok": True})
    return until(server, lambda s: s["finished"] or any(x["status"] == "failed" for x in s["steps"]), timeout=60)


def test_at_the_end_the_page_takes_the_student_to_the_dashboards_welcome(tmp_path, monkeypatch) -> None:
    """The dashboard's server starts in the background (it outlives this helper) and the page goes to its welcome."""
    import subprocess

    monkeypatch.setenv("PATH", f"{FAKES}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_CLUSTER", str(tmp_path / "cluster"))
    monkeypatch.setenv("FAKE_PASSWORD", "right horse battery")
    free = __import__("socket").socket()
    free.bind(("127.0.0.1", 0))
    monkeypatch.setenv("SCHUB_VIEW_PORT", str(free.getsockname()[1]))
    free.close()
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    paths = Paths(home=home)
    opened: list[str] = []
    engine = Engine(build(Setup(paths, open_dashboard=True, open_url=opened.append)), paths.state)
    server = OnboardServer(engine)
    server.opened = opened
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        to_the_agents(server, typo_first=False)
        state = sign_in_both(server, tmp_path / "cluster")
        dashboard = next(x for x in state["steps"] if x["id"] == "dashboard")
        assert state["finished"] and dashboard["status"] == "done", dashboard
        assert state["next"].startswith("http://127.0.0.1:") and state["next"].endswith("/go?to=%2Fwelcome")
        base = state["next"].split("/go?")[0]
        page = urllib.request.urlopen(base + "/welcome", timeout=10).read().decode()
        assert "sc-hub is ready" in page and "test.user@mbzuai.ac.ae" in page and "/l/users/test.user/schub" in page
        assert "sc-hub.localhost" in dashboard["detail"] and str(home / ".sc-hub" / "bin" / "schub-view") in \
            dashboard["detail"]
        config = json.loads((home / ".sc-hub" / "view-config.json").read_text())
        assert config["remote"] == "/l/users/test.user/schub" and config["alias"] == "mbzuai-schub"
    finally:
        server.shutdown()
        from sc_hub_onboard import __main__ as entry

        entry.stop_trial_dashboard(paths)  # what a trial's helper does when it leaves
    assert not (home / ".sc-hub" / "view.json").exists()  # stopped


def test_started_in_the_background_the_helper_leaves_the_callers_process_group(monkeypatch) -> None:
    """An assistant's command that ends may signal its whole process group: the page must not go with it."""
    import io

    from sc_hub_onboard import __main__ as entry

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    calls: list[str] = []
    monkeypatch.setattr(entry.os, "setsid", lambda: calls.append("setsid"), raising=False)
    monkeypatch.setattr(entry.sys, "stdin", io.StringIO())  # `nohup ... > log 2>&1 < /dev/null &`: no terminal
    monkeypatch.setattr(entry.sys, "stdout", io.StringIO())
    entry.detach()
    assert calls == ["setsid"]
    monkeypatch.setattr(entry.sys, "stdin", Terminal())  # `start.sh | tee log` from a terminal: Ctrl-C still reaches it
    entry.detach()
    monkeypatch.setattr(entry.sys, "stdout", Terminal())  # plainly from a terminal
    entry.detach()
    assert calls == ["setsid"]


def test_the_helper_leaves_when_done_or_forgotten_but_never_in_the_middle_of_a_step() -> None:
    from sc_hub_onboard import __main__ as entry

    class Seen:
        def __init__(self, finished: bool, busy: bool) -> None:
            self.finished, self._busy = finished, busy

        def busy(self) -> bool:
            return self._busy

    assert entry.leave(Seen(True, False), entry.IDLE_EXIT_S + 1) and not entry.leave(Seen(True, False), 60)
    assert entry.leave(Seen(False, False), entry.IDLE_UNFINISHED_S + 1)  # a form waits for a student who left
    assert not entry.leave(Seen(False, False), entry.IDLE_EXIT_S + 1)
    assert not entry.leave(Seen(False, True), 10 * entry.IDLE_UNFINISHED_S)  # a step at work (the cluster setup)


def test_a_restarted_helper_forgets_the_old_dashboard_address_and_brings_the_dashboard_back(helper) -> None:
    """After a restart of the computer the old address is gone: the dashboard step runs again at every start."""
    server, paths, _ = helper
    test_the_whole_onboarding_from_the_page(helper)
    saved = json.loads(paths.state.read_text())
    saved["values"]["next"] = "http://127.0.0.1:1/go?to=%2Fwelcome"  # as an older helper would have saved it
    paths.state.write_text(json.dumps(saved))
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert "next" not in engine.values and engine.states["dashboard"].status == "waiting"
    engine.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not engine.finished:
        time.sleep(0.05)
    assert engine.finished and engine.snapshot()["next"] == "" and engine.states["dashboard"].status == "skipped"


def test_the_page_goes_only_to_the_dashboards_welcome_on_this_computer() -> None:
    from sc_hub_onboard.page import PAGE

    assert "location.replace(next)" in PAGE and "NEXT.test(state.next" in PAGE
    assert r"const NEXT = /^http:\/\/127\.0\.0\.1:\d+\/go\?to=%2F\w*$/;" in PAGE


def finish(engine: Engine, step: str, timeout: float = 30) -> str:
    """Run `step` again and wait for its end; a form that asks for something on the way is reported, not answered."""
    wait_for_the_run_to_end(engine, timeout)
    engine.retry(step)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and engine.states[step].status not in ("done", "failed", "skipped", "asking"):
        time.sleep(0.05)
    return engine.states[step].status


def wait_for_the_run_to_end(engine: Engine, timeout: float = 30) -> None:
    """The last run's tail (the dashboard step): a retry meanwhile would not start a run."""
    deadline = time.monotonic() + timeout
    while engine._thread is not None and engine._thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)


def forget_the_own_key(cluster: Path, paths: Paths) -> None:
    """The cluster no longer knows the student's own key (the sc-hub key, limited, is all that is left), and the laptop's
    copy is gone too: an account set up before the own key existed."""
    entries = json.loads((cluster / "authorized.json").read_text())
    entries.pop(paths.login_key.with_suffix(".pub").read_text().split()[1])
    (cluster / "authorized.json").write_text(json.dumps(entries))
    paths.login_key.unlink()
    paths.login_key.with_suffix(".pub").unlink()


def a_finished_setup(helper) -> tuple:
    """The whole onboarding done, and an engine that loads its saved state (a restarted helper)."""
    server, paths, cluster = helper
    test_the_whole_onboarding_from_the_page(helper)
    wait_for_the_run_to_end(server.engine)
    return paths, cluster, Engine(build(Setup(paths, open_dashboard=False)), paths.state)


def test_a_rerun_skips_what_is_done_and_asks_the_password_for_setup(helper) -> None:
    """The key is limited now, and so is the way into the cluster that the page uses: it asks the password once for an update,
    whoever else could log in (the student's own key is for the student, not for the page: a human is in the loop)."""
    paths, cluster, engine = a_finished_setup(helper)
    assert [engine.states[s.id].status for s in engine.steps] == ["done"] * 10 + ["waiting"]
    engine.retry("cluster")  # e.g. an update of the cluster side: the key only opens sc-hub now
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and engine.states["cluster"].status != "asking":
        time.sleep(0.05)
    assert engine.states["cluster"].ask["title"] == "Your cluster password, for this run"
    assert "choices" not in engine.states["cluster"].ask  # (nothing to put off here)
    engine.answer({"password": "right horse battery", "form_id": engine.states["cluster"].ask["id"]})
    while time.monotonic() < deadline and engine.states["cluster"].status not in ("done", "failed"):
        time.sleep(0.05)
    assert engine.states["cluster"].status == "done", engine.states["cluster"].detail
    assert any("bootstrap_cluster.sh" in c["command"] and "PubkeyAuthentication=no" in c["args"] for c in calls_to(cluster))


def test_the_own_key_of_an_account_set_up_before_it_existed_asks_what_it_installs_and_installs_it(helper) -> None:
    """Students who finished the setup when only sc-hub's key existed: `retry login-key` asks the password once (sc-hub's key is
    limited now), says what it installs, and only then makes the key. The terminal and the welcome follow by themselves."""
    paths, cluster, _ = a_finished_setup(helper)
    forget_the_own_key(cluster, paths)
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert finish(engine, "login-key") == "asking"
    ask = engine.states["login-key"].ask
    assert ask["title"] == "Install your own key on the cluster" and "not limited" not in json.dumps(ask) \
        and "normal key" in json.dumps(ask) and "only opens sc-hub" in json.dumps(ask)
    assert [c["name"] for c in ask["choices"]] == ["skip"] and ask["choices"][0]["label"] == "Not now"
    assert not paths.login_key.exists()  # nothing made before the student has agreed
    engine.answer({"password": "right horse battery", "form_id": ask["id"]})
    wait_for_the_run_to_end(engine)
    assert engine.states["login-key"].status == "done" and authorized(cluster, paths.login_key.with_suffix(".pub"))["options"] == ""
    assert "right horse battery" not in paths.state.read_text()  # never saved
    assert engine.states["terminal"].status == "done" and "job 207131 on gpu-03" in engine.states["terminal"].detail
    welcome = json.loads((paths.home / ".sc-hub" / "welcome.json").read_text())
    assert welcome["login_command"].endswith(" login")  # the welcome says it too, without waiting for a restart


def test_not_now_puts_the_own_key_off_and_the_setup_goes_on(helper) -> None:
    paths, cluster, _ = a_finished_setup(helper)
    forget_the_own_key(cluster, paths)
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert finish(engine, "login-key") == "asking"
    engine.answer({"choice": "skip", "form_id": engine.states["login-key"].ask["id"]})
    wait_for_the_run_to_end(engine)
    assert engine.states["login-key"].status == "skipped" and "retry login-key" in engine.states["login-key"].detail
    assert engine.finished and not paths.login_key.exists()  # the dashboard step ran after it
    assert json.loads(paths.state.read_text())["values"]["login_key"] is False


def test_a_refused_password_or_a_stop_skips_the_own_key_instead_of_stopping_the_page(helper) -> None:
    paths, cluster, _ = a_finished_setup(helper)
    forget_the_own_key(cluster, paths)
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert finish(engine, "login-key") == "asking"
    engine.answer({"password": "not it", "form_id": engine.states["login-key"].ask["id"]})
    wait_for_the_run_to_end(engine)
    state = engine.states["login-key"]
    assert state.status == "skipped" and "refused the password" in state.detail and "retry login-key" in state.detail
    assert engine.finished  # the steps after it, the dashboard's, ran
    assert finish(engine, "login-key") == "asking"  # the stop button: the same
    engine.answer({"cancel": True, "form_id": engine.states["login-key"].ask["id"]})
    wait_for_the_run_to_end(engine)
    assert engine.states["login-key"].status == "skipped" and engine.finished


def test_a_failing_own_key_is_reported_but_does_not_stop_the_setup(helper, monkeypatch) -> None:
    """The own key is a convenience: if the cluster does not take it, the page says so and the setup goes on."""
    server, paths, cluster = helper
    monkeypatch.setenv("FAKE_OWN_KEY", "ignored")  # the cluster answers "ok" but the key never works
    to_the_agents(server, typo_first=False)
    form(server, "Sign in to Codex")  # the steps after login-key ran
    state = api(server, "/api/state")
    by_id = {x["id"]: x for x in state["steps"]}
    assert by_id["login-key"]["status"] == "skipped" and "retry login-key" in by_id["login-key"]["detail"]
    assert by_id["cluster"]["status"] == "done"
    assert by_id["terminal"]["status"] == "skipped" and "needs your own key" in by_id["terminal"]["detail"]
    values = json.loads(paths.state.read_text())["values"]
    assert values["login_key"] is False and "terminal_command" not in values  # `schub` and `schub login` are not promised
    assert (paths.home / ".sc-hub" / "bin" / "schub").exists()  # (the command is there for when the key is)


def test_a_cluster_that_cannot_be_reached_stops_the_page_to_retry_not_to_skip(helper, monkeypatch) -> None:
    """A network blip is no refusal: a skipped step would never be tried again by itself."""
    server, paths, cluster = helper
    monkeypatch.setenv("FAKE_UNREACHABLE_OWN", "1")
    to_the_agents(server, typo_first=False)
    state = until(server, lambda s: any(x["status"] == "failed" for x in s["steps"]), timeout=60)
    own = next(x for x in state["steps"] if x["id"] == "login-key")
    assert own["status"] == "failed" and "cannot be reached" in own["detail"] and "VPN" in own["hint"]
    monkeypatch.delenv("FAKE_UNREACHABLE_OWN")
    api(server, "/api/retry", {"step": "login-key"})
    form(server, "Install your own key on the cluster")  # (asked again: it is a new try)
    reply(server, {})
    form(server, "Sign in to Codex")  # the run went on
    assert next(x for x in api(server, "/api/state")["steps"] if x["id"] == "login-key")["status"] == "done"


def test_a_workbench_that_is_not_running_is_not_started_by_the_setup(helper, monkeypatch) -> None:
    server, paths, cluster = helper
    monkeypatch.setenv("FAKE_WORKBENCH", "stopped")
    to_the_agents(server, typo_first=False)
    form(server, "Sign in to Codex")
    terminal = next(x for x in api(server, "/api/state")["steps"] if x["id"] == "terminal")
    assert terminal["status"] == "done" and "it starts when you type schub" in terminal["detail"]
    assert not any("sbatch" in c["command"] for c in calls_to(cluster))  # the setup starts nothing
    probe = next(c for c in calls_to(cluster) if "shell --no-start" in c["command"])
    assert probe["args"][-1] == "mbzuai-login"  # through the student's own key, and it only looks


def test_a_cluster_with_an_older_sc_hub_says_to_update_it_and_does_not_stop_the_key_limit(helper, monkeypatch) -> None:
    server, paths, cluster = helper
    monkeypatch.setenv("FAKE_OLD_SCHUB", "1")  # no `schub shell` there yet
    to_the_agents(server, typo_first=False)
    form(server, "Sign in to Codex")  # the steps after the terminal ran: the key is limited next
    terminal = next(x for x in api(server, "/api/state")["steps"] if x["id"] == "terminal")
    assert terminal["status"] == "skipped" and "retry cluster" in terminal["detail"] and "retry terminal" in terminal["detail"]


def test_with_vs_code_the_editor_gets_its_host_into_the_workbench_job_too(helper, tmp_path) -> None:
    server, paths, cluster = helper
    bin_dir = tmp_path / "vscode-bin"
    bin_dir.mkdir()
    (bin_dir / "code").write_text("#!/bin/sh\n[ \"$1\" = --list-extensions ] && echo ms-vscode-remote.remote-ssh\nexit 0\n")
    (bin_dir / "code").chmod(0o755)
    os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"  # (the fixture's monkeypatch restores PATH)
    to_the_agents(server)
    form(server, "Sign in to Codex")
    state = api(server, "/api/state")
    terminal = next(x for x in state["steps"] if x["id"] == "terminal")
    assert terminal["status"] == "done" and "job 207131 on gpu-03" in terminal["detail"]
    assert "VS Code" in terminal["detail"] and "Remote-SSH" in terminal["detail"]
    config = paths.ssh_config.read_text()  # the editor's way in is sc-hub's key through the gate; the terminal's is the own key
    assert "Host mbzuai-schub-ide\n" in config and "/l/users/test.user/schub/bin/schub ide-proxy" in config
    assert "Host schub\n" in config and "Host schub mbzuai-schub-ide" not in config


def test_a_failing_vs_code_does_not_take_the_terminal_with_it(helper, tmp_path, monkeypatch) -> None:
    server, paths, cluster = helper
    bin_dir = tmp_path / "vscode-bin"
    bin_dir.mkdir()
    (bin_dir / "code").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "code").chmod(0o755)
    os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    (cluster).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FAKE_NO_IDE", "1")  # ide-setup never wrote the job's key: VS Code's connection is refused
    to_the_agents(server, typo_first=False)
    form(server, "Sign in to Codex")  # the key limit etc. were not held back
    terminal = next(x for x in api(server, "/api/state")["steps"] if x["id"] == "terminal")
    assert terminal["status"] == "done" and "VS Code was not set up" in terminal["detail"] and "retry terminal" in terminal["detail"]
    assert "job 207131 on gpu-03" in terminal["detail"]


def test_a_key_that_replaces_the_limited_one_is_limited_again(helper) -> None:
    """The laptop lost sc-hub's key and the student signs in again: the new key comes without the limit, and the step that
    limits it must run once more (it was done), or the assistants' key would stay a full shell."""
    paths, cluster, _ = a_finished_setup(helper)
    entries = json.loads((cluster / "authorized.json").read_text())
    entries.pop(paths.key.with_suffix(".pub").read_text().split()[1])  # gone from the cluster ...
    (cluster / "authorized.json").write_text(json.dumps(entries))
    paths.key.unlink()
    paths.key.with_suffix(".pub").unlink()  # ... and from the laptop
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert finish(engine, "sign-in") == "asking"
    engine.answer({"login": "test.user", "password": "right horse battery", "form_id": engine.states["sign-in"].ask["id"]})
    wait_for_the_run_to_end(engine)
    assert engine.states["sign-in"].status == "done" and engine.states["limit"].status == "done"
    entry = authorized(cluster, paths.key.with_suffix(".pub"))
    assert entry["options"] == 'restrict,port-forwarding,command="/l/users/test.user/schub/bin/schub-gate"'


def test_a_helper_updated_over_a_finished_setup_runs_the_new_steps_and_never_gets_stuck(helper) -> None:
    """The saved state of an older helper has no `login-key` or `terminal` and has `vscode`: the next start runs the new steps.
    The password form can be put off, and nothing after it waits for it."""
    paths, cluster, _ = a_finished_setup(helper)
    forget_the_own_key(cluster, paths)
    saved = json.loads(paths.state.read_text())
    for new in ("login-key", "terminal"):
        saved["steps"].pop(new)
    saved["steps"]["vscode"] = {**saved["steps"]["agents"], "id": "vscode", "status": "skipped"}
    saved["values"].pop("login_key", None)
    saved["values"].pop("terminal_command", None)
    paths.state.write_text(json.dumps(saved))
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert engine.states["login-key"].status == "waiting" and engine.states["terminal"].status == "waiting"
    engine.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and engine.states["login-key"].status != "asking":
        time.sleep(0.05)
    assert engine.states["login-key"].status == "asking"  # nothing else ran: it asks before it does anything
    assert not paths.login_key.exists() and not any(c["command"].startswith("R=") for c in calls_to(cluster)[-1:])
    engine.answer({"choice": "skip", "form_id": engine.states["login-key"].ask["id"]})
    wait_for_the_run_to_end(engine)
    assert engine.finished and engine.states["terminal"].status == "skipped" and engine.states["dashboard"].status == "skipped"


def commands_since(cluster: Path, before: int) -> list[str]:
    return [c["command"] for c in calls_to(cluster)[before:]]


def test_a_replaced_key_on_a_limited_account_is_never_written_plain(helper) -> None:
    """The laptop lost sc-hub's key and the student signs in again: the key goes in already limited, so there is not a moment
    (or a failed check, or a page that stops) in which the assistants' key is a full shell."""
    paths, cluster, _ = a_finished_setup(helper)
    entries = json.loads((cluster / "authorized.json").read_text())
    entries.pop(paths.key.with_suffix(".pub").read_text().split()[1])
    (cluster / "authorized.json").write_text(json.dumps(entries))
    paths.key.unlink()
    paths.key.with_suffix(".pub").unlink()
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    before = len(calls_to(cluster))
    assert finish(engine, "sign-in") == "asking"
    engine.answer({"login": "test.user", "password": "right horse battery", "form_id": engine.states["sign-in"].ask["id"]})
    wait_for_the_run_to_end(engine)
    writes = [c for c in commands_since(cluster, before) if "OPTS=" in c]  # (the first 300 characters of each command are logged)
    assert writes and all("OPTS='restrict,port-forwarding,command=\"/l/users/test.user/schub/bin/schub-gate\"'" in c
                          for c in writes), writes
    entry = authorized(cluster, paths.key.with_suffix(".pub"))
    assert entry["options"].startswith("restrict,") and engine.states["limit"].status == "done"
    assert json.loads(paths.state.read_text())["values"]["limited"] is True


def test_without_the_gate_a_replaced_key_is_not_written_at_all(helper) -> None:
    """The gate script is not there (the cluster's /l is down): a key that must be limited is not installed, and the page
    says why, instead of leaving it plain."""
    paths, cluster, _ = a_finished_setup(helper)
    entries = json.loads((cluster / "authorized.json").read_text())
    entries.pop(paths.key.with_suffix(".pub").read_text().split()[1])
    (cluster / "authorized.json").write_text(json.dumps(entries))
    paths.key.unlink()
    paths.key.with_suffix(".pub").unlink()
    os.environ["FAKE_NO_GATE"] = "1"
    try:
        engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
        assert finish(engine, "sign-in") == "asking"
        engine.answer({"login": "test.user", "password": "right horse battery", "form_id": engine.states["sign-in"].ask["id"]})
        wait_for_the_run_to_end(engine)
        state = engine.states["sign-in"]  # stopped with the reason and what to do: not a password to type again
        assert state.status == "failed" and "gate script is not there" in state.detail and "pilot owner" in state.hint
        assert paths.key.with_suffix(".pub").read_text().split()[1] not in (cluster / "authorized.json").read_text()
    finally:
        del os.environ["FAKE_NO_GATE"]


def test_a_key_that_works_but_is_not_limited_on_a_limited_account_is_limited_again(helper) -> None:
    """Sign-in finds the key working (the line was written plain some other way): the state said `limited`, so the page looks
    at the key itself and runs the limit step again. (That the key is never written plain by the page is the test above's.)"""
    paths, cluster, _ = a_finished_setup(helper)
    entries = json.loads((cluster / "authorized.json").read_text())
    entry = entries[paths.key.with_suffix(".pub").read_text().split()[1]]
    entry["options"] = ""  # a plain line again, whatever the way
    (cluster / "authorized.json").write_text(json.dumps(entries))
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert finish(engine, "sign-in") == "done"  # the key works: no form
    wait_for_the_run_to_end(engine)
    entry = authorized(cluster, paths.key.with_suffix(".pub"))
    assert entry["options"].startswith("restrict,") and engine.states["limit"].status == "done"


def test_an_unreachable_cluster_after_the_limit_is_not_called_a_refused_password(helper, monkeypatch) -> None:
    paths, cluster, _ = a_finished_setup(helper)
    forget_the_own_key(cluster, paths)
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert finish(engine, "login-key") == "asking"
    monkeypatch.setenv("FAKE_UNREACHABLE", "1")  # the VPN went off while the student typed
    engine.answer({"password": "right horse battery", "form_id": engine.states["login-key"].ask["id"]})
    wait_for_the_run_to_end(engine)
    state = engine.states["login-key"]
    assert state.status == "failed" and "cannot be reached" in state.detail and "VPN" in state.hint  # a Retry, not a skip
    assert "refused" not in state.detail


def test_retrying_the_own_key_without_a_network_does_not_offer_to_install_a_key_that_is_there(helper, monkeypatch) -> None:
    paths, cluster, engine = a_finished_setup(helper)
    monkeypatch.setenv("FAKE_UNREACHABLE", "1")
    assert finish(engine, "login-key") == "failed"
    assert "cannot be reached" in engine.states["login-key"].detail and engine.states["login-key"].ask is None


def test_not_now_is_not_overridden_by_a_password_the_page_already_has(helper) -> None:
    """The student put the key off; a later `retry cluster` made the page ask for the password and keep it for the run.
    `retry login-key` must still show what it installs and offer "Not now": the form never depends on that cache."""
    paths, cluster, _ = a_finished_setup(helper)
    forget_the_own_key(cluster, paths)
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert finish(engine, "login-key") == "asking"
    engine.answer({"choice": "skip", "form_id": engine.states["login-key"].ask["id"]})
    wait_for_the_run_to_end(engine)
    assert finish(engine, "cluster") == "asking"  # the password, for an update
    engine.answer({"password": "right horse battery", "form_id": engine.states["cluster"].ask["id"]})
    wait_for_the_run_to_end(engine)
    assert engine.states["cluster"].status == "done" and engine.values.get("password")  # it is kept for this run
    assert finish(engine, "login-key") == "asking"
    ask = engine.states["login-key"].ask
    assert ask["title"] == "Install your own key on the cluster" and "passphrase" in json.dumps(ask)
    assert not paths.login_key.exists()


def test_the_terminal_and_vs_code_do_not_wait_for_each_other(helper, tmp_path, monkeypatch) -> None:
    """VS Code's connection timing out must not fail the step (it once held back the key limit), and VS Code that was set up
    before is not set up again by a rerun (that asked for the password and started the workbench job)."""
    import subprocess

    server, paths, cluster = helper
    bin_dir = tmp_path / "vscode-bin"
    bin_dir.mkdir()
    (bin_dir / "code").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "code").chmod(0o755)
    os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    from sc_hub_onboard import steps

    real_run = steps.subprocess.run

    def hanging(args, *more, **kwargs):
        if "mbzuai-schub-ide" in args:  # VS Code's probe into the job
            raise subprocess.TimeoutExpired(args, 1200)
        return real_run(args, *more, **kwargs)

    monkeypatch.setattr(steps.subprocess, "run", hanging)
    to_the_agents(server, typo_first=False)
    form(server, "Sign in to Codex")  # the steps after the terminal ran: nothing was held back
    terminal = next(x for x in api(server, "/api/state")["steps"] if x["id"] == "terminal")
    assert terminal["status"] == "done" and "VS Code was not set up" in terminal["detail"] and "job 207131" in terminal["detail"]


def test_a_rerun_does_not_set_vs_code_up_again(helper, tmp_path) -> None:
    server, paths, cluster = helper
    bin_dir = tmp_path / "vscode-bin"
    bin_dir.mkdir()
    (bin_dir / "code").write_text("#!/bin/sh\n[ \"$1\" = --list-extensions ] && echo ms-vscode-remote.remote-ssh\nexit 0\n")
    (bin_dir / "code").chmod(0o755)
    os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    to_the_agents(server, typo_first=False)  # a whole setup, VS Code and all
    sign_in_both(server, cluster)
    wait_for_the_run_to_end(server.engine)
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert "vscode_link" in engine.values and engine.states["terminal"].status == "done"
    before = len(calls_to(cluster))
    assert finish(engine, "terminal") == "done"  # no form: nothing here needs the password
    assert not any("ide-setup" in c for c in commands_since(cluster, before)) and engine.states["terminal"].ask is None
    assert "Remote-SSH" in engine.states["terminal"].detail


def test_an_unreachable_cluster_at_the_terminal_look_skips_it_and_holds_nothing_back(helper, monkeypatch) -> None:
    server, paths, cluster = helper
    monkeypatch.setenv("FAKE_SHELL_UNREACHABLE", "1")
    to_the_agents(server, typo_first=False)
    form(server, "Sign in to Codex")  # agents came after it
    terminal = next(x for x in api(server, "/api/state")["steps"] if x["id"] == "terminal")
    assert terminal["status"] == "skipped" and "cannot be reached" in terminal["detail"] and "retry terminal" in terminal["detail"]
    assert "terminal_command" not in json.loads(paths.state.read_text())["values"]
    assert "Host schub" not in paths.ssh_config.read_text()  # no half-working alias


def test_exit_codes_of_the_look_mean_one_thing_each(helper, monkeypatch) -> None:
    """3 is what sc-hub's own bootstrap says when its Python is missing: it must not read as "the workbench is not running"."""
    server, paths, cluster = helper
    monkeypatch.setenv("FAKE_WORKBENCH", "broken")
    to_the_agents(server, typo_first=False)
    form(server, "Sign in to Codex")
    terminal = next(x for x in api(server, "/api/state")["steps"] if x["id"] == "terminal")
    assert terminal["status"] == "skipped" and "unavailable" in terminal["detail"] and "starts when you type" not in terminal["detail"]


def test_a_stopped_bench_is_said_not_promised(helper, monkeypatch) -> None:
    server, paths, cluster = helper
    monkeypatch.setenv("FAKE_WORKBENCH", "bench-stopped")
    to_the_agents(server, typo_first=False)
    form(server, "Sign in to Codex")
    terminal = next(x for x in api(server, "/api/state")["steps"] if x["id"] == "terminal")
    assert terminal["status"] == "skipped" and "bench/STOP" in terminal["detail"]
    assert "retry terminal" in terminal["detail"] and "then `schub` works" not in terminal["detail"]  # (no alias yet)
    assert "terminal_command" not in json.loads(paths.state.read_text())["values"]  # `schub` would refuse: not promised


def test_the_page_does_not_promise_a_terminal_without_the_own_key() -> None:
    from sc_hub_onboard.steps import summary_lines, terminal_commands

    assert terminal_commands({"terminal_command": "schub", "login_key": False}) == ("", "")
    assert terminal_commands({"terminal_command": "schub"}) == ("", "")
    assert terminal_commands({"terminal_command": "schub", "login_key": True}) == ("schub", "schub login")
    assert terminal_commands({"terminal_command": "ssh schub", "login_key": True}) == ("ssh schub", "ssh mbzuai-login")
    assert not any("Your terminal" in line for line in summary_lines({"terminal_command": "schub"}, "host"))


def test_a_folder_name_from_the_saved_state_cannot_run_commands_on_the_cluster(helper) -> None:
    """The state file is the helper's own; a quote in the saved folder name would end the single quotes of every command
    sent to the cluster, a password session's included."""
    paths, cluster, engine = a_finished_setup(helper)
    engine.values["remote_root"] = "/l/users/x'; touch /tmp/pwned; '"
    before = len(calls_to(cluster))
    for step in ("terminal", "limit"):
        assert finish(engine, step) == "failed", step
        assert "plain path" in engine.states[step].detail
        engine.states[step].status = "skipped"  # (a retry runs every failed step first: take this one out of the way)
    assert not any("pwned" in c for c in commands_since(cluster, before))


def test_the_first_run_says_what_the_own_key_is_and_lets_it_be_put_off(helper) -> None:
    server, paths, cluster = helper
    to_the_agents(server, typo_first=False, own_key="leave")
    ask = form(server, "Install your own key on the cluster")
    assert not ask.get("fields") and ask["submit"] == "Install my key"  # nothing to type: the account is not limited yet
    text = json.dumps(ask)
    assert "no passphrase" in text and "normal key" in text and "only opens sc-hub" in text
    assert not paths.login_key.exists()  # nothing is made before the student has agreed
    reply(server, {"choice": "skip"})
    form(server, "Sign in to Codex")  # the setup went on
    by_id = {x["id"]: x for x in api(server, "/api/state")["steps"]}
    assert by_id["login-key"]["status"] == "skipped" and "retry login-key" in by_id["login-key"]["detail"]
    assert by_id["terminal"]["status"] == "skipped" and by_id["cluster"]["status"] == "done"
    assert not paths.login_key.exists() and "Host mbzuai-login" not in paths.ssh_config.read_text()


def test_the_assistants_are_connected_only_once_the_key_is_limited(helper) -> None:
    """Pressing Stop on a sign-in, or any step between, ends the run: an assistant connected before that would hold a key that
    is still a full shell. They come after the limit."""
    from sc_hub_onboard.steps import build as build_steps, Setup as SetupSteps

    ids = [s.id for s in build_steps(SetupSteps(Paths(home=Path("/nonexistent"))))]
    assert ids.index("assistants") > ids.index("limit") and ids.index("agents") < ids.index("limit")
    server, paths, cluster = helper
    to_the_agents(server)
    form(server, "Sign in to Codex")  # the run is at `agents`: the key is not limited, and nothing is connected
    assert not (paths.home / ".codex" / "config.toml").exists() and not paths.workspace.exists()
    reply(server, {"cancel": True})  # Stop here
    until(server, lambda s: any(x["id"] == "agents" and x["status"] == "failed" for x in s["steps"]))
    assert not (paths.home / ".codex" / "config.toml").exists()


def test_a_limit_the_state_forgot_is_taken_up_not_fought(helper) -> None:
    """The line on the cluster is limited but the state does not say so (the helper stopped before it saved, a blip at the
    probe): `retry limit` used to send its rewrite through the limited key, be refused, and say "could not limit" for ever.
    Now the page looks at the key, sees the gate, and asks for the password like any update."""
    paths, cluster, engine = a_finished_setup(helper)
    engine.values.pop("limited", None)  # what a lost or late save leaves
    assert finish(engine, "limit") == "asking" and engine.states["limit"].ask["title"] == "Your cluster password, for this run"
    engine.answer({"password": "right horse battery", "form_id": engine.states["limit"].ask["id"]})
    wait_for_the_run_to_end(engine)
    assert engine.states["limit"].status == "done" and engine.values["limited"] is True


def test_a_blip_at_the_limit_probe_is_unreachable_not_a_key_that_opens_a_shell(helper, monkeypatch) -> None:
    server, paths, cluster = helper
    monkeypatch.setenv("FAKE_UNREACHABLE_PROBE", "1")  # the network goes at the one command that looks at the key
    to_the_agents(server, typo_first=False)
    state = sign_in_both(server, cluster)  # up to the step that ended the run
    limit = next(x for x in state["steps"] if x["id"] == "limit")
    assert limit["status"] == "failed" and "cannot be reached" in limit["detail"] and "VPN" in limit["hint"]
    assert "still opens a shell" not in limit["detail"]


def test_an_own_key_that_is_sc_hubs_key_is_refused_and_the_gate_stays(helper) -> None:
    """A copy of sc-hub's key at the own key's place (a hand copy, a mixed-up .pub) would rewrite sc-hub's line as a plain one."""
    import shutil

    paths, cluster, _ = a_finished_setup(helper)
    forget_the_own_key(cluster, paths)
    shutil.copy(paths.key, paths.login_key)
    shutil.copy(paths.key.with_suffix(".pub"), paths.login_key.with_suffix(".pub"))
    before = authorized(cluster, paths.key.with_suffix(".pub"))
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert finish(engine, "login-key") == "asking"
    engine.answer({"password": "right horse battery", "form_id": engine.states["login-key"].ask["id"]})
    wait_for_the_run_to_end(engine)
    state = engine.states["login-key"]
    assert state.status == "skipped" and "same key as sc-hub's" in state.detail
    assert authorized(cluster, paths.key.with_suffix(".pub")) == before  # still the gate's line


def test_a_lost_state_file_does_not_take_the_gate_off_the_key_the_assistants_use(helper) -> None:
    """The saved values are gone (login, `limited`) but the key is on the cluster: sign-in asks for the login, finds the key
    working and writes nothing, instead of putting a plain line over the limited one."""
    paths, cluster, _ = a_finished_setup(helper)
    saved = json.loads(paths.state.read_text())
    saved["values"] = {}
    paths.state.write_text(json.dumps(saved))
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    before = len(calls_to(cluster))
    assert finish(engine, "sign-in") == "asking"
    engine.answer({"login": "test.user", "password": "right horse battery", "form_id": engine.states["sign-in"].ask["id"]})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and engine.states["sign-in"].status not in ("done", "failed"):
        time.sleep(0.05)
    assert engine.states["sign-in"].status == "done" and engine.values["limited"] is True  # taken up from the key itself
    assert not any("OPTS=" in c for c in commands_since(cluster, before))  # no write to authorized_keys at all
    assert authorized(cluster, paths.key.with_suffix(".pub"))["options"].startswith("restrict,")


@pytest.mark.parametrize("kept", [(), ("login", "remote_root")])
def test_a_gate_that_does_not_run_when_the_state_forgot_the_limit_never_gets_a_plain_line(helper, monkeypatch, kept) -> None:
    """The saved values are gone (all of them, or `limited` alone: the helper stopped before it saved) and right then the gate
    cannot run (the cluster's /l is not mounted): the key's login fails, but in the gate, so its line is the limited one.
    Sign-in wrote a plain line over it, and with the limit step done nothing limited it again: the assistants' key was a
    shell for good (found in review). Now nothing is written plain, and the page says why."""
    paths, cluster, _ = a_finished_setup(helper)
    saved = json.loads(paths.state.read_text())
    saved["values"] = {name: saved["values"][name] for name in kept}
    paths.state.write_text(json.dumps(saved))
    monkeypatch.setenv("FAKE_GATE_GONE", "1")
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    before = len(calls_to(cluster))
    assert finish(engine, "sign-in") == "asking"
    engine.answer({"login": "test.user", "password": "right horse battery", "form_id": engine.states["sign-in"].ask["id"]})
    wait_for_the_run_to_end(engine)
    state = engine.states["sign-in"]  # the folder known: limited, so not without the gate; unknown: no gate to point at
    assert state.status == "failed" and "not written" in state.detail and "/l" in state.hint and "pilot owner" in state.hint
    assert not any("OPTS=''" in c for c in commands_since(cluster, before))  # no plain line, not even for a moment
    assert authorized(cluster, paths.key.with_suffix(".pub"))["options"].startswith("restrict,")


def test_a_plain_line_is_limited_again_even_when_the_state_forgot_it_was_limited(helper) -> None:
    """The key works and opens a shell (its line was written plain some other way), and the saved `limited` is gone too: the
    limit step is done, so the key must be limited again, or the assistants' key stays a shell for good."""
    paths, cluster, _ = a_finished_setup(helper)
    entries = json.loads((cluster / "authorized.json").read_text())
    entries[paths.key.with_suffix(".pub").read_text().split()[1]]["options"] = ""
    (cluster / "authorized.json").write_text(json.dumps(entries))
    saved = json.loads(paths.state.read_text())
    del saved["values"]["limited"]
    paths.state.write_text(json.dumps(saved))
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert finish(engine, "sign-in") == "done"  # the key works: no form
    wait_for_the_run_to_end(engine)
    assert authorized(cluster, paths.key.with_suffix(".pub"))["options"].startswith("restrict,")
    assert engine.states["limit"].status == "done"


def test_a_failing_terminal_leaves_no_alias_behind_and_never_holds_back_the_limit(helper, monkeypatch) -> None:
    from sc_hub_onboard import assistants

    paths, cluster, engine = a_finished_setup(helper)
    assert "Host schub\n" in paths.ssh_config.read_text()
    monkeypatch.setenv("FAKE_WORKBENCH", "broken")  # sc-hub on the cluster cannot open the shell this time
    assert finish(engine, "terminal") == "skipped"
    assert "Host schub\n" not in paths.ssh_config.read_text() and "terminal_command" not in engine.values
    monkeypatch.delenv("FAKE_WORKBENCH")
    monkeypatch.setattr(assistants, "install_tools", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert finish(engine, "terminal") == "skipped" and "disk full" in engine.states["terminal"].detail  # not a failure


def test_a_network_blip_at_a_rerun_of_the_terminal_keeps_the_schub_that_worked(helper, monkeypatch) -> None:
    """The terminal step runs again after `retry login-key` or a replaced key, and looks at the job again: a network that is
    down right then decides nothing, so the `schub` that worked stays (it was taken away until a `retry terminal`: found in
    review)."""
    paths, cluster, engine = a_finished_setup(helper)
    command = engine.values["terminal_command"]
    monkeypatch.setenv("FAKE_SHELL_UNREACHABLE", "1")
    assert finish(engine, "terminal") == "skipped" and "cannot be reached" in engine.states["terminal"].detail
    assert "Host schub\n" in paths.ssh_config.read_text() and engine.values["terminal_command"] == command


def test_a_terminal_that_works_later_reaches_the_welcome_and_a_cluster_update_sets_it_up(helper, monkeypatch) -> None:
    """An account set up before `schub shell` existed: the terminal step is skipped until sc-hub on the cluster is updated.
    `retry cluster` alone then sets the terminal up, and the dashboard's welcome says what to type (it kept saying nothing
    until the page was started again: found in review)."""
    paths, cluster, engine = a_finished_setup(helper)
    welcome = paths.state.parent / "welcome.json"
    monkeypatch.setenv("FAKE_OLD_SCHUB", "1")
    assert finish(engine, "terminal") == "skipped"
    wait_for_the_run_to_end(engine)  # (the dashboard step ran in this run: it is done now)
    assert json.loads(welcome.read_text())["terminal_command"] == ""
    monkeypatch.delenv("FAKE_OLD_SCHUB")
    assert finish(engine, "cluster") == "asking"  # an update of sc-hub on the cluster: the key is limited, the password
    engine.answer({"password": "right horse battery", "form_id": engine.states["cluster"].ask["id"]})
    wait_for_the_run_to_end(engine, 60)
    assert engine.states["terminal"].status == "done" and "job 207131 on gpu-03" in engine.states["terminal"].detail
    assert json.loads(welcome.read_text())["terminal_command"] == engine.values["terminal_command"] != ""


def test_a_cluster_update_sets_up_the_terminal_also_when_vs_code_kept_the_step_done(helper, monkeypatch) -> None:
    """With VS Code set up, the terminal step is done even when its terminal part failed (an older sc-hub on the cluster):
    `retry cluster` must still set the terminal up (found in review)."""
    paths, cluster, engine = a_finished_setup(helper)
    engine.values.update(vscode=True, vscode_link="vscode://vscode-remote/ssh-remote+mbzuai-schub-ide/x",
                         ide_proxy="ssh -T mbzuai-schub /x/bin/schub ide-proxy")  # (set up before: not done again)
    monkeypatch.setenv("FAKE_OLD_SCHUB", "1")
    assert finish(engine, "terminal") == "done" and "terminal_command" not in engine.values
    wait_for_the_run_to_end(engine)
    monkeypatch.delenv("FAKE_OLD_SCHUB")
    assert finish(engine, "cluster") == "asking"
    engine.answer({"password": "right horse battery", "form_id": engine.states["cluster"].ask["id"]})
    wait_for_the_run_to_end(engine, 60)
    assert engine.values.get("terminal_command") and "Host schub\n" in paths.ssh_config.read_text()


def test_without_the_own_key_on_this_computer_the_terminal_is_not_promised(helper) -> None:
    """The own key's file is gone from this computer (the state still says it was installed): the look would fail as a host
    ssh cannot resolve, which reads as a network that is down. The step says what is missing instead."""
    paths, cluster, engine = a_finished_setup(helper)
    paths.login_key.unlink()
    paths.login_key.with_suffix(".pub").unlink()
    assert finish(engine, "terminal") == "skipped" and "retry login-key" in engine.states["terminal"].detail
    assert "cannot be reached" not in engine.states["terminal"].detail
    assert "terminal_command" not in engine.values and "Host schub\n" not in paths.ssh_config.read_text()


def test_the_own_key_step_looks_at_the_limit_itself_before_it_asks(helper) -> None:
    """The limit step's last look met a blip: the line on the cluster is limited, but `limited` was never saved. The own key's
    step must not send its program through a key that refuses it (it offered "Not now" and then stopped on the gate's
    refusal): it looks first, then asks for the password (found in review)."""
    paths, cluster, _ = a_finished_setup(helper)
    forget_the_own_key(cluster, paths)
    saved = json.loads(paths.state.read_text())
    del saved["values"]["limited"]
    paths.state.write_text(json.dumps(saved))
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert finish(engine, "login-key") == "asking"
    assert any(f["name"] == "password" for f in engine.states["login-key"].ask.get("fields", []))
    engine.answer({"password": "right horse battery", "form_id": engine.states["login-key"].ask["id"]})
    wait_for_the_run_to_end(engine)
    assert engine.states["login-key"].status == "done" and authorized(cluster, paths.login_key.with_suffix(".pub"))


def test_windows_students_are_told_the_login_they_have() -> None:
    """There is no `schub` command on Windows (`ssh schub` is the terminal there): the own key's forms must not say `schub
    login` to a Windows student (found in review)."""
    from sc_hub_onboard import steps

    assert steps.own_login(windows=True) == "ssh mbzuai-login" and steps.own_login(windows=False) == "schub login"
    assert steps.own_login() in steps.OWN_KEY_CONSENT["text"][0] and steps.own_login() in steps.OWN_KEY_FORM["text"][0]


def test_a_replaced_key_makes_vs_code_set_up_again_for_the_new_key(helper, tmp_path) -> None:
    """The job's own sshd holds the old public key: after a key replacement the editor's host must be set up again."""
    server, paths, cluster = helper
    bin_dir = tmp_path / "vscode-bin"
    bin_dir.mkdir()
    (bin_dir / "code").write_text("#!/bin/sh\n[ \"$1\" = --list-extensions ] && echo ms-vscode-remote.remote-ssh\nexit 0\n")
    (bin_dir / "code").chmod(0o755)
    os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
    to_the_agents(server, typo_first=False)
    sign_in_both(server, cluster)
    wait_for_the_run_to_end(server.engine)
    entries = json.loads((cluster / "authorized.json").read_text())
    entries.pop(paths.key.with_suffix(".pub").read_text().split()[1])
    (cluster / "authorized.json").write_text(json.dumps(entries))
    paths.key.unlink()
    paths.key.with_suffix(".pub").unlink()
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    before = len(calls_to(cluster))
    assert finish(engine, "sign-in") == "asking"
    engine.answer({"login": "test.user", "password": "right horse battery", "form_id": engine.states["sign-in"].ask["id"]})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and engine.states["terminal"].status != "asking":
        time.sleep(0.05)
    assert engine.states["terminal"].status == "asking"  # the editor's part asks the password again, for ide-setup
    engine.answer({"password": "right horse battery", "form_id": engine.states["terminal"].ask["id"]})
    wait_for_the_run_to_end(engine)
    assert engine.states["terminal"].status == "done" and any("ide-setup" in c for c in commands_since(cluster, before))
    assert "vscode_link" in engine.values


def test_a_new_key_on_a_limited_account_goes_in_limited_through_the_password_window_too(helper, monkeypatch) -> None:
    """Windows 10's ssh takes no password from the page: ssh asks in a window of its own. That path writes the same limited
    line (and nothing without a gate: the next test)."""
    from sc_hub_onboard import steps

    paths, cluster, _ = a_finished_setup(helper)
    entries = json.loads((cluster / "authorized.json").read_text())
    entries.pop(paths.key.with_suffix(".pub").read_text().split()[1])
    (cluster / "authorized.json").write_text(json.dumps(entries))
    paths.key.unlink()
    paths.key.with_suffix(".pub").unlink()
    monkeypatch.setenv("FAKE_NO_ASKPASS", "1")
    monkeypatch.setenv("FAKE_CONSOLE_PASSWORD", "right horse battery")
    monkeypatch.setattr(steps, "console_available", lambda: True)
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    before = len(calls_to(cluster))
    assert finish(engine, "sign-in") == "asking"
    engine.answer({"login": "test.user", "password": "x", "form_id": engine.states["sign-in"].ask["id"]})
    wait_for_the_run_to_end(engine)
    writes = [c for c in commands_since(cluster, before) if "OPTS=" in c]
    assert writes and all("OPTS='restrict,port-forwarding,command=" in c for c in writes), writes
    assert authorized(cluster, paths.key.with_suffix(".pub"))["options"].startswith("restrict,")


def test_the_password_window_writes_nothing_without_a_gate_and_says_so(helper, monkeypatch) -> None:
    """The same path when the gate script is not there (the cluster's /l is down): the program writes nothing (exit 3), and
    the step says so, not as a wrong password."""
    from sc_hub_onboard import steps

    paths, cluster, _ = a_finished_setup(helper)
    entries = json.loads((cluster / "authorized.json").read_text())
    entries.pop(paths.key.with_suffix(".pub").read_text().split()[1])
    (cluster / "authorized.json").write_text(json.dumps(entries))
    paths.key.unlink()
    paths.key.with_suffix(".pub").unlink()
    monkeypatch.setenv("FAKE_NO_ASKPASS", "1")
    monkeypatch.setenv("FAKE_CONSOLE_PASSWORD", "right horse battery")
    monkeypatch.setenv("FAKE_NO_GATE", "1")
    monkeypatch.setattr(steps, "console_available", lambda: True)
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert finish(engine, "sign-in") == "asking"
    engine.answer({"login": "test.user", "password": "x", "form_id": engine.states["sign-in"].ask["id"]})
    wait_for_the_run_to_end(engine)
    state = engine.states["sign-in"]
    assert state.status == "failed" and "gate script is not there" in state.detail and "refused" not in state.detail
    assert "pilot owner" in state.hint
    assert paths.key.with_suffix(".pub").read_text().split()[1] not in (cluster / "authorized.json").read_text()


def test_codex_without_device_codes_and_a_personal_account(helper, monkeypatch) -> None:
    from sc_hub_onboard import cluster_agents

    server, _, cluster = helper
    # the fake ssh opens no tunnel: port 1455 of the computer running the tests may be taken (VS Code's Codex holds it)
    monkeypatch.setattr(cluster_agents, "port_free", lambda port: True)
    to_the_agents(server)
    form(server, "Sign in to Codex")
    reply(server, {"choice": "browser"})  # device codes are off in this workspace
    ask = form(server, "a tunnel carries that to Codex on the cluster")
    assert ask["links"][0]["url"].startswith("https://auth.openai.com/oauth/authorize?") and "code" not in ask
    (cluster / "codex_approved").write_text("me@gmail.com")  # oops: the personal account
    form(server, "Sign in to Claude Code")
    reply(server, {"choice": "skip"})
    ask = form(server, "Are these your student accounts?")
    assert "Codex: me@gmail.com" in ask["text"] and not any("Claude Code" in line for line in ask["text"])
    assert any("not an @mbzuai.ac.ae address" in line for line in ask["text"])
    assert [c["name"] for c in ask["choices"]] == ["codex"]
    reply(server, {"choice": "codex"})  # sign out and in again, with the student account
    form(server, "Sign in to Codex")
    (cluster / "codex_approved").write_text("test.user@mbzuai.ac.ae")
    ask = form(server, "Are these your student accounts?")
    assert "Codex: test.user@mbzuai.ac.ae" in ask["text"]
    reply(server, {"ok": True})
    state = until(server, lambda s: s["finished"] or any(x["status"] == "failed" for x in s["steps"]), timeout=60)
    agents = next(x for x in state["steps"] if x["id"] == "agents")
    assert agents["status"] == "done" and agents["detail"] == "Codex: test.user@mbzuai.ac.ae", agents
    assert "The lab agent on the cluster uses Codex (test.user@mbzuai.ac.ae)." in state["summary"]["lines"]


def test_an_ssh_without_askpass_gets_a_password_window(helper, monkeypatch) -> None:
    """Windows 10's OpenSSH 8.1: the page's password does not reach ssh, so ssh asks in a console window."""
    from sc_hub_onboard import steps

    server, paths, cluster = helper
    monkeypatch.setenv("FAKE_NO_ASKPASS", "1")
    monkeypatch.setenv("FAKE_CONSOLE_PASSWORD", "right horse battery")  # what the student types in that window
    monkeypatch.setattr(steps, "console_available", lambda: True)
    to_the_agents(server, typo_first=False)
    form(server, "Sign in to Codex")  # the key works: the run went on through the cluster steps
    state = api(server, "/api/state")
    sign_in = next(x for x in state["steps"] if x["id"] == "sign-in")
    assert sign_in["status"] == "done" and "key installed" in sign_in["detail"]
    calls = [json.loads(line) for line in (cluster / "calls.jsonl").read_text().splitlines()]
    assert any("printf '%s\\n' 'ssh-ed25519 " in c["command"] for c in calls)
    assert authorized(cluster, paths.key.with_suffix(".pub"))["key"] == paths.key.with_suffix(".pub").read_text().strip()


def test_a_rerun_needing_the_password_says_to_update_an_old_ssh(helper, monkeypatch) -> None:
    server, paths, _ = helper
    test_the_whole_onboarding_from_the_page(helper)
    monkeypatch.setenv("FAKE_NO_ASKPASS", "1")
    engine = Engine(build(Setup(paths, open_dashboard=False, open_url=lambda url: None)), paths.state)
    engine.retry("cluster")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and engine.states["cluster"].status != "asking":
        time.sleep(0.05)
    engine.answer({"password": "right horse battery", "form_id": engine.states["cluster"].ask["id"]})
    while time.monotonic() < deadline and engine.states["cluster"].status not in ("done", "failed"):
        time.sleep(0.05)
    assert engine.states["cluster"].status == "failed" and "Update OpenSSH" in engine.states["cluster"].hint


def test_the_first_job_passes_only_when_its_check_passes(helper, monkeypatch) -> None:
    """The cluster reports a check as "pass" (CheckResult.status); anything else stops the setup at `hello`."""
    server, _, _ = helper
    monkeypatch.setenv("FAKE_CHECK_STATUS", "fail")
    to_the_agents(server, typo_first=False)
    state = until(server, lambda s: any(x["status"] == "failed" for x in s["steps"]), timeout=60)
    hello = next(x for x in state["steps"] if x["id"] == "hello")
    assert hello["status"] == "failed" and "checks ['fail']" in hello["detail"], hello


def test_the_assistant_commands_take_their_arguments_in_any_order(tmp_path, monkeypatch, capsys) -> None:
    from sc_hub_onboard.__main__ import main

    monkeypatch.chdir(tmp_path)  # a relative --home is the caller's, not the onboard/ folder's
    (tmp_path / "home" / ".sc-hub").mkdir(parents=True)
    (tmp_path / "home" / ".sc-hub" / "onboard.json").write_text(json.dumps(
        {"values": {}, "steps": {"computer": {"id": "computer", "title": "Check this computer", "status": "done"}}}))
    assert main(["--home", "home", "status"]) == 0
    assert "[done] computer" in capsys.readouterr().out
    assert main(["review-prompt", "hello: the job ended as ['COMPLETED'] with checks ['pass']"]) == 0
    prompt = capsys.readouterr().out
    assert prompt.startswith("# Review of a fix to sc-hub") and "checks ['pass']" in prompt
    assert str(ONBOARD.parent) in prompt  # the reviewer is told where the checkout is


def test_only_an_answer_to_the_form_on_the_page_counts(helper) -> None:
    server, _, cluster = helper
    to_the_agents(server)
    codex = form(server, "Sign in to Codex")
    for wrong in ({}, {"choice": "delete-everything"}, {"ok": True}, {"choice": "skip", "form_id": "0"}):
        with pytest.raises(urllib.error.HTTPError) as caught:  # a waiting form takes only its own buttons
            api(server, "/api/answer", {"form_id": codex["id"], **wrong})
        assert caught.value.code == 409
    (cluster / "codex_approved").write_text("test.user@mbzuai.ac.ae")
    claude = form(server, "Sign in to Claude Code")
    with pytest.raises(urllib.error.HTTPError):  # the old form's button, clicked late
        api(server, "/api/answer", {"choice": "skip", "form_id": codex["id"]})
    with pytest.raises(urllib.error.HTTPError):  # a required field left empty
        api(server, "/api/answer", {"code": "", "form_id": claude["id"]})
    reply(server, {"code": "good-code#fake"})
    confirm = form(server, "Are these your student accounts?")
    with pytest.raises(urllib.error.HTTPError):  # the box is not ticked: no confirmation
        api(server, "/api/answer", {"ok": False, "form_id": confirm["id"]})
    reply(server, {"ok": True})
    state = until(server, lambda s: s["finished"] or any(x["status"] == "failed" for x in s["steps"]), timeout=60)
    assert state["finished"]


def test_the_assistant_follows_and_retries_without_seeing_secrets(helper, capsys) -> None:
    from sc_hub_onboard import agent_cli
    from sc_hub_onboard.__main__ import main

    server, paths, _ = helper
    page = agent_cli.write_page_file(paths, server.port, server.token)
    assert page.stat().st_mode & 0o077 == 0  # the page's token: this account only
    to_the_agents(server)
    form(server, "Sign in to Codex")
    assert main(["status", "--home", str(paths.home)]) == 0
    shown = capsys.readouterr().out
    assert "page: running" in shown and "waiting for the student on the page: Sign in to Codex" in shown
    assert "FAKE-C0DE1" not in shown and server.token not in shown and "right horse battery" not in shown
    reply(server, {"cancel": True})  # the student pressed Stop: the step fails and waits for a retry
    until(server, lambda s: any(x["id"] == "agents" and x["status"] == "failed" for x in s["steps"]))
    agent_cli.status(paths, as_json=True)
    report = json.loads(capsys.readouterr().out)
    agents = next(x for x in report["steps"] if x["id"] == "agents")
    assert agents["detail"] == "stopped on the page" and "Retry" in agents["hint"]
    assert main(["retry", "agents", "--home", str(paths.home)]) == 0
    form(server, "Sign in to Codex")  # asked again
    page.unlink()  # the page stopped: the saved state is still there
    assert agent_cli.status(paths) == 0 and "not running (saved state)" in capsys.readouterr().out
    assert agent_cli.retry(paths, "") == 1 and "not running" in capsys.readouterr().out


def test_stop_ends_the_page_and_the_saved_state_says_finished(helper, capsys) -> None:
    from sc_hub_onboard import agent_cli

    server, paths, cluster = helper
    agent_cli.write_page_file(paths, server.port, server.token)
    test_the_whole_onboarding_from_the_page(helper)
    assert agent_cli.stop(paths) == 0
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            api(server, "/api/state")
        except (urllib.error.URLError, ConnectionError, OSError):
            break
        time.sleep(0.1)
    else:
        raise AssertionError("the page is still answering after stop")
    capsys.readouterr()
    agent_cli.status(paths, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert report["page"] == "not running (saved state)" and report["finished"] is True


def test_open_shows_the_page_to_the_student_without_printing_its_link(helper, monkeypatch, capsys) -> None:
    import webbrowser

    from sc_hub_onboard import agent_cli

    server, paths, _ = helper
    agent_cli.write_page_file(paths, server.port, server.token)
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    assert agent_cli.open_page(paths) == 0
    assert opened == [server.url] and server.token not in capsys.readouterr().out


def test_the_sign_in_output_is_read_through_colours_and_links() -> None:
    codex = ("Welcome to Codex [v\x1b[90m0.155.1\x1b[0m]\n1. Open this link in your browser\n   "
             "\x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n\n2. Enter this one-time code \x1b[90m(expires in "
             "15 minutes)\x1b[0m\n   \x1b[94mAB12-CD34E\x1b[0m\n")
    match = DEVICE.search(clean(codex))
    assert match and match["url"] == "https://auth.openai.com/codex/device" and match["code"] == "AB12-CD34E"
    url = "https://claude.com/cai/oauth/authorize?code=true&client_id=x&redirect_uri=https%3A%2F%2Fplatform.claude.com"
    osc8 = f"If the browser didn't open, visit: \x1b]8;;{url}\x07{url}\x1b]8;;\x07\r\nPaste code here if prompted > "
    assert CLAUDE_URL.search(clean(osc8))[0] == url


def test_error_messages_carry_no_sign_in_links_or_codes() -> None:
    from sc_hub_onboard.cluster_agents import Remote

    job = Remote.__new__(Remote)
    job._chunks = ["open https://auth.openai.com/codex/device?x=1 code AB12-CD34E\n",
                   "visit: https://claude.com/cai/oauth/authorize?state=secret\nLogin failed: 400 for abc#def\n"]
    tail = job.tail(hide=["abc#def", "abc", "def"])
    assert "https://auth.openai.com…" in tail and "state=secret" not in tail and "AB12-CD34E" not in tail
    assert "abc" not in tail and "Login failed: 400" in tail


def test_the_page_is_only_for_this_computer_and_this_link(helper) -> None:
    server, _, _ = helper
    with pytest.raises(urllib.error.HTTPError) as caught:
        api(server, "/api/state", token="guess")
    assert caught.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as caught:
        api(server, "/api/state", host="evil.example:80")  # DNS rebinding
    assert caught.value.code == 403
    with urllib.request.urlopen(server.url, timeout=10) as response:
        page = response.read().decode()
        assert response.headers["Referrer-Policy"] == "no-referrer"  # its address carries the token
    assert "Setting up sc-hub" in page and server.token in page
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{server.port}/?t=nope", timeout=10)


def test_older_dashboard_files_leave_the_workspace_but_a_students_own_stay(tmp_path) -> None:
    from sc_hub_onboard import assistants

    paths = Paths(home=tmp_path / "home")
    paths.workspace.mkdir(parents=True)
    (paths.workspace / "schub-view").write_text("#!/usr/bin/env bash\n# Mirror your sc-hub dashboard to this laptop\n")
    (paths.workspace / "schub-view.cmd").write_text("@echo off\r\nrem Runs schub-view.ps1 even where ...\r\n")
    (paths.workspace / "schub-lab").write_text("#!/usr/bin/env bash\n# Open your running sc-hub session\n")
    (paths.workspace / "schub-lab.ps1").write_text("# Open your running sc-hub session (JupyterLab)\r\n")
    (paths.workspace / "schub_view_pages.py").write_text("my own notes, not a program\n")
    assistants.workspace(paths, ONBOARD.parent)
    gone = ("schub-view", "schub-view.cmd", "schub-lab", "schub-lab.ps1")
    assert not any((paths.workspace / name).exists() for name in gone)
    assert (paths.workspace / "schub_view_pages.py").read_text() == "my own notes, not a program\n"
    (paths.workspace / "schub-lab").write_text("#!/bin/sh\necho my own script\n")  # a student's own file by that name
    assistants.workspace(paths, ONBOARD.parent)
    assert (paths.workspace / "schub-lab").read_text() == "#!/bin/sh\necho my own script\n"


def test_windows_launchers_are_written_the_way_windows_reads_them(tmp_path) -> None:
    """Windows PowerShell 5.1 reads a script without a BOM in the ANSI code page: a pinned Python under a user name
    like Дмитрий would break it. .cmd and .ps1 get Windows line endings."""
    from sc_hub_onboard import assistants

    launcher = tmp_path / "schub-view.ps1"
    assistants._write(launcher, '$pinned = "C:\\Users\\Дмитрий\\python.exe"\nexit 0\n')
    raw = launcher.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw and raw.count(b"\n") == raw.count(b"\r\n")
    assert "Дмитрий" in raw.decode("utf-8-sig")
    script = tmp_path / "schub-view"
    assistants._write(script, "#!/bin/sh\necho ok\n", executable=True)
    assert script.read_bytes() == b"#!/bin/sh\necho ok\n" and (os.name == "nt" or os.access(script, os.X_OK))


def test_logins_and_the_config_block() -> None:
    assert check_login(" Test.User ") == "test.user"
    for bad in ("", "a", "root; rm -rf", "leo@x", "../x"):
        with pytest.raises(SshError):
            check_login(bad)
    assert strip_block("a\n# >>> sc-hub >>>\nHost x\n# <<< sc-hub <<<\nb\n") == "a\nb\n"
    # sc-hub's folder on the cluster: the default one of a firstname.lastname login, nothing that breaks quoting
    assert REMOTE_ROOT.fullmatch("/l/users/test.user/schub") and REMOTE_ROOT.fullmatch("/l/users/a-b_c/schub-2")
    for bad in ("", "l/users/x/schub", "/l/users/x/it's", "/l/users/$USER/schub", "/l/a b", '/l/"x"', "/l/x\n"):
        assert not REMOTE_ROOT.fullmatch(bad), bad


def test_writing_the_block_twice_keeps_one(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.write_text("Host other\n    User me\n")
    write_block(config, "# >>> sc-hub >>>\nHost a\n# <<< sc-hub <<<\n")
    write_block(config, "# >>> sc-hub >>>\nHost b\n# <<< sc-hub <<<\n")
    assert config.read_text() == "# >>> sc-hub >>>\nHost b\n# <<< sc-hub <<<\nHost other\n    User me\n"



def test_codex_keeps_what_the_student_wrote(tmp_path) -> None:
    """A trust table the student already has is not defined twice (Codex would not start); a server they wrote
    by hand is left alone."""
    from sc_hub_onboard import assistants
    from sc_hub_onboard.sshkit import Paths

    paths = Paths(home=tmp_path / "home")
    workspace = assistants.workspace(paths, ONBOARD.parent)
    config = paths.home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    table = f'[projects.{json.dumps(str(workspace))}]'
    config.write_text(f'model = "x"\n\n{table}\ntrust_level = "trusted"\n')
    assistants.codex(paths, "/l/users/u/schub", workspace, ONBOARD.parent)
    assert config.read_text().count(table) == 1 and "enabled = false" in config.read_text()
    assistants.codex(paths, "/l/users/u/schub", workspace, ONBOARD.parent)  # a re-run replaces its own block
    assert config.read_text().count("[mcp_servers.schub]") == 1
    config.write_text('[mcp_servers.schub]\ncommand = "mine"\n')
    assert "by hand" in assistants.codex(paths, "/l/users/u/schub", workspace, ONBOARD.parent)
    assert config.read_text() == '[mcp_servers.schub]\ncommand = "mine"\n'


def test_codex_trust_written_another_way_and_tables_codex_added_survive(tmp_path) -> None:
    import sys

    import pytest

    from sc_hub_onboard import assistants
    from sc_hub_onboard.sshkit import BEGIN, END, Paths

    if sys.version_info < (3, 11):
        pytest.skip("tomllib (Python 3.11+) tells the ways a table can be written apart")
    import tomllib

    paths = Paths(home=tmp_path / "home")
    workspace = assistants.workspace(paths, ONBOARD.parent)
    config = paths.home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f"[projects.'{workspace}']\ntrust_level = \"trusted\"\n")  # single quotes: another spelling
    assistants.codex(paths, "/l/users/u/schub", workspace, ONBOARD.parent)
    tomllib.loads(config.read_text())  # still valid: the table was not defined twice
    # Codex itself appended a table after sc-hub's last one, inside the block
    text = config.read_text().replace(END, "[features]\nmulti_agent = true\n" + END)
    config.write_text(text)
    assistants.codex(paths, "/l/users/u/schub", workspace, ONBOARD.parent)
    data = tomllib.loads(config.read_text())
    assert data["features"]["multi_agent"] is True and config.read_text().count(BEGIN) == 1
    config.write_text("model = \n")  # broken already: sc-hub does not touch it
    assert "not valid TOML" in assistants.codex(paths, "/l/users/u/schub", workspace, ONBOARD.parent)
    assert config.read_text() == "model = \n"



def test_without_a_toml_parser_only_the_quoted_folder_counts_as_trusted(tmp_path, monkeypatch) -> None:
    import builtins

    from sc_hub_onboard import assistants

    real_import = builtins.__import__

    def no_tomllib(name, *args, **kwargs):  # Python 3.9/3.10: macOS's own python3 has none
        if name == "tomllib":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_tomllib)
    workspace = tmp_path / "sc-hub-workspace"
    assert assistants._trusted(f'[projects."{workspace}"]\n', workspace)
    assert assistants._trusted(f"[projects.'{workspace}']\n", workspace)
    assert not assistants._trusted(f'[projects."{workspace}-old"]\n', workspace)  # only a name that starts the same


@pytest.mark.skipif(sys.platform == "win32", reason="the cluster's shell")
def test_the_key_line_is_written_under_a_login_shell_with_noclobber(tmp_path) -> None:
    import subprocess
    from sc_hub_onboard.sshkit import KEY_LINE_REMOTE
    (tmp_path / ".ssh").mkdir()
    keys = tmp_path / ".ssh" / "authorized_keys"
    keys.write_text("ssh-rsa AAAAother other@laptop\n")
    key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA schub@laptop"
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "schub-gate").write_text("")
    (tmp_path / "bin" / "schub-gate").chmod(0o755)
    for options in ("", "restrict"):  # sign-in, then a rewrite of the same line (as `limit` does)
        done = subprocess.run(["bash", "-C", "-c", f"R='{tmp_path}'; OPTS='{options}'; {KEY_LINE_REMOTE}"], input=key + "\n",
                              capture_output=True, text=True, env={**os.environ, "HOME": str(tmp_path)})
        assert done.returncode == 0, done.stderr
    assert keys.read_text().splitlines() == ["ssh-rsa AAAAother other@laptop", f"restrict {key}"]
