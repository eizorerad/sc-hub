"""The page on http://127.0.0.1:<port>/?t=<token> and its small JSON API.

Only this computer can reach it (127.0.0.1), and only with the random token in the link:
another program or a web page in the browser cannot drive the onboarding or read the
state. The Host header is checked too (DNS rebinding). Nothing is sent anywhere else.
"""

from __future__ import annotations

import http.server
import json
import secrets
import threading
import time
from typing import Any

from .engine import Engine
from .page import PAGE

MAX_BODY = 64 * 1024


class Handler(http.server.BaseHTTPRequestHandler):
    server: "OnboardServer"

    def log_message(self, *_: Any) -> None:  # the page polls every second; keep the terminal quiet
        pass

    def _allowed(self) -> bool:
        host = self.headers.get("Host", "")
        return host in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}")

    def _token_ok(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Onboard-Token", ""), self.server.token)

    def _send(self, code: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; "
                                                    "script-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode())

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        self.server.last_seen = time.time()
        if not self._allowed():
            return self._send(403, b"")
        if self.path.split("?")[0] == "/":
            query = self.path.partition("?")[2]
            if not secrets.compare_digest(query.removeprefix("t="), self.server.token):
                return self._send(403, b"Open the link the helper printed (it carries a one-time key).",
                                  "text/plain")
            return self._send(200, PAGE.replace("__TOKEN__", self.server.token).encode(), "text/html; charset=utf-8")
        if self.path.startswith("/api/") and not self._token_ok():
            return self._send(403, b"")
        if self.path == "/api/state":
            return self._json(self.server.engine.snapshot())
        self._send(404, b"")

    def do_POST(self) -> None:  # noqa: N802
        if not self._allowed() or not self._token_ok():
            return self._send(403, b"")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413, b"")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, b"")
        engine = self.server.engine
        if self.path == "/api/start":
            engine.start()
        elif self.path == "/api/answer":
            if not engine.answer(data if isinstance(data, dict) else {}):
                return self._json({"ok": False, "error": "nothing is being asked"}, 409)
        elif self.path == "/api/retry":
            engine.retry(str(data.get("step", "")) or None)
        elif self.path == "/api/quit":
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            return self._send(404, b"")
        self._json({"ok": True})


class OnboardServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, engine: Engine, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.engine = engine
        self.token = secrets.token_urlsafe(24)
        self.port = self.server_address[1]
        self.last_seen = time.time()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"
