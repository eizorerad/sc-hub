"""A paper's code in a cell: its repository at a recorded commit, and its own environment.

    repo = bench.clone("https://github.com/lab/model", ref="v1.2")        # work/repos/model
    python = bench.repo_env(repo, python="3.10", torch="2.4.1", cuda="cu121",
                            requirements="requirements.txt")
    %%slurm --gpus 1 --python <that python>                               # the paper's code, its env

The journal records the commit the ref resolved to (not the branch), so the run can
be repeated. Environments are built with uv once per spec (python, torch, CUDA,
requirements) under $SCHUB_ROOT/repo-envs/<hash>, shared by the student's projects,
with environment.lock (uv pip freeze) beside them. The nodes' driver runs CUDA up to
12.8, so torch comes from the cu118-cu128 builds; the default cu130 wheels import
fine but see no GPU.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence

from ..hashing import stable_hash
from ..locking import long_held
from ..project_env import EnvError, check_packages
from . import ledger
from .clock import stamp
from .fsio import write_json_atomic

CUDA_BUILDS = ("cpu", "cu118", "cu121", "cu124", "cu126", "cu128")
URL = re.compile(r"https://[A-Za-z0-9.-]+(:\d+)?/[A-Za-z0-9._~/+-]+")
REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+-]{0,199}")
PYTHON = re.compile(r"3\.\d{1,2}(\.\d{1,3})?")
TORCH = re.compile(r"\d{1,2}(\.\d{1,3}){0,2}")
CLONE_TIMEOUT_S = 1800
BUILD_TIMEOUT_S = 3600


class RepoError(RuntimeError):
    pass


def _git(repo: Path | None, *args: str, timeout: int = 300) -> str:
    command = ["git", *(("-C", str(repo)) if repo else ()), *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # a private repository fails instead of asking
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepoError(f"git {args[0]} failed: {exc}") from exc
    if done.returncode != 0:
        raise RepoError(f"git {args[0]} failed: {done.stderr.strip()[-600:]}")
    return done.stdout.strip()


def _check_url(url: str) -> None:
    if not URL.fullmatch(url):
        raise RepoError(f"{url!r} is not a plain https repository URL (no user, token, ssh or other schemes)")


def _default_dest(url: str) -> Path:
    from .kernel_api import work_dir

    name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    return work_dir() / "repos" / name


def _resolve(repo: Path, ref: str) -> str:
    """The commit a ref names: the remote branch first (a fetched branch moves), else a tag or commit."""
    for candidate in (f"origin/{ref}", ref):
        try:
            return _git(repo, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
        except RepoError:
            continue
    raise RepoError(f"'{ref}' is not a branch, tag or commit of {repo.name}")


def clone(url: str, ref: str | None = None, dest: str | os.PathLike | None = None) -> Path:
    """Clone (or update) the repository and check out `ref`; returns its folder."""
    _check_url(url)
    if ref is not None and not REF.fullmatch(ref):
        raise RepoError(f"{ref!r} is not a branch, tag or commit name")
    target = Path(dest) if dest else _default_dest(url)
    if (target / ".git").is_dir():
        origin = _git(target, "config", "--get", "remote.origin.url")  # as cloned, before any url rewriting
        if origin != url:
            raise RepoError(f"{target} holds another repository ({origin}); pass dest= for a new folder")
        if ref is not None and not _at(target, ref):  # replaying a setup cell: nothing to do
            # files the code wrote (checkpoints/, outputs) are untracked, not local changes to the code
            if _git(target, "status", "--porcelain", "--untracked-files=no"):
                raise RepoError(f"{target} has local changes; commit or copy them before checking out {ref}")
            _git(target, "fetch", "--quiet", "--tags", "origin", timeout=CLONE_TIMEOUT_S)
    elif target.exists() and any(target.iterdir()):
        raise RepoError(f"{target} exists and is not a git repository; pass dest= for a new folder")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        _git(None, "clone", "--quiet", "--", url, str(target), timeout=CLONE_TIMEOUT_S)
    if ref is not None:
        _git(target, "checkout", "--quiet", "--detach", _resolve(target, ref))
    commit = _git(target, "rev-parse", "HEAD")
    write_json_atomic(target / ".git" / "schub-repo.json", {"url": url, "ref": ref, "commit": commit, "at": stamp()})
    ledger.record("download", url=url, path=str(target), size=0, sha256="", commit=commit)
    print(f"{url} at commit {commit[:12]} -> {target}")
    return target


def _at(target: Path, ref: str) -> bool:
    """HEAD is already the commit `ref` names (no fetch needed). Only for a commit id: a branch may have moved."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", ref):
        return False
    try:
        return _git(target, "rev-parse", "HEAD") == _git(target, "rev-parse", f"{ref}^{{commit}}")
    except RepoError:
        return False


def _uv() -> Path:
    candidates = [os.environ.get("SCHUB_UV", "")]
    library = os.environ.get("SCHUB_LIBRARY", "")
    candidates += [str(Path(library) / "bin" / "uv")] if library else []
    candidates += [str(Path(os.environ.get("SCHUB_ROOT", "")) / "bin" / "uv"), shutil.which("uv") or ""]
    found = next((Path(c) for c in candidates if c and Path(c).is_file()), None)
    if found is None:
        raise RepoError("uv is not available (the shared library's bin/, $SCHUB_ROOT/bin or PATH)")
    return found


def _spec(repo: Path, python: str, torch: str | None, cuda: str, requirements: str | None, install_repo: bool,
          extra: Sequence[str]) -> tuple[dict, Path | None]:
    if cuda not in CUDA_BUILDS:
        raise RepoError(f"cuda={cuda!r}: the nodes' driver runs CUDA up to 12.8; use one of {', '.join(CUDA_BUILDS)} "
                        "(cu128 for recent torch)")
    if not PYTHON.fullmatch(python):
        raise RepoError(f"python={python!r} is not a version like 3.10")
    if torch is not None and not TORCH.fullmatch(torch):
        raise RepoError(f"torch={torch!r} is not a version like 2.4.1")
    try:
        check_packages(list(extra), [])
    except EnvError as exc:
        raise RepoError(str(exc)) from exc
    req_path = None
    if requirements is not None:
        req_path = (repo / requirements).resolve()
        if not req_path.is_relative_to(repo.resolve()) or not req_path.is_file():
            raise RepoError(f"requirements={requirements!r} must be a file inside the repository")
    local = install_repo or (req_path is not None and re.search(r"^\s*-e\s", req_path.read_text(), re.M))
    spec = {"python": python, "torch": torch, "cuda": cuda, "extra": sorted(extra),
            "requirements": stable_hash(req_path.read_text()) if req_path else None,
            # an editable install (install_repo, or '-e .' in the requirements) points at this checkout: two
            # projects' clones of one repo must not share it
            "install_repo": str(repo.resolve()) if local else None,
            # the repository's own package: its dependencies change with its packaging files
            "packaging": _packaging_hash(repo) if local else None}
    return spec, req_path


def _packaging_hash(repo: Path) -> str:
    files = [repo / name for name in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")]
    return stable_hash([f.read_text(errors="replace") if f.is_file() else None for f in files])


def environment(repo: str | os.PathLike, python: str = "3.11", torch: str | None = None, cuda: str = "cu128",
                requirements: str | None = None, install_repo: bool = False, extra: Sequence[str] = ()) -> Path:
    """The python of an environment for the repository's code (built once per spec)."""
    repo = Path(repo)
    spec, req_path = _spec(repo, python, torch, cuda, requirements, install_repo, extra)
    root = Path(os.environ.get("SCHUB_ROOT", Path.home() / "schub"))
    env = root / "repo-envs" / stable_hash(spec)
    env.parent.mkdir(parents=True, exist_ok=True)
    with long_held(env.parent / f"{env.name}.lock"):  # a second cell asking for the same spec waits, then reuses it
        if (env / "ready").exists():
            print(f"environment already built: {env / 'bin' / 'python'}")
            return env / "bin" / "python"
        shutil.rmtree(env, ignore_errors=True)  # an interrupted build
        try:
            _build(env, root, spec, req_path)
            commit = _git(repo, "rev-parse", "HEAD") if (repo / ".git").exists() else ""
            write_json_atomic(env / "spec.json", {**spec, "repo": str(repo), "commit": commit, "built": stamp()})
        except RepoError:
            shutil.rmtree(env, ignore_errors=True)
            raise
        (env / "ready").write_text(stamp())  # last: an environment without it is rebuilt
    print(f"environment for {repo.name}: {env / 'bin' / 'python'} (lock: {env / 'environment.lock'})")
    return env / "bin" / "python"


def _build(env: Path, root: Path, spec: dict, req_path: Path | None) -> None:
    uv = str(_uv())
    run_env = {**os.environ, "UV_CACHE_DIR": str(root / "cache" / "uv"),
               "UV_PYTHON_INSTALL_DIR": str(root / "python"), "UV_PYTHON_PREFERENCE": "managed"}
    python = str(env / "bin" / "python")

    cwd = req_path.parent if req_path else Path(spec["install_repo"] or env)  # "-e ." in a requirements file

    def uv_run(*args: str) -> str:
        try:
            done = subprocess.run([uv, *args], capture_output=True, text=True, env=run_env, timeout=BUILD_TIMEOUT_S,
                                  check=False, cwd=cwd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepoError(f"uv {args[0]} failed: {exc}") from exc
        if done.returncode != 0:
            raise RepoError(f"uv {' '.join(args[:2])} failed: {done.stderr.strip()[-1500:]}")
        return done.stdout

    uv_run("venv", "--python", spec["python"], str(env))  # the paths inside are absolute: cwd does not matter
    wanted = [f"torch=={spec['torch']}"] if spec["torch"] else []
    wanted += ["-r", str(req_path)] if req_path else []
    wanted += ["-e", spec["install_repo"]] if spec["install_repo"] else []
    wanted += list(spec["extra"])
    if wanted:
        uv_run("pip", "install", "--python", python, "--torch-backend", spec["cuda"], *wanted)
    (env / "environment.lock").write_text(uv_run("pip", "freeze", "--python", python))
