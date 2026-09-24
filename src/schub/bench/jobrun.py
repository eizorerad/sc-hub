"""Inside a `%%slurm` job: verify the snapshot, run the cell, then report to the journal.

    python -m schub.bench.jobrun --job-dir projects/<p>/jobs/<cid>-<key>

It writes result.json next to the snapshot and addenda to the cell's journal entry
(job state and exit code, files it changed, downloads, check results). The cell
entry itself is never rewritten: only the runner writes cells.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import runpy
import signal
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Iterator, Sequence

from ..config import load_settings
from .checks import run_checks
from .clock import stamp
from .filesnap import diff, scan, sha256_file
from .fsio import write_json_atomic
from .journal import Journal
from .ledger import drain
from .models import CheckSpec

EXIT_OK, EXIT_FAILED, EXIT_TAMPERED = 0, 1, 3
PORTABLE = Path(__file__).with_name("portable")  # stdlib-only helpers (schub_ckpt) for any environment
STOP_ENV = "SCHUB_STOP_FILE"
BODIES = ("cell.py", "cell.sh")
TERMINATED: list[int] = []  # scancel or the time limit reached the body (a child process) with TERM


def verify(job_dir: Path) -> str | None:
    """None if every file listed in SHA256SUMS is unchanged, else what differs."""
    for line in (job_dir / "SHA256SUMS").read_text().splitlines():
        expected, _, name = line.partition("  ")
        if sha256_file(job_dir / name) != expected:
            return f"{name} changed after submission"
    return None


def _bench(meta: dict[str, Any]):
    """The kernel's `bench` (fetch, twin, run_brick, the project's folders), so a %%slurm cell reads like a
    kernel cell."""
    from . import kernel_api

    kernel_api.set_cell(meta.get("ref", ""), json.dumps(meta.get("checks", [])))
    return kernel_api


def run_body(job_dir: Path, meta: dict[str, Any], cwd: Path) -> int:
    body = job_dir / meta["body"]
    os.chdir(cwd)
    if meta["body"].endswith(".sh"):
        path = os.pathsep.join(filter(None, (str(PORTABLE), os.environ.get("PYTHONPATH", ""))))
        return _child(["bash", str(body)], {**os.environ, "PYTHONPATH": path})
    if meta.get("body_python"):  # e.g. a paper's own environment: no sc-hub, only the portable helpers
        return _child([meta["body_python"], str(body)], {**os.environ, "PYTHONPATH": str(PORTABLE)})
    with _importable(PORTABLE):
        try:
            runpy.run_path(str(body), init_globals={"bench": _bench(meta)}, run_name="__main__")
            return EXIT_OK
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else (EXIT_OK if exc.code is None else EXIT_FAILED)
        except BaseException:  # noqa: BLE001 - the cell's own error goes to the log and the journal
            traceback.print_exc()
            return EXIT_FAILED


def _child(argv: list[str], env: dict[str, str]) -> int:
    """Run the body in its own process. scancel's TERM is passed on; the time-limit warning is not (a
    process without a USR1 handler dies of it): the child sees it as the file $SCHUB_STOP_FILE."""
    process = subprocess.Popen(argv, env=env)

    def forward(number: int, _frame: Any) -> None:
        TERMINATED.append(number)
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(number)

    previous = signal.signal(signal.SIGTERM, forward)
    try:
        code = process.wait()
    finally:
        signal.signal(signal.SIGTERM, previous)
    return code if code >= 0 else 128 - code  # killed by a signal: the shell's convention


@contextlib.contextmanager
def _time_warning(stop_file: Path) -> Iterator[None]:
    """For the whole run (the body, the file scan, the checks): USR1 (the time limit is near, sent by
    --signal=B:USR1 to jobrun only) writes $SCHUB_STOP_FILE, which schub_ckpt.Run.stop_requested reads in
    any process, and never kills jobrun before it reports."""
    def warn(_number: int, _frame: Any) -> None:
        with contextlib.suppress(OSError):
            stop_file.touch()
        sys.stderr.write("[bench] SIGUSR1: the job's time limit is near; save a checkpoint and stop "
                         "(schub_ckpt: Run.stop_requested)\n")

    previous_env = os.environ.get(STOP_ENV)
    os.environ[STOP_ENV] = str(stop_file)
    previous = signal.signal(signal.SIGUSR1, warn)
    try:
        yield
    finally:
        signal.signal(signal.SIGUSR1, previous)
        if previous_env is None:
            os.environ.pop(STOP_ENV, None)
        else:
            os.environ[STOP_ENV] = previous_env


@contextlib.contextmanager
def _importable(folder: Path) -> Iterator[None]:
    added = str(folder) not in sys.path
    if added:
        sys.path.insert(0, str(folder))
    try:
        yield
    finally:
        if added and str(folder) in sys.path:
            sys.path.remove(str(folder))


def report(job_dir: Path, meta: dict[str, Any], exit_code: int, files: tuple, events: list[dict], checks: tuple,
           started: str, ended_by: str = "") -> dict[str, Any]:
    job_id = os.environ.get("SLURM_JOB_ID", meta.get("job_id", "local"))
    result = {
        "job_id": job_id, "status": "ok" if exit_code == 0 else "failed", "exit_code": exit_code,
        "ended_by": ended_by,  # TIMEOUT or CANCELLED when Slurm's TERM ended the body
        "started": started, "finished": stamp(), "files": [f.model_dump() for f in files],
        "downloads": [e for e in events if e.get("kind") == "download"],
        "checks": [c.model_dump() for c in checks],
    }
    write_json_atomic(job_dir / "result.json", result)
    return result


def to_journal(settings, meta: dict[str, Any], job_dir: Path, result: dict[str, Any]) -> None:
    project_dir = settings.projects_dir / meta["project"]
    journal = Journal(project_dir, meta["project"])
    cid, job_id = meta["cid"], result["job_id"]
    state = result.get("ended_by") or ("COMPLETED" if result["exit_code"] == 0 else "FAILED")
    journal.add_addendum(cid, f"job-{job_id}", {"kind": "job", "job": {
        "job_id": job_id, "state": state, "exit_code": result["exit_code"], "finished": result["finished"],
        "log": str(job_dir / f"slurm-{job_id}.log")}})
    if result["files"]:
        journal.add_addendum(cid, f"files-{job_id}", {"kind": "files", "files": result["files"]})
    for index, download in enumerate(result["downloads"]):
        payload = {k: v for k, v in download.items() if k != "kind"}
        journal.add_addendum(cid, f"download-{job_id}-{index}", {"kind": "download", "download": payload})
    for index, check in enumerate(result["checks"]):
        journal.add_addendum(cid, f"check-{job_id}-{index}", {"kind": "check", "check": check})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="schub.bench.jobrun")
    parser.add_argument("--job-dir", required=True, type=Path)
    job_dir = parser.parse_args(argv).job_dir.resolve()
    os.umask(0o077)  # the job's files are the student's alone (/l/users is often readable by everyone)
    with _time_warning(job_dir / "time-limit-near"):
        return _run(job_dir)


def submission(job_dir: Path) -> dict[str, Any]:
    """What was submitted: job.json, with the fields that decide what runs taken from frozen.json (listed in
    SHA256SUMS, so editing a queued job's job.json changes nothing)."""
    meta = json.loads((job_dir / "job.json").read_text())
    frozen = job_dir / "frozen.json"
    if frozen.exists():
        meta = {**meta, **json.loads(frozen.read_text())}
    return meta


def _run(job_dir: Path) -> int:
    meta = submission(job_dir)
    settings = load_settings()
    project_dir = settings.projects_dir / meta["project"]
    started = stamp()
    tampered = verify(job_dir) or (None if meta.get("body") in BODIES else f"unexpected body {meta.get('body')!r}")
    if tampered:
        sys.stderr.write(f"[bench] refusing to run: {tampered}\n")
        result = report(job_dir, meta, EXIT_TAMPERED, (), [], (), started)
        to_journal(settings, meta, job_dir, result)
        return EXIT_TAMPERED
    before = scan(project_dir, settings.bench.snapshot_max_files)
    exit_code = run_body(job_dir, meta, project_dir / "work")
    ended_by = ("TIMEOUT" if (job_dir / "time-limit-near").exists() else "CANCELLED") if TERMINATED else ""
    events = drain()
    after = scan(project_dir, settings.bench.snapshot_max_files)
    files = diff(before, after, project_dir, settings.bench.snapshot_hash_max_mb * 1024 * 1024)
    specs = [CheckSpec.model_validate(c) for c in meta.get("checks", [])]
    if meta.get("body_python"):
        os.environ["SCHUB_CHECK_PYTHON"] = meta["body_python"]  # gpu_visible probes the job's own environment
    checks = run_checks(settings, meta["project"], specs) if exit_code == 0 else ()
    result = report(job_dir, meta, exit_code, files, events, checks, started, ended_by)
    to_journal(settings, meta, job_dir, result)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
