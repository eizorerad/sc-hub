"""A paper's code in a cell: its repository at a recorded commit, and its own environment.

    repo = bench.clone("https://github.com/lab/model", ref="v1.2")        # work/repos/model
    python = bench.repo_env(repo, python="3.10", torch="2.4.1", cuda="cu121",
                            requirements="requirements.txt")
    %%slurm --gpus 1 --python <that python>                               # the paper's code, its env

The journal records the commit the ref resolved to (not the branch), so the run can
be repeated. Environments are built with uv once per spec (python, torch, CUDA,
requirements with the files they include and the local packages they install) under
$SCHUB_ROOT/repo-envs/<hash>, shared by the student's projects, with environment.lock
(uv pip freeze) beside them. The nodes' driver runs CUDA up to 12.8, so torch comes
from the cu118-cu128 builds; the default cu130 wheels import fine but see no GPU.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urlsplit

from ..hashing import file_fingerprint, stable_hash
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
# Requirements-file lines (pip's and uv's syntax) that bring in more than their own text; a short option's
# value may also be attached, as pip reads it (-rbase.txt).
INCLUDE = re.compile(r"(?:-[rc]\s*=?|--(?:requirement|constraint)(?:\s*=|\s))\s*(\S.*)")
EDITABLE = re.compile(r"(?:-e\s*=?|--editable(?:\s*=|\s))\s*(\S+)")
LINKS = re.compile(r"(?:-f\s*=?|--find-links(?:\s*=|\s))\s*(\S+)")
INDEX = re.compile(r"(?:-i\s*=?|--(?:index-url|extra-index-url)(?:\s*=|\s))\s*(\S+)")
NAMED = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\s*(\[[^\]]*\])?\s*@\s*(\S+)")  # name @ url
UNSET = re.compile(r"\$\{[A-Z0-9_]+\}")  # left by _filled: a variable that is not set
ARCHIVES = (".whl", ".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")


class RepoError(RuntimeError):
    pass


class Unfollowable(RepoError):
    """The requirements include a file by URL, which no spec can follow: no new environment. `before` is the
    spec sc-hub gave such files before it followed includes, when that covered all the rest (else None)."""

    def __init__(self, message: str, before: dict | None) -> None:
        super().__init__(message)
        self.before = before


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
    brought = _brought(req_path, repo.resolve(), req_path.parent, {req_path}) if req_path else []
    if local:  # this checkout installed editable is in the spec already (install_repo, packaging below)
        brought = [b for b in brought if b != [str(repo.resolve()), _packaging_hash(repo)]]
    spec = {"python": python, "torch": torch, "cuda": cuda, "extra": sorted(extra),
            # the text, as sc-hub had it before it followed what the file brings in (added below)
            "requirements": stable_hash(req_path.read_text()) if req_path else None,
            # an editable install (install_repo, or '-e .' in the requirements) points at this checkout: two
            # projects' clones of one repo must not share it
            "install_repo": str(repo.resolve()) if local else None,
            # the repository's own package: its dependencies change with its packaging files
            "packaging": _packaging_hash(repo) if local else None}
    by_url = [b[1] for b in brought if isinstance(b, tuple)]
    if by_url:  # an environment built before still serves its checkout when that spec covered all the rest
        raise Unfollowable(by_url[0], spec if len(by_url) == len(brought) else None)
    # with what the file brings in; one that brings in nothing hashes as before: its environment stays
    return {**spec, "requirements": stable_hash(req_path.read_text(), *brought) if req_path else None}, req_path


def _brought(req_path: Path, repo: Path, cwd: Path, seen: set[Path]) -> list:
    """What a requirements file brings in besides its own text: the files it includes (_included), and what it
    takes from this computer (relative to where uv runs, `cwd`: see _build): packages by _local_package,
    --find-links folders by _local_links. A local package index is refused."""
    found: list = []
    for line in _statements(req_path.read_text()):
        include, links, index = INCLUDE.fullmatch(line), LINKS.match(line), INDEX.match(line)
        editable = EDITABLE.match(line)
        if include:
            found += _included(include[1], req_path, repo, cwd, seen)
        elif links and _is_local(_filled(links[1]), bare_is_path=True):
            found.append(_local_links(_filled(links[1]), cwd))
        elif index and _is_local(_filled(index[1]), bare_is_path=True):
            raise RepoError(f"{req_path.name}: {line!r} is a package index on this computer, which the environment's "
                            "spec cannot follow: pass requirements= a copy of the requirements, in the repository, "
                            "that reads its package files with --find-links <their folder> instead")
        elif editable or not line.startswith("-"):
            named = NAMED.match(line)
            target = _filled(editable[1] if editable else named[2] if named else re.split(r"[\s;]", line)[0])
            if _is_local(target, bare_is_path=bool(editable)):
                found.append(_local_package(target, cwd))
    return found


def _included(text: str, req_path: Path, repo: Path, cwd: Path, seen: set[Path]) -> list:
    """An -r/-c file, relative to the one including it and inside the repository: its content and what it
    brings in, once (a loop ends). By URL it cannot be followed: ("url", what to do instead)."""
    value = _filled(text)
    if value.lower().startswith(("http://", "https://")):
        return [("url", f"{req_path.name} includes {text!r} by URL, whose content the environment's spec cannot "
                        "follow: save it in the repository (bench.fetch(url, dest=...)) and pass requirements= a copy "
                        "of the requirements, also in the repository, that includes the saved file instead")]
    path = (req_path.parent / _file_path(value)).resolve()
    if not path.is_file():
        raise RepoError(f"{req_path.name} includes {text!r}, which is not a file")
    if not path.is_relative_to(repo):
        raise RepoError(f"{req_path.name} includes {text!r}, which is outside the repository: copy it into the "
                        "repository and pass requirements= a copy of the requirements, also in the repository, that "
                        "includes that copy instead")
    if path in seen:
        return []
    seen.add(path)
    return [stable_hash(path.read_text()), *_brought(path, repo, cwd, seen)]


def _statements(text: str) -> list[str]:
    """A requirements file's lines as pip and uv read them: continuations joined, comments dropped."""
    lines = (re.sub(r"(^|\s)#.*", "", line).strip() for line in re.sub(r"\\\r?\n", "", text).splitlines())
    return [line for line in lines if line]


def _filled(value: str) -> str:
    """${VARS} filled in from the environment, as uv does; only to find files (a message shows the text: a
    variable may hold a token)."""
    return re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: os.environ.get(m[1], m[0]), value)


def _is_local(target: str, bare_is_path: bool = False) -> bool:
    """A path, an archive or a file: URL (after -e, -f or -i any value without a URL scheme is a path:
    uv reads '-e name' as './name'). With a variable that is not set it names nothing: uv, run with the
    same variables, cannot read a folder through it either."""
    if UNSET.search(target):
        return False
    scheme = re.match(r"([A-Za-z][A-Za-z0-9+.-]*):", target)
    if scheme:
        return scheme[1].lower() == "file"
    return bare_is_path or target.startswith((".", "/", "~")) or "/" in target or target.lower().endswith(ARCHIVES)


def _local_package(target: str, cwd: Path) -> list:
    """A package installed from this computer: a folder by its path and packaging files (as install_repo),
    an archive by its path, size and time."""
    path = (cwd / _file_path(re.sub(r"\[[^\]]*\]$", "", target))).resolve()  # without [extras]
    return [str(path), file_fingerprint(path) if path.is_file() else _packaging_hash(path)]


def _local_links(value: str, cwd: Path) -> list:
    """A --find-links on this computer: a folder by the package files in it (uv looks no deeper), a page by its
    path, size and time."""
    path = (cwd / _file_path(value)).resolve()
    files = sorted(p for p in path.iterdir() if p.name.lower().endswith(ARCHIVES)) if path.is_dir() else [path]
    return [str(path), [file_fingerprint(f) for f in files if f.is_file()]]


def _file_path(value: str) -> Path:
    """A path as written, or a file: URL's."""
    return Path(unquote(urlsplit(value).path) if value.lower().startswith("file:") else value).expanduser()


def _packaging_hash(repo: Path) -> str:
    files = [repo / name for name in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")]
    return stable_hash([f.read_text(errors="replace") if f.is_file() else None for f in files])


def environment(repo: str | os.PathLike, python: str = "3.11", torch: str | None = None, cuda: str = "cu128",
                requirements: str | None = None, install_repo: bool = False, extra: Sequence[str] = ()) -> Path:
    """The python of an environment for the repository's code (built once per spec)."""
    repo = Path(repo)
    root = Path(os.environ.get("SCHUB_ROOT", Path.home() / "schub"))
    try:
        spec, req_path = _spec(repo, python, torch, cuda, requirements, install_repo, extra)
    except Unfollowable as exc:  # no new environment; a replayed cell keeps the one built for this checkout
        before = root / "repo-envs" / stable_hash(exc.before) if exc.before else None
        if before is None or not _built_for(before, repo):
            raise
        print(f"environment already built for this checkout: {before / 'bin' / 'python'} "
              f"(a new one would be refused: {exc})")
        return before / "bin" / "python"
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


def _built_for(env: Path, repo: Path) -> bool:
    """A finished environment built for this checkout, at the commit it is at now (its spec.json)."""
    try:
        record = json.loads((env / "spec.json").read_text())
        commit = _git(repo, "rev-parse", "HEAD") if (repo / ".git").exists() else ""
    except (OSError, ValueError, RepoError):
        return False
    return ((env / "ready").exists() and bool(record.get("repo")) and record.get("commit") == commit
            and Path(record["repo"]).resolve() == repo.resolve())


def _build(env: Path, root: Path, spec: dict, req_path: Path | None) -> None:
    uv = str(_uv())
    run_env = {**{k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "CLICOLOR_FORCE")},
               "NO_COLOR": "1",  # a kernel forces colour: uv would write escape codes into environment.lock
               "UV_CACHE_DIR": str(root / "cache" / "uv"),
               "UV_PYTHON_INSTALL_DIR": str(root / "python"), "UV_PYTHON_PREFERENCE": "managed"}
    python = str(env / "bin" / "python")

    # "-e ." in a requirements file; without one, a folder that exists (uv venv makes env) and, like the
    # spec, belongs to no checkout
    cwd = req_path.parent if req_path else Path(spec["install_repo"] or env.parent)

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
