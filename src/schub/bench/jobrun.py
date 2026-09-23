"""Inside a `%%slurm` job: verify the snapshot, run the cell, then report to the journal.

    python -m schub.bench.jobrun --job-dir projects/<p>/jobs/<cid>-<key>

It writes result.json next to the snapshot and addenda to the cell's journal entry
(job state and exit code, files it changed, downloads, check results). The cell
entry itself is never rewritten: only the runner writes cells.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Sequence

from ..config import load_settings
from .checks import run_checks
from .clock import stamp
from .filesnap import diff, scan, sha256_file
from .fsio import write_json_atomic
from .journal import Journal
from .ledger import drain
from .models import CheckSpec

EXIT_OK, EXIT_FAILED, EXIT_TAMPERED = 0, 1, 3


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
        return subprocess.run(["bash", str(body)], check=False).returncode
    if meta.get("body_python"):  # e.g. a paper's own environment, which has no sc-hub
        return subprocess.run([meta["body_python"], str(body)], check=False).returncode
    try:
        runpy.run_path(str(body), init_globals={"bench": _bench(meta)}, run_name="__main__")
        return EXIT_OK
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (EXIT_OK if exc.code is None else EXIT_FAILED)
    except BaseException:  # noqa: BLE001 - the cell's own error goes to the log and the journal
        traceback.print_exc()
        return EXIT_FAILED


def report(job_dir: Path, meta: dict[str, Any], exit_code: int, files: tuple, events: list[dict], checks: tuple,
           started: str) -> dict[str, Any]:
    job_id = os.environ.get("SLURM_JOB_ID", meta.get("job_id", "local"))
    result = {
        "job_id": job_id, "status": "ok" if exit_code == 0 else "failed", "exit_code": exit_code,
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
    state = "COMPLETED" if result["exit_code"] == 0 else "FAILED"
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
    meta = json.loads((job_dir / "job.json").read_text())
    settings = load_settings()
    project_dir = settings.projects_dir / meta["project"]
    started = stamp()
    tampered = verify(job_dir)
    if tampered:
        sys.stderr.write(f"[bench] refusing to run: {tampered}\n")
        result = report(job_dir, meta, EXIT_TAMPERED, (), [], (), started)
        to_journal(settings, meta, job_dir, result)
        return EXIT_TAMPERED
    before = scan(project_dir, settings.bench.snapshot_max_files)
    exit_code = run_body(job_dir, meta, project_dir / "work")
    events = drain()
    after = scan(project_dir, settings.bench.snapshot_max_files)
    files = diff(before, after, project_dir, settings.bench.snapshot_hash_max_mb * 1024 * 1024)
    specs = [CheckSpec.model_validate(c) for c in meta.get("checks", [])]
    checks = run_checks(settings, meta["project"], specs) if exit_code == 0 else ()
    result = report(job_dir, meta, exit_code, files, events, checks, started)
    to_journal(settings, meta, job_dir, result)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
