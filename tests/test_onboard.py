"""The onboarding helper end to end against a fake cluster (tests/onboard_fakes/ssh), through its page's API."""

from __future__ import annotations

import json
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
from sc_hub_onboard.sshkit import Paths, check_login, strip_block, write_block  # noqa: E402
from sc_hub_onboard.steps import REMOTE_ROOT, Setup, build  # noqa: E402


@pytest.fixture
def helper(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PATH", f"{FAKES}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_CLUSTER", str(tmp_path / "cluster"))
    monkeypatch.setenv("FAKE_PASSWORD", "right horse battery")
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


def to_the_agents(server: OnboardServer, typo_first: bool = True) -> None:
    api(server, "/api/start", {})
    assert "student ChatGPT and Claude accounts" in json.dumps(form(server, "browser for the sign-ins"))
    reply(server, {"ok": True})
    form(server, "Sign in to the MBZUAI cluster")
    if typo_first:
        reply(server, {"login": "Test.User", "password": "wrong"})
        ask = form(server, "refused the login")
        assert ask["fields"][0]["value"] == "test.user"  # the login is kept, the password is not
    reply(server, {"login": "test.user", "password": "right horse battery"})


def test_the_whole_onboarding_from_the_page(helper) -> None:
    server, paths, cluster = helper
    to_the_agents(server)
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
    state = until(server, lambda s: s["finished"] or any(x["status"] == "failed" for x in s["steps"]), timeout=60)
    failed = [x for x in state["steps"] if x["status"] == "failed"]
    assert not failed, failed
    assert state["progress"] == 100 and [x["status"] for x in state["steps"]][-1] == "skipped"
    vscode = next(x for x in state["steps"] if x["id"] == "vscode")  # no VS Code here: ready, then skipped
    assert vscode["status"] == "skipped" and "job 207131 on gpu-03" in vscode["detail"]
    # this computer: the alias first in ~/.ssh/config, the old settings kept, a key, the assistants' configs
    config = (paths.ssh_config).read_text()
    assert config.startswith("# >>> sc-hub >>>\nHost mbzuai-schub\n") and "ServerAliveInterval 30" in config
    assert "User test.user" in config and paths.key.exists()
    assert "Host mbzuai-schub-ide" in config and "/l/users/test.user/schub/bin/schub ide-proxy" in config
    codex = (paths.home / ".codex" / "config.toml").read_text()
    assert "[mcp_servers.schub]" in codex and "/l/users/test.user/schub/bin/schub-mcp" in codex and str(paths.ssh_config) in codex
    assert (paths.workspace / "AGENTS.md").exists() and (paths.workspace / "schub-view").exists()
    # research there; the way back to fixing sc-hub names the folder the setup ran from
    assert f"(sc-hub setup folder: {ONBOARD.parent})" in (paths.workspace / "AGENTS.md").read_text()
    # the cluster: the key limited to the gate at the end, sc-hub uploaded, bootstrap and a first run
    authorized = json.loads((cluster / "authorized.json").read_text())
    assert authorized["options"] == 'restrict,port-forwarding,command="/l/users/test.user/schub/bin/schub-gate"'
    calls = [json.loads(line)["command"] for line in (cluster / "calls.jsonl").read_text().splitlines()]
    assert any("bootstrap_cluster.sh" in c for c in calls) and sum("bench-run" in c for c in calls) == 2
    assert (cluster / "bundle.b64").stat().st_size > 10_000
    saved = paths.state.read_text()
    assert "right horse battery" not in saved and "wrong" not in saved  # no password on disk, ever


def test_a_rerun_skips_what_is_done_and_asks_the_password_for_setup(helper) -> None:
    server, paths, cluster = helper
    test_the_whole_onboarding_from_the_page(helper)
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    assert [engine.states[s.id].status for s in engine.steps] == ["done"] * 6 + ["skipped", "done", "done", "skipped"]
    engine.retry("cluster")  # e.g. an update of the cluster side: the key only opens sc-hub now
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and engine.states["cluster"].status != "asking":
        time.sleep(0.05)
    assert "password" in json.dumps(engine.states["cluster"].ask)
    engine.answer({"password": "right horse battery", "form_id": engine.states["cluster"].ask["id"]})
    while time.monotonic() < deadline and engine.states["cluster"].status not in ("done", "failed"):
        time.sleep(0.05)
    assert engine.states["cluster"].status == "done", engine.states["cluster"].detail


def test_codex_without_device_codes_and_a_personal_account(helper) -> None:
    server, _, cluster = helper
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
    assert json.loads((cluster / "authorized.json").read_text())["key"] == paths.key.with_suffix(".pub").read_text().strip()


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
    page = urllib.request.urlopen(server.url, timeout=10).read().decode()
    assert "Setting up sc-hub" in page and server.token in page
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"http://127.0.0.1:{server.port}/?t=nope", timeout=10)


def test_logins_and_the_config_block() -> None:
    assert check_login(" Test.User ") == "test.user"
    for bad in ("", "a", "root; rm -rf", "leo@x", "../x"):
        with pytest.raises(Exception):
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
