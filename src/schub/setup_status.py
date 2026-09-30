"""Where the background part of a private setup stands: $SCHUB_ROOT/setup-extras.json.

bootstrap_cluster.sh installs the analysis tools and downloads the starter datasets first, so the
student can start within minutes; a background job (scripts/setup_steps.sh extras) then adds the
deep-learning stack (torch with CUDA, scvi-tools: up to 6.5 GB). The dashboard, cluster(),
`schub doctor`, the gpu_visible check, bench.brick, pipeline plans, project package builds and a
cell that misses one of those modules read this file to say what is still coming, instead of an
error that looks like a broken install (and makes an agent install torch by hand). A starter
dataset the setup could not download is named too (setup-datasets-missing.txt).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Collection
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .library import find_asset
from .state import Frozen

STATUS_FILE = "setup-extras.json"
MISSING_FILE = "setup-datasets-missing.txt"  # starter datasets the last download left out (setup_steps.sh)
LOCK_FILE = "env-lock.txt"  # the versions of both parts, resolved together (scripts/setup_steps.sh)
# What the `ml` extra (pyproject.toml) adds that a cell may import.
HEAVY_MODULES = frozenset({"torch", "triton", "scvi", "lightning", "pytorch_lightning", "torchmetrics", "pyro",
                           "mudata", "xarray", "tensorboard"})
PENDING = ("queued", "running")
STALE_H = 3.0  # without the queue: no word for longer than the job's time limit (2 h) may mean it is gone
JUST_WRITTEN_S = 120  # a queue listed just before the job was queued does not show it yet
RETRY = ("ask your assistant to run the setup's cluster step again (in the sc-hub setup folder: "
         "sh onboard/start.sh, then sh onboard/start.sh retry cluster; on Windows onboard\\start.cmd, then "
         "onboard\\start.cmd retry cluster); if it stops again, send its log to the pilot owner")
MISSING = re.compile(r"No module named '([A-Za-z0-9_]+)")
ASSET_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")


class SetupStatus(Frozen):
    state: str = ""  # queued | running | done | failed
    step: str = ""
    job: str = ""
    updated: str = ""
    started: str = ""
    error: str = ""


def private_env(settings: Settings) -> bool:
    """The environment in use is this folder's own (not a shared library's): the only one the file is about."""
    own = settings.root / "env" / "bin" / "python"
    return _dir_resolved(settings.python) == _dir_resolved(own)


def _dir_resolved(path: Path) -> str:  # the folders resolved, not the interpreter link (venvs share their base)
    return os.path.join(os.path.realpath(path.parent), path.name)


def read(settings: Settings) -> SetupStatus | None:
    if not private_env(settings):
        return None  # a shared library's environment has everything; a leftover file is not about it
    try:
        data = json.loads((settings.root / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return SetupStatus.model_validate({k: str(v) for k, v in data.items() if k in SetupStatus.model_fields})


def live_queue(settings: Settings, slurm: Any) -> set[str] | None:
    """The student's queued and running job ids while a background part is queued or running (else None, and
    Slurm is not asked); None too when Slurm does not answer."""
    from .slurm import SlurmError

    status = read(settings)
    if status is None or status.state not in PENDING:
        return None
    try:
        return {j.job_id for j in slurm.my_jobs()}
    except SlurmError:
        return None


def _age_s(stamp: str, now: datetime | None = None) -> float | None:
    try:
        said = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if said.tzinfo is None:
        said = said.astimezone()  # (a stamp without its offset: this machine's time)
    return ((now or datetime.now(timezone.utc)) - said).total_seconds()


def stale(status: SetupStatus, now: datetime | None = None) -> bool:
    age = _age_s(status.updated, now)
    return age is not None and age > STALE_H * 3600


def gone(status: SetupStatus, queue: Collection[str] | None, now: datetime | None = None) -> bool:
    """Its job left the queue without a word (cancelled while waiting, killed): known only with the queue."""
    if queue is None or not status.job or status.job in queue:
        return False
    age = _age_s(status.updated, now)
    return age is None or age > JUST_WRITTEN_S


def on_its_way(status: SetupStatus | None, queue: Collection[str] | None = None) -> bool:
    """Queued or running. With the queue at hand it decides (a job may wait for a slot for hours);
    without it, a long silence counts against it."""
    if status is None or status.state not in PENDING:
        return False
    return not gone(status, queue) if queue is not None else not stale(status)


def pending(settings: Settings, queue: Collection[str] | None = None) -> bool:
    return on_its_way(read(settings), queue)


def note(settings: Settings, queue: Collection[str] | None = None) -> str:
    """What the student should know about the setup's two parts; "" when everything is there.

    `queue`: the job ids of the student's queued and running jobs when known (live_queue), so a job
    that is gone is not called on its way."""
    return " ".join(text for text in (describe(read(settings), queue), missing_note(settings)) if text)


def describe(status: SetupStatus | None, queue: Collection[str] | None = None) -> str:
    if status is None or status.state not in (*PENDING, "failed"):
        return ""
    job = f"job {status.job}" if status.job else "its job"
    last = status.updated[:16].replace("T", " ") or "none"
    if status.state == "failed":
        return (f"The background install of the deep-learning tools (torch, scvi-tools) stopped at "
                f"{status.step or 'its start'} ({status.error or 'no reason given'}). To finish it, {RETRY}.")
    if not on_its_way(status, queue):
        if queue is not None:
            return (f"The background install of the deep-learning tools (torch, scvi-tools; {job}) ended without "
                    f"finishing (its last word: {last}). To finish it, {RETRY}.")
        return (f"The background install of the deep-learning tools (torch, scvi-tools; {job}) has not reported "
                f"since {last}. If that job is no longer in the queue, {RETRY}.")
    if status.state == "queued":
        return (f"The deep-learning tools (torch, scvi-tools) install in the background ({job}, queued): everything "
                "else works now. While it runs (a few minutes to half an hour) it takes one of the student's Slurm "
                "job slots.")
    since = f" since {status.started[11:16]}" if len(status.started) >= 16 else ""
    doing = f": now {status.step}" if status.step else ""
    return (f"Still installing the deep-learning tools (torch, scvi-tools) in the background{since} ({job}{doing}). "
            "Kernel cells work now; a new Slurm job may wait for a free job slot until it ends.")


def install_note(settings: Settings, queue: Collection[str] | None = None) -> str:
    """The deep-learning part alone (for torch's own messages)."""
    return describe(read(settings), queue)


def missing_note(settings: Settings) -> str:
    """The starter datasets the setup could not download and that are still not there."""
    if not private_env(settings):
        return ""
    try:
        names = (settings.root / MISSING_FILE).read_text().split()
    except OSError:
        return ""
    missing = [n for n in names if ASSET_NAME.fullmatch(n) and find_asset(settings, n) is None]
    if not missing:
        return ""
    return (f"The setup could not download the starter data {', '.join(missing)}: running the setup's cluster step "
            "again downloads it.")


def missing_module_hint(settings: Settings, error_text: str, queue: Collection[str] | None = None) -> str:
    """A hint for a cell that failed on a module the background install adds; "" otherwise."""
    missing = MISSING.search(error_text)
    if missing is None or missing.group(1) not in HEAVY_MODULES:
        return ""
    status = read(settings)
    text = describe(status, queue)
    if not text:
        return ""
    return f"{text} Run this cell again {'when it has finished' if on_its_way(status, queue) else 'after that'}."
