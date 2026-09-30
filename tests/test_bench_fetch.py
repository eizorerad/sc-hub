from __future__ import annotations

import hashlib
import http.server
import json
import socket
import threading
import traceback
from functools import partial
from pathlib import Path

import pytest

from schub import locking
from schub.bench import fetch as fetch_module
from schub.bench import ledger
from schub.bench.fetch import FetchError, fetch

PAYLOAD = bytes(range(256)) * 400  # 100 KB


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves files with Range support; /flaky.bin drops the connection once, halfway."""

    dropped = False

    def log_message(self, *args) -> None:
        pass

    requests = 0
    chunked_dropped = False

    def do_GET(self) -> None:
        Handler.requests += 1
        if self.path == "/chunked.bin":
            self._chunked(drop=not Handler.chunked_dropped)
            return
        if self.path.startswith("/nolength.bin"):
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(PAYLOAD)
            self.close_connection = True
            return
        if self.path == "/flaky.bin":
            self._range_response(PAYLOAD, drop=not Handler.dropped)
            return
        if self.path == "/data.bin":
            self._range_response(PAYLOAD)
            return
        if self.path == "/busy.bin":  # a server that is busy once (503), then answers
            if Handler.requests == 1:
                self.send_error(503, "Service Unavailable")
                return
            self._range_response(PAYLOAD)
            return
        if self.path.startswith("/forbidden.bin"):
            self.send_error(403, "Forbidden")
            return
        if self.path.startswith("/down.bin"):  # failing on every try
            self.send_error(503, "Service Unavailable")
            return
        if self.path.startswith("/empty.bin"):  # a success status with nothing to download
            self.send_response(204)
            self.end_headers()
            return
        if self.path.startswith("/moved.bin"):  # a redirect urllib refuses to follow, keeping the query
            self.send_response(302)
            self.send_header("Location", f"file://{self.path}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/misrange.bin"):  # cut short, then resumed from byte 0 whatever was asked
            if not self.headers.get("Range"):
                self._range_response(PAYLOAD, drop=True)
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes 0-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
            return
        super().do_GET()

    def _chunked(self, drop: bool) -> None:
        self.protocol_version = "HTTP/1.1"
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        pieces = [PAYLOAD[i:i + 8192] for i in range(0, len(PAYLOAD), 8192)]
        for index, piece in enumerate(pieces):
            if drop and index == len(pieces) // 2:
                Handler.chunked_dropped = True
                self.wfile.write(b"2000\r\n" + piece[:100])  # a chunk cut short: IncompleteRead
                self.wfile.flush()
                self.connection.shutdown(2)
                return
            self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def _range_response(self, data: bytes, drop: bool = False) -> None:
        start = 0
        header = self.headers.get("Range")
        if header:
            start = int(header.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        body = data[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if drop:
            Handler.dropped = True
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            self.connection.shutdown(2)
            return
        self.wfile.write(body)


@pytest.fixture
def server(tmp_path: Path):
    root = tmp_path / "www"
    root.mkdir()
    (root / "small.txt").write_text("hello")
    Handler.dropped = False
    Handler.chunked_dropped = False
    Handler.requests = 0
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(root)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def project(tmp_path: Path, monkeypatch) -> Path:
    folder = tmp_path / "project"
    monkeypatch.setenv("SCHUB_PROJECT_DIR", str(folder))
    ledger.drain()
    return folder


def test_downloads_into_the_project_and_records_it(server: str, project: Path) -> None:
    path = fetch(f"{server}/data.bin")
    assert path == project / "data" / "data.bin" and path.read_bytes() == PAYLOAD
    [event] = ledger.drain()
    assert event["kind"] == "download" and event["sha256"] == hashlib.sha256(PAYLOAD).hexdigest()
    assert event["size"] == len(PAYLOAD) and event["status"] == "ok"


def test_a_dropped_connection_resumes(server: str, project: Path) -> None:
    path = fetch(f"{server}/flaky.bin", pause_s=0)
    assert path.read_bytes() == PAYLOAD  # not the first half twice


def test_a_404_page_is_never_a_dataset(server: str, project: Path) -> None:
    with pytest.raises(FetchError, match="404"):
        fetch(f"{server}/missing.h5ad", pause_s=0)
    assert not (project / "data" / "missing.h5ad").exists()


def test_checksum_mismatch_deletes_the_file(server: str, project: Path) -> None:
    with pytest.raises(FetchError, match="sha256"):
        fetch(f"{server}/small.txt", sha256="0" * 64)
    assert not (project / "data" / "small.txt").exists()
    assert ledger.drain()[-1]["status"] == "failed"
    good = hashlib.sha256(b"hello").hexdigest()
    assert fetch(f"{server}/small.txt", dest=project / "data" / "greeting.txt", sha256=good).read_text() == "hello"


def test_only_web_urls(project: Path) -> None:
    for url in ("file:///etc/passwd", "ssh://x/y", "ftp://ftp.ncbi.nlm.nih.gov/geo/x"):
        with pytest.raises(FetchError):
            fetch(url)


def test_a_partial_file_of_another_url_is_never_resumed(server: str, project: Path) -> None:
    stale = project / "data" / "data.bin.part"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"x" * 50_000)  # left by a download of some other file with the same name
    assert fetch(f"{server}/data.bin").read_bytes() == PAYLOAD
    assert not (project / "data" / "data.bin.part.json").exists()


def test_gives_up_after_repeated_network_errors(project: Path, monkeypatch) -> None:
    monkeypatch.setattr(fetch_module, "_open", lambda url, offset, validator="": (_ for _ in ()).throw(ConnectionResetError("reset")))
    with pytest.raises(FetchError, match="after 2 attempts"):
        fetch("https://example.org/x.bin", attempts=2, pause_s=0)


def test_md5_as_zenodo_publishes_it(server: str, project: Path) -> None:
    good = "md5:" + hashlib.md5(b"hello").hexdigest()
    assert fetch(f"{server}/small.txt", md5=good).read_text() == "hello"
    with pytest.raises(FetchError, match="md5"):
        fetch(f"{server}/small.txt", dest=project / "data" / "other.txt", md5="0" * 32)


SHA = hashlib.sha256(PAYLOAD).hexdigest()


def test_a_cut_chunked_response_is_retried(server: str, project: Path) -> None:
    path = fetch(f"{server}/chunked.bin", sha256=SHA, pause_s=0)
    assert path.read_bytes() == PAYLOAD and Handler.chunked_dropped


def test_no_length_and_no_checksum_is_said(server: str, project: Path) -> None:
    fetch(f"{server}/nolength.bin", pause_s=0)
    [event] = ledger.drain()
    assert event["status"] == "ok" and "not verified" in event["message"]


def test_credentials_never_reach_the_journal(server: str, project: Path) -> None:
    with pytest.raises(FetchError, match="user or password"):
        fetch("https://me:secret@example.org/x.h5ad")
    fetch(f"{server}/nolength.bin?token=abc123&download=1", pause_s=0)
    [event] = ledger.drain()
    assert "abc123" not in event["url"] and "token=REDACTED" in event["url"] and "download=1" in event["url"]
    assert not any("abc123" in f.name or "abc123" in f.read_text(errors="ignore")
                   for f in (project / "data").iterdir() if f.is_file() and f.suffix != ".bin")


TOKEN = "FAKE_TEST_TOKEN_5f3a9c"  # artificial: it must never show in an error, a job's log or the journal


def _shown(error: BaseException) -> str:
    """What a cell's output or a job's log shows of an error: its traceback, with the exceptions chained to it."""
    return "".join(traceback.format_exception(error))


@pytest.mark.parametrize("path, kwargs, said", [
    ("/missing.h5ad", {}, "HTTP 404"),  # a client error, not retried
    ("/forbidden.bin", {}, "HTTP 403"),
    ("/empty.bin", {}, "HTTP 204"),  # a status that is not a download
    ("/down.bin", {}, "after 2 attempts: HTTP 503"),  # a server error until the retries run out
    ("/misrange.bin", {}, "another byte"),  # a resume from the wrong byte
    ("/moved.bin", {}, "HTTP 302"),  # urllib's own error quotes the URL it was sent to
    ("/small.txt", {"sha256": "0" * 64}, "sha256"),
])
def test_a_failed_download_never_shows_a_secret_query_value(server: str, project: Path, capsys, path: str,
                                                            kwargs: dict, said: str) -> None:
    """Found in an audit: public() hid the token in the journal, but the error text kept the URL whole."""
    with pytest.raises(FetchError, match=said) as caught:
        fetch(f"{server}{path}?token={TOKEN}&download=1", attempts=2, pause_s=0, **kwargs)
    shown = _shown(caught.value)
    assert TOKEN not in shown and "token=REDACTED" in shown and "download=1" in shown
    assert TOKEN not in json.dumps(ledger.drain()) + "".join(capsys.readouterr())


@pytest.mark.parametrize("url, said", [
    ("http://127.0.0.1:{closed}/x.bin?token={token}", "after 1 attempt"),  # nothing listens there
    ("{server}/data.bin?token={token}&note=a b", "control characters"),  # http.client quotes the path it refuses
])
def test_a_network_error_never_shows_a_secret_query_value(server: str, project: Path, url: str, said: str) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    with pytest.raises(FetchError, match=said) as caught:
        fetch(url.format(server=server, closed=closed, token=TOKEN), attempts=1, pause_s=0)
    assert TOKEN not in _shown(caught.value) and "token=REDACTED" in _shown(caught.value)


def test_a_cached_download_never_shows_a_secret_query_value(server: str, project: Path, tmp_path: Path,
                                                            monkeypatch, capsys) -> None:
    root = tmp_path / "root"
    monkeypatch.setenv("SCHUB_ROOT", str(root))
    for name in ("a.txt", "b.txt"):  # downloaded, then linked from the cache
        fetch(f"{server}/small.txt?token={TOKEN}", dest=tmp_path / name, sha256=hashlib.sha256(b"hello").hexdigest())
    kept = "".join(f.read_text(errors="ignore") for f in root.rglob("*") if f.is_file())
    assert TOKEN not in json.dumps(ledger.drain()) + "".join(capsys.readouterr()) + kept


def test_a_file_already_there_is_not_fetched_again_and_fetches_take_turns(server: str, project: Path) -> None:
    import threading

    results = []
    threads = [threading.Thread(target=lambda: results.append(fetch(f"{server}/data.bin", sha256=SHA, pause_s=0)))
               for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(results)) == 1 and results[0].read_bytes() == PAYLOAD
    assert Handler.requests == 1  # the others found it done under the lock
    events = ledger.drain()
    assert sum("already there" in e.get("message", "") for e in events) == 2


def test_projects_share_one_download_of_a_checksummed_file(server: str, project: Path, tmp_path: Path,
                                                           monkeypatch) -> None:
    """Found in the evaluation: four K562 projects fetched the same 1.55 GB file at once, each into its own
    data/, at 0.7 MB/s. With a checksum the file is fetched once into the student's cache and linked."""
    import threading

    root = tmp_path / "root"
    monkeypatch.setenv("SCHUB_ROOT", str(root))
    targets = [tmp_path / f"p{i}" / "data" / "data.bin" for i in range(3)]
    threads = [threading.Thread(target=fetch, args=(f"{server}/data.bin",), kwargs={"dest": t, "sha256": SHA,
                                                                                      "pause_s": 0})
               for t in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert Handler.requests == 1 and all(t.read_bytes() == PAYLOAD for t in targets)
    cached = list((root / "cache" / "fetch").rglob("data.bin"))
    assert len(cached) == 1 and cached[0].stat().st_ino == targets[0].stat().st_ino  # a link, not a copy
    events = ledger.drain()
    assert len(events) == 3 and {e["path"] for e in events} == {str(t) for t in targets}
    assert sum("download cache" in e.get("message", "") for e in events) == 2


def test_the_cache_is_read_only_shared_across_names_and_re_fetched_if_edited(server: str, project: Path,
                                                                             tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SCHUB_ROOT", str(tmp_path / "root"))
    first = fetch(f"{server}/data.bin", dest=tmp_path / "a" / "data.bin", sha256=SHA, pause_s=0)
    second = fetch(f"{server}/data.bin", dest=tmp_path / "b" / "renamed.bin", sha256=SHA, pause_s=0)
    assert Handler.requests == 1 and second.stat().st_ino == first.stat().st_ino  # one download, any name
    assert not first.stat().st_mode & 0o222  # editing one project's copy in place would change every copy
    first.chmod(0o644)
    first.write_bytes(b"edited")  # someone made it writable and changed it
    third = fetch(f"{server}/data.bin", dest=tmp_path / "c" / "data.bin", sha256=SHA, pause_s=0)
    assert Handler.requests == 2 and third.read_bytes() == PAYLOAD


@pytest.mark.parametrize("name", ["record.json", ".lock"])
def test_a_download_named_like_the_cache_s_own_files_is_the_download(server: str, project: Path, tmp_path: Path,
                                                                    monkeypatch, name: str) -> None:
    """Found in an audit: a download named record.json was replaced by the cache's record of it, and the journal
    said ok with the expected checksum; one named .lock took the place of the cache's lock."""
    monkeypatch.setenv("SCHUB_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr(fetch_module, "long_held", lambda path: locking.exclusive(path, wait_s=2))  # not 12 h
    for folder in ("p1", "p2"):  # downloaded, then from the cache
        path = fetch(f"{server}/data.bin", dest=tmp_path / folder / name, sha256=SHA, pause_s=0)
        assert path.read_bytes() == PAYLOAD
        [event] = ledger.drain()
        assert event["status"] == "ok" and event["sha256"] == SHA and event["size"] == len(PAYLOAD)
    assert Handler.requests == 1


def test_a_changed_cached_copy_is_never_recorded_as_ok(server: str, project: Path, tmp_path: Path,
                                                       monkeypatch) -> None:
    """The journal's checksum is the returned file's: a cached copy changed where the size check cannot see it
    (same size, read-only again) is not handed out, and the next fetch downloads it again."""
    root = tmp_path / "root"
    monkeypatch.setenv("SCHUB_ROOT", str(root))
    fetch(f"{server}/data.bin", dest=tmp_path / "a" / "data.bin", sha256=SHA, pause_s=0)
    [cached] = (root / "cache" / "fetch").rglob("data.bin")
    cached.chmod(0o644)
    cached.write_bytes(bytes(len(PAYLOAD)))
    cached.chmod(0o444)
    ledger.drain()
    with pytest.raises(FetchError, match="sha256"):
        fetch(f"{server}/data.bin", dest=tmp_path / "b" / "data.bin", sha256=SHA, pause_s=0)
    assert ledger.drain()[-1]["status"] == "failed" and not (tmp_path / "b" / "data.bin").exists()
    again = fetch(f"{server}/data.bin", dest=tmp_path / "b" / "data.bin", sha256=SHA, pause_s=0)
    assert again.read_bytes() == PAYLOAD and Handler.requests == 2


def test_a_cache_an_older_version_wrote_is_still_used(server: str, project: Path, tmp_path: Path,
                                                      monkeypatch) -> None:
    """An older version kept the file beside record.json in the checksum's folder: it is used without a new
    download, and stays the one file the projects that link it share."""
    root = tmp_path / "root"
    monkeypatch.setenv("SCHUB_ROOT", str(root))
    folder = root / "cache" / "fetch" / f"sha256-{SHA}"
    folder.mkdir(parents=True)
    (folder / "data.bin").write_bytes(PAYLOAD)
    (folder / "data.bin").chmod(0o444)
    (folder / "record.json").write_text(json.dumps({"url": "https://example.org/data.bin", "size": len(PAYLOAD),
                                                    "sha256": SHA}))
    linked = tmp_path / "old" / "data.bin"
    linked.parent.mkdir()
    linked.hardlink_to(folder / "data.bin")
    path = fetch(f"{server}/data.bin", dest=tmp_path / "new" / "renamed.bin", sha256=SHA, pause_s=0)
    assert Handler.requests == 0 and path.read_bytes() == PAYLOAD and path.stat().st_ino == linked.stat().st_ino
    [event] = ledger.drain()
    assert event["status"] == "ok" and "download cache" in event["message"]


def test_a_busy_server_is_retried(server: str, project: Path) -> None:
    path = fetch(f"{server}/busy.bin", pause_s=0)
    assert path.read_bytes() == PAYLOAD and Handler.requests == 2
