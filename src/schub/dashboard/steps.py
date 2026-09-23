"""Per-step details read from the step folder: timing, resources, log tail, figures."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..headlines import duration  # noqa: F401 - re-exported for the views
from ..state import Frozen
from ..stepfile import SUCCESS

LOG_LINES = 15
LOG_LINE_CHARS = 200
SBATCH = re.compile(r"^#SBATCH --([a-z-]+)=(.+)$")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SHOWN_RESOURCES = {"cpus-per-task": "cpus", "mem": "mem", "time": "time", "gres": "gpu", "partition": "partition"}


class StepExtras(Frozen):
    started: str | None = None
    finished: str | None = None
    seconds: float | None = None
    resources: dict[str, str] = {}
    log_tail: tuple[str, ...] = ()
    figures: tuple[str, ...] = ()


def _mtime(path: Path) -> float | None:
    """None when the file vanished between listing and stat (a job may be writing)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _timing(step_dir: Path) -> tuple[str | None, str | None, float | None]:
    timing_file = step_dir / "results" / "timing.json"
    try:
        data = json.loads(timing_file.read_text())
        return data.get("started"), data.get("finished"), data.get("seconds")
    except (OSError, json.JSONDecodeError):
        pass
    done = _mtime(step_dir / SUCCESS)
    return None, (_iso(done) if done is not None else None), None


def _resources(step_dir: Path) -> dict[str, str]:
    try:
        text = (step_dir / "job.sbatch").read_text(errors="replace")
    except OSError:
        return {}
    found = {}
    for line in text.splitlines():
        match = SBATCH.match(line)
        if match and match.group(1) in SHOWN_RESOURCES:
            found[SHOWN_RESOURCES[match.group(1)]] = match.group(2)[:40]
    return found


def _log_tail(step_dir: Path) -> tuple[str, ...]:
    stamped = [(t, p) for p in step_dir.glob("slurm-*.log") if (t := _mtime(p)) is not None]
    if not stamped:
        return ()
    try:
        with max(stamped)[1].open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 8192))
            text = handle.read().decode(errors="replace")
    except OSError:
        return ()
    lines = [ANSI.sub("", line)[:LOG_LINE_CHARS] for line in text.splitlines() if line.strip()]
    return tuple(lines[-LOG_LINES:])


def step_extras(step_dir: Path, with_log: bool) -> StepExtras:
    started, finished, seconds = _timing(step_dir)
    results = step_dir / "results"
    figures = tuple(
        sorted(p.name for p in results.glob("*.png") if not p.stem.endswith("_thumb"))
    ) if results.is_dir() else ()
    return StepExtras(
        started=started,
        finished=finished,
        seconds=seconds,
        resources=_resources(step_dir),
        log_tail=_log_tail(step_dir) if with_log else (),
        figures=figures,
    )


def _size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def trained_model_size_mb(folder: Path) -> float:
    return round(sum(_size(p) for p in folder.rglob("*")) / 1e6, 1)


def slurm_seconds(text: str) -> int | None:
    """Parse Slurm durations like 45:00, 8:00:00 or 1-02:03:04."""
    if not text or text.upper() in {"UNLIMITED", "INVALID", "NOT_SET"}:
        return None
    days, _, rest = text.rpartition("-")
    parts = [int(p) for p in rest.split(":") if p.isdigit()]
    if not parts:
        return None
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, secs = parts[-3:]
    return (int(days) if days.isdigit() else 0) * 86400 + hours * 3600 + minutes * 60 + secs


def as_json(value: Any) -> str:
    return json.dumps(value, indent=1, default=str)
