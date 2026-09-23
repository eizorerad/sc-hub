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
"""

from __future__ import annotations

import os
import sys

from ..config import Settings, load_settings
from ..slurm import Slurm, SlurmError
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
    has_job = bool(bench.jobs(WORKBENCH))
    inbox = Inbox(settings.bench_dir)
    swept = Runner(settings, now=now, job_id=f"watchdog-{own_job_id or os.getpid()}", slurm=slurm).sweep()
    if swept:
        actions.append(f"swept {swept} cell(s) a dead workbench left behind")
    ended = reap(settings, slurm)
    if ended:
        actions.append(f"recorded {len(ended)} job(s) that ended without a result: {', '.join(ended)}")
    waiting = inbox.pending()
    if waiting and not has_job:
        job_id = bench.submit_workbench()
        actions.append(f"started the workbench (job {job_id}) for {len(waiting)} waiting cell(s)")
    goals = active_goals(settings)
    if goals:
        actions += _lab_agents(settings, slurm)
    rearmed = None
    if waiting or bench.jobs(WORKBENCH) or open_records(settings) or goals or not bench.dormant():
        rearmed = bench.ensure_watchdog(own_job_id=own_job_id)
    else:
        actions.append("dormant: no bench activity lately, not re-arming")
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


def _save(settings: Settings, report: WatchdogReport) -> WatchdogReport:
    write_json_atomic(settings.bench_dir / "watchdog.json", report.model_dump(mode="json"))
    return report


def main() -> int:
    settings = load_settings()
    try:
        report = check(settings, Slurm(), own_job_id=os.environ.get("SLURM_JOB_ID"))
    except SlurmError as exc:
        sys.stderr.write(f"watchdog: Slurm failed: {exc}\n")
        Workbench(settings, Slurm()).ensure_watchdog(own_job_id=os.environ.get("SLURM_JOB_ID"))
        return 1
    sys.stdout.write(report.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
