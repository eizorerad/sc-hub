"""The watchdog job: every few minutes, check the bench and re-arm itself.

    python -m schub.bench.watchdog      (a 1-CPU Slurm job, rescheduled with --begin)

- bench/STOP: do nothing and do not re-arm.
- A runner that is gone (its job ended) left claimed cells: sweep them (lost / back
  to the inbox), so the journal never shows a cell "running" forever.
- Cells wait in the inbox and no workbench job exists: start one.
- %%slurm jobs that Slurm ended without a result (killed, time limit, out of memory):
  record that in their cell's journal entry.
- A lab agent whose goal is active but has no slice queued (a dead node, a failed
  sbatch): queue one. While goals are active, probe the engines every few hours.
- Otherwise idle for dormant_after_h: let the chain lapse (the next cell re-arms it).
- Its own old logs and the submitted job scripts are pruned (Slurm keeps its own copy of
  a script once the job is queued).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from ..config import Settings, load_settings
from ..locking import LockTimeout
from ..slurm import Slurm
from ..state import Frozen
from .clock import Clock, stamp
from .engines.policy import PolicyError
from .engines.probe import due as probe_due, probe
from .fsio import write_json_atomic
from .goal_agent import active_goals, revive
from .inbox import Inbox
from .jobs import open_records, reap
from .runner import Runner
from .workbench import WORKBENCH, Workbench


class WatchdogReport(Frozen):
    checked: str
    actions: tuple[str, ...] = ()
    rearmed: str | None = None
    workbench: str = ""


def check(settings: Settings, slurm: Slurm, own_job_id: str | None = None, now: Clock = stamp) -> WatchdogReport:
    bench = Workbench(settings, slurm, now)
    if bench.stopped():
        return _save(settings, WatchdogReport(checked=now(), actions=("bench/STOP exists: not re-arming",)))
    actions: list[str] = []
    try:
        pruned = prune(settings.bench_dir)
        if pruned:
            actions.append(f"pruned {pruned} old watchdog log(s) and job script(s)")
    except OSError as exc:
        actions.append(f"pruning failed: {exc}"[:300])
    has_job = bool(bench.jobs(WORKBENCH))
    inbox = Inbox(settings.bench_dir)
    try:  # a file-server error here must not end the chain: the re-arm below still happens
        swept = Runner(settings, now=now, job_id=f"watchdog-{own_job_id or os.getpid()}", slurm=slurm).sweep()
        if swept:
            actions.append(f"swept {swept} cell(s) a dead workbench left behind")
    except OSError as exc:
        actions.append(f"sweep failed: {exc}"[:300])
    try:
        ended = reap(settings, slurm)
        if ended:
            actions.append(f"recorded {len(ended)} job(s) that ended without a result: {', '.join(ended)}")
    except OSError as exc:
        actions.append(f"recording ended jobs failed: {exc}"[:300])
    waiting = inbox.pending()
    if waiting and not has_job:
        try:  # its lock and second look keep a parallel run() from starting another one
            job_id = bench.start_if_missing()
            if job_id is not None:
                actions.append(f"started the workbench (job {job_id}) for {len(waiting)} waiting cell(s)")
        except LockTimeout as exc:
            actions.append(f"workbench not started: {exc}"[:300])
    goals = active_goals(settings)
    rearmed = None  # re-armed before the lab-agent work: a slow probe must not end the watchdog's chain
    if waiting or bench.jobs(WORKBENCH) or open_records(settings) or goals or not bench.dormant():
        rearmed = bench.ensure_watchdog(own_job_id=own_job_id)
    else:
        actions.append("dormant: no bench activity lately, not re-arming")
    if goals:
        try:
            actions += _lab_agents(settings, slurm)
        except Exception as exc:  # noqa: BLE001 - one broken goal or policy must not stop the watchdog
            actions.append(f"lab agents: {type(exc).__name__}: {exc}"[:300])
    return _save(settings, WatchdogReport(checked=now(), actions=tuple(actions), rearmed=rearmed,
                                          workbench=bench.state().summary()))


def _lab_agents(settings: Settings, slurm: Slurm) -> list[str]:
    actions = []
    revived = revive(settings, slurm)
    if revived:
        actions.append(f"re-armed lab agent(s): {', '.join(revived)}")
    if probe_due(settings):
        try:
            results = probe(settings)
        except (OSError, PolicyError) as exc:
            return actions + [f"engine probe failed: {exc}"]
        actions.append("engine probe: " + ", ".join(f"{k} {'ok' if v['ok'] else v['status']}" for k, v in results.items()))
    return actions


KEEP_LOGS_S, KEEP_SCRIPTS_S, KEEP_LAST = 3 * 86400, 2 * 86400, 10


def prune(bench_dir: Path, now: float | None = None) -> int:
    """Watchdog logs older than three days (the last KEEP_LAST stay) and job scripts older than two:
    one of each every few minutes would otherwise pile up for good."""
    now = time.time() if now is None else now
    removed = 0
    for folder, pattern, keep_s in ((bench_dir / "logs", "watchdog-*.log", KEEP_LOGS_S),
                                    (bench_dir / "scripts", "*.sbatch", KEEP_SCRIPTS_S)):
        if not folder.is_dir():
            continue
        dated = sorted(((path.stat().st_mtime, path) for path in folder.glob(pattern)), reverse=True)
        for mtime, path in dated[KEEP_LAST:]:
            if now - mtime > keep_s:
                path.unlink(missing_ok=True)
                removed += 1
    return removed


def _save(settings: Settings, report: WatchdogReport) -> WatchdogReport:
    write_json_atomic(settings.bench_dir / "watchdog.json", report.model_dump(mode="json"))
    return report


def main() -> int:
    os.umask(0o077)  # its logs and the scripts it submits (a workbench's too) are the student's alone
    settings = load_settings()
    try:
        report = check(settings, Slurm(), own_job_id=os.environ.get("SLURM_JOB_ID"))
    except Exception as exc:  # noqa: BLE001 - whatever failed, the chain must go on
        sys.stderr.write(f"watchdog: {type(exc).__name__}: {exc}\n")
        try:
            Workbench(settings, Slurm()).ensure_watchdog(own_job_id=os.environ.get("SLURM_JOB_ID"))
        except Exception as again:  # noqa: BLE001
            sys.stderr.write(f"watchdog: could not re-arm: {again}\n")
        return 1
    sys.stdout.write(report.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
