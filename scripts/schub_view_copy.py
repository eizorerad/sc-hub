"""The copy of the dashboard on this computer, for schub_view.py: fetched from the cluster through sc-hub's own key.

`schub dashboard`, then a read-only rsync of <root>/view with the page last (an open page reloading in between never
meets project files that are not there yet); where rsync is missing (Windows), `view-sum` and a tar stream from
`view-pack`, the whole copy only when the checksum of its figures, notebooks and reports changed. The copy is only
ever written into a folder that has the marker file (or an empty one). Standard library only, Python 3.9+.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable

NT = os.name == "nt"
NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if NT else {}  # ssh without a console flash
MARKER = ".schub-view"
# Names Windows cannot hold (a ':' would even write into a hidden stream of another file): such a file is left out.
NOT_ON_WINDOWS = re.compile(r'[<>:"|?*\x00-\x1f]|[ .]$|^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$', re.I)


SOCKET_MAX = 104  # bytes in a Unix socket's path on macOS (Linux: 108)
COPY_WAIT_S = 120


class MirrorError(RuntimeError):
    pass


class FileLock:
    """An exclusive lock on a file, let go when closed or when the process ends, however it ends."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def acquire(self, wait: float = 0) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + wait
        while True:
            self.handle = open(self.path, "a+b")  # noqa: SIM115 - kept open on purpose
            try:
                if NT:
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                self.handle.close()
                self.handle = None
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)

    def release(self) -> None:
        if self.handle is None:
            return
        if NT:  # (Windows may keep a lock a while after its handle closes)
            import msvcrt

            try:
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        self.handle.close()
        self.handle = None


def explain(stderr: str, code: int) -> str:
    """One sentence a student can act on, from what ssh or the cluster said."""
    low = stderr.lower()
    last = next((line.strip() for line in reversed(stderr.strip().splitlines()) if line.strip()), "")[:200]
    if any(s in low for s in ("could not resolve", "name or service not known", "nodename nor servname",
                              "network is unreachable", "no route to host", "timed out", "connection refused")):
        return f"The cluster cannot be reached from this computer: on campus Wi-Fi or the VPN? ({last})"
    if "permission denied" in low:
        return "The cluster refused sc-hub's key: run the setup again (sh onboard/start.sh, then retry sign-in)."
    if "only opens sc-hub" in low:
        return ("The cluster's sc-hub refused this request; it may be older than this dashboard: run the setup's "
                "cluster step again (start.sh retry cluster).")
    if "operation not permitted" in low:
        return ("ssh was not allowed to run. If an assistant started the dashboard in its sandbox, run "
                "~/.sc-hub/bin/schub-view from a terminal instead.")
    if "host key verification failed" in low:
        return "The cluster's host key changed; ask the pilot owner before you trust it."
    if "disk quota" in low:
        return "Your disk quota on the cluster is full: free some space in /l/users (see the cluster overview)."
    if code == 127 or ("not found" in low and "ssh" in low):
        return "ssh is not installed on this computer (or not on PATH)."
    return last or f"the copy failed (exit code {code})"


def _rsync_word(arg: str) -> str:
    """rsync splits -e on spaces; it keeps double-quoted words together (a home with a space)."""
    return f'"{arg}"' if " " in arg else arg


class Mirror:
    """`place` and `settings` are schub_view's: where things live on this computer, and which cluster folder."""

    def __init__(self, place: Any, settings: Any, log: Callable[[str], None] = lambda line: None) -> None:
        self.place, self.settings, self.log = place, settings, log
        self.dest: Path = settings.dir
        self.heavy_sum = ""  # view-pack: the checksum the last full copy had
        self.child: subprocess.Popen | None = None  # the ssh or rsync running now (stopped with the server)
        self.cancelled = False

    def ssh_args(self) -> list[str]:
        args = [shutil.which("ssh") or "ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
        if self.place.custom:
            args += ["-F", str(self.place.ssh_config),
                     "-o", f"UserKnownHostsFile={self.place.home / '.ssh' / 'known_hosts'}"]
        prefix = self.place.home / ".ssh" / "cm-schub-"
        # One reused connection, where its socket's path fits: ssh writes %C as 40 characters and first binds a
        # temporary name 17 characters longer.
        if not NT and len(os.fsencode(str(prefix))) + 40 + 17 < SOCKET_MAX:
            args += ["-o", "ControlMaster=auto", "-o", f"ControlPath={prefix}%C", "-o", "ControlPersist=10m"]
        return args

    def _run(self, args: list[str], timeout: int, out: Any = subprocess.PIPE) -> str:
        if self.cancelled:
            raise MirrorError("stopped")
        try:
            self.child = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.PIPE,
                                          **NO_WINDOW)
        except OSError as exc:
            raise MirrorError(explain(str(exc), 127)) from None
        try:
            stdout, stderr = self.child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.communicate()
            raise MirrorError(f"the cluster did not answer within {timeout // 60} minutes") from None
        finally:
            code, self.child = self.child.returncode, None
        if self.cancelled:
            raise MirrorError("stopped")
        if code != 0:
            raise MirrorError(explain(stderr.decode(errors="replace"), code))
        return stdout.decode(errors="replace") if isinstance(stdout, bytes) else ""

    def cancel(self) -> None:
        """The server stops: the transfer running now stops too, and none starts after it."""
        self.cancelled = True
        child = self.child
        if child is not None and child.poll() is None:
            child.terminate()

    def _schub(self, words: str, timeout: int = 300, out: Any = subprocess.PIPE) -> str:
        return self._run(self.ssh_args() + [self.settings.alias, f"{self.settings.remote}/bin/schub {words}"],
                         timeout, out)

    def prepare(self) -> None:
        """Only a folder this created (or an empty one) is ever written; --delete must not reach anything else."""
        dest = self.dest
        if dest.is_dir() and not (dest / MARKER).exists() and any(dest.iterdir()):
            raise MirrorError(f"{dest} exists and is not sc-hub's copy of the dashboard; set SCHUB_VIEW_DIR to an "
                              "empty folder")
        dest.mkdir(parents=True, exist_ok=True)
        (dest / MARKER).touch()

    def refresh(self) -> None:
        """One update of the copy; one at a time on this computer (the server's and a `--once` never overlap)."""
        self.place.private()
        lock = FileLock(self.place.folder / "view-copy.lock")
        if not lock.acquire(wait=COPY_WAIT_S):
            raise MirrorError("another update of the copy is still running on this computer")
        try:
            self.prepare()
            if self.settings.transfer == "rsync":
                self._rsync()
            else:
                self._pack()
        finally:
            lock.release()

    def _rsync(self) -> None:
        self._schub("dashboard >/dev/null")
        shell = " ".join(_rsync_word(a) for a in self.ssh_args())
        source = f"{self.settings.alias}:{self.settings.remote}/view/"
        target = str(self.dest) + os.sep
        self._run(["rsync", "-a", "--delete", "--safe-links", "--exclude", MARKER, "--exclude", "/index.html",
                   "-e", shell, source, target], timeout=900)
        self._run(["rsync", "-a", "--safe-links", "--include", "/index.html", "--exclude", "*", "-e", shell, source,
                   target], timeout=300)

    def _pack(self) -> None:
        heavy = self._schub("view-sum").strip()
        full = heavy != self.heavy_sum or not (self.dest / "index.html").exists()
        incoming = self.dest.with_name(self.dest.name + ".incoming")
        archive = self.place.folder / f"view-{os.getpid()}.tar"
        self.place.private()
        try:
            with archive.open("wb") as handle:
                self._schub(f"view-pack {'full' if full else 'light'}", timeout=900, out=handle)
            shutil.rmtree(incoming, ignore_errors=True)
            names, skipped = unpack(archive, incoming)
            if "index.html" not in names:
                raise MirrorError("the cluster sent no dashboard page")
            if skipped:
                self.log(f"left out {skipped} file(s) whose names this computer cannot hold")
            swap(incoming, self.dest, names, full)
        except OSError as exc:
            raise MirrorError(f"could not update the copy on this computer: {exc}") from None
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(incoming, ignore_errors=True)
        if full:
            self.heavy_sum = heavy


def unpack(archive: Path, target: Path) -> tuple[set[str], int]:
    """Regular files and folders only, each inside `target`: the top-level names, and how many files were left out
    because this computer cannot name them. A path out of `target` refuses the whole archive."""
    target.mkdir(parents=True)
    names: set[str] = set()
    skipped = 0
    with tarfile.open(archive, "r:*") as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if not parts or member.name.startswith(("/", "\\")) or "\\" in member.name or \
                    any(p in ("..", "", ".") for p in parts):
                raise MirrorError(f"unsafe name in the dashboard archive: {member.name[:80]}")
            if not (member.isdir() or member.isfile()):
                continue  # links and devices: never
            if NT and any(NOT_ON_WINDOWS.search(p) for p in parts):
                skipped += member.isfile()
                continue
            path = target.joinpath(*parts)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                with path.open("wb") as handle:
                    shutil.copyfileobj(source, handle)  # type: ignore[arg-type]
            names.add(parts[0])
    return names, skipped


def swap(incoming: Path, dest: Path, names: set[str], full: bool) -> None:
    """Each top-level file or folder replaced as a whole, the page last; a full copy also drops what is gone."""
    for name in sorted(names - {"index.html"}):
        old = dest / name
        if old.is_dir() and not old.is_symlink():
            shutil.rmtree(old)
        elif old.exists() or old.is_symlink():
            old.unlink()
        os.replace(incoming / name, old)
    if full:
        for old in dest.iterdir():
            if old.name not in names | {MARKER, "index.html"}:
                shutil.rmtree(old) if old.is_dir() and not old.is_symlink() else old.unlink()
    os.replace(incoming / "index.html", dest / "index.html")
