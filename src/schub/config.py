"""Runtime settings resolved from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

DEFAULT_PARTITION = "ws-ia"
LUSTRE_USERS = Path("/l/users")


@dataclass(frozen=True)
class Limits:
    """Guard rails that apply to every plan, whoever (or whatever) submits it."""

    max_active_runs: int = 3
    max_steps: int = 12
    max_gpu_hours_per_plan: float = 24.0
    # QOS ia-std on ws-ia allows 2 running jobs, 24 CPUs and ~107 GB per user;
    # a single job above that would wait in the queue forever.
    max_cpus: int = 24
    max_mem_gb: int = 100
    max_time_min: int = 24 * 60


def readable_dir(path: Path | None) -> bool:
    """True if `path` is a directory this user can list and enter.

    Never raises: on Python 3.12 Path.is_dir() raises PermissionError when a
    parent directory cannot be entered, which is exactly the fallback case.
    """
    if path is None:
        return False
    try:
        return os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)
    except OSError:
        return False


@dataclass(frozen=True)
class Settings:
    """Where things live.

    `library` is the pilot owner's shared, read-only library (datasets, models,
    environments). It may be missing or unreadable for a given student; then
    everything falls back to `local_library` inside the student's own root.
    """

    root: Path
    python: Path
    library: Path | None = None
    partition: str = DEFAULT_PARTITION
    job_prefix: str = "schub"
    limits: Limits = field(default_factory=Limits)
    extra_roots: tuple[Path, ...] = ()

    @property
    def local_library(self) -> Path:
        return self.root / "library-local"

    @property
    def shared_library(self) -> Path | None:
        return self.library if readable_dir(self.library) else None

    @property
    def library_roots(self) -> tuple[Path, ...]:
        """Lookup order for datasets and models: shared first, then local."""
        shared = self.shared_library
        return (shared, self.local_library) if shared else (self.local_library,)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def projects_dir(self) -> Path:
        return self.root / "projects"

    @property
    def plans_dir(self) -> Path:
        return self.root / "plans"

    @property
    def runs_dir(self) -> Path:
        return self.root / "runs"

    @property
    def steps_dir(self) -> Path:
        return self.root / "cache" / "steps"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.root / ".cache"

    @property
    def view_dir(self) -> Path:
        return self.root / "view"

    @property
    def celltypist_home(self) -> Path:
        """Writable CELLTYPIST_FOLDER (the package creates folders on import)."""
        return self.local_library / "models" / "celltypist"

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        return (self.root, *self.library_roots, *self.extra_roots)


def _default_root(env: Mapping[str, str]) -> Path:
    user = env.get("USER", "")
    if user and LUSTRE_USERS.is_dir():
        return LUSTRE_USERS / user / "schub"
    return Path.home() / "schub"


def _number(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    root = Path(env.get("SCHUB_ROOT") or _default_root(env)).expanduser()
    library = env.get("SCHUB_LIBRARY")
    python = Path(env.get("SCHUB_PYTHON") or root / "env" / "bin" / "python")
    extra = tuple(
        Path(p).expanduser() for p in env.get("SCHUB_EXTRA_ROOTS", "").split(":") if p
    )
    limits = Limits(
        max_active_runs=int(_number(env, "SCHUB_MAX_ACTIVE_RUNS", Limits.max_active_runs)),
        max_gpu_hours_per_plan=_number(
            env, "SCHUB_MAX_GPU_HOURS", Limits.max_gpu_hours_per_plan
        ),
    )
    return Settings(
        root=root,
        python=python,
        library=Path(library).expanduser() if library else None,
        partition=env.get("SCHUB_PARTITION", DEFAULT_PARTITION),
        limits=limits,
        extra_roots=extra,
    )
