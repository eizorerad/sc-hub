"""One Jupyter kernel per project, started with an allowlisted environment.

Anything that looks like a secret never reaches the kernel's environment: a cell
that prints os.environ must not put a session token or an API key into the chat.
The runner itself re-executes with the same allowlist (runner_env), so reading
/proc/<parent>/environ gives nothing more. This covers environment variables only:
the kernel runs as the student, so files the student can read (~/.netrc, tokens
under ~/.cache) stay readable to their own code.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from ..config import Settings
from ..project_env import kernel_ready, slug

ALLOW_EXACT = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TZ", "TERM", "TMPDIR",
    "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMBA_NUM_THREADS",
    "PYTHONPATH", "PYTHONUNBUFFERED", "XDG_CACHE_HOME", "NUMBA_CACHE_DIR", "CELLTYPIST_FOLDER",
    "HF_HOME", "TORCH_HOME", "MAMBA_ROOT_PREFIX", "UV_CACHE_DIR", "VIRTUAL_ENV", "CONDA_PREFIX",
    "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CUDA_VISIBLE_DEVICES",
})
ALLOW_PREFIXES = ("LC_", "SLURM_", "SCHUB_")
SECRET = re.compile(r"TOKEN|SECRET|PASSW|CREDENTIAL|API_?KEY|_KEY$|COOKIE|AUTH", re.IGNORECASE)
PROXIES = frozenset({"http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"})
DEFAULT_KERNEL = "python3"
RUNNER_CLEAN = "SCHUB_RUNNER_CLEAN"


def _without_credentials(url: str) -> str:
    """http://user:pass@proxy:3128 -> http://proxy:3128."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def kernel_env(base: Mapping[str, str], extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The kernel's environment: allowlisted names from `base`, plus `extra`; never secrets."""
    env = {
        key: _without_credentials(value) if key in PROXIES else value for key, value in base.items()
        if (key in ALLOW_EXACT or key.startswith(ALLOW_PREFIXES)) and not SECRET.search(key)
    }
    for key, value in (extra or {}).items():
        if SECRET.search(key):
            raise ValueError(f"refusing to pass {key} into a kernel")
        env[key] = value
    return env


def runner_env(base: Mapping[str, str]) -> dict[str, str]:
    """The environment the runner re-executes with (see the module docstring)."""
    return kernel_env(base, {RUNNER_CLEAN: "1"})


def kernel_name(settings: Settings, project: str) -> str:
    """The project's own kernel if it was built (add_project_packages), else the shared one."""
    return f"schub-{slug(project)}" if kernel_ready(settings, project) else DEFAULT_KERNEL


class ProjectKernel:
    """A started kernel and its blocking client, for one project in one workbench job.

    The kernel talks over Unix sockets in a private local folder (0700), not TCP:
    compute nodes are shared, and ipykernel's TCP transport is unencrypted.
    """

    def __init__(self, name: str, cwd: Path, env: Mapping[str, str], epoch: str) -> None:
        self.name = name
        self.cwd = cwd
        self.env = dict(env)
        self.epoch = epoch
        self._manager = None
        self.client = None
        self._sockets: Path | None = None

    def start(self, timeout_s: float = 120) -> None:
        from jupyter_client import KernelManager

        self.cwd.mkdir(parents=True, exist_ok=True)
        self._sockets = Path(tempfile.mkdtemp(prefix="schub-k-", dir="/tmp" if Path("/tmp").is_dir() else None))
        manager = KernelManager(kernel_name=self.name, transport="ipc", ip=str(self._sockets / "k"))
        manager.start_kernel(cwd=str(self.cwd), env=self.env)
        client = manager.client()
        try:
            client.start_channels()
            client.wait_for_ready(timeout=timeout_s)
        except Exception:
            client.stop_channels()
            manager.shutdown_kernel(now=True)
            shutil.rmtree(self._sockets, ignore_errors=True)
            raise
        self._manager, self.client = manager, client

    def alive(self) -> bool:
        return self._manager is not None and bool(self._manager.is_alive())

    def interrupt(self) -> None:
        if self._manager is not None:
            self._manager.interrupt_kernel()

    def shutdown(self) -> None:
        if self.client is not None:
            self.client.stop_channels()
        if self._manager is not None:
            try:
                self._manager.shutdown_kernel(now=True)
            except Exception:  # noqa: BLE001 - a dead kernel must not stop the runner
                pass
        if self._sockets is not None:
            shutil.rmtree(self._sockets, ignore_errors=True)
        self._manager = self.client = None
        self._sockets = None
