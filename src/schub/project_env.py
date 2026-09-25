"""Per-project extra software, kept small and reproducible.

- pip packages: a thin venv layered on the shared environment (same Python; the
  shared packages stay visible through a .pth file). uv resolves the requested
  packages against the shared environment's exact versions and installs only
  what the shared environment lacks, so nothing is duplicated or upgraded under
  the pipelines' feet.
- conda packages (non-Python tools: samtools, bedtools, R packages...): a
  micromamba environment from conda-forge + bioconda, put on PATH.

The result is a Jupyter kernel "sc-hub: <project>"; the pipeline bricks keep
using the shared environment. The request is recorded in project.yaml
(`pip:` / `conda:`) and the build runs in a Slurm job (downloads, Lustre writes),
or, from the bench, in the cell that asks (`bench.packages(...)`, on the workbench's
compute node: no second job slot); the project's next cell then starts a fresh
kernel on the new build.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .bricks import Resources
from .config import Settings
from .slurm import JobSpec, Slurm, render_script
from .state import Frozen
from .streaming import run_streamed

# A requirement like "harmonypy", "decoupler>=1.6", "r-seurat=5.1" (no URLs or paths).
REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}(\[[A-Za-z0-9_,-]+\])?([<>=!~]=?[A-Za-z0-9.*+!-]{1,40}(,[<>=!~]=?[A-Za-z0-9.*+!-]{1,40})*)?$")
MAX_PACKAGES = 30


class EnvError(ValueError):
    pass


class EnvJob(Frozen):
    project: str
    job_id: str
    log: str
    env: str
    kernel: str
    pip: tuple[str, ...] = ()
    conda: tuple[str, ...] = ()
    note: str


class BuiltEnv(Frozen):
    pip: tuple[str, ...] = ()
    conda: tuple[str, ...] = ()
    built: str = ""
    shared_env: str = ""


def slug(project: str) -> str:
    """Folder / kernel name for a project path; '.' never occurs in project names."""
    return project.replace("/", ".")


def env_root(settings: Settings, project: str) -> Path:
    return settings.root / "envs" / slug(project)


def kernel_dir(project: str) -> Path:
    return Path.home() / ".local" / "share" / "jupyter" / "kernels" / f"schub-{slug(project)}"


def built(settings: Settings, project: str) -> BuiltEnv | None:
    """What the current build of the project's environment contains (None: never built)."""
    try:
        return BuiltEnv.model_validate_json((env_root(settings, project) / "current" / "packages.json").read_text())
    except (OSError, ValueError):
        return None


def merged(settings: Settings, project: str, pip: Sequence[str], conda: Sequence[str],
           remove: bool = False) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The project's packages after this request: built on what is installed now, never on an earlier
    request that failed; `remove` drops the listed ones."""
    current = built(settings, project)
    have_pip, have_conda = (current.pip, current.conda) if current else ((), ())
    if remove:
        new_pip = tuple(x for x in have_pip if x not in set(pip))
        new_conda = tuple(x for x in have_conda if x not in set(conda))
    else:
        new_pip, new_conda = tuple(dict.fromkeys((*have_pip, *pip))), tuple(dict.fromkeys((*have_conda, *conda)))
    check_packages(new_pip, new_conda)
    return new_pip, new_conda


def build_here(settings: Settings, project: str, pip: Sequence[str], conda: Sequence[str],
               out=None) -> BuiltEnv | None:
    """Build the project's environment in this process (a bench cell on a compute node), its output
    streamed to `out`. None when no packages are left (the shared kernel again)."""
    if not pip and not conda:
        remove_env(settings, project)
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    logs = settings.logs_dir / "envs"
    logs.mkdir(parents=True, exist_ok=True)
    script = logs / f"build-{slug(project)}-{stamp}.sh"
    script.write_text(build_script(settings, project, pip, conda, stamp))
    code, tail = run_streamed(["bash", str(script)], out, cwd=str(settings.root))
    if code != 0:
        raise EnvError(f"the build failed (exit {code}); the project keeps its previous kernel. Last lines:\n"
                       + "".join(tail))
    return built(settings, project)


def check_packages(pip: Sequence[str], conda: Sequence[str]) -> None:
    if len(pip) + len(conda) > MAX_PACKAGES:
        raise EnvError(f"at most {MAX_PACKAGES} packages per project")
    bad = [p for p in (*pip, *conda) if not REQUIREMENT.fullmatch(p)]
    if bad:
        raise EnvError(f"not a package name or version requirement: {bad}")


def _tool(settings: Settings, name: str) -> Path | None:
    """uv / micromamba from the shared library, else the student's own sc-hub bin/ or own library's bin/
    (where building R + Seurat put micromamba)."""
    roots = [settings.shared_library] if settings.shared_library else []
    candidates = [r / "bin" / name for r in roots] + [settings.root / "bin" / name,
                                                       settings.local_library / "bin" / name]
    return next((c for c in candidates if c.is_file()), None)


def kernel_ready(settings: Settings, project: str) -> bool:
    return built(settings, project) is not None and (kernel_dir(project) / "kernel.json").is_file()


def build_script(settings: Settings, project: str, pip: Sequence[str], conda: Sequence[str], stamp: str) -> str:
    """Bash that builds the environment in its own folder and switches `current` to it
    only when everything worked (a failed build never breaks the working kernel)."""
    uv = _tool(settings, "uv")
    mamba = _tool(settings, "micromamba")
    if uv is None:
        raise EnvError("uv is not available (shared library bin/ or your sc-hub bin/)")
    if conda and mamba is None:
        raise EnvError("micromamba is not available (a shared library's bin/ or your sc-hub bin/); conda "
                       "packages cannot be installed")
    root = env_root(settings, project)
    q = shlex.quote
    record = json.dumps({"pip": list(pip), "conda": list(conda), "built": stamp, "shared_env": str(settings.python)})
    lines = [
        "set -euo pipefail",
        # The shared torch is a CUDA 12.8 build (+cu128): resolve against the same index.
        f"export UV_CACHE_DIR={q(str(settings.cache_dir / 'uv'))} UV_PYTHON_PREFERENCE=only-managed UV_TORCH_BACKEND=cu128",
        f"ROOT={q(str(root))}; NEW=\"$ROOT\"/{q(stamp)}; BASE={q(str(settings.python))}; UV={q(str(uv))}",
        'trap \'[ -e "$NEW/.ok" ] || rm -rf "$NEW"\' EXIT',
        'mkdir -p "$NEW" && cd "$NEW"',
        '"$UV" venv --quiet --python "$BASE" "$NEW/venv"',
        # The shared packages stay importable (addsitedir also runs their own .pth
        # files); ours come first on sys.path.
        '"$BASE" -c "import sysconfig; print(sysconfig.get_paths()[\'purelib\'])" > base-site.txt',
        '"$NEW/venv/bin/python" -c "import sysconfig; print(sysconfig.get_paths()[\'purelib\'])" > own-site.txt',
        'printf "import site; site.addsitedir(%s)\\n" "$("$BASE" -c "import sys; print(repr(open(\'base-site.txt\').read().strip()))")" > "$(cat own-site.txt)/_schub_shared.pth"',
    ]
    if pip:
        lines += [
            '"$UV" pip freeze --python "$BASE" > shared.txt',
            # sc-hub itself (local or editable) and URL installs are no constraint for others.
            "grep -v -E '^(-e |schub[ =@])|@ (file|git\\+)' shared.txt > constraints.txt || true",
            f"printf '%s\\n' {' '.join(q(p) for p in pip)} > requested.txt",
            # Resolve against the shared pins, then install only what is new.
            '"$UV" pip compile --quiet --python "$NEW/venv/bin/python" requested.txt -c constraints.txt -o resolved.txt',
            '"$BASE" - <<\'PY\'\n'
            "import re\n"
            "name = lambda line: re.split(r'[=<>!~ ;\\[]', line.strip(), maxsplit=1)[0].lower().replace('_', '-')\n"
            "shared = {name(l) for l in open('shared.txt') if l.strip() and not l.startswith(('#', '-e'))}\n"
            "new = [l.strip() for l in open('resolved.txt') if l.strip() and not l.startswith(('#', ' ')) and name(l) not in shared]\n"
            "open('extra.txt', 'w').write('\\n'.join(new) + '\\n')\n"
            "print('installing', len(new), 'new packages:', ' '.join(new))\n"
            "PY",
            '[ ! -s extra.txt ] || "$UV" pip install --quiet --no-deps --python "$NEW/venv/bin/python" -r extra.txt',
        ]
    if conda:
        lines += [
            f"export MAMBA_ROOT_PREFIX={q(str(settings.cache_dir / 'mamba'))}",
            f"{q(str(mamba))} create --yes --quiet --prefix \"$NEW/conda\" --override-channels "
            f"--channel conda-forge --channel bioconda {' '.join(q(c) for c in conda)} >/dev/null",
            f"{q(str(mamba))} list --prefix \"$NEW/conda\" --explicit > conda-explicit.txt",
        ]
    display = f"sc-hub: {project}"
    path_env = ' --env PATH "$NEW/conda/bin:/usr/local/bin:/usr/bin:/bin"' if conda else ""
    lines += [
        '"$NEW/venv/bin/python" -c "import scanpy, sys; print(\'environment works on\', sys.version.split()[0])"',
        f"printf '%s\\n' {q(record)} > packages.json",
        'touch "$NEW/.ok"',
        # Switch atomically, register the kernel, keep one older build.
        'ln -sfn "$(basename "$NEW")" "$ROOT/current.new" && mv -T "$ROOT/current.new" "$ROOT/current"',
        # The kernel points at this build: a running notebook never mixes two builds.
        f'"$NEW/venv/bin/python" -m ipykernel install --user --name {q("schub-" + slug(project))} '
        f"--display-name {q(display)}{path_env}",
        'ls -1dt "$ROOT"/2* 2>/dev/null | tail -n +3 | xargs -r rm -rf',
    ]
    return "\n".join(lines) + "\n"


def remove_env(settings: Settings, project: str) -> None:
    """No packages left: drop the kernel now and delete the builds in the background
    (a conda environment has many files; the login node must not wait on Lustre)."""
    shutil.rmtree(kernel_dir(project), ignore_errors=True)
    root = env_root(settings, project)
    if not root.exists():
        return
    trash = root.with_name(f".trash-{root.name}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}")
    root.rename(trash)
    subprocess.Popen(["nice", "-n", "19", "rm", "-rf", str(trash)], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def submit_env_build(settings: Settings, slurm: Slurm, project: str, pip: Sequence[str], conda: Sequence[str]) -> EnvJob:
    check_packages(pip, conda)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    script_body = build_script(settings, project, pip, conda, stamp)
    logs = settings.logs_dir / "envs"
    logs.mkdir(parents=True, exist_ok=True)
    build = logs / f"build-{slug(project)}-{stamp}.sh"
    build.write_text(script_body)
    spec = JobSpec(
        name=f"{settings.job_prefix}-env-{slug(project)}"[:120],
        partition=settings.partition,
        resources=Resources(cpus=4, mem_gb=16, time_min=90),
        log_path=logs / f"env-{slug(project)}-%j.log",
        workdir=settings.root,
        command=("bash", str(build)),
    )
    script = logs / f"env-{slug(project)}-{stamp}.sbatch"
    script.write_text(render_script(spec))
    job_id = slurm.submit(script)
    return EnvJob(
        project=project, job_id=job_id, log=str(logs / f"env-{slug(project)}-{job_id}.log"),
        env=str(env_root(settings, project) / "current"), kernel=f"sc-hub: {project}",
        pip=tuple(pip), conda=tuple(conda),
        note="When the job has finished, pick the kernel in JupyterLab (start_session kind=jupyter). "
        "If the build fails, the previous kernel keeps working. Bricks keep using the shared environment.",
    )
