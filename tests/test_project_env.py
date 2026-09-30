from __future__ import annotations

import os

import pytest

from schub.project_env import EnvError, build_script, built, check_packages, kernel_ready
from schub.projects import ProjectStore
from schub.service import Hub, HubError
from schub.slurm import Slurm


@pytest.fixture
def hub(settings, cluster, ctx) -> Hub:
    (settings.library / "bin").mkdir()
    for tool in ("uv", "micromamba"):
        (settings.library / "bin" / tool).write_text("#!/bin/sh\n")
    hub = Hub(settings, Slurm(cluster))
    hub.create_project("crispr")
    return hub


def test_package_names_are_checked():
    check_packages(["harmonypy", "decoupler>=1.6,<2", "scib[rpy2]"], ["samtools=1.20"])
    for bad in (["git+https://evil/x"], ["../x"], ["a; rm -rf /"], ["x @ file:///etc"]):
        with pytest.raises(EnvError, match="not a package"):
            check_packages(bad, [])


def test_build_script_layers_on_the_shared_env(hub, settings):
    script = build_script(settings, "crispr/screen", ["harmonypy"], ["samtools"], "20260923-120000")
    assert "site.addsitedir" in script and "--no-deps" in script and "-c constraints.txt" in script
    assert "UV_TORCH_BACKEND=cu128" in script and "--channel bioconda" in script
    assert "--name schub-crispr.screen" in script and "'sc-hub: crispr/screen'" in script
    assert "^(-e |schub[ =@])" in script  # editable / local installs are no constraints
    # built in its own folder; the .ok marker comes last, after the kernel and `current` (undo puts them back)
    publish = [script.index(s) for s in ("-m ipykernel install", 'point "$(basename "$NEW")"', 'touch "$NEW/.ok"')]
    assert publish == sorted(publish)
    assert "micromamba" not in build_script(settings, "crispr", ["harmonypy"], [], "x")


def test_packages_leave_what_the_shared_environment_is_still_getting_to_it(hub, settings, tmp_path):
    import re
    import subprocess
    import sys

    lock = settings.root / "env-lock.txt"
    lock.write_text("scanpy==1.12.4\n    # via schub\ntorch==2.11.0+cu128\nscvi-tools==1.5.1\n")
    script = build_script(settings, "crispr", ["scvi-tools", "harmonypy"], [], "20260930-120000")
    assert "-c constraints.txt -c coming.txt" in script  # the versions the shared environment will have
    work = tmp_path / "build"
    work.mkdir()
    (work / "shared.txt").write_text("scanpy==1.12.4\n-e file:///src/sc-hub\n")  # torch not there yet
    (work / "resolved.txt").write_text("harmonypy==0.0.10\nscvi-tools==1.5.1\ntorch==2.11.0+cu128\n")
    coming, extra = re.findall(r"<<'PY'\n(.*?)\nPY", script, re.S)
    subprocess.run([sys.executable, "-", str(lock)], input=coming, text=True, cwd=work, check=True)
    assert (work / "coming.txt").read_text() == "torch==2.11.0+cu128\nscvi-tools==1.5.1\n"
    subprocess.run([sys.executable, "-"], input=extra, text=True, cwd=work, check=True, capture_output=True)
    assert (work / "extra.txt").read_text().split() == ["harmonypy==0.0.10"]  # not 6.5 GB of torch here as well
    lock.unlink()
    assert "-c coming.txt" not in build_script(settings, "crispr", ["harmonypy"], [], "x")  # all is there


def test_packages_build_on_what_is_installed_and_can_be_removed(hub, settings, cluster):
    job = hub.add_project_packages("crispr", pip=["harmonypy"])
    assert job.kernel == "sc-hub: crispr" and job.pip == ("harmonypy",) and "bash" in cluster.scripts[job.job_id]
    with pytest.raises(HubError, match="being built"):  # one build at a time: no request is lost
        hub.add_project_packages("crispr", pip=["decoupler"])
    cluster.jobs[job.job_id] = "FAILED"
    # the first build failed: a new request does not inherit its packages
    second = hub.add_project_packages("crispr", pip=["decoupler"])
    assert second.pip == ("decoupler",)
    cluster.jobs[second.job_id] = "COMPLETED"
    current = settings.root / "envs" / "crispr" / "20260923-120000"
    current.mkdir(parents=True)
    (current / "packages.json").write_text('{"pip": ["harmonypy", "decoupler"], "conda": ["samtools"], "built": "x"}')
    (settings.root / "envs" / "crispr" / "current").symlink_to("20260923-120000")
    assert built(settings, "crispr").conda == ("samtools",) and not kernel_ready(settings, "crispr")
    added = hub.add_project_packages("crispr", pip=["scib"])
    assert added.pip == ("harmonypy", "decoupler", "scib") and added.conda == ("samtools",)
    cluster.jobs[added.job_id] = "COMPLETED"
    dropped = hub.add_project_packages("crispr", pip=["decoupler"], remove=True)
    assert dropped.pip == ("harmonypy",)
    cluster.jobs[dropped.job_id] = "COMPLETED"
    gone = hub.add_project_packages("crispr", pip=["harmonypy", "decoupler"], conda=["samtools"], remove=True)
    assert gone.job_id == "" and not (settings.root / "envs" / "crispr").exists()  # moved aside, deleted in the background
    with pytest.raises(HubError, match="not a package"):
        hub.add_project_packages("crispr", pip=["x; y"])


FAKE_BUILD = """set -euo pipefail
echo "resolving {pip}"
mkdir -p {root}/{stamp}
printf '%s\\n' '{{"pip": {pip_json}, "conda": {conda_json}, "built": "{stamp}"}}' > {root}/{stamp}/packages.json
ln -sfn {stamp} {root}/current
"""


def fake_build(settings, project, pip, conda, stamp):  # the real one needs uv and the network
    import json

    root = settings.root / "envs" / project.replace("/", ".")
    if "broken" in pip:
        return "echo 'No solution found when resolving dependencies'; exit 1\n"
    return FAKE_BUILD.format(pip=" ".join(pip), pip_json=json.dumps(list(pip)), conda_json=json.dumps(list(conda)),
                             root=root, stamp=stamp)


def test_bench_packages_builds_in_the_cell_and_keeps_the_old_build_on_failure(hub, settings, monkeypatch, capsys):
    """From a bench cell: no job slot, the build's output in the cell, then the next cell's fresh kernel."""
    from schub import project_env
    from schub.bench import kernel_api

    monkeypatch.setattr(project_env, "build_script", fake_build)
    monkeypatch.setenv("SCHUB_ROOT", str(settings.root))
    monkeypatch.setenv("SCHUB_PROJECT", "crispr")
    assert kernel_api.packages() == {"pip": [], "conda": [], "built": ""}
    first = kernel_api.packages(pip=["decoupler>=2"])
    assert first["pip"] == ["decoupler>=2"] and "fresh kernel" in first["next"]
    assert "resolving decoupler>=2" in capsys.readouterr().out
    assert kernel_api.packages()["pip"] == ["decoupler>=2"]
    with pytest.raises(RuntimeError, match="No solution found"):
        kernel_api.packages(pip=["broken"])
    assert built(settings, "crispr").pip == ("decoupler>=2",)  # the previous build stays
    with pytest.raises(RuntimeError, match="not a package"):
        kernel_api.packages(pip=["x; rm -rf ~"])
    assert kernel_api.packages(pip="scvelo")["pip"] == ["decoupler>=2", "scvelo"]  # one name, not six letters
    monkeypatch.delenv("SCHUB_PROJECT")
    with pytest.raises(RuntimeError, match="bench cell"):
        kernel_api.packages(pip=["decoupler"])


def test_a_new_build_moves_the_projects_next_cell_to_a_fresh_kernel(hub, settings, monkeypatch):
    from types import SimpleNamespace

    from schub.bench import worker as worker_module
    from schub.bench.worker import ProjectWorker

    started = []

    class Kernel:
        def __init__(self, name, cwd, env, epoch):
            self.name, self.epoch, self.up = name, epoch, False

        def start(self):
            self.up = True
            started.append((self.name, self.epoch))

        def alive(self):
            return self.up

        def shutdown(self):
            self.up = False

    kernels = settings.root / "jupyter-kernels"  # stands in for ~/.local/share/jupyter/kernels
    monkeypatch.setattr(worker_module, "kernel_dir", lambda project: kernels / f"schub-{project}")
    monkeypatch.setattr(worker_module, "prime", lambda kernel: True)
    host = SimpleNamespace(settings=settings, job_id="9", kernel_factory=Kernel, now=lambda: "2026-09-25T00:00:00Z")
    worker = ProjectWorker(host, "crispr")  # type: ignore[arg-type]
    project_dir = settings.projects_dir / "crispr"
    first = worker._ensure_kernel(project_dir)
    assert first.name == "python3" and worker._ensure_kernel(project_dir) is first  # the same kernel, variables kept
    build = settings.root / "envs" / "crispr" / "20260925-000000"
    build.mkdir(parents=True)
    (build / "packages.json").write_text('{"pip": ["decoupler"], "conda": [], "built": "20260925-000000"}')
    (settings.root / "envs" / "crispr" / "current").symlink_to(build.name)
    (kernels / "schub-crispr").mkdir(parents=True)
    (kernels / "schub-crispr" / "kernel.json").write_text("{}")
    second = worker._ensure_kernel(project_dir)
    assert second is not first and not first.up and second.name == "schub-crispr"
    assert [e for _, e in started] == ["9.1", "9.2"]
    notes = [e.text for e in worker_module.Journal(project_dir, "crispr").entries(kinds=("incident",), limit=5)]
    assert any("environment changed" in n for n in notes)
    (build / "packages.json").unlink()
    (build / "packages.json").mkdir()  # reading it now fails (as a file-server error would): no restart
    assert worker._ensure_kernel(project_dir) is second and second.up


def test_a_kernel_started_while_the_build_was_unreadable_is_not_restarted_for_it(hub, settings, monkeypatch):
    from types import SimpleNamespace

    from schub.bench import worker as worker_module
    from schub.bench.worker import ProjectWorker

    class Kernel:
        def __init__(self, name, cwd, env, epoch):
            self.name, self.up = name, False

        def start(self):
            self.up = True

        def alive(self):
            return self.up

        def shutdown(self):
            self.up = False

    readings = iter([None, None, ("python3", "")])  # unreadable at start, then read fine
    monkeypatch.setattr(ProjectWorker, "_environment", lambda self: next(readings))
    monkeypatch.setattr(worker_module, "prime", lambda kernel: True)
    host = SimpleNamespace(settings=settings, job_id="9", kernel_factory=Kernel, now=lambda: "2026-09-25T00:00:00Z")
    worker = ProjectWorker(host, "crispr")  # type: ignore[arg-type]
    first = worker._ensure_kernel(settings.projects_dir / "crispr")
    assert worker._ensure_kernel(settings.projects_dir / "crispr") is first
    assert worker._ensure_kernel(settings.projects_dir / "crispr") is first and worker.kernel_env == ("python3", "")


def real_tools(settings, tmp_path, monkeypatch):
    """What the real build script needs, faked: uv (its venv's python registers kernels, as jupyter_client
    does: the old spec is removed first; exit 42 while `fail-kernel` exists), the shared python (this one),
    GNU mv -T (the cluster's) and a clock for distinct build stamps. Returns the failure switch."""
    import sys
    from datetime import datetime

    from schub import project_env

    failing = tmp_path / "fail-kernel"
    python = tmp_path / "venv-python"
    python.write_text(f"""#!/bin/sh
if [ "$1" = -m ]; then  # -m ipykernel install --user --name NAME --display-name ...
  rm -rf "$JUPYTER_DATA_DIR/kernels/$6"
  [ ! -e {failing} ] || {{ echo ipykernel_registration_failed >&2; exit 42; }}
  mkdir -p "$JUPYTER_DATA_DIR/kernels/$6" && printf '{{"argv": ["%s"]}}' "$0" > "$JUPYTER_DATA_DIR/kernels/$6/kernel.json"
fi
case "$2" in *purelib*) mkdir -p "$0-site" && echo "$0-site" ;; *scanpy*) echo "environment works on 3.12" ;; esac
""")
    uv = settings.library / "bin" / "uv"
    uv.write_text(f"""#!/bin/sh
if [ "$1" = venv ]; then for last; do :; done; mkdir -p "$last/bin" && cp {python} "$last/bin/python"; fi
if [ "$1 $2" = "pip compile" ]; then cp requested.txt resolved.txt; fi
exit 0
""")
    gnu = tmp_path / "gnu"
    gnu.mkdir()
    (gnu / "mv").write_text(f"""#!/bin/sh
if [ "$1" = -T ]; then exec {sys.executable} -c 'import os, sys; os.replace(*sys.argv[1:])' "$2" "$3"; fi
exec /bin/mv "$@"
""")
    for tool in (python, uv, gnu / "mv"):
        tool.chmod(0o755)
    settings.python.parent.mkdir(parents=True)
    settings.python.symlink_to(sys.executable)
    monkeypatch.setenv("PATH", f"{gnu}:{os.environ['PATH']}")
    monkeypatch.setenv("JUPYTER_DATA_DIR", str(tmp_path / "jupyter"))
    seconds = iter(range(1, 60))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 30, 0, 0, next(seconds), tzinfo=tz)

    monkeypatch.setattr(project_env, "datetime", Clock)
    return failing


def test_a_failed_kernel_registration_keeps_the_working_environment(hub, settings, tmp_path, monkeypatch):
    """The generated build, run: `ipykernel install` failing after a good build left `current` (so built() and
    the build the worker reported) on the new build while kernel.json still started the old one."""
    import json
    from types import SimpleNamespace

    from schub.bench.kernels import DEFAULT_KERNEL
    from schub.bench.worker import ProjectWorker
    from schub.project_env import build_here, env_root, kernel_dir

    failing = real_tools(settings, tmp_path, monkeypatch)
    worker = ProjectWorker(SimpleNamespace(settings=settings), "crispr")  # type: ignore[arg-type]
    root = env_root(settings, "crispr")

    def state():  # what `current`, kernel.json and the worker each say
        spec = kernel_dir("crispr") / "kernel.json"
        return (os.readlink(root / "current") if (root / "current").is_symlink() else None,
                json.loads(spec.read_text())["argv"][0] if spec.is_file() else None, worker._environment())

    failing.touch()
    with pytest.raises(EnvError, match="exit 42"):  # the very first build
        build_here(settings, "crispr", ["harmonypy"], [])
    assert state() == (None, None, (DEFAULT_KERNEL, "")) and built(settings, "crispr") is None
    failing.unlink()
    first = build_here(settings, "crispr", ["harmonypy"], [])
    working = (first.built, str(root / first.built / "venv" / "bin" / "python"), ("schub-crispr", first.built))
    assert state() == working
    failing.touch()
    with pytest.raises(EnvError, match="keeps its previous kernel"):
        build_here(settings, "crispr", ["harmonypy", "decoupler"], [])
    assert state() == working and built(settings, "crispr") == first  # all of it still the working build
    assert sorted(p.name for p in root.iterdir()) == [first.built, "current"]  # the failed one is gone
    failing.unlink()
    second = build_here(settings, "crispr", ["harmonypy", "decoupler"], [])
    assert state() == (second.built, str(root / second.built / "venv" / "bin" / "python"),
                       ("schub-crispr", second.built))


def test_conda_packages_bring_their_own_micromamba(settings, monkeypatch, tmp_path, capsys):
    """A student's own install has no micromamba: the first conda request installs it (checked), once."""
    from schub import project_env, seurat
    from schub.bench import kernel_api

    (settings.root / "bin").mkdir(parents=True, exist_ok=True)
    (settings.root / "bin" / "uv").write_text("#!/bin/sh\n")
    installer = tmp_path / "build_tools.sh"
    installer.write_text('[ "$SCHUB_TOOLS" = micromamba ] || exit 2\nmkdir -p "$1/bin" && touch "$1/bin/micromamba" '
                         '&& echo installed micromamba\n')
    monkeypatch.setattr(seurat, "BUILD_TOOLS", installer)
    monkeypatch.setattr(project_env, "build_script", fake_build)
    monkeypatch.setenv("SCHUB_ROOT", str(settings.root))
    monkeypatch.delenv("SCHUB_LIBRARY", raising=False)
    monkeypatch.setenv("SCHUB_PROJECT", "crispr")
    ProjectStore(settings).create("crispr")
    assert kernel_api.packages(conda="samtools")["conda"] == ["samtools"]
    assert "installed micromamba" in capsys.readouterr().out
    assert (settings.local_library / "bin" / "micromamba").exists()
    installer.write_text("echo must not run again; exit 1\n")
    assert kernel_api.packages(conda=["bedtools"])["conda"] == ["samtools", "bedtools"]


def test_builds_ignore_a_kernels_forced_colour_and_keep_its_path(hub, settings):
    """A Jupyter kernel sets FORCE_COLOR: uv then wrote escape codes into constraints.txt and every
    in-cell build failed; a conda kernel's own PATH used to drop the guards and sbatch."""
    script = build_script(settings, "crispr", ["harmonypy"], ["samtools"], "20260925-000000")
    assert "unset FORCE_COLOR CLICOLOR_FORCE; export NO_COLOR=1" in script.splitlines()[1]
    assert '--env PATH "$NEW/conda/bin:\\${PATH}"' in script


def test_a_queued_slurm_job_keeps_its_python_after_old_builds_go(settings, monkeypatch):
    from schub.bench import slurm_cell

    build = settings.root / "envs" / "crispr" / "20260925-000000" / "venv" / "bin"
    build.mkdir(parents=True)
    (build / "python").write_text("")
    (settings.root / "envs" / "crispr" / "current").symlink_to("20260925-000000")
    monkeypatch.setattr(slurm_cell.sys, "executable", str(build / "python"))
    assert slurm_cell._stable_python(settings, "crispr") == str(settings.root / "envs/crispr/current/venv/bin/python")
    monkeypatch.setattr(slurm_cell.sys, "executable", "/usr/bin/python3")
    assert slurm_cell._stable_python(settings, "crispr") == "/usr/bin/python3"


def test_the_project_kernel_is_looked_for_where_jupyter_installs_it(tmp_path, monkeypatch):
    from schub.project_env import kernel_dir

    monkeypatch.setenv("JUPYTER_DATA_DIR", str(tmp_path / "jupyter"))
    assert kernel_dir("crispr/screen") == tmp_path / "jupyter" / "kernels" / "schub-crispr.screen"
