"""A cell sent to Slurm (`%%slurm`): validated, snapshotted, submitted once.

- The cell's code is copied into projects/<p>/jobs/<cid>-<key>/ with SHA256SUMS; the
  job verifies them before it runs, so later edits cannot change a running job (in
  VCC2026 an edit under a running experiment voided 3 h 48 min of GPU work).
- The intent (with a unique --comment) is written before sbatch. If sbatch fails
  ambiguously, the job is looked up by its comment instead of being submitted again.
- The job is registered as the project's own, so only such jobs can be cancelled
  through sc-hub (in VCC2026 an agent cancelled the owner's terminal job).
"""

from __future__ import annotations

import argparse
import ast
import builtins
import hashlib
import json
import os
import re
import secrets
import shlex
import sys
from pathlib import Path

from ..bricks import Resources
from ..config import Settings
from ..slurm import JobSpec, Slurm, SlurmError, render_script
from ..state import Frozen
from .clock import stamp
from .fsio import create_json_exclusive, write_json_atomic
from .inbox import slug
from .jobs import JobRecord, register
from .models import JobRef

PARTITION_LIMITS = {"ws-ia": (24, 24, 100), "gpu": (8, 16, 90)}  # hours, CPUs, GB per job and student
DEFAULT_LIMITS = (24, 24, 100)
TIME = re.compile(r"^(?:(\d+):(\d\d):(\d\d)|(\d+(?:\.\d+)?)\s*(h|m|min|hours?|minutes?))$")
MEM = re.compile(r"^(\d+)\s*([GgMm])?[Bb]?$")


class SlurmCellError(ValueError):
    pass


class SlurmCellSpec(Frozen):
    gpus: int = 0
    cpus: int = 4
    mem_gb: int = 16
    minutes: int = 60
    partition: str = ""
    python: str = ""  # interpreter for the job; default: the kernel's own
    bash: bool = False
    name: str = ""


def _minutes(text: str) -> int:
    match = TIME.match(text.strip())
    if not match:
        raise SlurmCellError(f"--time {text!r}: use e.g. 90m, 6h or 02:30:00")
    if match.group(1) is not None:
        return int(match.group(1)) * 60 + int(match.group(2)) + (1 if int(match.group(3)) else 0)
    value, unit = float(match.group(4)), match.group(5)
    return max(1, int(round(value * 60 if unit.startswith("h") else value)))


def _mem_gb(text: str) -> int:
    match = MEM.match(text.strip())
    if not match:
        raise SlurmCellError(f"--mem {text!r}: use e.g. 32G")
    value = int(match.group(1))
    return max(1, value // 1024) if (match.group(2) or "G").upper() == "M" else value


def parse_line(line: str, default_partition: str) -> SlurmCellSpec:
    parser = argparse.ArgumentParser(prog="%%slurm", add_help=False, exit_on_error=False)
    parser.add_argument("--gpus", type=int, default=0)
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--mem", default="16G")
    parser.add_argument("--time", default="1h")
    parser.add_argument("--partition", default=default_partition)
    parser.add_argument("--python", default="")
    parser.add_argument("--bash", action="store_true")
    parser.add_argument("--name", default="")
    try:
        args, rest = parser.parse_known_args(shlex.split(line))
    except (argparse.ArgumentError, ValueError) as exc:
        raise SlurmCellError(f"%%slurm: {exc}") from exc
    if rest:
        raise SlurmCellError(f"%%slurm: unknown options {rest}")
    spec = SlurmCellSpec(gpus=args.gpus, cpus=args.cpus, mem_gb=_mem_gb(args.mem), minutes=_minutes(args.time),
                         partition=args.partition, python=args.python, bash=args.bash, name=args.name)
    return check_limits(spec)


def check_limits(spec: SlurmCellSpec) -> SlurmCellSpec:
    hours, cpus, mem = PARTITION_LIMITS.get(spec.partition, DEFAULT_LIMITS)
    problems = []
    if not 0 <= spec.gpus <= 1:
        problems.append("--gpus must be 0 or 1 (one GPU per student)")
    if not 1 <= spec.cpus <= cpus:
        problems.append(f"--cpus must be 1-{cpus} on {spec.partition}")
    if not 1 <= spec.mem_gb <= mem:
        problems.append(f"--mem must be at most {mem}G on {spec.partition}")
    if not 1 <= spec.minutes <= hours * 60:
        problems.append(f"--time must be at most {hours}h on {spec.partition}; checkpoint and continue in a new job")
    if spec.name and not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", spec.name):
        problems.append("--name: letters, digits, '.', '_', '-' only")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", spec.partition):
        problems.append("--partition is not a partition name")
    if problems:
        raise SlurmCellError("%%slurm: " + "; ".join(problems))
    return spec


def kernel_names(code: str) -> list[str]:
    """Names the code reads but never defines: they exist in the kernel, not in the job."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise SlurmCellError(f"the cell does not parse: {exc}") from exc
    loaded, stored = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            (loaded if isinstance(node.ctx, ast.Load) else stored).add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            stored.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            stored.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.arg):
            stored.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            stored.add(node.name)
    return sorted(loaded - stored - set(dir(builtins)) - {"__file__", "__name__"})


class Submitted(Frozen):
    job: JobRef
    job_dir: str
    warnings: tuple[str, ...] = ()


def submit_cell(settings: Settings, slurm: Slurm, project: str, project_dir: Path, ref: str, code: str,
                spec: SlurmCellSpec, checks: list[dict]) -> Submitted:
    cid = ref.partition("#")[2] or "c0000"
    key = secrets.token_hex(4)
    job_dir = project_dir / "jobs" / f"{cid}-{key}"
    job_dir.mkdir(parents=True, exist_ok=False)
    body = job_dir / ("cell.sh" if spec.bash else "cell.py")
    body.write_text(code)
    digest = hashlib.sha256(body.read_bytes()).hexdigest()
    (job_dir / "SHA256SUMS").write_text(f"{digest}  {body.name}\n")
    comment = f"schub-{slug(project).replace('.', '-')[:40]}-{cid}-{key}"
    python = spec.python or sys.executable
    meta = {"project": project, "ref": ref, "cid": cid, "spec": spec.model_dump(), "checks": checks,
            "python": python, "comment": comment, "created": stamp(), "body": body.name}
    write_json_atomic(job_dir / "job.json", meta)
    create_json_exclusive(job_dir / "intent.json", {"comment": comment, "written": stamp()})
    job_id = _submit(settings, slurm, job_dir, _job_spec(settings, project, project_dir, job_dir, spec, python,
                                                          comment, cid))
    write_json_atomic(job_dir / "job.json", {**meta, "job_id": job_id})
    register(settings, JobRecord(job_id=job_id, project=project, ref=ref, job_dir=str(job_dir)))
    job = JobRef(job_id=job_id, name=_job_name(settings, project, cid), partition=spec.partition, comment=comment,
                 state="PENDING", submitted=stamp(), log=str(job_dir / f"slurm-{job_id}.log"))
    warnings = () if spec.bash else tuple(
        f"the job will not have these kernel names: {', '.join(names)}" for names in [kernel_names(code)] if names)
    return Submitted(job=job, job_dir=str(job_dir), warnings=warnings)


def _job_name(settings: Settings, project: str, cid: str) -> str:
    return f"{settings.job_prefix}-cell-{slug(project)}-{cid}"[:120]


def _job_spec(settings: Settings, project: str, project_dir: Path, job_dir: Path, spec: SlurmCellSpec,
              python: str, comment: str, cid: str) -> JobSpec:
    env = [(k, v) for k, v in os.environ.items()
           if k.startswith("SCHUB_") or k in ("PYTHONPATH", "HOME", "PATH", "LANG", "TMPDIR")]
    env += [("SCHUB_PROJECT", project), ("SCHUB_PROJECT_DIR", str(project_dir)),
            ("OMP_NUM_THREADS", str(spec.cpus)), ("PYTHONUNBUFFERED", "1")]
    return JobSpec(
        name=_job_name(settings, project, cid), partition=spec.partition,
        resources=Resources(cpus=spec.cpus, mem_gb=spec.mem_gb, time_min=spec.minutes, gpus=spec.gpus),
        log_path=job_dir / "slurm-%j.log", workdir=project_dir / "work",
        command=(python, "-m", "schub.bench.jobrun", "--job-dir", str(job_dir)),
        env=tuple(dict(env).items()), comment=comment,
    )


def _submit(settings: Settings, slurm: Slurm, job_dir: Path, spec: JobSpec) -> str:
    script = job_dir / "job.sbatch"
    script.write_text(render_script(spec))
    try:
        return slurm.submit(script)
    except SlurmError as exc:
        found = slurm.find_by_comment(spec.comment)  # did it go through before the error?
        if found:
            return found
        write_json_atomic(job_dir / "failed.json", {"error": str(exc), "at": stamp()})
        raise SlurmCellError(f"sbatch refused the job: {exc}") from exc


def job_meta(job_dir: Path) -> dict:
    return json.loads((job_dir / "job.json").read_text())
