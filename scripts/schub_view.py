#!/usr/bin/env python3
"""Your sc-hub dashboard on this computer, at http://sc-hub.localhost:27182 (or http://127.0.0.1:27182).

sc-hub builds the dashboard on the cluster; this keeps a copy of it in ~/sc-hub-view, refreshes it every minute
while you look at it (rarely when nobody does), and serves it to this computer only. It runs in the background
until you stop it or restart the computer. The setup installs it in ~/.sc-hub/bin, outside the folders your
assistants write in. Standard library only, Python 3.9+, macOS, Linux, Windows.

    ~/.sc-hub/bin/schub-view            start it (if it is not running) and open it   (Windows: schub-view.cmd)
    ~/.sc-hub/bin/schub-view status     is it running, where, when it last updated
    ~/.sc-hub/bin/schub-view stop       stop it
    ~/.sc-hub/bin/schub-view serve      run it in this terminal instead (Ctrl-C stops it)
    ~/.sc-hub/bin/schub-view --once     update the copy once, then exit

Only this computer can open it: it listens on the loopback address, checks the Host header, and refuses what other
sites' pages ask for. Its own pages run only their own scripts; any other page of the copy (a report a notebook
made) runs in a sandbox. Settings: SCHUB_ALIAS, SCHUB_VIEW_DIR, SCHUB_VIEW_EVERY, SCHUB_REMOTE_ROOT, SCHUB_VIEW_PORT.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import json
import os
import secrets
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))  # its two companions, wherever it was started from
import schub_view_pages as pages  # noqa: E402
from schub_view_copy import FileLock, Mirror, MirrorError  # noqa: E402

HERE = Path(__file__).resolve().parent
PARTS = ("schub_view.py", "schub_view_copy.py", "schub_view_pages.py")
APP = "sc-hub-view"
NAME = pages.NAME
PORT = 27182  # unusual on purpose; the next ones are tried when it is taken (and 27182 first again next time)
TRIAL_PORT = 27282  # a trial's home (--home): never in the way of the real dashboard's address
PORTS_TRIED = 10
EVERY_S = 60
IDLE_AFTER_S = 20 * 60  # nobody looked at the page for this long: refresh rarely
IDLE_EVERY_S = 30 * 60
MAX_BACKOFF_S = 10 * 60
MIN_GAP_S = 5
START_WAIT_S = 20
NT = os.name == "nt"
NO_IPV6 = {getattr(errno, n, None) for n in ("EADDRNOTAVAIL", "EAFNOSUPPORT", "WSAEADDRNOTAVAIL", "WSAEAFNOSUPPORT")}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".gif": "image/gif", ".svg": "image/svg+xml", ".ipynb": "application/x-ipynb+json",
         ".txt": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8", ".pdf": "application/pdf"}
# The dashboard sc-hub builds: its two inline scripts (and its own policy in the page pins them by hash), files of
# this server, nothing from elsewhere.
OWN_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
           "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
           "form-action 'none'; frame-ancestors 'none'")
# Any other page of the copy (a report a notebook made) gets no origin: it cannot fetch the copy, and every file here
# refuses to load into another origin (Cross-Origin-Resource-Policy), so it cannot pull in other projects' data either.
OTHER_CSP = "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals; " \
            "frame-ancestors 'none'"
OWN_PAGES = {"index.html", "guide.html"}
STREAM_ABOVE = 1 << 20


def version() -> str:
    """A hash of its own code: a copy that changed replaces a running server of the old code on start."""
    digest = hashlib.sha256()
    for name in PARTS:
        try:
            digest.update((HERE / name).read_bytes())
        except OSError:
            digest.update(name.encode())
    return digest.hexdigest()[:12]


VERSION = version()


class Place:
    """Where it keeps things on this computer; another `home` is for trials and tests (then ssh gets -F)."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = (home or Path.home()).resolve()
        self.custom = self.home != Path.home().resolve()
        self.folder = self.home / ".sc-hub"
        self.state = self.folder / "view.json"  # the running server: pid, port, token (removed when it stops)
        self.config = self.folder / "view-config.json"  # what the setup chose: cluster folder, alias, port
        self.welcome = self.folder / "welcome.json"  # what the setup installed, for the welcome page
        self.lock = self.folder / "view.lock"
        self.log = self.folder / "view.log"
        self.ssh_config = self.home / ".ssh" / "config"

    def private(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        if not NT:
            os.chmod(self.folder, 0o700)


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)
    os.replace(temp, path)


class Settings:
    def __init__(self, place: Place) -> None:
        saved = read_json(place.config)
        self.alias = os.environ.get("SCHUB_ALIAS") or str(saved.get("alias") or "mbzuai-schub")
        self.remote = os.environ.get("SCHUB_REMOTE_ROOT") or str(saved.get("remote") or "schub")
        # the usual address first every time; only a port chosen with --port is remembered
        self.port = int(os.environ.get("SCHUB_VIEW_PORT") or saved.get("port") or (TRIAL_PORT if place.custom else PORT))
        self.every = max(30, int(os.environ.get("SCHUB_VIEW_EVERY") or EVERY_S))
        wanted = os.environ.get("SCHUB_VIEW_DIR") or saved.get("dir")
        self.dir = Path(wanted).expanduser() if wanted else place.home / "sc-hub-view"
        self.transfer = os.environ.get("SCHUB_VIEW_TRANSFER") or ("rsync" if shutil.which("rsync") and not NT else "pack")


# ---- keeping it fresh ---------------------------------------------------------------------------------------------


class Refresher:
    """Every minute while someone looks at the page, every half hour otherwise; after a failure it waits longer
    each time (up to ten minutes). A page opened after a quiet spell, or "Try again", refreshes at once."""

    def __init__(self, mirror: Mirror, every: int, log: Callable[[str], None] = lambda line: None) -> None:
        self.mirror, self.every, self.log = mirror, every, log
        self.lock = threading.Lock()
        self.wanted, self.stopping = threading.Event(), threading.Event()
        self.last_view = time.monotonic()  # started to be looked at
        self.last_try: float | None = None
        self.last_ok_at: float | None = None  # wall clock, for the page
        self.error = ""
        self.failures = 0
        self.refreshing = False

    def interval(self) -> float:
        if time.monotonic() - self.last_view > IDLE_AFTER_S:
            return IDLE_EVERY_S
        return min(self.every * 2 ** (self.failures - 1), MAX_BACKOFF_S) if self.failures else self.every

    def until_due(self) -> float:
        return 0.0 if self.last_try is None else max(0.0, self.last_try + self.interval() - time.monotonic())

    def seen(self) -> None:
        quiet = time.monotonic() - self.last_view > IDLE_AFTER_S
        self.last_view = time.monotonic()
        if quiet:  # back after a quiet spell: fresh now, not in half an hour
            self.poke()

    def poke(self) -> None:
        self.wanted.set()

    def stale(self) -> bool:
        return self.last_ok_at is None or time.time() - self.last_ok_at > 3 * self.every

    def once(self) -> bool:
        if not self.lock.acquire(blocking=False):
            return False  # one at a time; this request is answered by the running one
        try:
            self.refreshing = True
            self.last_try = time.monotonic()
            self.mirror.refresh()
            self.error, self.failures, self.last_ok_at = "", 0, time.time()
            return True
        except MirrorError as exc:
            self.error, self.failures = str(exc), self.failures + 1
            self.log(f"refresh failed: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001 - the server keeps serving the last copy whatever happened
            self.error, self.failures = f"{type(exc).__name__}: {exc}", self.failures + 1
            self.log(f"refresh failed: {self.error}")
            return False
        finally:
            self.refreshing = False
            self.lock.release()

    def run(self) -> None:
        asked = False  # a refresh was asked for: at most one every MIN_GAP_S, however often the button is pressed
        while not self.stopping.is_set():
            wait = self.until_due()
            if asked and self.last_try is not None:
                wait = min(wait, max(0.0, self.last_try + MIN_GAP_S - time.monotonic()))
            if self.wanted.wait(timeout=max(wait, 0.001)):
                self.wanted.clear()
                asked = True
                continue
            if self.stopping.is_set():
                return
            asked = False
            self.once()

    def status(self) -> dict[str, Any]:
        return {"last_ok": _iso(self.last_ok_at), "error": self.error, "refreshing": self.refreshing,
                "stale": self.stale()}


def _iso(stamp: float | None) -> str | None:
    return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds") if stamp else None


# ---- the server -------------------------------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server: "ViewServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_: Any) -> None:  # the page reloads every minute; the log keeps only what went wrong
        pass

    @property
    def app(self) -> "App":
        return self.server.app

    def _host_ok(self) -> bool:  # DNS rebinding: only this computer's own names for it
        return self.headers.get("Host", "") in {f"{h}:{self.app.port}" for h in ("127.0.0.1", "localhost", NAME, "[::1]")}

    def _other_site(self) -> bool:
        """A request another site's page made (a script, an image, a fetch): refused. Opening a page from a link is
        fine; the page is then this server's, not theirs."""
        return self.headers.get("Sec-Fetch-Site") == "cross-site" and self.headers.get("Sec-Fetch-Mode") != "navigate"

    def _head(self, code: int, kind: str, length: int, csp: str, shared: bool = False,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if not shared:  # nothing loads into another origin's page (a sandboxed report's included)
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if csp:
            self.send_header("Content-Security-Policy", csp)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _send(self, code: int, body: bytes, kind: str = "text/html; charset=utf-8", csp: str = OWN_CSP,
              shared: bool = False, extra: dict[str, str] | None = None) -> None:
        self._head(code, kind, len(body), csp, shared, extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _own(self, body: Callable[[str], bytes], images: str = "") -> None:
        """A page of this server's own, its script allowed by a nonce of this response only."""
        nonce = secrets.token_urlsafe(16)
        self._send(200, body(nonce), csp=pages.policy(nonce, images))

    def _json(self, data: dict[str, Any], code: int = 200) -> None:
        self._send(code, json.dumps(data).encode(), "application/json")

    def do_HEAD(self) -> None:  # noqa: N802 - http.server's naming
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._send(403, b"This page opens only as sc-hub.localhost or 127.0.0.1 on this computer.",
                              "text/plain; charset=utf-8")
        parsed = urllib.parse.urlsplit(self.path)
        path, app = parsed.path, self.app
        if path == "/_schub/ping.png":  # the /go page's probe comes from the other address: shared on purpose
            return self._send(200, pages.PING, "image/png", csp="", shared=True)
        if self._other_site():
            return self._send(403, b"", "text/plain")
        if path == "/_schub/status":
            nonce = urllib.parse.parse_qs(parsed.query).get("nonce", [""])[0][:64]
            return self._json(app.describe(nonce))
        if path == "/_schub/bar.js":
            return self._send(200, pages.BAR_JS.encode(), "text/javascript; charset=utf-8")
        if path == "/go":
            to = pages.target(urllib.parse.parse_qs(parsed.query).get("to", ["/"])[0])
            if self.headers.get("Host", "").startswith(NAME + ":"):
                return self._send(302, b"", extra={"Location": to})
            return self._own(lambda nonce: pages.go_page(app.port, to, nonce), images=f"http://{NAME}:{app.port}")
        if path in ("/welcome", "/welcome/"):
            app.refresher.seen()
            facts = read_json(app.place.welcome)
            return self._own(lambda nonce: pages.welcome_page(facts, app.port, nonce))
        self._file(path)

    def _file(self, url_path: str) -> None:
        app = self.app
        relative = urllib.parse.unquote(url_path).lstrip("/") or "index.html"
        if relative.endswith("/"):
            relative += "index.html"
        parts = PurePosixPath(relative).parts
        if any(p in ("..", ".") or p.startswith(".") or "\\" in p or (NT and ":" in p) for p in parts) or \
                any(ord(c) < 32 for c in relative):
            return self._send(404, pages.not_found_page())
        root = app.mirror.dest.resolve()
        try:
            real = root.joinpath(*parts).resolve()
            found = (real == root or root in real.parents) and real.is_file()
        except (OSError, ValueError):
            found = False
        name = parts[-1] if parts else "index.html"
        own = len(parts) == 1 and name in OWN_PAGES
        if own:
            app.refresher.seen()
            if not found:
                if name == "guide.html" and (root / "index.html").is_file():
                    return self._send(200, pages.older_cluster_page())
                what = "Your dashboard" if name == "index.html" else "The cluster guide"
                return self._send(200, pages.waiting_page(app.refresher.status(), what))
        if not found:
            return self._send(404, pages.not_found_page())
        kind = TYPES.get(Path(name).suffix.lower(), "application/octet-stream")
        csp = OWN_CSP if own else (OTHER_CSP if kind.startswith(("text/html", "image/svg")) else "")
        try:
            if own or real.stat().st_size <= STREAM_ABOVE:
                data = real.read_bytes()
                if own and name == "index.html":
                    bar = pages.status_bar(app.refresher.status())
                    at = data.rfind(b"</body>")
                    data = (data[:at] + bar + data[at:] if at >= 0 else data + bar) if bar else data
                return self._send(200, data, kind, csp)
            with real.open("rb") as handle:  # a large report or notebook: streamed, not held in memory
                self._head(200, kind, os.fstat(handle.fileno()).st_size, csp)
                if self.command != "HEAD":
                    shutil.copyfileobj(handle, self.wfile, 1 << 16)
        except OSError:  # gone or replaced by a refresh meanwhile
            if not self.wfile.closed:
                self.close_connection = True

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= 4096 or not self._host_ok() or self._other_site():
            # (a body it did not read must not be taken for the next request)
            return self._send(403 if 0 <= length <= 4096 else 413, b"", "text/plain", extra={"Connection": "close"})
        self.rfile.read(length)
        if self.path == "/_schub/refresh" and self.headers.get("X-Schub-View") == "1":  # (a form elsewhere cannot)
            self.app.refresher.seen()
            self.app.refresher.poke()
            return self._json({"ok": True})
        if self.path == "/_schub/quit" and secrets.compare_digest(self.headers.get("X-Schub-Token", ""),
                                                                   self.app.token):
            self._json({"ok": True})
            threading.Thread(target=self.app.stop, daemon=True).start()
            return None
        return self._send(403, b"", "text/plain")


class ViewServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not NT  # on Windows it would let two programs share a port

    def __init__(self, address: tuple, app: "App", family: int = socket.AF_INET) -> None:
        self.address_family = family
        self.app = app
        super().__init__(address, Handler)

    def server_bind(self) -> None:
        if self.address_family == socket.AF_INET6:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        if NT and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # nobody else may bind this port while it is ours
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)  # (HTTPServer's own would look the name up in DNS)
        self.server_name, self.server_port = "localhost", self.server_address[1]


def bind(app: "App", preferred: int) -> list[ViewServer]:
    """127.0.0.1 and, where the computer has it, [::1] on the same port (sc-hub.localhost may resolve to either):
    the preferred port, the next ones, then any free one."""
    candidates = [preferred + i for i in range(PORTS_TRIED)] + [0] if preferred else [0]
    for port in candidates:
        try:
            first = ViewServer(("127.0.0.1", port), app)
        except OSError:
            continue
        chosen = first.server_address[1]
        try:
            return [first, ViewServer(("::1", chosen, 0, 0), app, socket.AF_INET6)]
        except OSError as exc:
            if exc.errno in NO_IPV6:
                return [first]  # no IPv6 loopback on this computer
            first.server_close()  # someone else has [::1] on this port: sc-hub.localhost could reach them
    raise OSError("no free port on 127.0.0.1")


def proof(token: str, nonce: str) -> str:
    return hmac.new(token.encode(), nonce.encode(), hashlib.sha256).hexdigest()


class App:
    def __init__(self, place: Place, settings: Settings, log: Callable[[str], None] = print) -> None:
        self.place, self.settings, self.log = place, settings, log
        self.mirror = Mirror(place, settings, log)
        self.refresher = Refresher(self.mirror, settings.every, log)
        self.token = secrets.token_urlsafe(24)
        self.servers: list[ViewServer] = []
        self.port = 0
        self.done = threading.Event()

    def start(self, refresh: bool = True) -> None:
        self.servers = bind(self, self.settings.port)
        self.port = self.servers[0].server_address[1]
        for server in self.servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        if refresh:
            threading.Thread(target=self.refresher.run, daemon=True).start()

    def describe(self, nonce: str = "") -> dict[str, Any]:
        """What `status` shows; with a nonce, the proof that this server holds the token its home wrote down."""
        return {"app": APP, "version": VERSION, "pid": os.getpid(), "port": self.port, "url": f"http://{NAME}:{self.port}",
                "dir": str(self.mirror.dest), **({"proof": proof(self.token, nonce)} if nonce else {}),
                **self.refresher.status()}

    def stop(self) -> None:
        self.refresher.stopping.set()
        self.refresher.poke()
        self.mirror.cancel()  # an rsync or ssh running now stops too
        for server in self.servers:
            server.shutdown()
            server.server_close()
        self.done.set()


# ---- one server per home ------------------------------------------------------------------------------------------


Lock = FileLock  # the server holds its home's lock for its lifetime


def held(place: Place) -> bool:
    """A server of this home is alive, whether or not it answers."""
    probe = Lock(place.lock)
    if probe.acquire():
        probe.release()
        return False
    return True


LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 127.0.0.1 never through a proxy


def running(place: Place) -> dict[str, Any] | None:
    """The server of this home, if one answers and proves it holds the token its home wrote down (another program on
    that port, after a crash, cannot)."""
    state = read_json(place.state)
    try:
        port, token = int(state["port"]), str(state["token"])
    except (KeyError, TypeError, ValueError):
        return None
    nonce = secrets.token_hex(16)
    try:
        with LOCAL.open(f"http://127.0.0.1:{port}/_schub/status?nonce={nonce}", timeout=3) as response:
            info = json.loads(response.read())
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if not isinstance(info, dict) or info.get("app") != APP or \
            not hmac.compare_digest(str(info.get("proof", "")), proof(token, nonce)):
        return None
    return {**info, "token": token, "pid": state.get("pid")}


def serve(place: Place, settings: Settings, foreground: bool, refresh: bool = True) -> int:
    place.private()
    lock = Lock(place.lock)
    if not lock.acquire():
        print("sc-hub's dashboard is already running for this home: schub-view status")
        return 0
    log = _logger(place, foreground)
    app = App(place, settings, log)
    try:
        app.start(refresh)
    except OSError as exc:
        log(f"could not start: {exc}")
        return 1
    write_json(place.state, {"pid": os.getpid(), "port": app.port, "token": app.token, "version": VERSION,
                             "dir": str(app.mirror.dest), "started": _iso(time.time())})
    log(f"serving {app.mirror.dest} on http://{NAME}:{app.port} (and http://127.0.0.1:{app.port}), pid {os.getpid()}")
    if not NT:
        signal.signal(signal.SIGTERM, lambda *_: app.stop())
    try:
        while not app.done.wait(1.0):
            pass
    except KeyboardInterrupt:
        app.stop()
    finally:
        if read_json(place.state).get("pid") == os.getpid():
            place.state.unlink(missing_ok=True)
        log("stopped")
    return 0


def _logger(place: Place, foreground: bool) -> Callable[[str], None]:
    if foreground:
        return lambda line: print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)
    try:
        if place.log.stat().st_size > 256 * 1024:
            place.log.write_text(place.log.read_text(encoding="utf-8", errors="replace")[-64 * 1024:], encoding="utf-8")
    except OSError:
        pass

    def log(line: str) -> None:
        try:
            with place.log.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError:
            pass

    return log


def save_config(place: Place, **values: Any) -> None:
    config = read_json(place.config)
    changed = {k: v for k, v in values.items() if v not in (None, "") and config.get(k) != v}
    if changed:
        place.private()
        write_json(place.config, {**config, **changed})


def spawn(place: Place) -> None:
    """`serve` in the background, in a session of its own: closing the terminal or the assistant that started it
    does not stop it, and it holds none of the caller's pipes (a caller waiting for output would wait forever)."""
    place.private()
    args = [sys.executable, str(HERE / "schub_view.py"), "serve"] + (["--home", str(place.home)] if place.custom else [])
    log = place.log.open("ab")
    try:
        if NT:
            flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
                     | subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
            try:  # out of an assistant's job object too, where that is allowed
                subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                                 creationflags=flags | 0x01000000, cwd=str(place.home))  # CREATE_BREAKAWAY_FROM_JOB
            except OSError:
                subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                                 creationflags=flags, cwd=str(place.home))
        else:
            subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True,
                             start_new_session=True, cwd=str(place.home))
    finally:
        log.close()


def ensure(place: Place) -> dict[str, Any]:
    """The running server of this home, started (or replaced, when it is another version or stuck) if needed."""
    info = running(place)
    if info and info.get("version") == VERSION:
        return info
    if info or held(place):
        stop(place, quiet=True)
    if held(place):
        raise RuntimeError(f"an earlier dashboard process does not answer and still holds {place.lock}; stop it "
                           f"(its pid is in {place.state}) or restart the computer")
    spawn(place)
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        info = running(place)
        if info:
            return info
        time.sleep(0.2)
    tail = ""
    try:
        tail = place.log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1]
    except (OSError, IndexError):
        pass
    raise RuntimeError(f"the dashboard did not start within {START_WAIT_S} s ({tail or 'see ' + str(place.log)})")


def ours(pid: int) -> bool:
    """That process is this program (the pid its state file names), checked before it gets a signal."""
    try:
        text = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        try:
            text = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                  timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return False
    return "schub_view.py" in text and " serve" in text


def stop(place: Place, quiet: bool = False) -> int:
    """Asks the server to stop (with the token only its home can read); one that does not answer but still holds
    the lock gets SIGTERM (macOS, Linux), by the pid its state file names and only if that process is this program."""
    info = running(place)
    if not info and not held(place):
        if not quiet:
            print("sc-hub's dashboard is not running.")
        return 0
    asked = False
    if info:
        request = urllib.request.Request(f"http://127.0.0.1:{info['port']}/_schub/quit", data=b"{}", method="POST",
                                         headers={"X-Schub-Token": str(info.get("token", ""))})
        try:
            LOCAL.open(request, timeout=5).read()
            asked = True
        except (OSError, urllib.error.URLError):
            pass
    pid = read_json(place.state).get("pid")
    if not asked and not NT and isinstance(pid, int) and pid > 1 and ours(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and held(place):
        time.sleep(0.2)
    gone = not held(place)
    if not quiet:
        print("sc-hub's dashboard stopped." if gone else f"It did not stop; see {place.log}")
    return 0 if gone else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="schub-view", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("command", nargs="?", default="start", choices=("start", "open", "status", "stop", "serve"))
    parser.add_argument("--once", action="store_true", help="update the copy once and exit")
    parser.add_argument("--home", type=Path, help="another home folder (trials, tests)")
    parser.add_argument("--remote", help="sc-hub's folder on the cluster (remembered)")
    parser.add_argument("--alias", help="the ssh host of sc-hub's key (remembered)")
    parser.add_argument("--port", type=int, help=f"the port it tries first (remembered; default {PORT})")
    parser.add_argument("--no-open", action="store_true", help="start it without opening the browser")
    parser.add_argument("--no-refresh", action="store_true", help="serve: the folder as it is (tests, a copy to look at)")
    parser.add_argument("--json", action="store_true", help="start/status: one line of JSON")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 9):
        print("sc-hub's dashboard needs Python 3.9 or newer", file=sys.stderr)
        return 2
    place = Place(args.home)
    save_config(place, remote=args.remote, alias=args.alias, port=args.port)
    settings = Settings(place)
    if args.once:
        refresher = Refresher(Mirror(place, settings, print), settings.every)
        ok = refresher.once()
        print(f"updated {settings.dir}" if ok else f"could not update: {refresher.error}")
        return 0 if ok else 1
    if args.command == "serve":
        return serve(place, settings, foreground=bool(sys.stdin and sys.stdin.isatty()), refresh=not args.no_refresh)
    if args.command == "stop":
        return stop(place)
    if args.command == "status":
        info = running(place)
        if args.json:
            print(json.dumps({k: v for k, v in (info or {"running": False}).items() if k not in ("token", "proof")}))
        elif info:
            print(f"running (pid {info['pid']}): {info['url']} (also http://127.0.0.1:{info['port']})\n"
                  f"copy in {info['dir']}; last update {info['last_ok'] or 'not yet'}"
                  + (f"\nlast problem: {info['error']}" if info.get("error") else ""))
        else:
            print("not running: schub-view starts it")
        return 0 if info else 1
    try:
        info = ensure(place)
    except (OSError, RuntimeError) as exc:
        print(f"sc-hub's dashboard could not start: {exc}", file=sys.stderr)
        return 1
    port = int(info["port"])
    opener = f"http://127.0.0.1:{port}/go?to=%2F"
    if args.json:
        print(json.dumps({"url": info["url"], "port": port, "open": opener, "dir": info["dir"]}))
    else:
        print(f"sc-hub dashboard: {info['url']} (or http://127.0.0.1:{port})")
    if not args.no_open and not webbrowser.open(opener):
        print(f"open {info['url']} in your browser")
    return 0


if __name__ == "__main__":
    sys.exit(main())
