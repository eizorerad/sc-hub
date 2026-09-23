"""One Jupyter kernel per project, started with an allowlisted environment.

Anything that looks like a secret never reaches the kernel: a cell that prints
os.environ must not put a session token or an API key into the chat.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

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
DEFAULT_KERNEL = "python3"


def kernel_env(base: Mapping[str, str], extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The kernel's environment: allowlisted names from `base`, plus `extra`; never secrets."""
    env = {
        key: value for key, value in base.items()
        if (key in ALLOW_EXACT or key.startswith(ALLOW_PREFIXES)) and not SECRET.search(key)
    }
    for key, value in (extra or {}).items():
        if SECRET.search(key):
            raise ValueError(f"refusing to pass {key} into a kernel")
        env[key] = value
    return env


def kernel_name(settings: Settings, project: str) -> str:
    """The project's own kernel if it was built (add_project_packages), else the shared one."""
    return f"schub-{slug(project)}" if kernel_ready(settings, project) else DEFAULT_KERNEL


class ProjectKernel:
    """A started kernel and its blocking client, for one project in one workbench job."""

    def __init__(self, name: str, cwd: Path, env: Mapping[str, str], epoch: str) -> None:
        self.name = name
        self.cwd = cwd
        self.env = dict(env)
        self.epoch = epoch
        self._manager = None
        self.client = None

    def start(self, timeout_s: float = 120) -> None:
        from jupyter_client import KernelManager

        self.cwd.mkdir(parents=True, exist_ok=True)
        manager = KernelManager(kernel_name=self.name)
        manager.start_kernel(cwd=str(self.cwd), env=self.env)
        client = manager.client()
        client.start_channels()
        try:
            client.wait_for_ready(timeout=timeout_s)
        except RuntimeError:
            client.stop_channels()
            manager.shutdown_kernel(now=True)
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
        self._manager = self.client = None
