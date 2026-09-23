"""A paper's repository at a recorded commit, and its own environment (fake uv)."""

from __future__ import annotations

import json
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
echo "$@" >> {log}
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
    calls = fake_uv.read_text().splitlines()
    assert calls[0].startswith("venv --python 3.10")
    install = next(c for c in calls if c.startswith("pip install"))
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
