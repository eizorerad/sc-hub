"""Serve one .h5ad with cellxgene behind a token check (runs with the cellxgene
tool environment's Python, so it imports nothing from sc-hub).

cellxgene has no authentication and compute nodes are shared, so every request
must carry the session token: `/?token=...` sets a cookie, later requests
present the cookie, anything else gets 403.

    python cellxgene_session.py <file.h5ad> <port>      (token in SCHUB_SESSION_TOKEN)
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import sys
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

COOKIE = "schub_session"


class TokenGate:
    """WSGI middleware: pass requests that prove they know the token."""

    def __init__(self, app, token: str) -> None:
        self.app = app
        self.token = token

    def _valid(self, value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value.encode(), self.token.encode())

    def __call__(self, environ, start_response):
        cookie = SimpleCookie(environ.get("HTTP_COOKIE", ""))
        if COOKIE in cookie and self._valid(cookie[COOKIE].value):
            return self.app(environ, start_response)
        query = parse_qs(environ.get("QUERY_STRING", ""))
        if self._valid((query.get("token") or [None])[0]):
            # Trade the token in the URL for a cookie, then drop it from the address bar.
            start_response("302 Found", [
                ("Location", environ.get("SCRIPT_NAME", "") + environ.get("PATH_INFO", "/")),
                ("Set-Cookie", f"{COOKIE}={self.token}; HttpOnly; SameSite=Strict; Path=/"),
            ])
            return [b""]
        start_response("403 Forbidden", [("Content-Type", "text/plain")])
        return [b"sc-hub: open this session with schub-lab (the link carries its token)\n"]


def build_app(datapath: str):
    from server.cli.launch import CliLaunchServer
    from server.common.config.app_config import AppConfig

    config = AppConfig()
    config.update_server_config(
        single_dataset__datapath=datapath,
        single_dataset__title=os.path.basename(datapath),
        app__flask_secret_key=secrets.token_hex(32),
    )
    # Annotations and saved gene sets would write files next to the data (and ask for a
    # folder first); in sc-hub cellxgene is for exploring.
    config.update_dataset_config(user_annotations__enable=False, user_annotations__gene_sets__readonly=True)
    config.complete_config(lambda message: print(f"[cellxgene] {message}", flush=True))
    return CliLaunchServer(config).app


def main(argv: list[str]) -> int:
    datapath, port = argv[1], int(argv[2])
    token = os.environ.pop("SCHUB_SESSION_TOKEN")
    app = build_app(datapath)
    app.wsgi_app = TokenGate(app.wsgi_app, token)
    # werkzeug logs each request line, and the first one carries ?token= (the Slurm log).
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    print(f"[sc-hub] cellxgene serving {datapath} on port {port}", flush=True)
    app.run(host="0.0.0.0", port=port, threaded=True, use_debugger=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
