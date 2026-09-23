"""The contract every engine adapter keeps: build argv, run with a time limit, parse
the result, and classify it the same way for both engines.

A turn runs one CLI process. Its stdout and stderr go to files in the turn's folder
(the evidence), and the outcome says: ok, usage_limited (pause the engine),
session_missing (start a new session with a hand-over), timed_out or failed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .cooldown import is_limit

Status = Literal["ok", "usage_limited", "session_missing", "timed_out", "failed"]
GUARD_DIR = Path(__file__).resolve().parent.parent / "guard"
CREDENTIALS = {"claude": (".claude/.credentials.json", ".claude.json"), "codex": (".codex/auth.json",)}


@dataclass(frozen=True)
class McpServer:
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Turn:
    prompt: str
    cwd: Path
    run_dir: Path
    timeout_s: int
    session_id: str | None = None  # resume this session
    new_session_id: str | None = None  # start a session with this id (engines that accept one)
    mcp: McpServer | None = None
    model: str = ""
    effort: str = ""


@dataclass(frozen=True)
class Outcome:
    engine: str
    status: Status
    session_id: str | None = None
    text: str = ""
    error: str = ""
    cost_usd: float | None = None
    turns: int | None = None
    returncode: int | None = None
    details: dict = field(default_factory=dict)


class Engine:
    name = ""

    def argv(self, binary: str, turn: Turn) -> list[str]:
        raise NotImplementedError

    def parse(self, stdout: str, stderr: str, returncode: int) -> Outcome:
        raise NotImplementedError

    def run(self, turn: Turn, binary: str | None = None, env: dict[str, str] | None = None) -> Outcome:
        binary = binary or str(find_binary(self.name))
        turn.run_dir.mkdir(parents=True, exist_ok=True)
        out_path, err_path = turn.run_dir / "stdout.txt", turn.run_dir / "stderr.txt"
        with out_path.open("w") as out, err_path.open("w") as err:
            try:
                process = subprocess.Popen(self.argv(binary, turn), cwd=turn.cwd, stdout=out, stderr=err,
                                           stdin=subprocess.DEVNULL, env=env, start_new_session=True)
            except OSError as exc:
                return Outcome(self.name, "failed", error=f"could not start {binary}: {exc}")
            timed_out = _wait(process, turn.timeout_s)
        stdout, stderr = out_path.read_text(errors="replace"), err_path.read_text(errors="replace")
        if timed_out:
            parsed = self.parse(stdout, stderr, process.returncode if process.returncode is not None else -9)
            return Outcome(self.name, "timed_out", session_id=parsed.session_id or turn.session_id or
                           turn.new_session_id, text=parsed.text, error=f"stopped after {turn.timeout_s} s",
                           returncode=process.returncode)
        return self.parse(stdout, stderr, process.returncode)


def _wait(process: subprocess.Popen, timeout_s: int) -> bool:
    """True if the turn ran out of time: SIGTERM to its process group, SIGKILL 20 s later."""
    try:
        process.wait(timeout=timeout_s)
        return False
    except subprocess.TimeoutExpired:
        pass
    for sig, grace in ((signal.SIGTERM, 20), (signal.SIGKILL, 10)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=grace)
            break
        except subprocess.TimeoutExpired:
            continue
    return True


def classify(ok: bool, error: str, missing_session: bool) -> Status:
    if ok:
        return "ok"
    if missing_session:
        return "session_missing"
    return "usage_limited" if is_limit(error) else "failed"


def install_guards(bench_dir: Path) -> Path:
    """The guards as executable files under the bench folder (an installed wheel may drop the exec bit)."""
    target = bench_dir / "guard"
    target.mkdir(parents=True, exist_ok=True)
    for engine in ("claude", "codex"):
        source, path = (GUARD_DIR / engine).read_text(), target / engine
        if not path.exists() or path.read_text() != source:
            temp = target / f".{engine}.tmp"
            temp.write_text(source)
            temp.chmod(0o755)
            temp.replace(path)
    return target


def _is_guard_dir(entry: str) -> bool:
    return Path(entry).parts[-2:] == ("bench", "guard")


def find_binary(engine: str) -> Path:
    """The real CLI: $SCHUB_<ENGINE>_BIN, else PATH without the guard folders, else ~/.local/bin."""
    explicit = os.environ.get(f"SCHUB_{engine.upper()}_BIN", "")
    if explicit:
        return Path(explicit)
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep) if p and not _is_guard_dir(p))
    found = shutil.which(engine, path=path)
    if found:
        return Path(found)
    local = Path.home() / ".local" / "bin" / engine
    if local.is_file():
        return local
    raise FileNotFoundError(f"{engine} is not installed for this account (log in on the cluster first)")


def version(binary: str) -> str:
    try:
        done = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (done.stdout or done.stderr).strip().splitlines()[0][:80] if (done.stdout or done.stderr).strip() else ""


def credential_fingerprint(engine: str) -> str:
    """Which login an engine used, without reading it: size and modification time of its files, hashed."""
    parts = []
    for relative in CREDENTIALS.get(engine, ()):
        path = Path.home() / relative
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append(f"{relative}:{stat.st_size}:{stat.st_mtime_ns}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12] if parts else ""


def elapsed(start: float) -> float:
    return round(time.monotonic() - start, 1)
