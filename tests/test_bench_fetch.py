from __future__ import annotations

import hashlib
import http.server
import threading
from functools import partial
from pathlib import Path

import pytest

from schub.bench import fetch as fetch_module
from schub.bench import ledger
from schub.bench.fetch import FetchError, fetch

PAYLOAD = bytes(range(256)) * 400  # 100 KB


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves files with Range support; /flaky.bin drops the connection once, halfway."""

    dropped = False

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/flaky.bin":
            self._range_response(PAYLOAD, drop=not Handler.dropped)
            return
        if self.path == "/data.bin":
            self._range_response(PAYLOAD)
            return
        super().do_GET()

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
