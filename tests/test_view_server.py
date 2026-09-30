"""The dashboard's local server (scripts/schub_view*.py): the copy from the cluster, what it serves and to whom,
how it keeps going when the cluster is out of reach, and one server per home that outlives whoever started it."""

from __future__ import annotations

import http.client
import io
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FAKES = Path(__file__).resolve().parent / "view_fakes"
sys.path.insert(0, str(SCRIPTS))

import schub_view as view  # noqa: E402
import schub_view_copy as copying  # noqa: E402
import schub_view_pages as pages  # noqa: E402


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def cluster_view(folder: Path) -> Path:
    """What `schub dashboard` leaves in <root>/view on the cluster."""
    (folder / "jproj").mkdir(parents=True)
    (folder / "jfig" / "a").mkdir(parents=True)
    (folder / "index.html").write_text("<!doctype html><html><body><h1>dashboard</h1></body></html>")
    (folder / "guide.html").write_text("<!doctype html><html><body><h1>guide</h1></body></html>")
    (folder / "jproj" / "versions.json").write_text("{}")
    (folder / "jproj" / "a.js").write_text("window.x=1;")
    (folder / "jfig" / "a" / "c1.png").write_bytes(bytes(range(256)) + b"\r\n\x00\x1a")
    return folder


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """A home, a cluster view folder and the fake ssh first on PATH."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "config").write_text("Host mbzuai-schub\n  HostName example.invalid\n")
    source = cluster_view(tmp_path / "cluster-view")
    monkeypatch.setenv("PATH", f"{FAKES}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_VIEW_SRC", str(source))
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.jsonl"))
    for name in ("SCHUB_ALIAS", "SCHUB_VIEW_DIR", "SCHUB_VIEW_EVERY", "SCHUB_REMOTE_ROOT", "SCHUB_VIEW_TRANSFER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SCHUB_VIEW_PORT", str(free_port()))
    return view.Place(home), source, tmp_path


@pytest.fixture
def app(env):
    """A running server over a copy that is already there (no refresh thread)."""
    place, source, _ = env
    settings = view.Settings(place)
    shutil.copytree(source, settings.dir)
    (settings.dir / copying.MARKER).touch()
    server = view.App(place, settings, log=lambda line: None)
    server.start(refresh=False)
    yield server
    server.stop()


def get(port: int, path: str, host: str | None = None, method: str = "GET", headers: dict | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(method, path, body=b"" if method == "POST" else None,
                       headers={"Host": host or f"127.0.0.1:{port}", **(headers or {})})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, dict(response.getheaders()), body


def nonce_of(headers: dict) -> str:
    return re.search(r"'nonce-([^']+)'", headers["Content-Security-Policy"])[1]


def assert_no_script(headers: dict) -> None:
    policy = headers["Content-Security-Policy"]
    assert re.search(r"script-src ([^;]+)", policy)[1] == "'none'" and "frame-ancestors 'none'" in policy


# ---- what it serves, and to whom ----------------------------------------------------------------------------------


def test_the_copy_is_served_under_this_computers_own_names_only(app) -> None:
    port = app.port
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"sc-hub.localhost:{port}"):
        status, headers, body = get(port, "/", host)
        assert status == 200 and b"<h1>dashboard</h1>" in body, host
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"] and headers["Cache-Control"] == "no-store"
        assert headers["Cross-Origin-Resource-Policy"] == "same-origin" and headers["Referrer-Policy"] == "no-referrer"
    status, _, _ = get(port, "/", "evil.example:80")  # DNS rebinding
    assert status == 403
    status, headers, body = get(port, "/jproj/a.js")
    assert status == 200 and headers["Content-Type"].startswith("text/javascript") and body == b"window.x=1;"
    status, headers, body = get(port, "/jfig/a/c1.png")
    assert headers["Content-Type"] == "image/png" and body == bytes(range(256)) + b"\r\n\x00\x1a"


def test_what_other_sites_pages_ask_for_is_refused(app) -> None:
    """A page elsewhere may include a script from here (no CORS needed) or post to it: refused; following a link to
    the dashboard is fine."""
    other = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "script"}
    assert get(app.port, "/jproj/a.js", headers=other)[0] == 403
    assert get(app.port, "/_schub/status", headers={**other, "Sec-Fetch-Mode": "cors"})[0] == 403
    assert get(app.port, "/_schub/refresh", method="POST", headers={**other, "X-Schub-View": "1"})[0] == 403
    assert get(app.port, "/", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})[0] == 200
    # the /go probe loads from the other address, with the key only /go shows (see the probe test below)
    status, headers, _ = get(app.port, f"/_schub/ping.png?k={app.probe_key}", headers=other)
    assert status == 200 and "Cross-Origin-Resource-Policy" not in headers


def test_nothing_outside_the_copy_and_no_hidden_files(app, tmp_path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    (app.mirror.dest / "jfig" / "link.txt").symlink_to(secret)
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/jfig/..%2f..%2fsecret.txt", "/.schub-view",
                 "/jfig/link.txt", "/%00", "/jfig/a/..\\..\\x"):
        status, _, body = get(app.port, path)
        assert status == 404 and b"private" not in body, path


def test_pages_that_sc_hub_did_not_write_run_without_this_origin(app) -> None:
    report = app.mirror.dest / "jrep" / "p" / "r1"
    report.mkdir(parents=True)
    (report / "report.html").write_text("<script>fetch('/index.html')</script>")
    _, headers, _ = get(app.port, "/jrep/p/r1/report.html")
    policy = headers["Content-Security-Policy"]
    assert policy.startswith("sandbox allow-scripts") and "allow-same-origin" not in policy
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"  # nor can it load another project's data
    _, headers, _ = get(app.port, "/guide.html")
    assert headers["Content-Security-Policy"] == pages.STATIC_CSP


def test_only_the_dashboards_own_data_files_are_javascript(app) -> None:
    """A `.js` anywhere else in the copy would run in the dashboard's origin if a page loaded it (a planted file,
    markup reaching a page from data): it goes out as plain text, which `nosniff` keeps the browser from running."""
    dest = app.mirror.dest
    own = ("jproj/a.js", "jnb/p.js", "nb/r1.js", "pts/k.js", "jrep/p/r1/report.js")
    other = ("img/evil.js", "jfig/a/plot.js", "jrep/p/r1/other.js", "jrep/report.js", "x.js")
    for name in (*own, *other):
        (dest / name).parent.mkdir(parents=True, exist_ok=True)
        (dest / name).write_text("window.x=1;")
    for name in own:
        status, headers, _ = get(app.port, "/" + name)
        assert status == 200 and headers["Content-Type"].startswith("text/javascript"), name
    for name in other:
        status, headers, _ = get(app.port, "/" + name)
        assert status == 200 and headers["Content-Type"].startswith("text/plain") and \
            headers["X-Content-Type-Options"] == "nosniff", name


def test_a_large_file_is_streamed_whole(app) -> None:
    big = app.mirror.dest / "jrep" / "big.ipynb"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(os.urandom(view.STREAM_ABOVE + 12345))
    status, headers, body = get(app.port, "/jrep/big.ipynb")
    assert status == 200 and body == big.read_bytes() and int(headers["Content-Length"]) == len(body)
    status, headers, body = get(app.port, "/jrep/big.ipynb", method="HEAD")
    assert status == 200 and body == b"" and int(headers["Content-Length"]) == big.stat().st_size


def test_until_the_first_copy_arrives_a_page_says_so_and_reloads(env) -> None:
    place, _, _ = env
    server = view.App(place, view.Settings(place), log=lambda line: None)
    server.start(refresh=False)
    try:
        status, headers, body = get(server.port, "/")
        assert status == 200 and b"Your dashboard is on its way" in body and b'http-equiv="refresh"' in body
        assert_no_script(headers)
        server.refresher.error = "The cluster cannot be reached <from here>"
        _, headers, body = get(server.port, "/guide.html")
        assert b"The cluster guide is on its way" in body and b"&lt;from here&gt;" in body
        assert_no_script(headers)
        assert_no_script(get(server.port, "/no/such/page")[1])
    finally:
        server.stop()


def test_a_copy_from_an_older_sc_hub_says_the_guide_comes_with_its_update(app) -> None:
    (app.mirror.dest / "guide.html").unlink()
    _, headers, body = get(app.port, "/guide.html")
    assert b"comes with sc-hub's next update" in body and b"retry cluster" in body and b"refresh" not in body
    assert_no_script(headers)


def test_the_guide_runs_no_script_and_the_dashboard_only_its_own(app) -> None:
    """guide.html has none; the dashboard page's inline scripts are pinned by hash in a policy of its own."""
    assert_no_script(get(app.port, "/guide.html")[1])
    assert get(app.port, "/")[1]["Content-Security-Policy"] == view.OWN_CSP


def test_an_old_copy_says_so_and_offers_to_try_again(app) -> None:
    app.refresher.last_ok_at = time.time() - 3600
    _, _, body = get(app.port, "/")
    assert b"schub-local" not in body  # old, but nothing went wrong: no note
    app.refresher.error = "The cluster cannot be reached from this computer: on campus Wi-Fi or the VPN?"
    _, _, body = get(app.port, "/")
    note = body[body.index(b'id="schub-local"'):body.index(b"</body>")]
    assert b"on campus Wi-Fi or the VPN?" in note and b"onclick" not in note  # no inline script: pinned pages allow none
    assert b'<script src="/_schub/bar.js"></script>' in note
    status, headers, script = get(app.port, "/_schub/bar.js")
    assert status == 200 and headers["Content-Type"].startswith("text/javascript") and b"/_schub/refresh" in script
    status, _, _ = get(app.port, "/_schub/refresh", method="POST")  # a form on another site cannot set the header
    assert status == 403
    status, _, _ = get(app.port, "/_schub/refresh", method="POST", headers={"X-Schub-View": "1"})
    assert status == 200 and app.refresher.wanted.is_set()


def test_quit_needs_the_token_from_the_state_file(app) -> None:
    status, _, _ = get(app.port, "/_schub/quit", method="POST", headers={"X-Schub-Token": "guess"})
    assert status == 403 and not app.done.is_set()
    status, _, _ = get(app.port, "/_schub/quit", method="POST", headers={"X-Schub-Token": app.token})
    assert status == 200 and app.done.wait(5)


def test_a_bad_body_length_closes_the_connection(app) -> None:
    for length in ("-5", "abc", "99999"):
        connection = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
        connection.putrequest("POST", "/_schub/refresh")
        connection.putheader("Host", f"127.0.0.1:{app.port}")
        connection.putheader("Content-Length", length)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status in (403, 413) and response.getheader("Connection") == "close", length
        connection.close()


def test_the_welcome_page_says_what_the_setup_installed(app) -> None:
    view.write_json(app.place.welcome, {
        "login": "test.user", "host": "login-student-lab.mbzu.ae", "remote_root": "/l/users/test.user/schub",
        "workspace": "/Users/t/sc-hub workspace", "assistants": ["Codex", "Claude Code", "Claude Desktop"],
        "cluster_agents": {"codex": "test.user@mbzuai.ac.ae", "claude": "<b>x</b>"}, "cluster_path": ["codex", "claude"],
        "vscode_host": "mbzuai-schub-ide", "view_command": "~/.sc-hub/bin/schub-view"})
    status, headers, body = get(app.port, "/welcome")
    text = body.decode()
    assert status == 200 and "sc-hub is ready" in text and "Set up for test.user." in text
    assert "/l/users/test.user/schub" in text and "Codex and Claude Code and Claude Desktop" in text
    assert "Restart Claude Desktop once" in text and "Codex as test.user@mbzuai.ac.ae" in text
    assert "&lt;b&gt;x&lt;/b&gt;" in text and "<b>x</b>" not in text
    assert "ssh test.user@login-student-lab.mbzu.ae" in text and "<code>claude</code> and <code>codex</code>" in text
    assert 'cd &quot;/Users/t/sc-hub workspace&quot; &amp;&amp; codex' in text and "mbzuai-schub-ide" in text
    assert 'href="/guide.html"' in text and 'href="/"' in text and "~/.sc-hub/bin/schub-view" in text
    assert f'<script nonce="{nonce_of(headers)}">' in text
    assert re.search(r"script-src ([^;]+)", headers["Content-Security-Policy"])[1] == f"'nonce-{nonce_of(headers)}'"


def test_the_welcome_page_without_the_setups_notes_is_still_useful(app) -> None:
    _, _, body = get(app.port, "/welcome")
    assert b"sc-hub is ready" in body and b"$schub" in body and b"Codex and Claude Code on the cluster" not in body
    assert b"Set up for" not in body and b"~/.sc-hub/bin/schub-view" in body


def test_go_picks_the_named_address_when_the_browser_reaches_it(app) -> None:
    status, headers, body = get(app.port, "/go?to=/welcome")
    policy = headers["Content-Security-Policy"]
    assert status == 200 and f"img-src 'self' data: http://sc-hub.localhost:{app.port};" in policy  # the probe loads
    assert "'unsafe-inline'" not in policy.split("style-src")[0] and f'<script nonce="{nonce_of(headers)}">' in \
        body.decode()
    assert b'data-to="/welcome"' in body and f'data-named="http://sc-hub.localhost:{app.port}"'.encode() in body
    status, headers, _ = get(app.port, "/go?to=/welcome", host=f"sc-hub.localhost:{app.port}")
    assert status == 302 and headers["Location"] == "/welcome"
    key = re.search(r'data-probe="([0-9a-f]+)"', body.decode())[1]
    named = f"sc-hub.localhost:{app.port}"
    status, headers, png = get(app.port, f"/_schub/ping.png?k={key}&t=1", host=named)
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and struct.unpack(">II", png[16:24]) == (3, 2)
    assert "Cross-Origin-Resource-Policy" not in headers  # (the /go page loads it from the other address)


def test_a_website_cannot_tell_that_the_dashboard_is_running_here(app) -> None:
    """The probe image answers only to the key /go hands out (no other site can read /go), so an image tag on
    another site sees the same error as when nothing listens on the port."""
    named = f"sc-hub.localhost:{app.port}"
    for path in ("/_schub/ping.png", "/_schub/ping.png?k=", "/_schub/ping.png?k=0000", "/_schub/ping.png?t=1"):
        status, headers, body = get(app.port, path, host=named)
        assert status == 404 and not body.startswith(b"\x89PNG"), path


@pytest.mark.parametrize("to", [
    "/</script><script>alert(1)</script>",  # found in review: a script out of the page's own
    "//evil.example/x", "/%09/evil.example", "/\\evil.example", "https://evil.example/", "javascript:alert(1)",
    "/%0d%0aSet-Cookie:%20x=1", "/welcome?next=//evil", '/"onmouseover="alert(1)'])
def test_go_leads_only_to_a_plain_path_here(app, to) -> None:
    status, headers, body = get(app.port, "/go?to=" + to.replace("<", "%3C").replace(">", "%3E").replace('"', "%22"))
    assert status == 200 and b'data-to="/"' in body and b"alert" not in body and b"evil" not in body
    status, headers, body = get(app.port, "/go?to=" + to.replace("<", "%3C").replace(">", "%3E").replace('"', "%22"),
                                host=f"sc-hub.localhost:{app.port}")
    assert status == 302 and headers["Location"] == "/" and "Set-Cookie" not in headers


def test_status_proves_the_server_holds_its_token(app) -> None:
    _, _, body = get(app.port, "/_schub/status?nonce=abc123")
    info = json.loads(body)
    assert info["app"] == "sc-hub-view" and info["port"] == app.port and "home" not in info
    assert info["proof"] == view.proof(app.token, "abc123") and app.token not in body.decode()


# ---- the copy ------------------------------------------------------------------------------------------------------


def calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "ssh.jsonl"
    return [json.loads(line)["command"] for line in log.read_text().splitlines()] if log.exists() else []


def test_the_copy_through_view_pack_is_byte_for_byte_and_light_when_nothing_heavy_changed(env, monkeypatch) -> None:
    place, source, tmp_path = env
    monkeypatch.setenv("SCHUB_VIEW_TRANSFER", "pack")
    mirror = copying.Mirror(place, view.Settings(place))
    mirror.refresh()
    dest = mirror.dest
    assert (dest / "jfig" / "a" / "c1.png").read_bytes() == (source / "jfig" / "a" / "c1.png").read_bytes()
    assert (dest / copying.MARKER).exists() and not dest.with_name(dest.name + ".incoming").exists()
    assert calls(tmp_path)[-2:] == ["schub/bin/schub view-sum", "schub/bin/schub view-pack full"]
    (source / "jproj" / "a.js").write_text("window.x=2;")
    mirror.refresh()
    assert calls(tmp_path)[-1] == "schub/bin/schub view-pack light" and (dest / "jproj" / "a.js").read_text() == "window.x=2;"
    shutil.rmtree(source / "jfig")  # gone on the cluster: gone here at the next full copy
    (source / "img").mkdir()
    (source / "img" / "t.png").write_bytes(b"png")
    mirror.refresh()
    assert calls(tmp_path)[-1] == "schub/bin/schub view-pack full"
    assert not (dest / "jfig").exists() and (dest / "img" / "t.png").read_bytes() == b"png"
    assert (dest / copying.MARKER).exists() and not list(place.folder.glob("view-*.tar"))


@pytest.mark.skipif(shutil.which("rsync") is None or os.name == "nt", reason="needs rsync")
def test_the_copy_through_rsync(env, monkeypatch) -> None:
    place, source, tmp_path = env
    monkeypatch.setenv("SCHUB_VIEW_TRANSFER", "rsync")
    mirror = copying.Mirror(place, view.Settings(place))
    mirror.refresh()
    assert (mirror.dest / "index.html").read_text() == (source / "index.html").read_text()
    assert (mirror.dest / "jfig" / "a" / "c1.png").read_bytes() == (source / "jfig" / "a" / "c1.png").read_bytes()
    commands = calls(tmp_path)
    assert commands[0] == "schub/bin/schub dashboard >/dev/null" and all("rsync --server --sender" in c for c in commands[1:])


def test_a_custom_cluster_folder_is_asked_for_by_its_path(env, monkeypatch) -> None:
    place, _, tmp_path = env
    monkeypatch.setenv("SCHUB_VIEW_TRANSFER", "pack")
    monkeypatch.setenv("FAKE_REMOTE", "/l/users/test.user/schub")
    view.save_config(place, remote="/l/users/test.user/schub")
    copying.Mirror(place, view.Settings(place)).refresh()
    assert calls(tmp_path)[0] == "/l/users/test.user/schub/bin/schub view-sum"


def test_one_update_of_the_copy_at_a_time(env, monkeypatch) -> None:
    """The server's refresh and a `--once` never write the copy together."""
    place, _, _ = env
    monkeypatch.setattr(copying, "COPY_WAIT_S", 0.5)
    held = copying.FileLock(place.folder / "view-copy.lock")
    assert held.acquire()
    try:
        with pytest.raises(copying.MirrorError, match="another update of the copy"):
            copying.Mirror(place, view.Settings(place)).refresh()
    finally:
        held.release()


def test_an_archive_with_a_path_out_of_the_folder_is_refused(tmp_path) -> None:
    archive = tmp_path / "a.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(copying.MirrorError, match="unsafe name"):
        copying.unpack(archive, tmp_path / "in")
    assert not (tmp_path / "escape.txt").exists()


def test_a_name_windows_cannot_hold_is_left_out_not_the_whole_copy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(copying, "NT", True)
    archive = tmp_path / "a.tar"
    with tarfile.open(archive, "w") as tar:
        for name in ("index.html", "jfig/p/plot 12:30.png", "jfig/p/fine.png", "jfig/p/aux.txt"):
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    names, skipped = copying.unpack(archive, tmp_path / "in")
    assert names == {"index.html", "jfig"} and skipped == 2 and (tmp_path / "in" / "jfig" / "p" / "fine.png").exists()


def test_a_folder_that_is_not_its_own_is_never_written(env) -> None:
    place, _, _ = env
    mine = place.home / "sc-hub-view"
    mine.mkdir()
    (mine / "thesis.docx").write_text("do not delete")
    with pytest.raises(copying.MirrorError, match="is not sc-hub's copy"):
        copying.Mirror(place, view.Settings(place)).refresh()
    assert (mine / "thesis.docx").exists()


def test_the_shared_ssh_connection_only_where_its_socket_path_fits(env) -> None:
    place, _, _ = env
    args = copying.Mirror(place, view.Settings(place)).ssh_args()
    fits = len(os.fsencode(str(place.home / ".ssh" / "cm-schub-"))) + 57 < copying.SOCKET_MAX
    assert any(a.startswith("ControlPath=") for a in args) == (fits and os.name != "nt")
    short = view.Place(Path("/tmp/h"))
    wanted = f"ControlPath={short.home / '.ssh' / 'cm-schub-'}%C"
    assert any(a == wanted for a in copying.Mirror(short, view.Settings(short)).ssh_args()) or os.name == "nt"
    long = view.Place(Path("/Users/mohammedabdullahalhashemi-the-long-name"))
    assert not any(a.startswith("ControlPath=") for a in copying.Mirror(long, view.Settings(long)).ssh_args())


def test_what_ssh_says_becomes_something_to_do() -> None:
    explain = copying.explain
    assert "campus Wi-Fi or the VPN" in explain("ssh: Could not resolve hostname login-student-lab.mbzu.ae", 255)
    assert "campus Wi-Fi or the VPN" in explain("ssh: connect to host x port 22: Operation timed out", 255)
    assert "retry sign-in" in explain("test.user@login: Permission denied (publickey).", 255)
    assert "retry cluster" in explain("sc-hub: this key only opens sc-hub (MCP server, dashboard, sessions)", 126)
    assert "sandbox" in explain("socket: Operation not permitted", 255)
    assert explain("", 3) == "the copy failed (exit code 3)"


def test_the_cluster_out_of_reach_keeps_the_last_copy_and_waits_longer_each_time(app, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_SSH_FAIL", "ssh: Could not resolve hostname login-student-lab.mbzu.ae: nodename nor servname")
    refresher = app.refresher
    assert refresher.once() is False and "VPN" in refresher.error and refresher.failures == 1
    assert refresher.interval() == refresher.every
    refresher.once()
    assert refresher.interval() == 2 * refresher.every
    refresher.failures = 20
    assert refresher.interval() == view.MAX_BACKOFF_S
    refresher.last_view -= view.IDLE_AFTER_S + 1  # nobody looked for a while
    assert refresher.interval() == view.IDLE_EVERY_S
    refresher.seen()  # someone opened the page again: at once
    assert refresher.wanted.is_set()
    status, _, body = get(app.port, "/")
    assert status == 200 and b"<h1>dashboard</h1>" in body  # the last copy is still served
    monkeypatch.delenv("FAKE_SSH_FAIL")
    assert refresher.once() is True and refresher.error == "" and refresher.failures == 0


def test_the_refresher_loop_runs_at_once_then_on_request_and_stops(app, monkeypatch) -> None:
    done: list[float] = []
    monkeypatch.setattr(app.mirror, "refresh", lambda: done.append(time.monotonic()))
    monkeypatch.setattr(view, "MIN_GAP_S", 0.2)
    import threading

    loop = threading.Thread(target=app.refresher.run, daemon=True)
    loop.start()
    deadline = time.monotonic() + 5
    while not done and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(done) == 1  # at start
    for _ in range(5):  # "Try again" pressed five times: one more refresh, not five
        app.refresher.poke()
    time.sleep(0.8)
    assert len(done) == 2
    app.refresher.stopping.set()
    app.refresher.poke()
    loop.join(timeout=5)
    assert not loop.is_alive()


def test_stopping_the_server_stops_a_transfer_in_progress(app, monkeypatch) -> None:
    import threading

    monkeypatch.setenv("FAKE_SSH_SLEEP", "30")
    results: list[bool] = []
    worker = threading.Thread(target=lambda: results.append(app.refresher.once()), daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while app.mirror.child is None and time.monotonic() < deadline:
        time.sleep(0.05)
    started = time.monotonic()
    app.stop()
    worker.join(timeout=10)
    assert results == [False] and time.monotonic() - started < 10 and app.refresher.error == "stopped"


@pytest.mark.skipif(not socket.has_ipv6, reason="no IPv6 here")
def test_a_port_whose_ipv6_side_is_taken_is_passed_over(env) -> None:
    """sc-hub.localhost may resolve to [::1] first: a program there on the same port would get the student's page."""
    place, _, _ = env
    port = free_port()
    try:
        squatter = socket.socket(socket.AF_INET6)
        squatter.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        squatter.bind(("::1", port))
        squatter.listen()
    except OSError:
        pytest.skip("no IPv6 loopback here")
    try:
        server = view.App(place, view.Settings(place), log=lambda line: None)
        servers = view.bind(server, port)
        try:
            assert servers[0].server_address[1] != port
        finally:
            for one in servers:
                one.server_close()
    finally:
        squatter.close()


# ---- one server per home, in the background -----------------------------------------------------------------------


def run_cli(place: view.Place, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / "schub_view.py"), *args, "--home", str(place.home)],
                          capture_output=True, text=True, timeout=timeout)


def test_start_status_and_stop_from_the_command_line(env, monkeypatch) -> None:
    place, _, _ = env
    monkeypatch.setenv("SCHUB_VIEW_TRANSFER", "pack")
    started = run_cli(place, "start", "--no-open", "--json")
    try:
        assert started.returncode == 0, started.stderr
        info = json.loads(started.stdout)
        assert info["url"] == f"http://sc-hub.localhost:{info['port']}" and info["open"].endswith("/go?to=%2F")
        deadline = time.monotonic() + 20  # the first copy, in the background
        while time.monotonic() < deadline and not (place.home / "sc-hub-view" / "index.html").exists():
            time.sleep(0.2)
        assert (place.home / "sc-hub-view" / "index.html").exists()
        again = run_cli(place, "start", "--no-open", "--json")
        assert json.loads(again.stdout)["port"] == info["port"]  # the same server, not a second one
        status = run_cli(place, "status", "--json")
        state = json.loads(status.stdout)
        assert status.returncode == 0 and state["pid"] != os.getpid() and "token" not in state and "proof" not in state
        assert oct(place.state.stat().st_mode & 0o777) == "0o600"
        assert "port" not in view.read_json(place.config)  # the usual address first next time, not this one
    finally:
        stopped = run_cli(place, "stop")
    assert "stopped" in stopped.stdout and run_cli(place, "status").returncode == 1 and not place.state.exists()


def test_a_trials_home_starts_at_its_own_port(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SCHUB_VIEW_PORT", raising=False)
    assert view.Settings(view.Place(tmp_path)).port == view.TRIAL_PORT
    assert view.Settings(view.Place()).port in (view.PORT, int(view.read_json(view.Place().config).get("port") or 0))


def test_a_taken_port_moves_it_to_the_next_one(env) -> None:
    place, _, _ = env
    taken = free_port()
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", taken))
        blocker.listen()
        settings = view.Settings(place)
        settings.port = taken
        server = view.App(place, settings, log=lambda line: None)
        server.start(refresh=False)
        try:
            assert server.port != taken
            assert get(server.port, "/_schub/status")[0] == 200
        finally:
            server.stop()


def test_another_program_on_the_recorded_port_is_not_taken_for_the_server(env) -> None:
    """After a crash its state file names a port someone else may now answer on, echoing the right words."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    place, _, _ = env

    class Impostor(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):  # noqa: N802
            body = json.dumps({"app": "sc-hub-view", "version": view.VERSION, "pid": 1, "port": 0,
                               "url": "x", "dir": "x", "proof": "0" * 64}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    impostor = HTTPServer(("127.0.0.1", 0), Impostor)
    threading.Thread(target=impostor.serve_forever, daemon=True).start()
    try:
        view.write_json(place.state, {"pid": 1, "port": impostor.server_address[1], "token": "old-token"})
        assert view.running(place) is None  # it cannot prove it holds the token
        assert view.stop(place, quiet=True) == 0  # and nothing is signalled: the lock is free, nobody runs
    finally:
        impostor.shutdown()


def test_only_this_homes_server_gets_a_signal(tmp_path) -> None:
    mine, other = view.Place(tmp_path / "mine"), view.Place(tmp_path / "other")
    assert not view.ours(os.getpid(), mine)  # pytest, not schub_view.py serve
    command = [sys.executable, "-c", "import time; time.sleep(30)", str(SCRIPTS / "schub_view.py"), "serve"]
    server_of_mine = subprocess.Popen([*command, "--home", str(mine.home)])
    real_homes_server = subprocess.Popen(command)  # started without --home: the real home's
    try:
        time.sleep(0.3)
        assert view.ours(server_of_mine.pid, mine)
        assert not view.ours(server_of_mine.pid, other)  # another home's state file naming this pid (pid reuse)
        assert not view.ours(server_of_mine.pid, view.Place())  # nor the real home's
        assert not view.ours(real_homes_server.pid, mine)
    finally:
        server_of_mine.kill()
        real_homes_server.kill()


def test_a_second_server_for_the_same_home_leaves_at_once(env) -> None:
    place, _, _ = env
    held = view.Lock(place.lock)
    assert held.acquire()
    try:
        done = run_cli(place, "serve", timeout=30)
        assert done.returncode == 0 and "already running" in done.stdout
    finally:
        held.release()


def test_once_updates_the_copy_and_says_what_went_wrong(env, monkeypatch) -> None:
    place, _, _ = env
    monkeypatch.setenv("SCHUB_VIEW_TRANSFER", "pack")
    done = run_cli(place, "--once")
    assert done.returncode == 0 and (place.home / "sc-hub-view" / "index.html").exists()
    monkeypatch.setenv("FAKE_SSH_FAIL", "test.user@login: Permission denied (publickey).")
    done = run_cli(place, "--once")
    assert done.returncode == 1 and "retry sign-in" in done.stdout


def test_its_version_is_its_code() -> None:
    assert view.VERSION == view.version() and len(view.VERSION) == 12 and all(
        (SCRIPTS / name).exists() for name in view.PARTS)


def test_the_go_target_allowlist() -> None:
    for ok in ("/", "/welcome", "/guide.html", "/a/b-c_d.e~f"):
        assert pages.target(ok) == ok
    for bad in ("", "welcome", "//x", "/\\x", "/\tx", "/x y", "/x\r\n", "/x?y", "/x#y", "/%2e"):
        assert pages.target(bad) == "/", bad
