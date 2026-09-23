"""Everything about the student's footprint on the cluster, in one place: storage
(Lustre quota, NFS home), logins, per-user job limits and how much of them is in
use, their jobs with CPU/RAM/GPU, and how busy each partition is.

Only cheap, read-only commands that work on the login node (no sacct/sacctmgr,
which need the unreachable accounting daemon); the per-folder sizes use `du`
and are cached for an hour.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .config import Settings
from .state import Frozen

Run = Callable[[Sequence[str]], str]
FOOTPRINT_TTL_S = 3600
OVERVIEW_TTL_S = 300  # the dashboard rebuilds every minute; Slurm need not be asked that often
FOOTPRINT_DIRS = ("cache/steps", "data", "library-local", "runs", "notebooks", "sessions", "view")
UNITS = {"k": 1 / 1024**2, "m": 1 / 1024, "g": 1, "t": 1024, "p": 1024**2}


class Storage(Frozen):
    name: str
    path: str
    used_gb: float | None = None
    limit_gb: float | None = None
    files: int | None = None
    files_limit: int | None = None
    note: str = ""


class Limit(Frozen):
    name: str
    used: float
    limit: float | None  # None: no per-user limit


class QosLimits(Frozen):
    qos: str
    partitions: tuple[str, ...]
    limits: tuple[Limit, ...]


class Job(Frozen):
    job_id: str
    name: str
    state: str
    partition: str
    cpus: int
    mem_gb: float
    gpus: int
    elapsed: str
    time_limit: str
    node: str
    reason: str


class PartitionLoad(Frozen):
    name: str
    qos: str
    max_time: str
    nodes: int
    cpus_alloc: int
    cpus_total: int
    gpus_used: int
    gpus_total: int
    mem_gb_per_node: float


class Overview(Frozen):
    user: str
    login_node: str
    generated_at: str
    logins: tuple[str, ...] = ()
    storage: tuple[Storage, ...] = ()
    footprint: dict[str, float] = {}
    limits: tuple[QosLimits, ...] = ()
    jobs: tuple[Job, ...] = ()
    partitions: tuple[PartitionLoad, ...] = ()
    problems: tuple[str, ...] = ()


def run_command(args: Sequence[str]) -> str:
    done = subprocess.run(list(args), capture_output=True, text=True, timeout=20, check=False)
    if done.returncode != 0 and not done.stdout:
        raise OSError(done.stderr.strip()[:200] or f"{args[0]} failed")
    return done.stdout


# ---- parsers (pure; tested with real command output) ---------------------------


def size_gb(text: str) -> float | None:
    """'2.021T' / '3T' / '40960' (MB) / '110000M' -> GB."""
    match = re.fullmatch(r"([\d.]+)([kKmMgGtTpP]?)\*?", text.strip())
    if not match:
        return None
    unit = match.group(2).lower() or "m"
    return round(float(match.group(1)) * UNITS[unit], 2)


def parse_lfs_quota(text: str) -> Storage | None:
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 7 and fields[0].startswith("/"):
            used, limit = size_gb(fields[1]), size_gb(fields[2]) or size_gb(fields[3])
            # Filesystem used bquota blimit bgrace files iquota ilimit igrace
            files, files_limit = fields[5].rstrip("*"), fields[6]
            return Storage(
                name="Lustre (/l)", path=fields[0], used_gb=used, limit_gb=limit or None,
                files=int(files) if files.isdigit() else None,
                files_limit=int(files_limit) if files_limit.isdigit() and files_limit != "0" else None,
                note="your quota: datasets, runs and caches live here",
            )
    return None


def parse_df(text: str, name: str) -> Storage | None:
    lines = [line.split() for line in text.splitlines()[1:] if line.strip()]
    if not lines or len(lines[-1]) < 6:
        return None
    fields = lines[-1]
    used, total = int(fields[2]) / 1024**2, int(fields[1]) / 1024**2
    return Storage(name=name, path=fields[5], used_gb=round(used, 1), limit_gb=round(total, 1),
                   note="shared by everyone; no per-user quota on this filesystem")


def _pairs(text: str) -> dict[str, tuple[str, str]]:
    """'cpu=24(12),mem=110000(40960),gres/gpu=N(0)' -> {name: (limit, used)}."""
    return {k: (lim, used) for k, lim, used in re.findall(r"([\w/]+)=([\w.]+)\((\d+(?:\.\d+)?)\)", text)}


def parse_user_qos(text: str, user: str) -> tuple[Limit, ...]:
    """The student's own line in `scontrol show assoc_mgr flags=qos` (User Limits)."""
    block = re.search(rf"^\s+{re.escape(user)}\(\d+\)\n((?:\s+Max.*\n?)+)", text, re.M)
    if block is None:
        return ()
    body = block.group(1)
    found = []
    jobs = re.search(r"MaxJobsPU=([\w]+)\((\d+)\)", body)
    if jobs:
        found.append(Limit(name="running jobs", used=float(jobs.group(2)), limit=None if jobs.group(1) == "N" else float(jobs.group(1))))
    submit = re.search(r"MaxSubmitJobsPU=([\w]+)\((\d+)\)", body)
    if submit:
        found.append(Limit(name="submitted jobs", used=float(submit.group(2)), limit=None if submit.group(1) == "N" else float(submit.group(1))))
    tres = re.search(r"MaxTRESPU=(\S+)", body)
    names = {"cpu": "CPUs", "mem": "memory (GB)", "gres/gpu": "GPUs"}
    for key, (limit, used) in (_pairs(tres.group(1)) if tres else {}).items():
        if key not in names:
            continue
        scale = 1 / 1024 if key == "mem" else 1  # MB -> GB
        found.append(Limit(name=names[key], used=round(float(used) * scale, 1),
                           limit=None if limit == "N" else round(float(limit) * scale, 1)))
    return tuple(found)


def parse_partitions(text: str) -> dict[str, dict[str, str]]:
    """`scontrol show partition -o`: one line per partition."""
    found = {}
    for line in text.splitlines():
        fields = dict(re.findall(r"(\w+)=(\S+)", line))
        if "PartitionName" in fields:
            found[fields["PartitionName"]] = fields
    return found


def parse_sinfo_cpus(text: str) -> dict[str, tuple[int, int, float, int]]:
    """'%P|%C|%m|%D' -> {partition: (allocated, total, memory GB per node, nodes)}."""
    found = {}
    for line in text.splitlines():
        parts = line.strip().split("|")
        try:
            alloc, _idle, _other, total = (int(x) for x in parts[1].split("/"))
            memory = int(parts[2].rstrip("+"))  # '230000+' when node sizes differ
            found[parts[0].rstrip("*")] = (alloc, total, round(memory / 1024, 1), int(parts[3]))
        except (IndexError, ValueError):
            continue
    return found


def parse_sinfo_gpus(text: str) -> dict[str, tuple[int, int]]:
    """Per-node 'Partition Gres GresUsed' lines -> {partition: (used, total)}."""
    found: dict[str, list[int]] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        total = sum(int(n) for n in re.findall(r"gpu(?::[\w-]+)*:(\d+)", fields[1]))
        used = sum(int(n) for n in re.findall(r"gpu(?::[\w-]+)*:(\d+)\(", fields[2]))
        counts = found.setdefault(fields[0].rstrip("*"), [0, 0])
        counts[0] += used
        counts[1] += total
    return {k: (v[0], v[1]) for k, v in found.items()}


def parse_jobs(text: str) -> tuple[Job, ...]:
    """squeue '%i|%j|%T|%P|%C|%m|%b|%M|%l|%N|%r'."""
    jobs = []
    for line in text.splitlines():
        p = line.strip().split("|")
        if len(p) != 11:
            continue
        gpus = sum(int(n) for n in re.findall(r"gpu(?::[\w-]+)*:(\d+)", p[6])) if "gpu" in p[6] else 0
        jobs.append(Job(job_id=p[0], name=p[1][:60], state=p[2], partition=p[3], cpus=int(p[4]) if p[4].isdigit() else 0,
                        mem_gb=size_gb(p[5]) or 0.0, gpus=gpus, elapsed=p[7], time_limit=p[8], node=p[9], reason=p[10]))
    return tuple(jobs)


# ---- collection ---------------------------------------------------------------


def _footprint(settings: Settings) -> dict[str, float]:
    """Sizes of the sc-hub folders (GB). `du` over many Lustre files is slow, so it runs
    detached (niced) at most once an hour and the page shows the last result."""
    cache = settings.cache_dir / "footprint.json"
    try:
        sizes = json.loads(cache.read_text())
        fresh = time.time() - cache.stat().st_mtime < FOOTPRINT_TTL_S
    except (OSError, json.JSONDecodeError):
        sizes, fresh = {}, False
    lock = settings.cache_dir / "footprint.running"
    stale_lock = lock.exists() and time.time() - lock.stat().st_mtime > FOOTPRINT_TTL_S
    folders = " ".join(shlex.quote(str(settings.root / n)) for n in FOOTPRINT_DIRS if (settings.root / n).is_dir())
    if folders and not fresh and (not lock.exists() or stale_lock):
        script = (f"touch {shlex.quote(str(lock))}; nice -n 19 timeout 1800 du -sk {folders} > {shlex.quote(str(cache))}.du; "
                  f"python3 -c {shlex.quote(_DU_TO_JSON)} {shlex.quote(str(cache))} {shlex.quote(str(settings.root))}; "
                  f"rm -f {shlex.quote(str(lock))}")
        try:
            settings.cache_dir.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["sh", "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            pass
    return {k: float(v) for k, v in sizes.items()} if isinstance(sizes, dict) else {}


_DU_TO_JSON = (
    "import json, os, sys\n"
    "cache, root = sys.argv[1], sys.argv[2]\n"
    "sizes = {}\n"
    "for line in open(cache + '.du'):\n"
    "    kb, _, path = line.rstrip('\\n').partition('\\t')\n"
    "    if kb.isdigit(): sizes[os.path.relpath(path, root)] = round(int(kb) / 1024 ** 2, 2)\n"
    "json.dump(sizes, open(cache + '.tmp', 'w')); os.replace(cache + '.tmp', cache)\n"
)


def cached_overview(settings: Settings) -> Overview:
    """The overview, recomputed at most every few minutes (it asks Slurm several things)."""
    cache = settings.cache_dir / "overview.json"
    try:
        if time.time() - cache.stat().st_mtime < OVERVIEW_TTL_S:
            return Overview.model_validate_json(cache.read_text())
    except (OSError, ValueError):
        pass
    overview = collect_overview(settings)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        partial = cache.with_name(f".overview.{os.getpid()}.json")
        partial.write_text(overview.model_dump_json())
        os.replace(partial, cache)
    except OSError:
        pass
    return overview


def collect_overview(settings: Settings, run: Run = run_command) -> Overview:
    user = os.environ.get("USER", "")
    problems: list[str] = []

    def attempt(what: str, args: Sequence[str]) -> str:
        try:
            return run(args)
        except (OSError, subprocess.TimeoutExpired) as exc:
            problems.append(f"{what}: {str(exc)[:120]}")
            return ""

    storage = [s for s in (
        parse_lfs_quota(attempt("Lustre quota", ["lfs", "quota", "-u", user, "-h", "/l"])),
        parse_df(attempt("home", ["df", "-P", "-k", str(Path.home())]), "Home (NFS)"),
    ) if s]
    partitions = parse_partitions(attempt("partitions", ["scontrol", "show", "partition", "-o"]))
    qos_names = sorted({p.get("QoS", "") for p in partitions.values()} - {"", "N/A"})
    limits = []
    for qos in qos_names:
        found = parse_user_qos(attempt(f"limits of {qos}", ["scontrol", "show", "assoc_mgr", "flags=qos", f"qos={qos}"]), user)
        if found:
            members = tuple(name for name, p in partitions.items() if p.get("QoS") == qos)
            limits.append(QosLimits(qos=qos, partitions=members, limits=found))
    cpus = parse_sinfo_cpus(attempt("cluster CPUs", ["sinfo", "-h", "-o", "%P|%C|%m|%D"]))
    gpus = parse_sinfo_gpus(attempt("cluster GPUs", ["sinfo", "-h", "-N", "-O", "Partition:30,Gres:60,GresUsed:80"]))
    loads = tuple(
        PartitionLoad(name=name, qos=partitions.get(name, {}).get("QoS", ""),
                      max_time=partitions.get(name, {}).get("MaxTime", ""), nodes=nodes,
                      cpus_alloc=alloc, cpus_total=total, gpus_used=gpus.get(name, (0, 0))[0],
                      gpus_total=gpus.get(name, (0, 0))[1], mem_gb_per_node=mem)
        for name, (alloc, total, mem, nodes) in cpus.items()
    )
    jobs = parse_jobs(attempt("your jobs", ["squeue", "--me", "-h", "-o", "%i|%j|%T|%P|%C|%m|%b|%M|%l|%N|%r"]))
    who = attempt("logins", ["who"])
    logins = tuple(" ".join(line.split()[1:]) for line in who.splitlines() if line.split()[:1] == [user])
    return Overview(
        user=user, login_node=socket.gethostname().split(".")[0],
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        logins=logins[:10], storage=tuple(storage), footprint=_footprint(settings),
        limits=tuple(limits), jobs=jobs, partitions=loads, problems=tuple(problems),
    )
