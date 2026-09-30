"""The pages schub_view.py writes itself: the welcome after the setup, the address check (/go), a page while the
first copy is on its way, and the note over a copy that could not be updated. Standard library only, Python 3.9+.

Nothing from a request goes into a script: /go takes only a plain path (an allowlist) and hands it to its script in an
escaped attribute. Each page's script runs under a nonce of its own response, so injected markup cannot run.
"""

from __future__ import annotations

import html
import re
import struct
import zlib
from datetime import datetime
from typing import Any

NAME = "sc-hub.localhost"  # its own address: nothing it stores in the browser mixes with other local pages
PLAIN_PATH = re.compile(r"/(?!/)[A-Za-z0-9._~/-]*")  # where /go may lead: a path on this server, nothing else
STYLE = """<style>:root{--bg:#f7f6f2;--card:#fff;--text:#1f1e1d;--muted:#72716b;--line:#e8e6df;--soft:#efeee8;
--accent:#185fa5;--onaccent:#fff;--ok:#0f6e56;--warn:#854f0b;--warnbg:#faeeda}@media (prefers-color-scheme:dark){
:root{--bg:#1b1a19;--card:#242321;--text:#ecebe6;--muted:#a3a19b;--line:#34332f;--soft:#2c2b28;--accent:#85b7eb;
--onaccent:#0f2a44;--ok:#9fe1cb;--warn:#fac775;--warnbg:#44300a}}*{box-sizing:border-box}body{margin:0;
background:var(--bg);color:var(--text);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:720px;margin:0 auto;padding:36px 16px 64px}h1{font-size:24px;font-weight:600;margin:0 0 4px}
h2{font-size:16px;font-weight:600;margin:0 0 10px}.sub{color:var(--muted);margin:0 0 22px}.card{background:var(--card);
border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin:0 0 16px}ul,ol{margin:0;padding-left:20px}
li{margin:0 0 8px}li:last-child{margin:0}code{font:13px ui-monospace,Menlo,monospace;background:var(--soft);
padding:1px 5px;border-radius:5px;overflow-wrap:anywhere}a{color:var(--accent)}.muted{color:var(--muted)}
.row{display:flex;gap:10px;flex-wrap:wrap;margin:22px 0 0}.button{display:inline-block;padding:9px 18px;
border-radius:9px;border:1px solid var(--line);background:var(--card);color:var(--text);text-decoration:none}
.button.primary{background:var(--accent);border-color:var(--accent);color:var(--onaccent);font-weight:500}
.check{color:var(--ok)}.note{background:var(--warnbg);color:var(--warn);padding:10px 12px;border-radius:10px}
q{font-style:italic}</style>"""
# The note over an old copy: its button's script comes from this server (/_schub/bar.js), never inline, so it runs
# under the dashboard's own strict script policy too.
BAR_JS = """(() => {
  const button = document.getElementById('schub-retry');
  if (!button) return;
  button.addEventListener('click', () => {
    button.disabled = true;
    button.textContent = 'Trying…';
    fetch('/_schub/refresh', {method: 'POST', headers: {'X-Schub-View': '1'}})
      .then(() => setTimeout(() => location.reload(), 4000), () => { button.disabled = false; });
  });
})();
"""


# The pages that hold no script at all (waiting, not found, an older cluster's missing guide, the guide itself).
STATIC_CSP = ("default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
              "connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def policy(nonce: str, images: str = "") -> str:
    """The Content-Security-Policy of these pages: their own nonce'd script, nothing from elsewhere."""
    return (f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"
            f"{' ' + images if images else ''}; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'")


def target(value: str) -> str:
    """Where /go may lead: a plain path on this server (no scheme, host, '//', control or quote characters)."""
    return value if PLAIN_PATH.fullmatch(value) else "/"


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"\x00\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


PING = _png(3, 2)  # the /go page loads it from sc-hub.localhost: 3×2 says it is this server that answered


def page(title: str, body: str, nonce: str = "", script: str = "", refresh: int = 0) -> bytes:
    head = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    code = f'<script nonce="{nonce}">{script}</script>' if script and nonce else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">{head}'
            f"<title>{html.escape(title)}</title>{STYLE}</head><body><main>{body}</main>{code}</body></html>").encode()


def go_page(port: int, to: str, nonce: str, key: str) -> bytes:
    """Opens `to` at sc-hub.localhost when this browser reaches this server there, else at the address it came in on
    (the probe is a 3×2 image this server gives only to `key`, which only its own /go page shows)."""
    named = f"http://{NAME}:{port}"
    body = (f'<p class="muted" id="go" data-to="{html.escape(to)}" data-named="{html.escape(named)}" '
            f'data-probe="{html.escape(key)}">Opening your dashboard…</p>'
            f'<noscript><a href="{html.escape(to)}">Open the dashboard</a></noscript>')
    script = ("(()=>{const box=document.getElementById('go'),to=box.dataset.to,named=box.dataset.named;let done=false;"
              "const go=u=>{if(!done){done=true;location.replace(u);}};const probe=new Image();"
              "probe.onload=()=>go(probe.naturalWidth===3&&probe.naturalHeight===2?named+to:to);"
              "probe.onerror=()=>go(to);probe.src=named+'/_schub/ping.png?k='+encodeURIComponent(box.dataset.probe)+"
              "'&t='+Date.now();setTimeout(()=>go(to),2500);})();")
    return page("sc-hub", body, nonce, script)


def _say(text: Any) -> str:
    return html.escape(str(text))


def welcome_page(facts: dict[str, Any], port: int, nonce: str) -> bytes:
    """The short brief after the setup: what is installed now and how to work with it."""
    login = str(facts.get("login") or "")
    workspace = str(facts.get("workspace") or "~/sc-hub-workspace")
    windows = bool(facts.get("windows"))
    view = str(facts.get("view_command") or (r"%USERPROFILE%\.sc-hub\bin\schub-view.cmd" if windows
                                             else "~/.sc-hub/bin/schub-view"))
    root = str(facts.get("remote_root") or (f"/l/users/{login}/schub" if login else "/l/users/<login>/schub"))
    assistants = [str(a) for a in facts.get("assistants") or []]
    terminal = [a for a in assistants if a in ("Codex", "Claude Code")]
    agents = facts.get("cluster_agents") or {}
    on_path = sorted(str(n) for n in facts.get("cluster_path") or [])
    host = str(facts.get("host") or "login-student-lab.mbzu.ae")
    have = [f"<li><b>Your lab bench on the cluster</b>, in <code>{_say(root)}</code>: Python with the single-cell "
            "tools, starter datasets and a first project, <code>hello</code>. The deep-learning tools (torch, "
            "scvi-tools) finish installing in the background.</li>"]
    if assistants:
        desktop = " Restart Claude Desktop once to load it." if "Claude Desktop" in assistants else ""
        have.append(f"<li><b>{_say(' and '.join(assistants))}</b> work with sc-hub in <code>{_say(workspace)}</code>."
                    f"{desktop}</li>")
    if isinstance(agents, dict) and agents:
        labels = {"codex": "Codex", "claude": "Claude Code"}
        who = ", ".join(f"{_say(labels.get(k, k))} as {_say(v or 'you')}" for k, v in agents.items())
        login_line = ""
        if on_path:
            names = " and ".join(f"<code>{_say(n)}</code>" for n in on_path)
            login_line = (f" After you log in there (<code>ssh {_say(login or 'LOGIN')}@{_say(host)}</code>), type "
                          f"{names}.")
        have.append(f"<li><b>Codex and Claude Code on the cluster</b>, signed in: {who}. The lab agent uses them."
                    f"{login_line}</li>")
    if facts.get("vscode_host"):
        have.append(f"<li><b>VS Code</b>: Remote-SSH → <code>{_say(facts['vscode_host'])}</code> opens your projects "
                    "inside your workbench job.</li>")
    have.append(f'<li><b>This dashboard</b>, at <a href="/" class="here">http://{NAME}:{port}</a>: your projects, '
                "updated every minute while it is open. Bookmark it.</li>")
    first = "codex" if not terminal or terminal[0] == "Codex" else "claude"
    folder = f'"{workspace}"' if " " in workspace else workspace
    command = ("; " if windows else " && ").join([f"cd {folder}", first])
    other = " (or <code>claude</code>)" if len(terminal) > 1 else ""
    for_whom = f"Set up for {_say(login)}. " if login else ""
    body = (
        f'<h1><span class="check">✓</span> sc-hub is ready</h1><p class="sub">{for_whom}What you have now, and how to '
        f'use it.</p><div class="card"><h2>What you have</h2><ul>{"".join(have)}</ul></div>'
        '<div class="card"><h2>How to work</h2><ol>'
        f"<li>Open a terminal in your workspace: <code>{_say(command)}</code>{other}.</li>"
        "<li>Start sc-hub with <code>$schub</code> in Codex or <code>/schub</code> in Claude Code, then ask your "
        "question, for example: <q>Create a project for my question: how does the IFN-beta response differ between "
        "PBMC cell types? Start from Kang 2018.</q></li>"
        "<li>Follow it here: every step the assistant runs lands in the project's <b>Journal</b>, with its code, "
        "results and checks.</li></ol></div>"
        '<div class="card"><h2>Good to know</h2><ul>'
        "<li>Heavy work runs as Slurm jobs on the compute nodes, never on the login node; your assistant sends it "
        'there. New to the cluster? <a href="/guide.html">Read the cluster guide</a> (five minutes).</li>'
        f"<li>After a restart of this computer, run <code>{_say(view)}</code> in a terminal to bring this page back."
        "</li></ul></div>"
        '<div class="row"><a class="button primary" href="/">Open my dashboard</a>'
        '<a class="button" href="/guide.html">Cluster guide</a></div>'
    )
    # the address this browser really uses: the one to bookmark
    script = "for(const a of document.querySelectorAll('a.here'))a.textContent=location.origin;"
    return page("sc-hub is ready", body, nonce, script)


def waiting_page(state: dict[str, Any], what: str) -> bytes:
    detail = (f'<p class="note">{_say(state["error"])}</p><p class="muted">It tries again by itself; this page '
              "reloads.</p>" if state.get("error") else '<p class="muted">The first copy comes from the cluster in a '
              "minute or two; this page reloads by itself.</p>")
    return page("sc-hub", f"<h1>{_say(what)} is on its way</h1>{detail}", refresh=5)


def older_cluster_page() -> bytes:
    """The copy is there but has no guide: sc-hub on the cluster predates it."""
    return page("sc-hub", "<h1>The cluster guide comes with sc-hub's next update</h1><p>sc-hub on the cluster is older "
                          "than this dashboard. Ask your assistant to update it, or run the setup's cluster step again "
                          '(<code>start.sh retry cluster</code>).</p><p><a href="/">Your dashboard</a></p>')


def not_found_page() -> bytes:
    return page("Not found", "<h1>Not found</h1><p>It may be gone from the cluster since this page was loaded. "
                             '<a href="/">Your dashboard</a></p>')


def status_bar(state: dict[str, Any]) -> bytes:
    """Shown over the dashboard when its copy is old because the cluster could not be reached."""
    if not state.get("error") or not state.get("stale"):
        return b""
    when = datetime.fromisoformat(state["last_ok"]).strftime("%H:%M") if state.get("last_ok") else "earlier"
    return (
        '<div id="schub-local" role="status" style="position:fixed;right:16px;bottom:16px;z-index:99;max-width:min(460px,'
        "calc(100vw - 32px));padding:12px 14px;border-radius:12px;background:#faeeda;color:#5c3706;font:13px/1.5 "
        '-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;box-shadow:0 8px 24px rgba(0,0,0,.18)">'
        f"<b>Showing the copy from {_say(when)}.</b> {_say(state['error'])} "
        '<button type="button" id="schub-retry" style="font:inherit;margin-left:4px;padding:2px 10px;border-radius:7px;'
        'border:1px solid #c9a25b;background:#fff8ec;color:inherit;cursor:pointer">Try again</button></div>'
        '<script src="/_schub/bar.js"></script>'
    ).encode()
