"""Interactive sessions on a compute node: JupyterLab (optionally with a GPU) and
cellxgene, reached from the laptop through an ssh tunnel via the login node.

Compute nodes accept no ssh logins, so the server listens on the node's address
and requires a random token. The token lives only in the session folder (0700,
connection.json 0600) and in the server's environment: never on a command line,
in a log, or in what the MCP tools return (that ends up in the chat transcript).
The laptop helper `schub-lab` reads it over ssh with `schub session-info` and
opens `ssh -N -L <port>:<node>:<port>` and the browser.

    python -m schub.sessions serve --kind jupyter [--target <path>]   (inside the job)
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Sequence

from .bricks import Resources
from .config import Settings
from .library import find_tool
from .slurm import ACTIVE_STATES, JobSpec, Slurm, render_script
from .state import Frozen

Kind = Literal["jupyter", "cellxgene"]
KINDS: tuple[str, ...] = ("jupyter", "cellxgene")
CONNECTION = "connection.json"
META = "session.json"
MAX_HOURS = 12
FINISHED_SHOWN_HOURS = 24
STARTUP_TIMEOUT_S = 300
CELLXGENE_LAUNCHER = Path(__file__).with_name("cellxgene_session.py")


class SessionError(ValueError):
    pass


class SessionInfo(Frozen):
    """What tools and the dashboard may show: everything except the token."""

    session_id: str
    kind: str
    state: str
    target: str = ""
    gpu: bool = False
    hours: int = 0
    started: str = ""
    node: str | None = None
    port: int | None = None
    how_to_open: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _write_private(path: Path, payload: dict) -> None:
    partial = path.with_name(f".{path.name}.partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(payload, handle)
    os.replace(partial, path)


class SessionStore:
    def __init__(self, settings: Settings, slurm: Slurm) -> None:
        self.settings = settings
        self.slurm = slurm
        self.dir = settings.root / "sessions"

    # ---- submitting side ---------------------------------------------------

    def start(self, kind: str, hours: int = 4, gpu: bool = False, target: str = "") -> SessionInfo:
        if kind not in KINDS:
            raise SessionError(f"unknown session kind '{kind}'; available: {', '.join(KINDS)}")
        if not 1 <= hours <= MAX_HOURS:
            raise SessionError(f"hours must be 1-{MAX_HOURS}")
        if kind == "cellxgene":
            gpu = False
            target = str(self._cellxgene_target(target))
        elif target:
            target = self._jupyter_target(target)
        running = [s for s in self.list() if s.kind == kind and s.state in ACTIVE_STATES]
        if running:
            raise SessionError(f"a {kind} session is already {running[0].state.lower()} (id {running[0].session_id}); stop it first")
        scripts = self.dir / "scripts"
        scripts.mkdir(parents=True, exist_ok=True, mode=0o700)
        resources = Resources(cpus=8, mem_gb=32, time_min=hours * 60, gpus=1 if gpu else 0) if kind == "jupyter" \
            else Resources(cpus=4, mem_gb=24, time_min=hours * 60)
        spec = JobSpec(
            name=f"{self.settings.job_prefix}-session-{kind}",
            partition=self.settings.partition,
            resources=resources,
            log_path=scripts / "session-%j.log",
            workdir=self.settings.root,
            command=(str(self.settings.python), "-m", "schub.sessions", "serve", "--kind", kind)
            + (("--target", target) if target else ()),
            env=(
                ("SCHUB_ROOT", str(self.settings.root)),
                ("SCHUB_LIBRARY", str(self.settings.library or "")),
                ("SCHUB_PYTHON", str(self.settings.python)),
                ("PYTHONUNBUFFERED", "1"),
            ),
        )
        script = scripts / f"{kind}-{secrets.token_hex(4)}.sbatch"
        script.write_text(render_script(spec))
        job_id = self.slurm.submit(script)
        try:
            folder = session_dir(self.settings, job_id)
            _write_private(folder / META, {"kind": kind, "target": target, "gpu": gpu, "hours": hours, "started": _now()})
        except OSError:
            self.slurm.cancel([job_id])  # an untracked session would hold a job slot unseen
            raise
        return self.info(job_id)

    def _cellxgene_target(self, target: str) -> Path:
        if not target:
            raise SessionError("cellxgene needs target=<.h5ad>, e.g. the cellxgene.h5ad from export_cellxgene")
        path = Path(target).expanduser()
        path = (path if path.is_absolute() else self.settings.root / path).resolve()
        roots = [r.resolve() for r in self.settings.allowed_roots if r.exists()]
        if path.suffix != ".h5ad" or not path.is_file() or not any(path.is_relative_to(r) for r in roots):
            raise SessionError(f"'{target}' is not an .h5ad file inside the sc-hub areas")
        if find_tool(self.settings.library_roots, "cellxgene", "bin/python") is None:
            raise SessionError("cellxgene is not installed in the library (tools/cellxgene); ask the library owner")
        return path

    def _jupyter_target(self, target: str) -> str:
        """A notebook or folder to open, relative to the workspace root JupyterLab serves."""
        root = self.settings.root.resolve()
        path = Path(target).expanduser()
        path = (path if path.is_absolute() else root / path).resolve()
        if not path.is_relative_to(root) or not path.exists():
            raise SessionError(f"'{target}' is not inside your sc-hub workspace ({root})")
        return path.relative_to(root).as_posix()

    def _meta(self, session_id: str) -> dict:
        folder = self.dir / session_id
        if not session_id.isdigit() or not (folder / META).is_file():
            raise SessionError(f"no session '{session_id}'")
        try:
            return json.loads((folder / META).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError(f"session '{session_id}' is being set up; try again") from exc

    def info(self, session_id: str, state: str | None = None) -> SessionInfo:
        meta = self._meta(session_id)
        if state is None:
            state = self.slurm.states([session_id]).get(session_id, "ENDED")
        info = SessionInfo(session_id=session_id, state=state, **meta)
        connection = _read_connection(self.dir / session_id) if state == "RUNNING" else None
        if connection is None:
            waiting = "waiting in the Slurm queue" if state == "PENDING" else (
                "starting" if state == "RUNNING" else f"not running ({state.lower()})")
            return info.model_copy(update={"how_to_open": f"The session is {waiting}."})
        return info.model_copy(update={
            "node": connection["node"], "port": connection["port"],
            "how_to_open": f"On the laptop: cd ~/sc-hub-workspace && ./schub-lab {meta['kind']} "
            f"(Windows: .\\schub-lab.cmd {meta['kind']}). It opens the tunnel and the browser.",
        })

    def list(self, queue: dict[str, str] | None = None) -> list[SessionInfo]:
        """Sessions still queued or running (however long they waited) and those started in
        the last day, from one squeue call (none when the dashboard passes its `queue`)."""
        if not self.dir.is_dir():
            return []
        if queue is None:
            queue = {j.job_id: j.state for j in self.slurm.active(self.settings.job_prefix) if "-session-" in j.name}
        horizon = time.time() - FINISHED_SHOWN_HOURS * 3600
        folders = []
        for folder in sorted(self.dir.iterdir(), key=lambda p: p.name, reverse=True):
            try:
                recent = folder.name.isdigit() and (folder / META).stat().st_mtime >= horizon
            except OSError:
                continue
            if recent or folder.name in queue:
                folders.append(folder.name)
        states = queue
        found = []
        for session_id in folders:
            try:
                found.append(self.info(session_id, states.get(session_id, "ENDED")))
            except SessionError:
                continue
        return found

    def stop(self, session_id: str) -> SessionInfo:
        info = self.info(session_id)
        if info.state in ACTIVE_STATES:
            self.slurm.cancel([session_id])
        return self.info(session_id)

    def connection_line(self, kind: str) -> str | None:
        """'<node> <port> <path with token>' of the newest running session, for schub-lab only."""
        running = next((s for s in self.list() if s.kind == kind and s.node), None)
        connection = _read_connection(self.dir / running.session_id) if running else None
        return f"{connection['node']} {connection['port']} {connection['path']}" if connection else None


def _read_connection(folder: Path) -> dict | None:
    try:
        data = json.loads((folder / CONNECTION).read_text())
        return {"node": str(data["node"]), "port": int(data["port"]), "path": str(data["path"])}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


# ---- inside the job ----------------------------------------------------------


def session_dir(settings: Settings, job_id: str) -> Path:
    """sessions/<job id>, private to the student (it holds the token)."""
    folder = settings.root / "sessions" / job_id
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("", 0))
        return int(probe.getsockname()[1])


def write_connection(folder: Path, node: str, port: int, path: str) -> None:
    _write_private(folder / CONNECTION, {"node": node, "port": port, "path": path})


def jupyter_command(python: str, port: int, root: Path, target: str) -> list[str]:
    command = [
        # The launcher moves the token from the environment into the server's configuration,
        # so kernels (and a cell printing os.environ) never see it.
        python, "-m", "schub.jupyter_launch", "--no-browser", "--ip=0.0.0.0", f"--port={port}",
        f"--ServerApp.root_dir={root}", "--ServerApp.port_retries=0",
        "--ServerApp.log_level=WARN",  # INFO prints the URL with the token into the Slurm log
    ]
    return command + ([target] if target else [])


def listener_uids(port: int, tables: tuple[str, ...] = ("/proc/net/tcp", "/proc/net/tcp6")) -> set[int] | None:
    """Owners of the sockets listening on `port` (Linux /proc), or None where unavailable."""
    owners: set[int] = set()
    found_table = False
    for table in tables:
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        found_table = True
        for line in lines:
            fields = line.split()
            if len(fields) > 7 and fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                owners.add(int(fields[7]))
    return owners if found_table else None


def wait_until_listening(process: subprocess.Popen, port: int, timeout_s: float = STARTUP_TIMEOUT_S) -> bool:
    """True once our own server listens on the port.

    The server is still alive (it would exit if the port were taken: port_retries=0)
    and every listener on the port belongs to this user: another user's process
    that grabbed the port during startup must never receive the token.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        except OSError:
            time.sleep(1)
            continue
        owners = listener_uids(port)
        return process.poll() is None and (owners is None or owners == {os.getuid()})
    return False


def serve(kind: str, target: str) -> int:
    from .config import load_settings

    settings = load_settings()
    folder = session_dir(settings, os.environ["SLURM_JOB_ID"])
    port, token = free_port(), secrets.token_urlsafe(24)
    node = socket.gethostname().split(".")[0]
    # The token travels in the environment, never on the command line (visible in ps).
    env = {**os.environ, "SCHUB_SESSION_TOKEN": token, "JUPYTER_TOKEN": token}
    if kind == "jupyter":
        command, path = jupyter_command(sys.executable, port, settings.root, target), f"/lab?token={token}"
    else:
        cellxgene = find_tool(settings.library_roots, "cellxgene", "bin/python")
        if cellxgene is None:
            raise SystemExit("cellxgene is not installed in the library")
        command, path = [str(cellxgene), str(CELLXGENE_LAUNCHER), target, str(port)], f"/?token={token}"
    (folder / CONNECTION).unlink(missing_ok=True)  # never point at a previous server
    process = subprocess.Popen(command, env=env)
    signal.signal(signal.SIGTERM, lambda *_: process.terminate())  # scancel / time limit
    if wait_until_listening(process, port):
        write_connection(folder, node, port, path)  # only now: the port is ours
    else:
        process.terminate()
    try:
        return process.wait()
    finally:
        (folder / CONNECTION).unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="schub.sessions")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("serve")
    run.add_argument("--kind", choices=KINDS, required=True)
    run.add_argument("--target", default="")
    args = parser.parse_args(argv)
    return serve(args.kind, args.target)


if __name__ == "__main__":
    raise SystemExit(main())
