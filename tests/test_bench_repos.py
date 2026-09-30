"""A paper's repository at a recorded commit, and its own environment (fake uv)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from schub.bench import ledger
from schub.bench.repos import RepoError, clone, environment

URL = "https://example.org/lab/model.git"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path, monkeypatch) -> Path:
    """A local repository that `https://example.org/lab/model.git` is rewritten to."""
    source = tmp_path / "origin"
    source.mkdir()
    git("init", "-q", "-b", "main", cwd=source)
    (source / "train.py").write_text("print('v1')\n")
    (source / "requirements.txt").write_text("numpy\n")
    git("add", ".", cwd=source)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "v1", cwd=source)
    git("tag", "v1", cwd=source)
    (source / "train.py").write_text("print('v2')\n")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-am", "v2", cwd=source)
    config = tmp_path / "gitconfig"
    config.write_text(f'[url "file://{source}"]\n\tinsteadOf = {URL}\n[protocol "file"]\n\tallow = always\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("SCHUB_PROJECT_DIR", str(tmp_path / "project"))
    ledger.drain()
    return source


def test_clone_records_the_commit_not_the_branch(origin: Path, tmp_path: Path) -> None:
    repo = clone(URL, ref="v1")
    assert repo == tmp_path / "project" / "work" / "repos" / "model"
    assert (repo / "train.py").read_text() == "print('v1')\n"
    v1 = git("rev-parse", "v1", cwd=origin)
    [event] = ledger.drain()
    assert event == {"kind": "download", "url": URL, "path": str(repo), "size": 0, "sha256": "", "commit": v1}
    record = json.loads((repo / ".git" / "schub-repo.json").read_text())
    assert (record["url"], record["ref"], record["commit"]) == (URL, "v1", v1)
    again = clone(URL, ref="main")  # the same folder moves to the asked ref
    assert again == repo and (repo / "train.py").read_text() == "print('v2')\n"


def test_clone_refuses_other_urls_and_a_foreign_folder(origin: Path, tmp_path: Path) -> None:
    for bad in ("http://example.org/x.git", "git@github.com:x/y.git", "https://user:tok@github.com/x/y.git",
                "file:///etc", "https://github.com/x/y.git --upload-pack=evil"):
        with pytest.raises(RepoError):
            clone(bad)
    clone(URL)
    with pytest.raises(RepoError, match="another repository"):
        clone("https://example.org/other/model.git")


def test_local_changes_are_not_thrown_away(origin: Path) -> None:
    repo = clone(URL)
    (repo / "train.py").write_text("print('patched')\n")
    with pytest.raises(RepoError, match="local changes"):
        clone(URL, ref="v1")
    assert (repo / "train.py").read_text() == "print('patched')\n"


@pytest.fixture
def fake_uv(tmp_path: Path, monkeypatch) -> Path:
    log = tmp_path / "uv.log"
    uv = tmp_path / "bin" / "uv"
    uv.parent.mkdir()
    uv.write_text(f"""#!/bin/sh
echo "$(pwd) :: $@" >> {log}
if [ "$1" = venv ]; then
  for last; do :; done
  mkdir -p "$last/bin" && ln -sf {sys.executable} "$last/bin/python"
fi
if [ "$1" = pip ] && [ "$2" = freeze ]; then echo "torch==2.4.1+cu121"; echo "numpy==2.0.0"; fi
exit 0
""")
    uv.chmod(0o755)
    monkeypatch.setenv("SCHUB_UV", str(uv))
    monkeypatch.setenv("SCHUB_ROOT", str(tmp_path / "root"))
    return log


def test_environment_is_built_once_per_spec_with_a_supported_cuda(origin: Path, fake_uv: Path, tmp_path: Path) -> None:
    repo = clone(URL, ref="v1")
    python = environment(repo, python="3.10", torch="2.4.1", cuda="cu121", requirements="requirements.txt")
    env = python.parent.parent
    assert env.parent == tmp_path / "root" / "repo-envs"
    calls = [c.split(" :: ", 1)[1] for c in fake_uv.read_text().splitlines()]
    assert calls[0].startswith("venv --python 3.10")
    install = next(c for c in calls if c.startswith("pip install"))
    assert all(c.startswith(f"{repo} :: ") for c in fake_uv.read_text().splitlines())  # "-e ." means this repo
    assert "--torch-backend cu121" in install and "torch==2.4.1" in install and f"-r {repo}/requirements.txt" in install
    assert "torch==2.4.1+cu121" in (env / "environment.lock").read_text()
    spec = json.loads((env / "spec.json").read_text())
    assert spec["commit"] == git("rev-parse", "v1", cwd=origin) and spec["cuda"] == "cu121"
    assert environment(repo, python="3.10", torch="2.4.1", cuda="cu121", requirements="requirements.txt") == python
    assert len(fake_uv.read_text().splitlines()) == len(calls)  # built once


def test_environment_refuses_cuda_the_driver_cannot_run(origin: Path, fake_uv: Path) -> None:
    repo = clone(URL)
    with pytest.raises(RepoError, match="cu128"):
        environment(repo, torch="2.9.0", cuda="cu130")
    with pytest.raises(RepoError, match="not a package"):
        environment(repo, extra=["numpy; rm -rf /"])
    with pytest.raises(RepoError, match="inside the repository"):
        environment(repo, requirements="../../etc/passwd")
    assert not fake_uv.exists()


def test_an_editable_repo_gets_a_new_environment_when_its_packaging_changes(origin: Path, fake_uv: Path) -> None:
    repo = clone(URL)
    (repo / "requirements.txt").write_text("-e .\nnumpy\n")
    (repo / "pyproject.toml").write_text('[project]\nname = "model"\ndependencies = ["scanpy"]\n')
    first = environment(repo, requirements="requirements.txt")
    assert environment(repo, requirements="requirements.txt") == first
    (repo / "pyproject.toml").write_text('[project]\nname = "model"\ndependencies = ["scanpy", "torch"]\n')
    assert environment(repo, requirements="requirements.txt") != first


def test_outputs_in_the_checkout_do_not_block_a_replayed_clone(origin: Path, tmp_path: Path) -> None:
    v1 = git("rev-parse", "v1", cwd=origin)
    repo = clone(URL, ref=v1)
    (repo / "checkpoints").mkdir()
    (repo / "checkpoints" / "epoch1.pt").write_text("weights")  # what the paper's code writes
    assert clone(URL, ref=v1) == repo  # a setup cell replayed after a kernel restart
    assert clone(URL, ref="main") == repo  # untracked outputs are not local changes to the code
    (repo / "train.py").write_text("print('edited')\n")
    with pytest.raises(RepoError, match="local changes"):
        clone(URL, ref="v1")


def test_an_editable_install_is_not_shared_between_checkouts(tmp_path: Path) -> None:
    from schub.bench.repos import _spec

    for name in ("a", "b"):
        folder = tmp_path / name / "model"
        folder.mkdir(parents=True)
        (folder / "requirements.txt").write_text("-e .\nnumpy\n")
        (folder / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    first, _ = _spec(tmp_path / "a" / "model", "3.11", None, "cu128", "requirements.txt", False, ())
    second, _ = _spec(tmp_path / "b" / "model", "3.11", None, "cu128", "requirements.txt", False, ())
    assert first != second  # the same files, but each env points at its own checkout


def test_an_environment_without_requirements_is_built_too(origin: Path, fake_uv: Path) -> None:
    """bench.repo_env(repo), or with only torch= or extra=: uv was started in the environment's own
    folder, which did not exist yet, and never ran (ENOENT)."""
    from schub.bench.kernel_api import repo_env

    repo = clone(URL)
    for options in ({}, {"torch": "2.4.1"}, {"extra": ["numpy"]}):
        env = repo_env(repo, **options).parent.parent
        assert (env / "ready").is_file() and "numpy==2.0.0" in (env / "environment.lock").read_text()
    calls = [c.split(" :: ", 1)[1] for c in fake_uv.read_text().splitlines()]
    assert [c.split()[0] for c in calls].count("venv") == 3
    installs = [c for c in calls if c.startswith("pip install")]
    assert len(installs) == 2 and installs[0].endswith(" torch==2.4.1") and installs[1].endswith(" numpy")


def test_files_the_requirements_include_are_part_of_the_spec(origin: Path, fake_uv: Path, tmp_path: Path) -> None:
    """A changed -r/-c file got the first environment back: the key hashed only the file named."""
    repo = clone(URL)
    (repo / "reqs").mkdir()
    (repo / "requirements.txt").write_text("-r reqs/base.txt\n")
    (repo / "reqs" / "base.txt").write_text("numpy\n-c pins.txt\n-r ../requirements.txt\n")  # beside it; a loop
    (repo / "reqs" / "pins.txt").write_text("numpy==1.26.4\n")
    first = environment(repo, requirements="requirements.txt")
    assert environment(repo, requirements="requirements.txt") == first  # nothing changed: reused
    (repo / "reqs" / "pins.txt").write_text("numpy==2.0.0\n")
    second = environment(repo, requirements="requirements.txt")
    assert second != first
    other = tmp_path / "other" / "model"  # another project's checkout
    shutil.copytree(repo, other)
    assert environment(other, requirements="requirements.txt") == second  # the same full spec: shared
    (other / "reqs" / "base.txt").write_text("numpy\nscipy\n-c pins.txt\n")  # the same top file
    assert environment(other, requirements="requirements.txt") not in (first, second)
    assert fake_uv.read_text().count(" :: venv ") == 3


def test_local_packages_in_the_requirements_are_part_of_the_spec(tmp_path: Path) -> None:
    """'.', '--editable .', './pkg', file: URLs and archives install from the checkout; only '-e ' reached the
    key, so another checkout (or changed packaging) got the first environment back."""
    from schub.bench.repos import _spec

    installs = [(".", "pyproject.toml"), ("--editable .", "pyproject.toml"), ("-r more.txt", "pyproject.toml"),
                ("--editable=./pkg", "pkg/setup.py"), ("-e ./pkg", "pkg/setup.py"), ("./pkg[extra]", "pkg/setup.py"),
                ("pkg @ file://{here}/pkg", "pkg/setup.py"),
                ("dist/pkg-1.0-py3-none-any.whl", "dist/pkg-1.0-py3-none-any.whl")]
    for n, (line, packaging) in enumerate(installs):
        specs = []
        for name in ("a", "b"):
            here = tmp_path / str(n) / name / "model"
            (here / "pkg").mkdir(parents=True)
            (here / "dist").mkdir()
            (here / "pyproject.toml").write_text('[project]\nname = "model"\n')
            (here / "pkg" / "setup.py").write_text("from setuptools import setup\nsetup()\n")
            (here / "dist" / "pkg-1.0-py3-none-any.whl").write_bytes(b"wheel")
            (here / "more.txt").write_text("--editable .\n")  # what `-r more.txt` installs
            (here / "requirements.txt").write_text(line.format(here=here) + "\nnumpy\n")
            specs.append(_spec(here, "3.11", None, "cu128", "requirements.txt", False, ())[0])
        assert specs[0] != specs[1], line  # the same files in another checkout
        (here / "README.md").write_text("notes\n")  # nothing the line installs
        (here / "outputs").mkdir()
        assert _spec(here, "3.11", None, "cu128", "requirements.txt", False, ())[0] == specs[1], line
        (here / packaging).write_bytes((here / packaging).read_bytes() + b"# with a new dependency\n")
        assert _spec(here, "3.11", None, "cu128", "requirements.txt", False, ())[0] != specs[1], line


def test_requirements_the_spec_cannot_follow_are_refused(origin: Path, fake_uv: Path, monkeypatch) -> None:
    repo = clone(URL)
    (repo.parent / "outside.txt").write_text("numpy\n")
    monkeypatch.setenv("PIP_TOKEN", "s3cret")
    for line, reason in (("-r ../outside.txt", "inside the repository"), ("--constraint missing.txt", "inside the"),
                         ("-c https://${PIP_TOKEN}@example.org/pins.txt", "inside the repository"),
                         ("--find-links ./wheels", "local folder"), ("-i file:///srv/index", "local folder")):
        (repo / "requirements.txt").write_text(f"numpy\n{line}\n")
        with pytest.raises(RepoError, match=reason) as refused:
            environment(repo, requirements="requirements.txt")
        assert "s3cret" not in str(refused.value)  # the file's text, not what a variable holds
    assert not fake_uv.exists()


def test_plain_requirements_keep_their_environment(tmp_path: Path) -> None:
    """Environments built before includes and local packages counted are still found for files that have
    neither (or only an editable '-e .', which was counted already)."""
    from schub.bench.repos import _spec
    from schub.hashing import stable_hash

    (tmp_path / "model").mkdir()
    for text in ("numpy\ntorch==2.4.1  # pinned\n--extra-index-url https://download.pytorch.org/whl/cu121\n",
                 "-e .\nnumpy\n"):
        (tmp_path / "model" / "requirements.txt").write_text(text)
        spec, _ = _spec(tmp_path / "model", "3.11", None, "cu128", "requirements.txt", False, ())
        assert spec["requirements"] == stable_hash(text)
