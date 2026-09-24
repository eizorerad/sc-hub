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

from sc_hub_onboard.engine import Engine  # noqa: E402
from sc_hub_onboard.server import OnboardServer  # noqa: E402
from sc_hub_onboard.sshkit import Paths, check_login, strip_block, write_block  # noqa: E402
from sc_hub_onboard.steps import Setup, build  # noqa: E402


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
    engine = Engine(build(Setup(paths, open_dashboard=False)), paths.state)
    server = OnboardServer(engine)
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


def asking(state: dict) -> dict | None:
    return next((s for s in state["steps"] if s["status"] == "asking"), None)


def test_the_whole_onboarding_from_the_page(helper) -> None:
    server, paths, cluster = helper
    api(server, "/api/start", {})
    state = until(server, lambda s: asking(s) and asking(s)["id"] == "browser")
    assert "student ChatGPT and Claude accounts" in json.dumps(asking(state)["ask"])
    api(server, "/api/answer", {"ok": True})
    until(server, lambda s: asking(s) and asking(s)["id"] == "sign-in")
    api(server, "/api/answer", {"login": "Test.User", "password": "wrong"})
    state = until(server, lambda s: asking(s) and "refused the login" in json.dumps(asking(s)["ask"]))
    assert asking(state)["ask"]["fields"][0]["value"] == "test.user"  # the login is kept, the password is not
    api(server, "/api/answer", {"login": "test.user", "password": "right horse battery"})
    state = until(server, lambda s: s["finished"] or any(x["status"] == "failed" for x in s["steps"]), timeout=60)
    failed = [x for x in state["steps"] if x["status"] == "failed"]
    assert not failed, failed
    assert state["progress"] == 100 and [x["status"] for x in state["steps"]][-1] == "skipped"
    # this computer: the alias first in ~/.ssh/config, the old settings kept, a key, the assistants' configs
    config = (paths.ssh_config).read_text()
    assert config.startswith("# >>> sc-hub >>>\nHost mbzuai-schub\n") and "ServerAliveInterval 30" in config
    assert "User test.user" in config and paths.key.exists()
    codex = (paths.home / ".codex" / "config.toml").read_text()
    assert "[mcp_servers.schub]" in codex and "/l/users/test.user/schub/bin/schub-mcp" in codex and str(paths.ssh_config) in codex
    assert (paths.workspace / "AGENTS.md").exists() and (paths.workspace / "schub-view").exists()
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
    assert [engine.states[s.id].status for s in engine.steps] == ["done"] * 7 + ["skipped"]
    engine.retry("cluster")  # e.g. an update of the cluster side: the key only opens sc-hub now
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and engine.states["cluster"].status != "asking":
        time.sleep(0.05)
    assert "password" in json.dumps(engine.states["cluster"].ask)
    engine.answer({"password": "right horse battery"})
    while time.monotonic() < deadline and engine.states["cluster"].status not in ("done", "failed"):
        time.sleep(0.05)
    assert engine.states["cluster"].status == "done", engine.states["cluster"].detail


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
    assert check_login(" Leonid.Klarov ") == "leonid.klarov"
    for bad in ("", "a", "root; rm -rf", "leo@x", "../x"):
        with pytest.raises(Exception):
            check_login(bad)
    assert strip_block("a\n# >>> sc-hub >>>\nHost x\n# <<< sc-hub <<<\nb\n") == "a\nb\n"


def test_writing_the_block_twice_keeps_one(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.write_text("Host other\n    User me\n")
    write_block(config, "# >>> sc-hub >>>\nHost a\n# <<< sc-hub <<<\n")
    write_block(config, "# >>> sc-hub >>>\nHost b\n# <<< sc-hub <<<\n")
    assert config.read_text() == "# >>> sc-hub >>>\nHost b\n# <<< sc-hub <<<\nHost other\n    User me\n"
